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
import os
import pathlib
import re
import subprocess
import sys
import tempfile
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


class _Export:
    """One native function, seen through the call recorder.

    Attribute access must not count as coverage: ``Engine.__init__`` declares
    argtypes / restype for every export it knows about whether or not a case ever
    calls it, so a recorder that logged declarations would report a full surface for
    a run that only configured pointers. The name is recorded in ``__call__``; the
    declaration writes are forwarded to the real function object untouched.
    """

    def __init__(self, name, fn, reached):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_fn", fn)
        object.__setattr__(self, "_reached", reached)

    def __call__(self, *args):
        self._reached.add(self._name)
        return self._fn(*args)

    def __getattr__(self, item):
        return getattr(self._fn, item)

    def __setattr__(self, key, value):
        if key.startswith("_"):
            object.__setattr__(self, key, value)
        else:
            setattr(self._fn, key, value)


class _Surface:
    """The ctypes handle, handing out recorded wrappers for every ``tt_*`` name.

    ``reached`` feeds the FFI-surface census at the end of the run, so the claim
    "every export has a cross-language caller" comes out of this execution instead
    of being asserted from a list maintained next to the tests.
    """

    def __init__(self, lib):
        object.__setattr__(self, "_lib", lib)
        object.__setattr__(self, "_reached", set())

    @property
    def reached(self):
        return self._reached

    def __getattr__(self, name):
        fn = getattr(self._lib, name)          # a typo still raises AttributeError
        if not name.startswith("tt_"):
            return fn
        return _Export(name, fn, self._reached)


class Engine:
    """ctypes call wrapper; returned strings are freed automatically."""

    def __init__(self, dll_path: pathlib.Path):
        self.lib = _Surface(ctypes.cdll.LoadLibrary(str(dll_path)))
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
        self.lib.tt_tm_flag_bad.argtypes = [ctypes.c_char_p] * 3
        self.lib.tt_tm_flag_bad.restype = ctypes.c_void_p
        self.lib.tt_tm_list.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        self.lib.tt_tm_list.restype = ctypes.c_void_p
        self.lib.tt_glossary_list.argtypes = [ctypes.c_char_p] * 2
        self.lib.tt_glossary_list.restype = ctypes.c_void_p
        self.lib.tt_glossary_upsert.argtypes = [ctypes.c_char_p]
        self.lib.tt_glossary_upsert.restype = ctypes.c_void_p
        self.lib.tt_glossary_delete.argtypes = [ctypes.c_char_p] * 3
        self.lib.tt_glossary_delete.restype = ctypes.c_void_p
        self.lib.tt_config_json.argtypes = []
        self.lib.tt_config_json.restype = ctypes.c_void_p
        self.lib.tt_engines.argtypes = []
        self.lib.tt_engines.restype = ctypes.c_void_p
        self.lib.tt_health.argtypes = []
        self.lib.tt_health.restype = ctypes.c_void_p
        # S11's two newest exports. They had a .NET caller and a dedicated loader
        # suite, so nothing shipped broken -- but this file is the one the CI job
        # names as the FFI smoke test while reaching 16 of the 18 exports.
        self.lib.tt_glossary_list_with_domain.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        self.lib.tt_glossary_list_with_domain.restype = ctypes.c_void_p
        self.lib.tt_glossary_import_pack.argtypes = [ctypes.c_char_p]
        self.lib.tt_glossary_import_pack.restype = ctypes.c_void_p

    @property
    def reached(self) -> set:
        """Which exports this run has actually called, filled in as it goes."""
        return self.lib.reached

    def _take(self, ptr) -> str:
        """Read the string from a c_void_p and free it; a null pointer returns empty."""
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

    def tm_flag_bad(self, text: str, src: str, tgt: str) -> dict:
        return json.loads(self._take(self.lib.tt_tm_flag_bad(
            text.encode("utf-8"), src.encode("utf-8"), tgt.encode("utf-8")
        )) or "{}")

    def tm_list(self, src: str, tgt: str, limit: int = 100) -> list:
        raw = self._take(self.lib.tt_tm_list(
            src.encode("utf-8"), tgt.encode("utf-8"), limit
        ))
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []

    def glossary_list(self, src: str, tgt: str) -> list:
        raw = self._take(self.lib.tt_glossary_list(
            src.encode("utf-8"), tgt.encode("utf-8")
        ))
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []

    def glossary_upsert(self, entry: dict) -> dict:
        return json.loads(self._take(
            self.lib.tt_glossary_upsert(json.dumps(entry).encode("utf-8"))
        ) or "{}")

    def glossary_delete(self, term: str, src: str, tgt: str) -> dict:
        return json.loads(self._take(self.lib.tt_glossary_delete(
            term.encode("utf-8"), src.encode("utf-8"), tgt.encode("utf-8")
        )) or "{}")

    def engines(self) -> list:
        return json.loads(self._take(self.lib.tt_engines()) or "[]")

    def config_json(self) -> dict:
        return json.loads(self._take(self.lib.tt_config_json()) or "{}")

    def health(self) -> dict:
        return json.loads(self._take(self.lib.tt_health()) or "{}")

    def glossary_list_with_domain(self, src: str, tgt: str, domain: str,
                                  limit: int = 100) -> list:
        raw = self._take(self.lib.tt_glossary_list_with_domain(
            src.encode("utf-8"), tgt.encode("utf-8"), domain.encode("utf-8"), limit
        ))
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []

    def glossary_import_pack(self, pack: dict) -> dict:
        return json.loads(self._take(
            self.lib.tt_glossary_import_pack(
                json.dumps(pack, ensure_ascii=False).encode("utf-8"))
        ) or "{}")


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


def test_tm_write_list_flag_bad_cycle(eng: Engine, ck: Checker):
    """TM management contract (N-07): write -> visible in list -> hit -> flag bad -> unreachable."""
    src = f"tm-manager-cycle {int(time.time() * 1000)}"
    target = "TM_MANAGER_MARK"

    put = eng.tm_put({
        "source_text": src, "source_lang": "zh",
        "target_text": target, "target_lang": "en",
        "engine": "manual", "quality": 1.0, "hit_count": 1, "domain": "",
    })
    if not put.get("ok"):
        ck.fail("tt_tm_put (manager cycle)", f"put={put}")
        return

    listed = eng.tm_list("zh", "en")
    if any(e.get("source_text") == src for e in listed):
        ck.ok(f"tt_tm_list shows the freshly written entry ({len(listed)} entries)")
    else:
        ck.fail("tt_tm_list", f"entry not listed; got {listed[:3]}")

    req = {"input": src, "source_lang": "zh", "target_lang": "en",
           "mode": "full", "privacy": False, "use_tm": True}
    r = eng.translate_full(req)
    if r.get("source") == "tm_hit" and r.get("output") == target:
        ck.ok("the UI-written TM entry is hit by the next request")
    else:
        ck.fail("TM hit after write", f"expected tm_hit, got {r}")
        return

    flag = eng.tm_flag_bad(src, "zh", "en")
    if not flag.get("ok"):
        ck.fail("tt_tm_flag_bad", f"flag={flag}")
        return

    if eng.tm_lookup(src, "zh", "en") is None:
        ck.ok("flagged entry is no longer returned by tt_tm_lookup")
    else:
        ck.fail("flag_bad lookup", "flagged entry still returned")

    if not any(e.get("source_text") == src for e in eng.tm_list("zh", "en")):
        ck.ok("flagged entry disappears from tt_tm_list")
    else:
        ck.fail("flag_bad list", "flagged entry still listed")

    r2 = eng.translate_full(req)
    if r2.get("source") != "tm_hit":
        ck.ok(f"after flagging, the pair is no longer served from TM (source={r2.get('source', r2.get('error'))})")
    else:
        ck.fail("flag_bad effect", f"flagged entry still served: {r2}")


def test_glossary_upsert_list_delete_cycle(eng: Engine, ck: Checker):
    """Glossary management contract (N-07): upsert -> listed -> delete -> gone."""
    term = f"术语管理验证-{int(time.time() * 1000)}"

    up = eng.glossary_upsert({
        "source_term": term, "source_lang": "zh",
        "target_term": "GLOSSARY_MANAGER_MARK", "target_lang": "en",
        "confidence": 1.0, "frequency": 1, "domain": "",
    })
    if not up.get("ok"):
        ck.fail("tt_glossary_upsert", f"upsert={up}")
        return

    entries = eng.glossary_list("zh", "en")
    match = [e for e in entries if e.get("source_term") == term]
    if match and match[0].get("target_term") == "GLOSSARY_MANAGER_MARK":
        ck.ok(f"tt_glossary_list returns the upserted term ({len(entries)} entries)")
    else:
        ck.fail("tt_glossary_list", f"term missing or wrong target: {match}")
        return

    # N-08 provenance: everything arriving through the user-edit endpoint is "manual",
    # so a machine-extracted ("distill") term can be told apart when reviewing the list.
    if match[0].get("source") == "manual":
        ck.ok("user-edited term is labelled source=manual")
    else:
        ck.fail("glossary provenance", f"expected source=manual, got {match[0].get('source')!r}")

    deleted = eng.glossary_delete(term, "zh", "en")
    if not deleted.get("ok"):
        ck.fail("tt_glossary_delete", f"delete={deleted}")
        return

    remaining = [e for e in eng.glossary_list("zh", "en") if e.get("source_term") == term]
    if not remaining:
        ck.ok("deleted term is gone from tt_glossary_list")
    else:
        ck.fail("delete effect", f"term still listed: {remaining}")


def test_malformed_glossary_payload_is_structured(eng: Engine, ck: Checker):
    """A malformed upsert payload must yield a structured error, never a crash."""
    raw = eng._take(eng.lib.tt_glossary_upsert(b"not json"))
    try:
        r = json.loads(raw)
    except Exception as e:
        ck.fail("malformed glossary upsert", f"unparseable: {e}; raw={raw[:100]}")
        return
    if r.get("ok") is False and r.get("error"):
        ck.ok(f"malformed glossary payload returned a structured error: {r['error']}")
    else:
        ck.fail("malformed glossary payload must report an error", f"got {r}")


def test_domain_scoped_reads_and_pack_import(eng: Engine, ck: Checker):
    """S11's two newest exports over the same boundary an agent would use.

    A non-empty domain returns generic rows plus that domain's rows and nothing else,
    and an untagged pack is refused -- the refusal is what stops a seed file from
    quietly becoming the user's global glossary.
    """
    src, tgt = "zhs11", "ens11"          # own language pair: other checks write zh/en
    pack = {
        "domain": "av", "source_lang": src, "target_lang": tgt,
        "entries": [
            {"source_term": "混音器", "target_term": "mixer"},
            {"source_term": "采样率", "target_term": "sample rate"},
        ],
    }

    ack = eng.glossary_import_pack(pack)
    if not (ack.get("ok") and ack.get("imported") == 2):
        ck.fail("tt_glossary_import_pack", f"ack={ack}")
        return
    ck.ok("tt_glossary_import_pack imported both rows of a tagged pack")

    scoped = eng.glossary_list_with_domain(src, tgt, "av", 100)
    generic = eng.glossary_list_with_domain(src, tgt, "", 100)
    terms = sorted(e.get("source_term") for e in scoped)
    if terms != sorted(p["source_term"] for p in pack["entries"]):
        ck.fail("tt_glossary_list_with_domain", f"scoped read returned {terms}")
        return
    if generic:
        ck.fail("domain rows must stay out of a generic read", f"got {generic}")
        return
    ck.ok("a scoped read sees the pack, a generic read does not")

    stamped = [e for e in scoped if e.get("source") == "seed:av"]
    if len(stamped) == len(scoped):
        ck.ok("imported rows carry source=seed:av so a reviewer can spot them")
    else:
        ck.fail("pack provenance", f"{len(stamped)}/{len(scoped)} stamped: {scoped}")

    untagged = dict(pack)
    untagged["domain"] = ""
    bad = eng.glossary_import_pack(untagged)
    if bad.get("ok") is False and bad.get("error"):
        ck.ok(f"an untagged pack is refused with a reason: {bad['error']}")
    else:
        ck.fail("untagged pack must be refused", f"got {bad}")


def test_config_json_shape(eng: Engine, ck: Checker):
    """tt_config_json (D15) must expose the effective routing, not just the defaults.

    The settings page renders this verbatim, so the shape is a contract: without
    upgrade_policy / tm_quality_floor the page cannot say what is actually in force,
    and without settings_file it cannot show where a change is written.
    """
    cfg = eng.config_json()
    routing = cfg.get("routing")
    if not isinstance(routing, dict):
        ck.fail("tt_config_json", f"missing routing block: {cfg}")
        return

    full = routing.get("full", {})
    missing = [k for k in ("upgrade_policy", "tm_quality_floor") if k not in full]
    if missing:
        ck.fail("tt_config_json routing.full", f"missing {missing}: {full}")
    else:
        ck.ok(f"tt_config_json exposes the full rule "
              f"(policy={full['upgrade_policy']}, floor={full['tm_quality_floor']})")

    if "realtime" in routing and "settings_file" in cfg and "routes_file" in cfg:
        ck.ok("tt_config_json names both config files (settings_file / routes_file)")
    else:
        ck.fail("tt_config_json files", f"got keys: {sorted(cfg)}")

    if isinstance(cfg.get("env_pinned"), list):
        ck.ok(f"env_pinned is a list (overriding vars: {cfg['env_pinned'] or 'none'})")
    else:
        ck.fail("tt_config_json env_pinned", f"expected a list, got {cfg.get('env_pinned')!r}")


# Run in a brand-new process so a fresh OnceLock reads the environment: the parent
# process already pinned its config, and precedence is exactly what is under test.
_CONFIG_PROBE = r'''
import ctypes, sys
lib = ctypes.cdll.LoadLibrary(sys.argv[1])
lib.tt_init.argtypes = [ctypes.c_char_p]; lib.tt_init.restype = ctypes.c_void_p
lib.tt_config_json.argtypes = []; lib.tt_config_json.restype = ctypes.c_void_p
lib.tt_free_string.argtypes = [ctypes.c_void_p]; lib.tt_free_string.restype = None
ack = lib.tt_init(b"{}")          # what the app does at start-up
lib.tt_free_string(ack)
p = lib.tt_config_json()
print(ctypes.string_at(p).decode("utf-8"))
lib.tt_free_string(p)
'''


def _config_in_fresh_process(dll: pathlib.Path, extra_env: dict) -> dict:
    """Load the engine in a clean child process and return its effective config."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LT_", "WONSLATE_"))}
    env.update(extra_env)
    with tempfile.TemporaryDirectory(prefix="wonslate-cfg-probe-") as tmp:
        probe = pathlib.Path(tmp) / "probe.py"
        probe.write_text(_CONFIG_PROBE, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(probe), str(dll)],
            capture_output=True, text=True, env=env, timeout=120,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"probe failed (rc={proc.returncode}): {proc.stderr[-400:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_settings_and_env_precedence(eng: Engine, ck: Checker, dll: pathlib.Path):
    """D15 precedence, end to end: defaults < settings file < environment variable.

    The environment must win, because that is the whole point of the top layer: support
    and reproduction have to be able to override what someone clicked in the UI.
    """
    with tempfile.TemporaryDirectory(prefix="wonslate-settings-") as tmp:
        data_dir = pathlib.Path(tmp)
        settings = data_dir / "config" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"quality_preference": "cost_first"}', encoding="utf-8")
        base = {"LT_DATA_DIR": str(data_dir)}

        try:
            plain = _config_in_fresh_process(dll, base)
        except Exception as e:
            ck.fail("settings precedence probe", str(e))
            return

        full = plain["routing"]["full"]
        if full["upgrade_policy"] == "low_confidence" and full["tm_quality_floor"] == 0.0:
            ck.ok("settings.json cost_first is applied (policy=low_confidence, floor=0.0)")
        else:
            ck.fail("settings.json application", f"got {full}")

        if plain.get("settings_file", "").replace("\\", "/").endswith("config/settings.json"):
            ck.ok(f"settings_file points inside the data dir: {plain['settings_file']}")
        else:
            ck.fail("settings_file path", f"got {plain.get('settings_file')!r}")

        try:
            overridden = _config_in_fresh_process(
                dll, {**base, "WONSLATE_QUALITY_PREFERENCE": "quality_first"})
        except Exception as e:
            ck.fail("env override probe", str(e))
            return

        full2 = overridden["routing"]["full"]
        if full2["upgrade_policy"] == "always" and abs(full2["tm_quality_floor"] - 0.95) < 1e-6:
            ck.ok("WONSLATE_QUALITY_PREFERENCE overrides the saved cost_first preference")
        else:
            ck.fail("environment precedence", f"expected always/0.95, got {full2}")

        if "WONSLATE_QUALITY_PREFERENCE" in overridden.get("env_pinned", []):
            ck.ok("env_pinned names the overriding variable (so the UI can explain it)")
        else:
            ck.fail("env_pinned", f"missing the overriding var: {overridden.get('env_pinned')}")

        # An explicit knob must beat the preference it is derived from: a deployment
        # pinning the floor keeps its value whatever preference the user saved.
        try:
            knobs = _config_in_fresh_process(dll, {
                **base,
                "WONSLATE_QUALITY_PREFERENCE": "quality_first",
                "WONSLATE_FULL_TM_QUALITY_FLOOR": "0.42",
            })
        except Exception as e:
            ck.fail("explicit knob probe", str(e))
            return

        floor = knobs["routing"]["full"]["tm_quality_floor"]
        if abs(floor - 0.42) < 1e-6:
            ck.ok("an explicit env knob beats the preference it is derived from")
        else:
            ck.fail("explicit knob precedence", f"expected floor 0.42, got {floor}")


# The serving floor (D15/D16) is only worth enforcing while an engine that can clear it is
# up: with the AI engine down, refusing a cached entry re-runs the very engine that wrote
# it and returns the same text at full latency. Each case therefore needs its own process
# AND its own reachability - which this probe gets, because the parent decides what is
# listening on the port the child is pointed at.
_FLOOR_PROBE = r"""
import ctypes, json, sys
lib = ctypes.cdll.LoadLibrary(sys.argv[1])
lib.tt_init.argtypes = [ctypes.c_char_p]; lib.tt_init.restype = ctypes.c_void_p
lib.tt_tm_put.argtypes = [ctypes.c_char_p]; lib.tt_tm_put.restype = ctypes.c_void_p
lib.tt_translate_full.argtypes = [ctypes.c_char_p]; lib.tt_translate_full.restype = ctypes.c_void_p
lib.tt_free_string.argtypes = [ctypes.c_void_p]; lib.tt_free_string.restype = None
lib.tt_free_string(lib.tt_init(b'{}'))
entry = {'source_text': sys.argv[2], 'source_lang': 'en', 'target_text': 'CACHED_MARK',
         'target_lang': 'zh', 'engine': 'argos', 'quality': float(sys.argv[3]),
         'hit_count': 1, 'domain': ''}
lib.tt_free_string(lib.tt_tm_put(json.dumps(entry).encode('utf-8')))
req = {'input': sys.argv[2], 'source_lang': 'en', 'target_lang': 'zh',
       'mode': 'full', 'privacy': False, 'use_tm': True}
out = lib.tt_translate_full(json.dumps(req).encode('utf-8'))
print(ctypes.string_at(out).decode('utf-8'))
lib.tt_free_string(out)
"""


def _free_port() -> int:
    """A port nothing is listening on (binding then closing leaves it free)."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _AcceptOnlyServer:
    """Accepts and immediately drops every connection.

    Enough for the reachability probe, which only needs a successful TCP connect, while a
    real request against it fails at once - so a test never waits out an engine timeout.
    """

    def __init__(self):
        import socket, threading
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.close()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def _floor_probe(dll: pathlib.Path, source_text: str, quality: float, extra_env: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LT_", "WONSLATE_"))}
    env.update(extra_env)
    with tempfile.TemporaryDirectory(prefix="wonslate-floor-probe-") as tmp:
        probe = pathlib.Path(tmp) / "probe.py"
        probe.write_text(_FLOOR_PROBE, encoding="utf-8")
        env["LT_DATA_DIR"] = str(pathlib.Path(tmp) / "data")
        proc = subprocess.run(
            [sys.executable, str(probe), str(dll), source_text, str(quality)],
            capture_output=True, text=True, env=env, timeout=120,
        )
    if proc.returncode != 0:
        raise RuntimeError("probe failed (rc=%d): %s" % (proc.returncode, proc.stderr[-400:]))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_quality_floor_needs_a_reachable_upgrade_engine(dll: pathlib.Path, ck: Checker):
    """D16: enforcing the floor is only worthwhile while something can clear it.

    Same entry, same request, same preference - the only difference is whether the upgrade
    engine is listening. Offline it must serve the cache and say the promise was not kept;
    online it must refuse, because something can actually do better.
    """
    # An input the local fallback chain can also produce, so a difference between the two
    # runs is attributable to the cache decision rather than to an engine returning nothing.
    source = "hello world"
    base = {"WONSLATE_QUALITY_PREFERENCE": "quality_first", "WONSLATE_OLLAMA_TIMEOUT_MS": "500"}
    local_quality = 0.88

    try:
        offline = _floor_probe(dll, source, local_quality,
                               {**base, "WONSLATE_OLLAMA_URL": "http://127.0.0.1:%d" % _free_port()})
    except Exception as e:
        ck.fail("floor probe (engine down)", str(e)); return

    if offline.get("source") == "tm_hit" and offline.get("output") == "CACHED_MARK":
        ck.ok("upgrade engine down: the cached 0.88 entry is served, not re-translated")
    else:
        ck.fail("offline cache reuse", "expected the cached entry, got %s" % offline)

    if "quality preference not honoured" in (offline.get("message") or ""):
        ck.ok("the unkept quality promise is stated in the response, not silent")
    else:
        ck.fail("unkept promise not stated", "message=%r" % offline.get("message"))

    server = _AcceptOnlyServer()
    try:
        online = _floor_probe(dll, source, local_quality,
                              {**base, "WONSLATE_OLLAMA_URL": "http://127.0.0.1:%d" % server.port})
    except Exception as e:
        ck.fail("floor probe (engine up)", str(e)); return
    finally:
        server.close()

    if online.get("source") != "tm_hit":
        ck.ok("upgrade engine reachable: the same entry is refused (source=%s)"
              % online.get("source", online.get("error")))
    else:
        ck.fail("online refusal", "the floor must still be enforced, got %s" % online)

    if "below this mode's" in (online.get("message") or ""):
        ck.ok("the refusal names the floor that caused it")
    else:
        ck.fail("refusal note", "message=%r" % online.get("message"))

    # An entry that already clears the floor is served either way: the gate must not turn
    # into "refuse everything while offline".
    try:
        high = _floor_probe(dll, source, 0.95,
                            {**base, "WONSLATE_OLLAMA_URL": "http://127.0.0.1:%d" % _free_port()})
    except Exception as e:
        ck.fail("floor probe (AI-quality entry)", str(e)); return

    if high.get("source") == "tm_hit" and not high.get("message"):
        ck.ok("an entry that clears the floor is served cleanly, offline included")
    else:
        ck.fail("AI-quality entry", "expected a clean tm_hit, got %s" % high)


def test_init_reports_ignored_config_keys(eng: Engine, ck: Checker):
    """tt_init must name the keys it did not understand instead of dropping them silently."""
    raw = eng._take(eng.lib.tt_init(json.dumps({
        "full": {"upgrade_policy": "always"},
        "not_a_real_key": 1,
    }).encode("utf-8")))
    ack = json.loads(raw or "{}")
    if ack.get("ok") and "not_a_real_key" in ack.get("ignored_config_keys", []):
        ck.ok(f"unknown tt_init keys are reported back: {ack['ignored_config_keys']}")
    else:
        ck.fail("tt_init ignored keys", f"got {ack}")


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


# ---------- FFI surface census ----------

def test_every_export_is_reached_across_the_abi(eng: Engine, ck: Checker):
    """The claim REQ-F3 used to write as a coverage number, `lib.rs FFI >=80%`.

    No measurement can move that number. llvm-cov instruments the binary `cargo test`
    builds, while lib.rs is exercised by Python ctypes and .NET P/Invoke loading the
    compiled cdylib -- outside the instrumented process -- so the file reports 0.00%
    however much of it the cross-language suites actually cover. A target no tool can
    measure is not a target. This is the decidable version of the same intent: every
    `#[no_mangle] extern "C"` export in lib.rs must have been called by this run.

    Stated limit: it proves each export is reachable across the ABI and answered, not
    that the deepest branch inside it ran -- that stays the job of the Rust suites.
    """
    lib_rs = (pathlib.Path(__file__).resolve().parents[1]
              / "translator-engine" / "src" / "lib.rs")
    try:
        source = lib_rs.read_text(encoding="utf-8")
    except OSError as e:
        ck.fail("ffi surface census", f"cannot read {lib_rs}: {e}")
        return

    exports = set(re.findall(r'pub extern "C" fn (tt_\w+)', source))
    if not exports:
        ck.fail("ffi surface census",
                f"no exports parsed out of {lib_rs} -- the census regex and lib.rs "
                "have drifted apart, which would make this check vacuous")
        return

    unreached = sorted(exports - eng.reached)
    if unreached:
        ck.fail("every export has a cross-language caller",
                f"{len(unreached)} of {len(exports)} never called in this run: {unreached}")
    else:
        ck.ok(f"all {len(exports)} exports were called across the C ABI in this run")


# ---------- Main flow ----------

def main() -> int:
    try:
        dll = _find_dll()
    except FileNotFoundError as e:
        _p(f"[ABORT] {e}")
        return 2

    _p(f"loaded DLL: {dll}")

    # Hermetic run: pin the engine to a throw-away data dir, the same isolation
    # _floor_probe() already applies to its child processes. Without it the suite
    # reads and writes the developer's live TM/glossary, so test rows accumulate in
    # the real store forever, and tm_list() - which returns the hit_count-ordered
    # top `limit` rows - can push a freshly written entry out of the window on a
    # host whose TM already holds more than `limit` rows for that language pair.
    # Ambient LT_/WONSLATE_ variables are dropped for the same reason: the result
    # must not depend on how this machine happens to be configured.
    for _key in [k for k in os.environ if k.startswith(("LT_", "WONSLATE_"))]:
        del os.environ[_key]
    store = tempfile.TemporaryDirectory(prefix="wonslate-ffi-")
    os.environ["LT_DATA_DIR"] = str(pathlib.Path(store.name) / "data")

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

        _p("")
        _p("== TM / glossary management (N-07) ==")
        test_tm_write_list_flag_bad_cycle(eng, ck)
        test_glossary_upsert_list_delete_cycle(eng, ck)
        test_malformed_glossary_payload_is_structured(eng, ck)
        test_domain_scoped_reads_and_pack_import(eng, ck)

        _p("")
        _p("== settings / routing configuration (D15) ==")
        test_config_json_shape(eng, ck)
        test_settings_and_env_precedence(eng, ck, dll)
        test_init_reports_ignored_config_keys(eng, ck)
        test_quality_floor_needs_a_reachable_upgrade_engine(dll, ck)
    finally:
        eng.shutdown()
        store.cleanup()
        _p("")
        _p("[tt_shutdown called]")

    # Last, so the census sees the whole run including the shutdown above. A suite
    # that declares an export but never calls it must not be allowed to count.
    test_every_export_is_reached_across_the_abi(eng, ck)

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
