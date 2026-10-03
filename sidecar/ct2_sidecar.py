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
    python -m pip install -r sidecar/requirements.txt
    # unpack Argos packages under <data_dir>/models/argos -- that default is
    # reported by default_model_dir(); pass --model-dir to point elsewhere
    #   (en_zh / zh_en .argosmodel from https://argos-net.com/v1/)
    # MADLAD-400 CT2 int8 (Apache-2.0, GB-scale): ONE checkpoint serves every
    # supported pair; fetch it with script/fetch_madlad_model.py, then
    python -m sidecar.ct2_sidecar --backend madlad --port 11436
    #   (or pass --model-dir to point at a directory holding model.bin +
    #   spiece.model, e.g. from huggingface-cli download <madlad400-ct2-repo>)
    python -m sidecar.ct2_sidecar --backend ct2 --model-dir <model_dir> --port 11435

The module is importable by unit tests (serve only blocks under __main__).
"""
import argparse
import json
import os
import pathlib
import sys
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


# SentencePiece marks word boundaries with U+2581. The Argos packages carry it as
# an ordinary piece, so decode() leaves the marker in the text; the detokenizer
# maps it back to a space.
_SPM_SPACE = "\u2581"

# Beam width used per model call (Argos' own default).
_BEAM_SIZE = 4


def default_data_dir():
    """Per-OS user data location, overridable by LT_DATA_DIR / WONSLATE_DATA_DIR
    (legacy spelling wins). Mirrors translator-engine/src/config.rs."""
    override = None
    for key in ("LT_DATA_DIR", "WONSLATE_DATA_DIR"):
        value = os.environ.get(key)
        if value and value.strip():
            override = value.strip()
            break

    if override:
        return pathlib.Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return pathlib.Path(local) / "Wonslate" if local else pathlib.Path("./lt-data")
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Wonslate"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "share"
    return base / "wonslate"


def default_model_dir():
    """Conventional location for unpacked Argos packages."""
    return default_data_dir() / "models" / "argos"


def default_madlad_model_dir():
    """Conventional location for the MADLAD-400 CT2 checkpoint directory."""
    return default_data_dir() / "models" / "madlad"


class CT2Backend:
    """Real CTranslate2 backend over unpacked Argos packages.

    A model root holds one directory per language pair, each in the Argos
    layout: metadata.json (from_code / to_code), model/ (CTranslate2) and
    sentencepiece.model. Pairs are discovered from metadata.json and loaded
    lazily, so an unused direction costs nothing.

    Raises MissingDependency -- never a bare crash and never a silently wrong
    answer -- when the dependencies, the model root or the requested pair is
    unavailable. Glossary injection is not supported by these checkpoints; the
    Rust pipeline owns glossary handling for engines that accept a prompt.
    """

    name = "ct2"

    def __init__(self, model_dir=None):
        try:
            import ctranslate2
            import sentencepiece
        except ImportError as e:
            raise MissingDependency(
                "ct2 backend is missing dependencies: {}. Run"
                " `python -m pip install -r sidecar/requirements.txt` first.".format(e))

        if not model_dir:
            model_dir = default_model_dir()
        root = pathlib.Path(model_dir)
        if not root.is_dir():
            raise MissingDependency(
                "ct2 backend model dir does not exist: {}. Download and unpack"
                " Argos packages into it (see the guide at the top of this"
                " file), or pass --model-dir.".format(root))

        self._ct2 = ctranslate2
        self._spm = sentencepiece
        self._root = root
        self._packages = {}   # (source, target) -> package directory
        self._loaded = {}     # (source, target) -> (translator, processor)

        for pkg in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (pkg / "model").is_dir():
                continue
            codes = self._pair_codes(pkg / "metadata.json")
            if codes:
                self._packages[codes] = pkg

        if not self._packages:
            raise MissingDependency(
                "ct2 backend found no language packages under {}. Expected"
                " <pair>/metadata.json plus <pair>/model/ (unpacked"
                " .argosmodel).".format(root))

    @staticmethod
    def _pair_codes(metadata):
        """Read (from_code, to_code) from an Argos metadata.json, else None."""
        if not metadata.is_file():
            return None
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        source, target = data.get("from_code"), data.get("to_code")
        return (source, target) if source and target else None

    def available_pairs(self):
        """Sorted 'source->target' strings for the packages found on disk."""
        return sorted("{0}->{1}".format(s, t) for s, t in self._packages)

    def _pair(self, source, target):
        key = (source, target)
        pkg = self._packages.get(key)
        if pkg is None:
            raise MissingDependency(
                "ct2 backend has no model for {}->{}; available: {}".format(
                    source, target, ", ".join(self.available_pairs()) or "none"))
        if key not in self._loaded:
            processor = self._spm.SentencePieceProcessor(
                model_file=str(pkg / "sentencepiece.model"))
            translator = self._ct2.Translator(str(pkg / "model"), device="cpu")
            self._loaded[key] = (translator, processor)
        return self._loaded[key]

    def translate(self, text, source, target, glossary=None):
        text = (text or "").strip()
        if not text:
            return ""
        translator, processor = self._pair(source, target)
        pieces = processor.encode(text, out_type=str)
        if not pieces:
            return ""
        result = translator.translate_batch([pieces], beam_size=_BEAM_SIZE)
        return self._detokenize(processor, result[0].hypotheses[0])

    @staticmethod
    def _detokenize(processor, hypothesis):
        """Turn a hypothesis into text and restore the word-boundary markers."""
        return processor.decode(hypothesis).replace(_SPM_SPACE, " ").strip()


class MadladBackend:
    """MADLAD-400 backend: ONE multilingual T5-style checkpoint serves every
    supported pair.

    Unlike Argos (one small Marian package per direction), MADLAD-400 is a
    single model covering 400+ languages; the direction is chosen at inference
    time by prefixing the source with the `<2xx>` target-language token. The
    model directory therefore holds model.bin + spiece.model (+ optionally
    shared_vocabulary.json / config.json) straight from a CT2 conversion, with
    no per-pair layout.

    Runs on CPU on purpose: the GPU slot belongs to the ollama upgrade engine,
    and CT2's CUDA path would add a cuDNN requirement for little gain at 3B
    int8 on a modern 8-core laptop. Glossary injection is not supported by
    these checkpoints (same as Argos); the Rust pipeline owns glossary
    handling for engines that accept a prompt.
    """

    name = "madlad"

    # Shown in "unsupported pair" messages; the full MADLAD-400 list has 450+ codes.
    _COMMON_CODES = ("en", "zh", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar")

    def __init__(self, model_dir=None):
        try:
            import ctranslate2
            import sentencepiece
        except ImportError as e:
            raise MissingDependency(
                "madlad backend is missing dependencies: {}. Run"
                " `python -m pip install -r sidecar/requirements.txt` first.".format(e))

        if not model_dir:
            model_dir = default_madlad_model_dir()
        root = pathlib.Path(model_dir)
        weights = root / "model.bin"
        tokenizer = root / "spiece.model"
        if not tokenizer.is_file():  # tolerate the alternate tokenizer name some repos use
            tokenizer = root / "sentencepiece.model"
        if not weights.is_file() or not tokenizer.is_file():
            raise MissingDependency(
                "madlad backend model dir {} is incomplete: need model.bin and"
                " spiece.model (fetch with `python script/fetch_madlad_model.py`,"
                " or point --model-dir at a MADLAD-400 CT2 conversion"
                " directory).".format(root))

        self._ct2 = ctranslate2
        self._spm = sentencepiece
        self._root = root
        self._processor = self._spm.SentencePieceProcessor(model_file=str(tokenizer))
        self._translator = None          # lazily: first request pays the load
        self._known_targets = set()

    def _known_target(self, target):
        """Whether MADLAD-400 has a `<2xx>` token for this target code.

        A known code encodes to exactly that one special piece (plus, if the
        tokenizer adds it, the dummy word-boundary marker); an unknown one
        falls back to per-character pieces. Data-driven, so the check follows
        the checkpoint rather than a hardcoded language list.
        """
        if target not in self._known_targets:
            token = "<2{}>".format(target)
            pieces = [p for p in self._processor.encode(token, out_type=str) if p != _SPM_SPACE]
            if pieces == [token]:
                self._known_targets.add(target)
            else:
                return False
        return True

    def _engine(self):
        if self._translator is None:
            self._translator = self._ct2.Translator(str(self._root), device="cpu")
        return self._translator

    def translate(self, text, source, target, glossary=None):
        text = (text or "").strip()
        if not text:
            return ""
        if not target or not self._known_target(target):
            raise MissingDependency(
                "madlad backend has no target language {!r}. Supported codes"
                " follow the MADLAD-400 vocabulary (450+ languages; common"
                " ones: {}).".format(target, " ".join(self._COMMON_CODES)))
        pieces = self._processor.encode("<2{}> {}".format(target, text), out_type=str)
        if not pieces:
            return ""
        eos = self._processor.eos_id()
        if eos is not None and eos >= 0:
            pieces.append(self._processor.id_to_piece(eos))
        result = self._engine().translate_batch(
            [pieces], beam_size=_BEAM_SIZE, max_decoding_length=300)
        return self._detokenize(result[0].hypotheses[0])

    def _detokenize(self, hypothesis):
        """Decode a hypothesis, dropping the special pieces a T5 decoder can emit."""
        pieces = [p for p in hypothesis
                  if p not in ("<pad>", "</s>", "<unk>") and not (p.startswith("<2") and p.endswith(">"))]
        if not pieces:
            return ""
        return self._processor.decode(pieces).replace(_SPM_SPACE, " ").strip()


def make_backend(kind, model_dir=None):
    kind = (kind or "mock").lower()
    if kind == "mock":
        return MockBackend()
    if kind == "ct2":
        return CT2Backend(model_dir)
    if kind == "madlad":
        return MadladBackend(model_dir)
    raise ValueError("unknown backend: {!r} (available: mock / ct2 / madlad)".format(kind))


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
    ap.add_argument("--backend", default="mock", choices=["mock", "ct2", "madlad"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None,
                    help="default: 11435 for mock/ct2 (argos slot), 11436 for madlad")
    ap.add_argument("--model-dir", default=None)
    args = ap.parse_args()

    if args.port is None:
        args.port = DEFAULT_MADLAD_PORT if args.backend == "madlad" else DEFAULT_ARGOS_PORT
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
