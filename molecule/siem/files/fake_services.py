#!/usr/bin/env python3
"""Lab stand-ins on 127.0.0.1:19093: Alertmanager's alert API and Keycloak's OIDC discovery document.

POST /api/v2/alerts appends the body to /var/log/fake-alertmanager.jsonl.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "http://127.0.0.1:19093/realms/org"
DISCOVERY = {
    "issuer": BASE,
    "authorization_endpoint": BASE + "/protocol/openid-connect/auth",
    "token_endpoint": BASE + "/protocol/openid-connect/token",
    "userinfo_endpoint": BASE + "/protocol/openid-connect/userinfo",
    "end_session_endpoint": BASE + "/protocol/openid-connect/logout",
    "jwks_uri": BASE + "/protocol/openid-connect/certs",
    "response_types_supported": ["code"],
    "subject_types_supported": ["public"],
    "id_token_signing_alg_values_supported": ["RS256"],
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/.well-known/openid-configuration"):
            self._send(200, DISCOVERY)
        elif self.path.endswith("/protocol/openid-connect/certs"):
            self._send(200, {"keys": []})
        else:
            self._send(404, {})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        if self.path == "/api/v2/alerts":
            with open("/var/log/fake-alertmanager.jsonl", "a", encoding="utf-8") as handle:
                handle.write(body.replace("\n", " ") + "\n")
            self._send(200, {})
        else:
            self._send(404, {})


ThreadingHTTPServer(("127.0.0.1", 19093), Handler).serve_forever()
