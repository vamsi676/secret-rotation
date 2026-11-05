#!/usr/bin/env python3
"""Tiny demo service for the secret-rotation demo.

Reads its own secret from the environment once at startup (systemd loads it
from /etc/demo-app/secrets.env). GET /health returns "OK <hash8>" where hash8
is the first 8 hex chars of sha256(secret). The secret value itself is never
returned or logged.

Demo-only fault injection: if /etc/demo-app/fault-svc-<name> exists, /health
reports a wrong hash so refresh_secrets.sh detects a failed refresh.
"""
import argparse
import hashlib
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTS = {"a": 8081, "b": 8082, "c": 8083}
FAULT_DIR = "/etc/demo-app"
WRONG_HASH = "00000000"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, choices=sorted(PORTS))
    args = parser.parse_args()
    name = args.name
    port = PORTS[name]

    secret = os.environ.get(f"SVC_{name.upper()}_SECRET", "")
    # Drop every demo secret from this process's environment once ours is read.
    for key in [k for k in os.environ if k.startswith("SVC_") and k.endswith("_SECRET")]:
        del os.environ[key]
    secret_hash = hashlib.sha256(secret.encode()).hexdigest()[:8] if secret else None
    del secret
    fault_flag = os.path.join(FAULT_DIR, f"fault-svc-{name}")

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: str) -> None:
            data = (body + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self._reply(404, "NOT_FOUND")
            elif secret_hash is None:
                self._reply(503, "NO_SECRET")
            elif os.path.exists(fault_flag):
                self._reply(200, f"OK {WRONG_HASH}")
            else:
                self._reply(200, f"OK {secret_hash}")

        def log_message(self, fmt, *a) -> None:
            # Only method + path + status reach the journal; never headers or bodies.
            sys.stderr.write(f"svc-{name}: {self.command} {self.path} {a[1] if len(a) > 1 else ''}\n")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    state = f"hash={secret_hash}" if secret_hash else "NO_SECRET"
    print(f"svc-{name} listening on 127.0.0.1:{port} ({state})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
