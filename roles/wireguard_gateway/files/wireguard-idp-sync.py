#!/usr/bin/env python3
"""Tie WireGuard peers to Keycloak users (IAM-04).

Every peer in /etc/wireguard/<iface>-peers.tsv names its owner (a Keycloak username or e-mail). On each
run (systemd timer) this script asks Keycloak, with a service-account client that may only view users,
whether each owner still exists, is enabled and holds one of the required realm roles. Peers that fail
are written to the deny list (/etc/wireguard/<iface>-denied.tsv) and removed from the running interface
at once; wireguard-gateway-check removes them again every minute, so an Ansible run or a reload cannot
bring them back. A peer whose owner is enabled again leaves the deny list and is restored by reloading
the interface from its configuration.

When Keycloak cannot be reached the deny list is kept as it is (users already denied stay denied) and
the metric wireguard_idp_sync_success is 0, which alerts. The VPN is how administrators reach a broken
Keycloak, so an outage does not lock everybody out.

Settings: JSON file given as the only argument (written by Ansible, no secrets inside):
    {"keycloak_url": "https://sso.example.internal", "realm": "org", "client_id": "vpn-idp-sync",
     "client_secret_file": "/run/compliant-secrets/vpn-idp-sync/client_secret",
     "ca_file": "/etc/ssl/certs/ca-certificates.crt", "interface": "wg0",
     "required_roles": ["platform-admin", "operator"], "metrics_dir": "/var/lib/node_exporter/textfile_collector"}
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15


class IdpError(Exception):
    """Keycloak could not answer; nothing is changed."""


def log(message: str) -> None:
    print(message, flush=True)  # journald through the systemd unit


def read_peers(path: str) -> list[dict]:
    peers = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 4:
                    continue
                name, key, expires, owner = cols[:4]
                idp_user = cols[4] if len(cols) > 4 and cols[4] else owner
                peers.append({"name": name, "key": key, "expires": expires, "owner": owner, "idp_user": idp_user})
    except FileNotFoundError:
        pass
    return peers


def read_denied(path: str) -> dict[str, dict]:
    denied = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 4:
                    denied[cols[0]] = {"key": cols[0], "name": cols[1], "owner": cols[2], "reason": cols[3]}
    except FileNotFoundError:
        pass
    return denied


def write_atomic(path: str, text: str, mode: int) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Keycloak:
    def __init__(self, cfg: dict):
        self.base = cfg["keycloak_url"].rstrip("/")
        self.realm = cfg["realm"]
        self.ctx = ssl.create_default_context(cafile=cfg.get("ca_file") or None)
        self.client_id = cfg["client_id"]
        with open(cfg["client_secret_file"], encoding="utf-8") as fh:
            self.client_secret = fh.read().strip()
        self.token = None

    def _request(self, url: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as resp:
                return json.loads(resp.read().decode() or "null")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise IdpError(f"{url.split('?')[0]}: {exc}") from exc

    def login(self) -> None:
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret}
        ).encode()
        answer = self._request(f"{self.base}/realms/{self.realm}/protocol/openid-connect/token", body,
                               {"Content-Type": "application/x-www-form-urlencoded"})
        if not isinstance(answer, dict) or "access_token" not in answer:
            raise IdpError("no access token in the token response")
        self.token = answer["access_token"]

    def get(self, path: str, **params):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self._request(f"{self.base}/admin/realms/{self.realm}{path}{query}",
                             headers={"Authorization": f"Bearer {self.token}"})

    def find_users(self, who: str) -> list[dict]:
        field = "email" if "@" in who else "username"
        users = self.get("/users", **{field: who, "exact": "true", "briefRepresentation": "true"})
        # exact=true is a prefix-free match, but compare again: e-mails and usernames are case-insensitive.
        return [u for u in users or [] if str(u.get(field, "")).lower() == who.lower()]

    def realm_roles(self, user_id: str) -> set[str]:
        return {r.get("name") for r in self.get(f"/users/{user_id}/role-mappings/realm/composite") or []}


def decide(peers: list[dict], idp, required_roles: list[str]) -> dict[str, str]:
    """Return {public key: reason} for peers whose owner may not use the VPN. Raises IdpError."""
    verdicts = {}
    for peer in peers:
        users = idp.find_users(peer["idp_user"])
        if not users:
            verdicts[peer["key"]] = "user-missing"
        elif len(users) > 1:
            verdicts[peer["key"]] = "user-ambiguous"
        elif not users[0].get("enabled", False):
            verdicts[peer["key"]] = "user-disabled"
        elif required_roles and not (idp.realm_roles(users[0]["id"]) & set(required_roles)):
            verdicts[peer["key"]] = "role-missing"
    return verdicts


def wg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["wg", *args], capture_output=True, text=True, check=False)


def metrics(cfg: dict, success: bool, peers: list[dict], denied: dict[str, dict]) -> None:
    out_dir = cfg.get("metrics_dir", "/var/lib/node_exporter/textfile_collector")
    iface = cfg["interface"]
    path = os.path.join(out_dir, f"wireguard_idp_sync_{iface}.prom")
    previous_success = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("wireguard_idp_sync_last_success_timestamp_seconds"):
                    previous_success = int(float(line.split()[-1]))
    except (FileNotFoundError, ValueError):
        pass
    now = int(time.time())
    lines = [
        "# HELP wireguard_idp_sync_success Whether the last check of VPN peer owners against Keycloak worked.",
        "# TYPE wireguard_idp_sync_success gauge",
        f'wireguard_idp_sync_success{{interface="{iface}"}} {1 if success else 0}',
        "# HELP wireguard_idp_sync_timestamp_seconds When the IdP sync last ran.",
        "# TYPE wireguard_idp_sync_timestamp_seconds gauge",
        f'wireguard_idp_sync_timestamp_seconds{{interface="{iface}"}} {now}',
        "# HELP wireguard_idp_sync_last_success_timestamp_seconds When the IdP sync last succeeded.",
        "# TYPE wireguard_idp_sync_last_success_timestamp_seconds gauge",
        f"wireguard_idp_sync_last_success_timestamp_seconds {now if success else previous_success}",
        "# HELP wireguard_peer_idp_denied Peers removed because their owner is missing, disabled or lacks the role.",
        "# TYPE wireguard_peer_idp_denied gauge",
    ]
    for peer in peers:
        entry = denied.get(peer["key"])
        reason = entry["reason"] if entry else "ok"
        lines.append(
            f'wireguard_peer_idp_denied{{interface="{iface}",peer="{peer["name"]}",owner="{peer["owner"]}",'
            f'reason="{reason}"}} {1 if entry else 0}'
        )
    os.makedirs(out_dir, exist_ok=True)
    write_atomic(path, "\n".join(lines) + "\n", 0o644)


def run(cfg: dict, idp=None) -> int:
    iface = cfg["interface"]
    peers_path = cfg.get("peers_file", f"/etc/wireguard/{iface}-peers.tsv")
    denied_path = cfg.get("denied_file", f"/etc/wireguard/{iface}-denied.tsv")
    peers = read_peers(peers_path)
    before = read_denied(denied_path)
    try:
        if idp is None:
            idp = Keycloak(cfg)
        idp.login()
        verdicts = decide(peers, idp, cfg.get("required_roles") or [])
    except (IdpError, OSError) as exc:
        log(f"IdP sync failed, deny list kept ({len(before)} peers): {exc}")
        metrics(cfg, False, peers, before)
        return 1

    by_key = {p["key"]: p for p in peers}
    after = {
        key: {"key": key, "name": by_key[key]["name"], "owner": by_key[key]["owner"], "reason": reason}
        for key, reason in verdicts.items()
    }
    header = "# Managed by wireguard-idp-sync (IAM-04). public_key\tname\towner\treason\n"
    write_atomic(denied_path, header + "".join(
        f"{d['key']}\t{d['name']}\t{d['owner']}\t{d['reason']}\n" for d in sorted(after.values(), key=lambda d: d["name"])
    ), 0o600)

    for key, entry in after.items():
        if key not in before:
            log(f"denied peer {entry['name']} (owner {entry['owner']}): {entry['reason']}")
        if not cfg.get("dry_run"):
            wg("set", iface, "peer", key, "remove")
    restored = [before[k] for k in before if k not in after and k in by_key]
    for entry in restored:
        log(f"restored peer {entry['name']} (owner {entry['owner']}): owner is enabled again")
    if restored and not cfg.get("dry_run"):
        # wg-quick reload = wg syncconf from the configuration; the check then drops expired peers again.
        subprocess.run(["systemctl", "reload", f"wg-quick@{iface}"], check=False)
        subprocess.run(["systemctl", "start", "wireguard-gateway-check.service"], check=False)
    metrics(cfg, True, peers, after)
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = json.load(fh)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
