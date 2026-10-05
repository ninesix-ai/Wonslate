# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for the seed-pack installer (Step 8.5 of the B-side runbook).

These assertions authored the shape of ``script/install_glossary_pack.py``. They
were first committed as RED-phase tests, back when the ``from
install_glossary_pack import ...`` line raised ``ModuleNotFoundError`` at
collection time - that was the intended RED signal. The module has since landed,
so the suite runs GREEN against the real implementation; what the assertions pin
has not changed.

Design in one sentence: the installer reads one or more pack JSONs from the
repo's ``docs/glossary-packs/`` directory and pipes each through the FFI
``tt_glossary_import_pack``. Everything that touches the DLL is behind a small
callable seam so tests stay hermetic.

Run: ``python -m unittest tests.test_install_glossary_pack -v``
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "script"))

from install_glossary_pack import (  # noqa: E402  (needs the sys.path shim above)
    DEFAULT_PACK_DIR, DEFAULT_REPO_ROOT, discover_packs, load_pack, import_pack,
    build_arg_parser,
)


def write_pack(root, domain="av", src_lang="zh", tgt_lang="en", entries=None):
    """Materialise one pack file at <root>/<domain>-<src>-<tgt>.json."""
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    pack = {
        "domain": domain,
        "source_lang": src_lang,
        "target_lang": tgt_lang,
        "version": "test-1",
        "entries": entries if entries is not None
                   else [{"source_term": "x", "target_term": "y"}],
    }
    path = root / "{}-{}-{}.json".format(domain, src_lang, tgt_lang)
    path.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    return path


class PathConstantTests(unittest.TestCase):
    """① Repo-root and default-pack-directory constants exist and are correct."""

    def test_default_repo_root_is_this_checkout(self):
        # The installer lives under <repo>/script, and its DEFAULT_PACK_DIR
        # must be a subdir of that same repo. This guards against a stray
        # absolute path (a S10 T1 lesson).
        #
        # D19: this used to assert the checkout directory is named "Wonslate",
        # which pinned one developer's folder wording instead of the intent
        # above, and failed on every renamed checkout - a git worktree, or CI's
        # actions/checkout with a path: input. The layout fact is what matters:
        # this file sits in <repo>/tests, so the repo root is two levels up.
        self.assertEqual(
            DEFAULT_REPO_ROOT,
            pathlib.Path(__file__).resolve().parents[1])

    def test_repo_root_follows_a_renamed_checkout(self):
        # The positive half of D19: resolve the installer from a clone whose
        # directory name is deliberately not "Wonslate" and confirm the root
        # tracks that location instead of a magic name. The module derives ROOT
        # from __file__ and imports nothing from the repository, so copying the
        # single file is enough to simulate the checkout under any name.
        origin = pathlib.Path(__file__).resolve().parents[1] / "script"
        with tempfile.TemporaryDirectory(prefix="wonslate-clone-") as td:
            clone = pathlib.Path(td) / "renamed-checkout"
            (clone / "script").mkdir(parents=True)
            copy = clone / "script" / "install_glossary_pack.py"
            shutil.copy2(origin / "install_glossary_pack.py", copy)
            self.assertNotEqual(clone.name, "Wonslate")
            spec = importlib.util.spec_from_file_location("igp_renamed", copy)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.DEFAULT_REPO_ROOT, clone.resolve())
            self.assertEqual(
                mod.DEFAULT_PACK_DIR.relative_to(mod.DEFAULT_REPO_ROOT).as_posix(),
                "docs/glossary-packs")

    def test_default_pack_dir_is_docs_glossary_packs(self):
        self.assertEqual(
            DEFAULT_PACK_DIR.relative_to(DEFAULT_REPO_ROOT).as_posix(),
            "docs/glossary-packs")


class DiscoveryTests(unittest.TestCase):
    """② ③: --domain filters pack files; unknown domains yield nothing."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_discover_yields_every_pack_when_domain_is_none(self):
        write_pack(self.tmp, domain="av", src_lang="zh", tgt_lang="en")
        write_pack(self.tmp, domain="medical", src_lang="zh", tgt_lang="en")
        found = sorted(p.name for p in discover_packs(self.tmp, domain=None))
        self.assertEqual(found, ["av-zh-en.json", "medical-zh-en.json"])

    def test_discover_filters_by_domain(self):
        write_pack(self.tmp, domain="av", src_lang="zh", tgt_lang="en")
        write_pack(self.tmp, domain="medical", src_lang="zh", tgt_lang="en")
        found = [p.name for p in discover_packs(self.tmp, domain="av")]
        self.assertEqual(found, ["av-zh-en.json"])

    def test_discover_returns_empty_list_when_no_match(self):
        write_pack(self.tmp, domain="medical")
        self.assertEqual(list(discover_packs(self.tmp, domain="av")), [])


class LoadPackTests(unittest.TestCase):
    """④: shape validation happens at load time so a bad file is caught before
    any FFI is called. Missing domain / source_lang / target_lang / entries is
    rejected with a readable ValueError."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_valid_pack_loads(self):
        p = write_pack(self.tmp)
        d = load_pack(p)
        self.assertEqual(d["domain"], "av")
        self.assertEqual(d["source_lang"], "zh")
        self.assertEqual(d["target_lang"], "en")
        self.assertGreaterEqual(len(d["entries"]), 1)

    def test_missing_domain_rejected(self):
        p = self.tmp / "bad-no-domain.json"
        p.write_text('{"source_lang":"zh","target_lang":"en","entries":[]}',
                     encoding="utf-8")
        with self.assertRaises(ValueError):
            load_pack(p)

    def test_missing_language_pair_rejected(self):
        p = self.tmp / "bad-no-langs.json"
        p.write_text('{"domain":"av","entries":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_pack(p)

    def test_missing_entries_rejected(self):
        p = self.tmp / "bad-no-entries.json"
        p.write_text('{"domain":"av","source_lang":"zh","target_lang":"en"}',
                     encoding="utf-8")
        with self.assertRaises(ValueError):
            load_pack(p)

    def test_malformed_json_rejected(self):
        p = self.tmp / "bad-syntax.json"
        p.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_pack(p)


class ImportPackTests(unittest.TestCase):
    """⑤ ⑥: import_pack(lib, pack) returns the FFI-reported count and raises
    on a failure ack. The lib is a duck-typed seam so no ctypes is touched
    in tests -- the real caller passes a ctypes-loaded library and this test
    passes a small stand-in that records what was sent."""

    def setUp(self):
        self.sent = []

    class _FakeLib:
        def __init__(self, on_call, ack_json):
            self._on_call = on_call
            self._ack = ack_json

        def tt_glossary_import_pack(self, body_bytes):
            # Records the exact bytes the caller built, then returns the
            # raw ack as a Python str so import_pack can parse it. The
            # real FFI returns a c_void_p; that shape is exercised by the
            # integration test in tests/test_domain_pack_loader.py.
            self._on_call(body_bytes)
            return self._ack

    def _lib(self, ack):
        return ImportPackTests._FakeLib(lambda b: self.sent.append(b), ack)

    def test_import_pack_returns_imported_count(self):
        pack = {"domain": "av", "source_lang": "zh", "target_lang": "en",
                "version": "test", "entries": [
                    {"source_term": "a", "target_term": "b"},
                    {"source_term": "c", "target_term": "d"},
                ]}
        lib = self._lib('{"ok":true,"imported":2}')
        n = import_pack(lib, pack)
        self.assertEqual(n, 2)
        self.assertEqual(len(self.sent), 1)
        body = json.loads(self.sent[0].decode("utf-8"))
        self.assertEqual(body["domain"], "av")
        self.assertEqual(len(body["entries"]), 2)

    def test_import_pack_raises_on_failure_ack(self):
        pack = {"domain": "", "source_lang": "zh", "target_lang": "en",
                "entries": [{"source_term": "a", "target_term": "b"}]}
        lib = self._lib('{"ok":false,"error":"INVALID_INPUT","message":"domain missing"}')
        with self.assertRaises(RuntimeError):
            import_pack(lib, pack)


class ArgParserTests(unittest.TestCase):
    """⑦ --domain and --all are mutually exclusive; without either the CLI
    errors out instead of silently importing nothing."""

    def test_domain_flag_accepted(self):
        parser = build_arg_parser()
        ns = parser.parse_args(["--domain", "av"])
        self.assertEqual(ns.domain, "av")
        self.assertFalse(ns.all)

    def test_all_flag_accepted(self):
        parser = build_arg_parser()
        ns = parser.parse_args(["--all"])
        self.assertTrue(ns.all)
        self.assertIsNone(ns.domain)

    def test_neither_domain_nor_all_exits(self):
        parser = build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])


if __name__ == "__main__":
    unittest.main()
