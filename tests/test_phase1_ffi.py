#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""
Phase 1 FFI smoke test (cross-language integration).

Validates the translator_engine native library boundary and the v2.0
full-featured pipeline. Complements translator-engine/tests/pipeline_e2e.rs:
- the Rust integration tests prove the pipeline logic itself is correct
- this file proves .NET / Python get a consistent JSON contract via the C ABI

Usage:
    # 1) build the release DLL first
    python script/build.py
    # 2) run this script
    python tests/test_phase1_ffi.py

Deps: standard library only (ctypes / json / pathlib / sys / time).
All output goes to stderr (like Rust eprintln!; redirects reliably even in
PowerShell).
"""

import ctypes
import json
import pathlib
import sys
import time


def _p(*args, **kw):
    """Single print funnel: everything goes to stderr."""
    kw.setdefault("file", sys.stderr)
    print(*args, **kw)


# ---------- Load the DLL ----------

def _find_dll() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    root = here.parent                              # repo root
    candidates = [
        root / "translator-engine" / "target" / "release" / "translator_engine.dll",
        root / "translator-engine" / "target" / "release" / "libtranslator_engine.so",
        root / "translator-engine" / "target" / "release" / "libtranslator_engine.dylib",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "translator_engine native library not found. Build first:\n"
        "    python script/build.py\n"
        f"Looked in:\n  " + "\n  ".join(str(c) for c in candidates)
    )


class Engine:
    """ctypes call wrapper; returned strings are freed automatically."""

    def __init__(self, dll_path: pathlib.Path):
        self.lib = ctypes.cdll.LoadLibrary(str(dll_path))
        # declare every pointer return as c_void_p, otherwise ctypes bytes-izes it and the pointer is lost
        self.lib.tt_version.argtypes = []
        self.lib.tt_version.restype = ctypes.c_void_p
        self.lib.tt_translate.argtypes = [ctypes.c_char_p] * 3
        self.lib.tt_translate.restype = ctypes.c_void_p
        self.lib.tt_free_string.argtypes = [ctypes.c_void_p]
        self.lib.tt_free_string.restype = None
        self.lib.tt_init.argtypes = [ctypes.c_char_p]
        self.lib.tt_init.restype = ctypes.c_void_p
        self.lib.tt_shutdown.argtypes = []
        self.lib.tt_shutdown.restype = None
        self.lib.tt_translate_full.argtypes = [ctypes.c_char_p]
        self.lib.tt_translate_full.restype = ctypes.c_void_p
        self.lib.tt_tm_lookup.argtypes = [ctypes.c_char_p] * 3
        self.lib.tt_tm_lookup.restype = ctypes.c_void_p
        self.lib.tt_tm_put.argtypes = [ctypes.c_char_p]
        self.lib.tt_tm_put.restype = ctypes.c_void_p
        self.lib.tt_engines.argtypes = []
        self.lib.tt_engines.restype = ctypes.c_void_p
        self.lib.tt_health.argtypes = []
        self.lib.tt_health.restype = ctypes.c_void_p

    def _take(self, ptr) -> str:
        """Read the string from a c_void_p and free it; a null pointer yields \"\"\."""
        if not ptr:
            return ""
        try:
            return ctypes.string_at(ptr).decode("utf-8", errors="replace")
        finally:
            self.lib.tt_free_string(ptr)

    def version(self) -> str:
        return self._take(self.lib.tt_version())

    def init(self, config_json: str = "{}") -> dict:
        return json.loads(self._take(self.lib.tt_init(config_json.encode("utf-8"))) or "{}")

    def shutdown(self):
        self.lib.tt_shutdown()

    def translate_full(self, req: dict) -> dict:
        return json.loads(self._take(
            self.lib.tt_translate_full(json.dumps(req).encode("utf-8"))
        ) or "{}")

    def translate_legacy(self, engine_id: str, text: str, lang_pair: str) -> dict:
        return json.loads(self._take(
            self.lib.tt_translate(engine_id.encode(), text.encode(), lang_pair.encode())
        ) or "{}")

    def tm_lookup(self, text: str, src: str, tgt: str):
        raw = self._take(self.lib.tt_tm_lookup(
            text.encode("utf-8"), src.encode("utf-8"), tgt.encode("utf-8")
        ))
        return json.loads(raw) if raw and raw != "null" else None

    def tm_put(self, entry: dict) -> dict:
        return json.loads(self._take(
            self.lib.tt_tm_put(json.dumps(entry).encode("utf-8"))
        ) or "{}")

    def engines(self) -> list:
        return json.loads(self._take(self.lib.tt_engines()) or "[]")

    def health(self) -> dict:
        return json.loads(self._take(self.lib.tt_health()) or "{}")


# ---------- Assertion helpers ----------

class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, name: str):
        self.passed += 1
        _p(f"  [PASS] {name}")

    def fail(self, name: str, msg: str):
        self.failed += 1
        _p(f"  [FAIL] {name}: {msg}")

    def skip(self, name: str, reason: str):
        self.skipped += 1
        _p(f"  [SKIP] {name}: {reason}")

    def summary(self) -> int:
        total = self.passed + self.failed + self.skipped
        _p("")
        _p(f"  passed {self.passed} / failed {self.failed} / skipped {self.skipped} / total {total}")
        return 0 if self.failed == 0 else 1


# ---------- Test cases ----------

def test_version_and_init(eng: Engine, ck: Checker):
    v = eng.version()
    if v and v[0].isdigit():
        ck.ok(f"tt_version reports a version: {v}")
    else:
        ck.fail("tt_version", f"bad version string: {v!r}")

    r = eng.init("{}")
    if r.get("ok"):
        ck.ok("tt_init succeeded")
    else:
        ck.fail("tt_init", f"returned: {r}")


def test_engines_and_health(eng: Engine, ck: Checker):
    engines = eng.engines()
    ids = [e.get("id") for e in engines]
    if "demo" in ids and "ollama" in ids:
        ck.ok(f"tt_engines contains demo + ollama: {ids}")
    else:
        ck.fail("tt_engines", f"missing expected ids, got: {ids}")

    h = eng.health()
    if h.get("status") == "ok" and "tm_entries" in h:
        ck.ok(f"tt_health: tm_entries={h['tm_entries']}")
    else:
        ck.fail("tt_health", f"unexpected: {h}")


def test_legacy_translate_still_works(eng: Engine, ck: Checker):
    """P0 stable-contract compatibility: tt_translate still works."""
    r = eng.translate_legacy("demo", "hello", "en-zh")
    if r.get("ok") and "你好" in (r.get("output") or ""):
        ck.ok("tt_translate(demo, hello, en-zh) returns the expected greeting")
    else:
        ck.fail("legacy tt_translate", f"got: {r}")


def test_translate_full_and_tm_hit(eng: Engine, ck: Checker):
    """Core loop: tm_put one entry, then translate_full on the same source must hit the TM."""
    unique = f"pipeline-tm-verification {int(time.time() * 1000)}"
    target = "PIPELINE_TM_HIT_MARK"

    # push one TM history entry straight through the FFI
    put = eng.tm_put({
        "source_text": unique,
        "source_lang": "zh",
        "target_text": target,
        "target_lang": "en",
        "engine": "manual",
        "quality": 0.95,
        "hit_count": 1,
        "domain": "",
    })
    if not put.get("ok"):
        ck.fail("tt_tm_put", f"put={put}")
        return

    req = {
        "input": unique,
        "source_lang": "zh",
        "target_lang": "en",
        "mode": "full",
        "privacy": False,
        "use_tm": True,
    }
    r = eng.translate_full(req)
    if r.get("source") == "tm_hit" and r.get("output") == target:
        ck.ok(f"translate_full hit the TM (latency={r.get('latency_ms')}ms)")
    else:
        ck.fail("TM hit", f"expected tm_hit/{target}, got {r}")


def test_privacy_never_escalates(eng: Engine, ck: Checker):
    """Privacy mode: no matter how low the confidence, ai_upgraded / ollama must never appear."""
    req = {
        "input": "这是一段机密内容验证隐私模式",
        "source_lang": "zh",
        "target_lang": "en",
        "mode": "full",
        "privacy": True,
        "use_tm": False,
    }
    r = eng.translate_full(req)
    src = r.get("source", "")
    eng_id = r.get("engine", "")
    # an Ok result or Err(NoResult) is fine, as long as it is not an AI upgrade
    if src == "ai_upgraded" or "ollama" in eng_id:
        ck.fail("PRIVACY guard", f"violation: source={src} engine={eng_id}")
    else:
        ck.ok(f"privacy mode did not escalate to AI (source={src or r.get('error', 'err')})")


def test_realtime_never_escalates(eng: Engine, ck: Checker):
    """Realtime mode: even low confidence must not escalate to AI (latency budget)."""
    req = {
        "input": f"realtime-verification {int(time.time() * 1000)}",
        "source_lang": "zh",
        "target_lang": "en",
        "mode": "realtime",
        "privacy": False,
        "use_tm": False,
    }
    r = eng.translate_full(req)
    if r.get("source") == "ai_upgraded":
        ck.fail("realtime latency contract", f"realtime escalated to AI: {r}")
    else:
        ck.ok(f"realtime did not escalate to AI (source={r.get('source', r.get('error', 'err'))})")


def test_engine_id_override(eng: Engine, ck: Checker):
    """Agent passthrough: an explicit engine_id must bypass the routing decision."""
    req = {
        "engine_id": "demo",
        "input": "hello",
        "source_lang": "en",
        "target_lang": "zh",
        "mode": "full",
        "use_tm": False,
    }
    r = eng.translate_full(req)
    if r.get("engine") == "demo":
        ck.ok("engine_id=demo passthrough honored")
    else:
        ck.fail("engine_id passthrough", f"got engine={r.get('engine')}")


def test_bad_input_handled(eng: Engine, ck: Checker):
    """Malformed JSON input must return an error structure instead of crashing."""
    ptr = eng.lib.tt_translate_full(b"this is not json")
    raw = eng._take(ptr)
    try:
        r = json.loads(raw)
    except Exception as e:
        ck.fail("malformed JSON input handling", f"unparseable: {e}; raw={raw[:100]}")
        return
    if r.get("ok") is False and r.get("error"):
        ck.ok(f"malformed input returned a structured error: error={r['error']}")
    else:
        ck.fail("malformed input must report an error", f"got {r}")


# ---------- Main flow ----------

def main() -> int:
    try:
        dll = _find_dll()
    except FileNotFoundError as e:
        _p(f"[ABORT] {e}")
        return 2

    _p(f"loaded DLL: {dll}")
    eng = Engine(dll)
    ck = Checker()

    try:
        _p("")
        _p("== basic interface ==")
        test_version_and_init(eng, ck)
        test_engines_and_health(eng, ck)

        _p("")
        _p("== backward compatibility (P0 stable contract) ==")
        test_legacy_translate_still_works(eng, ck)

        _p("")
        _p("== v2.0 full-featured pipeline ==")
        test_translate_full_and_tm_hit(eng, ck)
        test_privacy_never_escalates(eng, ck)
        test_realtime_never_escalates(eng, ck)
        test_engine_id_override(eng, ck)
        test_bad_input_handled(eng, ck)
    finally:
        eng.shutdown()
        _p("")
        _p("[tt_shutdown called]")

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
