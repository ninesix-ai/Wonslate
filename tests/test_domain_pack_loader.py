# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""S11 RED-PHASE tests for the domain-aware FFI glossary surface.

See ``docs/tasks/S11-领域路由与术语包接线.md``. These assertions author the
desired contract; on the current tree they fail because:

  * ``tt_glossary_import_pack`` is not exported by ``translator_engine``
    (tests ⑩ and ⑪);
  * ``tt_glossary_list`` takes only (source_lang, target_lang) today, so the
    domain-scoped call cannot be exercised.

This is the correct RED signal: the tests fail because the feature does not
exist yet, not because of a typo or a bad fixture.

Run:  python -m unittest tests.test_domain_pack_loader -v
"""
import ctypes
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The DLL is built to translator-engine/target/{debug,release}/. The existing
# test_phase1_ffi.py locates it the same way; keep the loader in sync.
_LIB_CANDIDATES = [
    os.path.join("translator-engine", "target", "release",
                 "translator_engine.dll" if os.name == "nt" else "libtranslator_engine.so"),
    os.path.join("translator-engine", "target", "debug",
                 "translator_engine.dll" if os.name == "nt" else "libtranslator_engine.so"),
]


def _load_engine():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for rel in _LIB_CANDIDATES:
        p = os.path.join(root, rel)
        if os.path.exists(p):
            return ctypes.CDLL(p)
    raise unittest.SkipTest(
        "translator_engine shared library not built; "
        "run `python script/build.py` first (SAC may also block, see docs/09)")


class GlossaryImportPackTests(unittest.TestCase):
    """⑩ ⑪: the new ``tt_glossary_import_pack`` entry point."""

    @classmethod
    def setUpClass(cls):
        cls.lib = _load_engine()
        cls.lib.tt_init(b"{}")

    @classmethod
    def tearDownClass(cls):
        cls.lib.tt_shutdown()

    def _call(self, name, *args):
        fn = getattr(self.lib, name, None)
        if fn is None:
            self.fail(
                "FFI symbol {} is not exported. Add it in translator-engine/src/lib.rs "
                "so the domain-scoped glossary can be seeded.".format(name))
        fn.restype = ctypes.c_void_p
        encoded = [a.encode("utf-8") if isinstance(a, str) else a for a in args]
        ptr = fn(*encoded)
        if not ptr:
            return ""
        raw = ctypes.c_char_p(ptr).value or b""
        self.lib.tt_free_string(ctypes.c_void_p(ptr))
        return raw.decode("utf-8", errors="replace")

    def test_import_pack_returns_ok_for_well_formed_pack(self):
        pack = {
            "domain": "av",
            "version": "test-1",
            "entries": [
                {"source_term": "语段", "target_term": "speech segment", "confidence": 1.0},
                {"source_term": "音色", "target_term": "speaker timbre", "confidence": 1.0},
            ],
        }
        ack = self._call("tt_glossary_import_pack", json.dumps(pack, ensure_ascii=False))
        parsed = json.loads(ack or "{}")
        self.assertTrue(parsed.get("ok"), "well-formed pack must import; got {}".format(ack))
        self.assertEqual(parsed.get("imported"), 2)

    def test_import_pack_rejects_malformed_json_without_panicking(self):
        ack = self._call("tt_glossary_import_pack", "{not json")
        parsed = json.loads(ack or "{}")
        self.assertFalse(parsed.get("ok"))
        self.assertIn("error", parsed)

    def test_import_pack_rejects_pack_without_domain(self):
        # A pack with an empty domain would pollute the generic table; refuse it
        # loudly so a caller does not think a domain-scoped import succeeded.
        pack = {"domain": "", "version": "test-1",
                "entries": [{"source_term": "x", "target_term": "y", "confidence": 1.0}]}
        ack = self._call("tt_glossary_import_pack", json.dumps(pack))
        parsed = json.loads(ack or "{}")
        self.assertFalse(parsed.get("ok"))


if __name__ == "__main__":
    unittest.main()
