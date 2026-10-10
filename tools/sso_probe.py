#!/usr/bin/env python3
"""Log in to services through Keycloak as a test user and report what the user gets (IAM-04, IAM-09).

Used by playbooks/test_role_matrix.yml and playbooks/test_offboarding.yml on the Ansible controller.
Reads one JSON request on stdin (so passwords never appear on a command line) and prints one JSON report.

Request:
  {"keycloak_url": "https://sso.example.internal", "realm": "org", "ca_file": "/path/ca.pem" | null,
   "user": {"username": "...", "password": "...", "id": "<keycloak user id>",
            "totp_secret": "<raw OTP secret>" | null,
            "webauthn": {"credential_id": "<b64url>", "private_key_pem": "<PEM>"} | null},
   "cookie_dir": "/tmp/x" | null,          # keep browser cookies between runs (offboarding test)
   "probes": [ {"name": "grafana", "flow": "browser", "start_url": ".../login/generic_oauth",
                "check_url": ".../api/user/orgs", "extract": "json:0.role", "reuse_session": false}, ... ]}

Flows:
  browser   open start_url, log in on Keycloak's form (password, then OTP or WebAuthn), come back to the
            service and GET check_url with the session cookies.
  token     authorization code + PKCE against Keycloak for client_id/redirect_uri (public client), then GET
            check_url with the access token. Also returns the refresh token's fate when "refresh": true.
  openbao   OpenBao OIDC auth (auth_url, Keycloak login, callback) then extract from the token:
            "openbao_policies" (identity policies) or "ssh_sign:<role>,<role>" (SSH CA roles that sign).
Extractors: "json:<dotted path>", "status_ok:<value>" (value when check_url answers 200, else "none"),
  "forgejo" (site-admin / restricted / user), "tags" (RabbitMQ whoami tags as tag:<t>), "openbao_policies",
  "ssh_sign:<roles>".

Subcommands (no stdin):
  sso_probe.py new-webauthn     print a new software WebAuthn credential (private key + Keycloak import JSON)
  sso_probe.py totp <secret>    print the current TOTP code (HmacSHA256, 6 digits, 30 s)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html.parser
import http.cookiejar
import json
import os
import re
import secrets
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20


# --------------------------------------------------------------------------- helpers
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def totp(secret: str, now: float | None = None, digits: int = 6, period: int = 30) -> str:
    """Keycloak's TOTP: the key is the raw secret string, HmacSHA256 (realm policy in roles/keycloak)."""
    counter = int((now if now is not None else time.time()) // period)
    digest = hmac.new(secret.encode(), struct.pack(">Q", counter), hashlib.sha256).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def cbor(value) -> bytes:
    """Minimal CBOR encoder (ints, bytes, text, maps) for COSE keys."""
    def head(major: int, n: int) -> bytes:
        if n < 24:
            return bytes([major << 5 | n])
        if n < 256:
            return bytes([major << 5 | 24, n])
        if n < 65536:
            return bytes([major << 5 | 25]) + struct.pack(">H", n)
        return bytes([major << 5 | 26]) + struct.pack(">I", n)

    if isinstance(value, bool):
        raise TypeError("bool not supported")
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


def new_webauthn() -> dict:
    """Software authenticator key (ES256) and the credential to import into Keycloak (partialImport)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    cose = cbor({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
    credential_id = secrets.token_bytes(32)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    credential_data = {
        "aaguid": "00000000-0000-0000-0000-000000000000",
        "credentialId": base64.b64encode(credential_id).decode(),
        "credentialPublicKey": base64.b64encode(cose).decode(),
        "counter": 0,
        "attestationStatementFormat": "none",
        "transports": [],
    }
    return {
        "credential_id": b64url(credential_id),
        "private_key_pem": pem,
        "keycloak_credential": {"type": "webauthn", "userLabel": "iam-test software key",
                                "secretData": "{}", "credentialData": json.dumps(credential_data)},
    }


def webauthn_assertion(page: str, origin: str, cred: dict, user_id: str) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    challenge = re.search(r"challenge\s*:\s*['\"]([^'\"]+)", page)
    rp_id = re.search(r"rpId\s*:\s*['\"]([^'\"]+)", page)
    if not challenge:
        raise ProbeError("WebAuthn page without a challenge")
    rp = rp_id.group(1) if rp_id and rp_id.group(1) else urllib.parse.urlsplit(origin).hostname
    client_data = json.dumps({"type": "webauthn.get", "challenge": challenge.group(1), "origin": origin,
                              "crossOrigin": False}, separators=(",", ":")).encode()
    auth_data = hashlib.sha256(rp.encode()).digest() + bytes([0x05]) + struct.pack(">I", int(time.time()))
    key = serialization.load_pem_private_key(cred["private_key_pem"].encode(), password=None)
    signature = key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
    return {"clientDataJSON": b64url(client_data), "authenticatorData": b64url(auth_data),
            "signature": b64url(signature), "credentialId": cred["credential_id"],
            "userHandle": b64url(user_id.encode()), "error": ""}


class ProbeError(Exception):
    pass


class FormParser(html.parser.HTMLParser):
    """Collects forms: action, method and input values."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"id": a.get("id", ""), "action": a.get("action", ""), "inputs": {}})
        elif tag == "input" and self.forms and a.get("name"):
            self.forms[-1]["inputs"][a["name"]] = a.get("value") or ""


def forms_of(page: str) -> list[dict]:
    parser = FormParser()
    parser.feed(page)
    return parser.forms


class StopAt(urllib.request.HTTPRedirectHandler):
    """Follows redirects except to the given prefix (the client's redirect_uri), which it records."""

    def __init__(self, prefix: str | None):
        self.prefix = prefix
        self.caught: str | None = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.prefix and newurl.startswith(self.prefix):
            self.caught = newurl
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Browser:
    def __init__(self, ca_file: str | None, stop_at: str | None = None, cookie_file: str | None = None):
        self.jar = http.cookiejar.LWPCookieJar(cookie_file) if cookie_file else http.cookiejar.CookieJar()
        if cookie_file and os.path.exists(cookie_file):
            self.jar.load(ignore_discard=True, ignore_expires=True)
        self.cookie_file = cookie_file
        self.stop = StopAt(stop_at)
        ctx = ssl.create_default_context(cafile=ca_file or None)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), urllib.request.HTTPSHandler(context=ctx), self.stop)

    def open(self, url: str, data: dict | None = None, headers: dict | None = None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers or {})
        try:
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.geturl(), resp.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            if self.stop.caught:
                return exc.code, self.stop.caught, ""
            return exc.code, exc.geturl() if hasattr(exc, "geturl") else url, exc.read().decode(errors="replace")

    def save(self):
        if self.cookie_file:
            self.jar.save(ignore_discard=True, ignore_expires=True)
            os.chmod(self.cookie_file, 0o600)


def keycloak_login(browser: Browser, status: int, url: str, page: str, kc_url: str, user: dict):
    """Drive Keycloak's login pages until they hand back to the service (or refuse)."""
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(kc_url))
    for _ in range(6):
        if browser.stop.caught or not url.startswith(kc_url):
            return status, url, page
        forms = forms_of(page)
        login = next((f for f in forms if "username" in f["inputs"] or f["id"] == "kc-form-login"), None)
        otp = next((f for f in forms if "otp" in f["inputs"] or f["id"] == "kc-otp-login-form"), None)
        webauthn = next((f for f in forms if f["id"] == "webauth"), None)
        if login:
            data = dict(login["inputs"], username=user["username"], password=user["password"])
            status, url, page = browser.open(urllib.parse.urljoin(url, login["action"]), data)
        elif otp and user.get("totp_secret"):
            data = dict(otp["inputs"], otp=totp(user["totp_secret"]))
            status, url, page = browser.open(urllib.parse.urljoin(url, otp["action"]), data)
        elif webauthn and user.get("webauthn"):
            data = webauthn_assertion(page, origin, user["webauthn"], user["id"])
            status, url, page = browser.open(urllib.parse.urljoin(url, webauthn["action"]), data)
        else:
            error = re.search(r'class="[^"]*(kc-feedback-text|alert-error)[^"]*"[^>]*>\s*([^<]+)', page)
            raise ProbeError(f"login refused: {error.group(2).strip() if error else 'no form to continue'}")
    raise ProbeError("too many login steps")


def extract(how: str, status: int, body: str):
    if how.startswith("status_ok:"):
        return how.split(":", 1)[1] if status == 200 else "none"
    if status != 200:
        return "none"
    data = json.loads(body) if body else None
    if how.startswith("json:"):
        for part in how.split(":", 1)[1].split("."):
            if data is None:
                break
            data = data[int(part)] if isinstance(data, list) and part.isdigit() and int(part) < len(data) else (
                data.get(part) if isinstance(data, dict) else None)
        return data if data not in (None, "", []) else "none"
    if how == "forgejo":
        return "site-admin" if data.get("is_admin") else "restricted" if data.get("restricted") else "user"
    if how == "tags":
        tags = data.get("tags") or []
        tags = tags.split(",") if isinstance(tags, str) else tags
        return sorted(f"tag:{t}" for t in tags if t) or "none"
    raise ProbeError(f"unknown extractor {how}")


def pkce() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def flow_browser(req: dict, probe: dict) -> dict:
    cookie_file = os.path.join(req["cookie_dir"], f"{probe['name']}.cookies") if req.get("cookie_dir") else None
    browser = Browser(req.get("ca_file"), cookie_file=cookie_file)
    if not probe.get("reuse_session"):
        status, url, page = browser.open(probe["start_url"])
        keycloak_login(browser, status, url, page, req["keycloak_url"], req["user"])
    status, _, body = browser.open(probe["check_url"], headers={"Accept": "application/json"})
    browser.save()
    return {"status": status, "value": extract(probe["extract"], status, body)}


def code_flow(req: dict, auth_url: str, redirect_uri: str) -> dict:
    browser = Browser(req.get("ca_file"), stop_at=redirect_uri)
    status, url, page = browser.open(auth_url)
    keycloak_login(browser, status, url, page, req["keycloak_url"], req["user"])
    if not browser.stop.caught:
        raise ProbeError("Keycloak did not redirect back with a code")
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(browser.stop.caught).query))


def http_json(url: str, ca_file: str | None, data=None, headers=None, form: bool = False):
    body = None
    headers = dict(headers or {})
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    ctx = ssl.create_default_context(cafile=ca_file or None)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:200]}


def flow_token(req: dict, probe: dict) -> dict:
    kc = f"{req['keycloak_url']}/realms/{req['realm']}/protocol/openid-connect"
    verifier, challenge = pkce()
    state = b64url(secrets.token_bytes(12))
    auth = f"{kc}/auth?" + urllib.parse.urlencode({
        "client_id": probe["client_id"], "redirect_uri": probe["redirect_uri"], "response_type": "code",
        "scope": "openid", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    params = code_flow(req, auth, probe["redirect_uri"])
    status, tokens = http_json(f"{kc}/token", req.get("ca_file"), form=True, data={
        "grant_type": "authorization_code", "code": params.get("code", ""), "client_id": probe["client_id"],
        "redirect_uri": probe["redirect_uri"], "code_verifier": verifier})
    if status != 200:
        raise ProbeError(f"token exchange failed: {status}")
    result = {}
    if probe.get("check_url"):
        status, body = http_json(probe["check_url"], req.get("ca_file"),
                                 headers={"Authorization": f"Bearer {tokens['access_token']}"})
        result = {"status": status, "value": extract(probe["extract"], status, json.dumps(body))}
    if probe.get("keep_refresh_token"):
        result["refresh_token"] = tokens.get("refresh_token")
    return result


def flow_openbao(req: dict, probe: dict) -> dict:
    addr = probe["addr"].rstrip("/")
    redirect = probe.get("redirect_uri", "http://localhost:8250/oidc/callback")
    nonce = b64url(secrets.token_bytes(16))
    status, body = http_json(f"{addr}/v1/auth/oidc/oidc/auth_url", req.get("ca_file"),
                             data={"role": probe.get("role", "default"), "redirect_uri": redirect, "client_nonce": nonce})
    if status != 200 or not body.get("data", {}).get("auth_url"):
        raise ProbeError(f"OpenBao auth_url failed: {status}")
    params = code_flow(req, body["data"]["auth_url"], redirect)
    query = urllib.parse.urlencode({"code": params.get("code", ""), "state": params.get("state", ""), "client_nonce": nonce})
    status, body = http_json(f"{addr}/v1/auth/oidc/oidc/callback?{query}", req.get("ca_file"))
    if status != 200:
        return {"status": status, "value": "none"}
    auth = body["auth"]
    how = probe["extract"]
    result = {"status": 200, "token_ttl": auth.get("lease_duration")}
    if how == "openbao_policies":
        result["value"] = sorted(auth.get("identity_policies") or []) or "none"
    elif how.startswith("ssh_sign:"):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        public = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        signed = []
        for role in how.split(":", 1)[1].split(","):
            code, answer = http_json(f"{addr}/v1/{probe.get('ssh_mount', 'ssh-client-signer')}/sign/{role}",
                                     req.get("ca_file"), data={"public_key": public},
                                     headers={"X-Vault-Token": auth["client_token"]})
            if code == 200:
                signed.append(role)
        result["value"] = sorted(signed) or "none"
    if probe.get("keep_token"):
        result["token"] = auth["client_token"]
    else:
        http_json(f"{addr}/v1/auth/token/revoke-self", req.get("ca_file"), data={},
                  headers={"X-Vault-Token": auth["client_token"]})
    return result


FLOWS = {"browser": flow_browser, "token": flow_token, "openbao": flow_openbao}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "new-webauthn":
        print(json.dumps(new_webauthn()))
        return 0
    if len(sys.argv) > 2 and sys.argv[1] == "totp":
        print(totp(sys.argv[2]))
        return 0
    req = json.load(sys.stdin)
    report = {}
    for probe in req["probes"]:
        try:
            report[probe["name"]] = dict(FLOWS[probe["flow"]](req, probe), login="ok")
        except ProbeError as exc:
            report[probe["name"]] = {"login": "denied", "value": "none", "detail": str(exc)}
        except (OSError, ValueError, KeyError) as exc:
            report[probe["name"]] = {"login": "error", "value": "error", "detail": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
