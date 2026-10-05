#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Minimal mock sidecar translation service (Python stdlib only, zero deps).

Purpose: with NO real Argos/MADLAD model available, demonstrate and manually
test the full chain Rust sidecar engine (engine/sidecar.rs) -> local HTTP ->
result. It does not really translate; it echoes the input tagged with the
target language so the chain can be asserted end to end.

Start it:
    python tests/mock_sidecar_server.py --port 11435

Then point the Rust engine at it (env-var convention, see engine/sidecar.rs;
LT_ARGOS_URL is the legacy spelling, WONSLATE_ARGOS_URL the brand one):
    $env:WONSLATE_ARGOS_URL = "http://127.0.0.1:11435"   # PowerShell
    python script/build.py --test                        # or run the GUI / FFI smoke

Contract (aligned with engine/sidecar.rs and sidecar/ct2_sidecar.py):
    POST /translate   body {"text","source","target"[,"glossary":[{src,tgt}]]}
    200 -> {"text": "<tagged pseudo-translation>"}
    GET  /health      -> {"status":"ok"}                 (liveness only)
    GET  /readyz      -> {"status":"ready",...}           (mock loads no model)
    GET  /languages   -> coverage answer; the mock echoes any pair, so it must
                         not claim an empty list is the whole set (D22/D23)
    POST /warmup      -> same report as /readyz (mock has nothing to load)
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok"})
        elif path == "/readyz":
            # Nothing to load here, so readiness is honestly always true; the
            # real backends answer this from their actual model state.
            self._send(200, {"status": "ready", "backend": "mock",
                             "loaded": ["mock (nothing to load)"]})
        elif path == "/languages":
            self._send(200, {"backend": "mock", "pairs": [], "target_codes": [],
                             "complete": False,
                             "note": "mock backend echoes any pair; it declares no coverage"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/warmup":
            # Nothing to load in the mock, so warm-up is an immediate yes. The real
            # backends answer this by actually loading, and report what it cost.
            self._send(200, {"status": "ready", "backend": "mock",
                             "warmed": False, "load_s": 0.0,
                             "loaded": ["mock (nothing to load)"]})
            return
        if path != "/translate":
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        text = req.get("text", "")
        target = req.get("target", "?")
        has_gloss = "glossary" in req and req["glossary"]
        # An identifiable pseudo-translation so humans and assertions can
        # confirm the chain went through and the glossary was forwarded.
        pseudo = f"[{target}] {text}" + (" +gloss" if has_gloss else "")
        self._send(200, {"text": pseudo})

    def log_message(self, *_):  # silence the access log
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11435)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"mock sidecar listening on http://{args.host}:{args.port}/translate")
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
