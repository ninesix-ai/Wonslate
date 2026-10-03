#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""install_glossary_pack.py -- import one or more seed packs from
``docs/glossary-packs/`` into the local glossary store via the FFI.

This is a thin CLI over the S11 FFI ``tt_glossary_import_pack``. Everything
that touches the ctypes-loaded library sits behind a duck-typed ``lib`` seam,
so the unit tests in tests/test_install_glossary_pack.py drive the same code
with a fake that records what was sent.

Prerequisite: the shared library must already be built. Run
``cargo build --release`` under ``translator-engine/`` first -- or, from
the repo root, ``python script/build.py``. Windows machines with Smart App
Control enabled may need a signed build; see docs/evidence and the S8 task
card for the signing procedure.

Usage:
    python script/install_glossary_pack.py --domain av
    python script/install_glossary_pack.py --all
    python script/install_glossary_pack.py --pack-dir path/to/packs --domain av
"""
import argparse
import ctypes
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_REPO_ROOT = ROOT
DEFAULT_PACK_DIR = ROOT / "docs" / "glossary-packs"

# The build artefact's file name is per-OS; the two candidate directories
# mirror what tests/test_domain_pack_loader.py already resolves.
_LIB_NAME = ("translator_engine.dll" if os.name == "nt"
             else "libtranslator_engine.dylib" if sys.platform == "darwin"
             else "libtranslator_engine.so")
_LIB_CANDIDATES = [
    ROOT / "translator-engine" / "target" / "release" / _LIB_NAME,
    ROOT / "translator-engine" / "target" / "debug" / _LIB_NAME,
]


def discover_packs(root, domain=None):
    """Yield pack paths under `root`, optionally filtered by pack-level domain.

    A "pack" is any ``*.json`` file. The domain filter reads the file's
    content rather than its name, so a misnamed file is still classified by
    what it claims to be. Files that fail validation are skipped silently
    here and surfaced by load_pack() at import time, so a partial tree does
    not prevent installing the rest."""
    root = pathlib.Path(root)
    if not root.exists():
        return
    for path in sorted(root.glob("*.json")):
        try:
            pack = load_pack(path)
        except ValueError:
            continue
        if domain is None or pack["domain"] == domain:
            yield path


def load_pack(path):
    """Parse one pack file and validate the shape the FFI importer requires.

    Any shape problem raises ValueError before we touch the library, so the
    FFI never receives a half-formed request. This is a client-side guard;
    the Rust side re-validates too (see tm/store.rs::glossary_import_pack)."""
    path = pathlib.Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("{}: malformed JSON: {}".format(path.name, exc))
    if not isinstance(raw, dict):
        raise ValueError("{}: top level must be an object".format(path.name))
    for key in ("domain", "source_lang", "target_lang", "entries"):
        if key not in raw:
            raise ValueError("{}: missing required key '{}'".format(path.name, key))
    if not isinstance(raw["entries"], list):
        raise ValueError("{}: entries must be an array".format(path.name))
    return raw


def import_pack(lib, pack):
    """Send one parsed pack through the FFI and return the imported count.

    `lib` is duck-typed. In production it is a ``ctypes.CDLL`` result whose
    ``tt_glossary_import_pack`` returns a raw pointer and whose
    ``tt_free_string`` releases it; in tests it is a small stand-in that
    records the bytes and hands back the ack as a str. Both paths converge
    on the same JSON shape and the same failure contract -- a non-``ok``
    ack raises RuntimeError so a batch loop can keep going."""
    body = json.dumps(pack, ensure_ascii=False).encode("utf-8")
    ack = lib.tt_glossary_import_pack(body)
    if isinstance(ack, (bytes, str)):
        text = ack.decode("utf-8") if isinstance(ack, bytes) else ack
    else:
        # ctypes path: `ack` is the pointer value; read the string then free.
        raw = ctypes.c_char_p(ack).value or b"{}"
        text = raw.decode("utf-8", errors="replace")
        lib.tt_free_string(ack)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFI returned non-JSON ack: {!r} ({})".format(text, exc))
    if not parsed.get("ok"):
        raise RuntimeError("FFI rejected pack: {}".format(text))
    return int(parsed.get("imported", 0))


def _load_engine():
    """Find and load translator_engine, set the two argtypes we call, and
    return the CDLL. Errors are surfaced with the exact rebuild command so
    a fresh clone knows what to run."""
    for p in _LIB_CANDIDATES:
        if p.exists():
            lib = ctypes.CDLL(str(p))
            lib.tt_init.argtypes = [ctypes.c_char_p]
            lib.tt_init.restype = ctypes.c_void_p
            lib.tt_shutdown.argtypes = []
            lib.tt_shutdown.restype = None
            lib.tt_glossary_import_pack.argtypes = [ctypes.c_char_p]
            lib.tt_glossary_import_pack.restype = ctypes.c_void_p
            lib.tt_free_string.argtypes = [ctypes.c_void_p]
            lib.tt_free_string.restype = None
            return lib
    raise SystemExit(
        "translator_engine shared library not found; expected one of:\n  "
        + "\n  ".join(str(c) for c in _LIB_CANDIDATES)
        + "\nRun `cargo build --release` under translator-engine/ first "
          "(or `python script/build.py` from the repo root).")


def build_arg_parser():
    """--domain and --all are mutually exclusive and one is required: an
    accidental no-op run (nothing selected) is more confusing than a hard
    argparse error."""
    parser = argparse.ArgumentParser(
        description="Install one or more glossary seed packs into the local store.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--domain",
                       help="install every pack whose `domain` matches (e.g. av)")
    group.add_argument("--all", action="store_true",
                       help="install every pack under --pack-dir regardless of domain")
    parser.add_argument("--pack-dir", default=str(DEFAULT_PACK_DIR),
                        help="root of *.json packs (default: docs/glossary-packs)")
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    pack_dir = pathlib.Path(args.pack_dir)
    domain = None if args.all else args.domain
    packs = list(discover_packs(pack_dir, domain=domain))
    if not packs:
        print("no packs found under {} (domain={})".format(
            pack_dir, domain or "any"))
        return 1
    lib = _load_engine()
    # tt_init is idempotent per the FFI contract and prepares the TM store;
    # without it, glossary_import_pack returns INVALID_INPUT because the
    # singleton is not yet materialised.
    ack_ptr = lib.tt_init(b"{}")
    if ack_ptr:
        lib.tt_free_string(ack_ptr)
    total = 0
    for p in packs:
        try:
            pack = load_pack(p)
            n = import_pack(lib, pack)
            print("  {:38s} -> {} entries".format(p.name, n))
            total += n
        except (ValueError, RuntimeError) as exc:
            print("  {:38s} -> FAIL: {}".format(p.name, exc))
    lib.tt_shutdown()
    print("installed {} entries from {} pack(s)".format(total, len(packs)))
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
