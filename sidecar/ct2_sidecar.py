#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Wonslate real sidecar translation service (local CT2 / Argos backend).

Contract (strictly aligned with Rust engine/sidecar.rs, .NET SidecarManager,
tests/mock_sidecar_server.py):
    POST /translate   body {"text","source","target"[,"glossary":[{"src","tgt"}]]}  -> 200 {"text": "<translation>"}
    GET  /health      -> 200 {"status":"ok","backend":"<mock|ct2>"}

Pluggable backends:
    --backend mock  no third-party deps; echoes an identifiable pseudo-translation.
                    Used for integration debugging, CI and model-free environments.
    --backend ct2   real CTranslate2 inference (MADLAD-400 / Argos .argosmodel);
                    requires installing dependencies and downloading a model.

Design rules:
    - On a missing dependency / missing model raise MissingDependency explicitly
      with install guidance -- never crash bare, never degrade silently.
    - The service binds 127.0.0.1 only (privacy: translated text never leaves
      the machine).
    - Licenses: deps ctranslate2(MIT)/sentencepiece(Apache); models
      MADLAD-400(Apache-2.0)/Argos(MIT) -- all permissive and commercially usable.

Preparing a real CT2 environment (run these yourself; the script never
downloads or installs anything automatically):
    python -m pip install ctranslate2 sentencepiece
    # MADLAD-400 CT2 int8 (Apache-2.0, GB-scale) example (pick one; must include
    # the tokenizer .model):
    #   huggingface-cli download <madlad400-ct2-repo> --local-dir <model_dir>
    python -m sidecar.ct2_sidecar --backend ct2 --model-dir <model_dir> --port 11435

The module is importable by unit tests (serve only blocks under __main__).
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ARGOS_PORT = 11435
DEFAULT_MADLAD_PORT = 11436


class MissingDependency(RuntimeError):
    """Raised when a third-party library or model required by a real backend
    is absent; the message carries actionable install guidance."""


# ---- Backend abstraction --------------------------------------------------

class MockBackend:
    """Dependency-free echo backend: produces an identifiable pseudo-translation
    for integration debugging and contract tests."""

    name = "mock"

    def translate(self, text, source, target, glossary=None):
        out = "[{}] {}".format(target, text)
        if glossary:
            out += " +gloss"
        return out


class CT2Backend:
    """Real CTranslate2 backend (MADLAD-400 / Argos). Raises MissingDependency
    when dependencies or the model are absent."""

    name = "ct2"

    def __init__(self, model_dir):
        if not model_dir:
            raise MissingDependency(
                "ct2 backend needs --model-dir pointing at a downloaded"
                " CTranslate2 model directory (MADLAD-400 ct2 / unpacked Argos"
                " .argosmodel). See the install/download guide at the top of"
                " this file.")
        try:
            import ctranslate2          # noqa: F401
            import sentencepiece        # noqa: F401
        except ImportError as e:
            raise MissingDependency(
                "ct2 backend is missing dependencies: {}. Run"
                " `python -m pip install ctranslate2 sentencepiece` first.".format(e))
        self._model_dir = model_dir
        # Actual loading is left to the concrete checkpoint integration (spiece /
        # speed configuration differs per model); when wiring it up, initialize
        # ctranslate2.Translator(model_dir) + sentencepiece here and verify.
        raise MissingDependency(
            "ct2 backend deps and model dir are in place, but checkpoint"
            " loading / tokenization / language-code mapping must be"
            " implemented and measured for the chosen MADLAD-400/Argos model.")

    def translate(self, text, source, target, glossary=None):  # pragma: no cover - needs a real model
        raise NotImplementedError("CT2Backend.translate lands with the real checkpoint integration")


def make_backend(kind, model_dir=None):
    kind = (kind or "mock").lower()
    if kind == "mock":
        return MockBackend()
    if kind == "ct2":
        return CT2Backend(model_dir)
    raise ValueError("unknown backend: {!r} (available: mock / ct2)".format(kind))


# ---- HTTP service ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "backend": self.server.backend.name})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/translate":
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        text = req.get("text")
        if not text or not str(text).strip():
            self._send(400, {"error": "missing 'text'"})
            return
        try:
            out = self.server.backend.translate(
                text, req.get("source", ""), req.get("target", ""), req.get("glossary"))
        except MissingDependency as e:
            self._send(503, {"error": "backend_unavailable", "message": str(e)})
            return
        self._send(200, {"text": out})

    def log_message(self, *_):  # silence the access log
        pass


def build_server(backend, host="127.0.0.1", port=0):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.backend = backend
    return srv


def serve_in_thread(backend, host="127.0.0.1", port=0):
    """Start the service on a background thread (for tests / embedding);
    returns (server, bound_port)."""
    import threading
    srv = build_server(backend, host, port)
    bound = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, bound


def main():
    ap = argparse.ArgumentParser(description="Wonslate sidecar translation server")
    ap.add_argument("--backend", default="mock", choices=["mock", "ct2"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_ARGOS_PORT)
    ap.add_argument("--model-dir", default=None)
    args = ap.parse_args()

    backend = make_backend(args.backend, args.model_dir)
    srv = build_server(backend, args.host, args.port)
    print("sidecar[{}] listening on http://{}:{}/translate".format(backend.name, args.host, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
