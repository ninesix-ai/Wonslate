# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Hermetic tests for the reverse-direction derivation (S12 T2; D62's third artefact).

`script/derive_av_direction.py` produces three derived files for `en-zh` -- the corpus with its
columns swapped, the seed pack with its term sides swapped, and the sub-domain group copy -- and
its `--check` mode is the claim that those three still match what they were derived from. Until
this suite existed that claim was never tested and never gated: the script was not registered in
either enumeration, and the two in-repo assertions covering the derivation (CorpusIntegrityTests)
only compare corpus and pack, not the groups.

Why a temp tree instead of mutating the repo's own files: a test that edits a published artefact
to prove a point damages the record it is supposed to protect. Here the forward corpus, pack and
grouping are copied out, `write()` derives all three, each case breaks exactly one derived copy,
and a control case asserts the untouched fixture passes -- so a red means drift, not a fixture
that cannot be satisfied.

Run:
    python tests/test_derive_av_direction.py
"""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "script"))

import derive_av_direction as D  # noqa: E402

FORWARD = "zh-en"
DERIVED = "en-zh"


class DriftTests(unittest.TestCase):
    """`check()` must report a drift in each of the three derived artefacts, and only that."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.eval_data = root / "eval-data"
        self.packs = root / "packs"
        self.ranges = root / "ranges.json"
        (self.eval_data / ("av-" + FORWARD)).mkdir(parents=True)
        self.packs.mkdir(parents=True)          # copyfile does not create the destination dir
        src = REPO / "script" / "eval-data" / ("av-" + FORWARD)
        for suffix in (".src", ".tgt"):
            shutil.copyfile(str(src / (("av-" + FORWARD) + suffix)),
                            str(self.eval_data / ("av-" + FORWARD) / (("av-" + FORWARD) + suffix)))
        shutil.copyfile(str(REPO / "docs" / "glossary-packs" / ("av-%s.json" % FORWARD)),
                        str(self.packs / ("av-%s.json" % FORWARD)))
        doc = json.loads((REPO / "script" / "av_domain_ranges.json").read_text(encoding="utf-8"))
        self.ranges.write_text(json.dumps(
            {"comment": "fixture: forward groups only", "ranges": {FORWARD: doc["ranges"][FORWARD]}},
            ensure_ascii=False), encoding="utf-8")
        self.orig = (D.EVAL_DATA, D.PACKS, D.RANGES)
        # Registered before the globals are touched, and the temp cleanup too: unittest does NOT
        # run tearDown when setUp raised, so a fixture that fails halfway would leave the module
        # pointed at a deleted directory and every later test in the suite would report a drift
        # that never happened -- which is exactly how one broken case here turned into six.
        self.addCleanup(self._restore_paths)
        self.addCleanup(self.tmp.cleanup)
        D.EVAL_DATA, D.PACKS, D.RANGES = self.eval_data, self.packs, self.ranges
        # Derive all three from the copies. Anything else in this class would then be testing
        # "missing files", which is a weaker claim than "a file that no longer matches".
        D.write()

    def _restore_paths(self):
        D.EVAL_DATA, D.PACKS, D.RANGES = self.orig

    def _derived_corpus(self, suffix):
        return (self.eval_data / ("av-" + DERIVED) / (("av-" + DERIVED) + suffix))

    def test_the_control_fixture_satisfies_the_check(self):
        # Without this, every red below is ambiguous: it could mean "drift detected" or "the
        # harness cannot produce a matching set in the first place".
        self.assertEqual(0, D.check())

    def test_drift_in_the_derived_corpus_is_reported(self):
        path = self._derived_corpus(".src")
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0], lines[-1] = lines[-1], lines[0]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(1, D.check(), "a reordered derived corpus passed as up to date")

    def test_drift_in_the_derived_seed_pack_is_reported(self):
        path = self.packs / ("av-%s.json" % DERIVED)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"][0]["target_term"] = payload["entries"][0]["target_term"] + "?"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(1, D.check(), "an edited derived term row passed as up to date")

    def test_drift_in_the_derived_group_copy_is_reported(self):
        # The artefact D62 added. If the derived direction's groups stop being the forward list,
        # the two corpora no longer share a row order and the sub-domain labels describe different
        # sentences -- the exact failure the per-direction loader was built to make visible.
        doc = json.loads(self.ranges.read_text(encoding="utf-8"))
        doc["ranges"][DERIVED] = doc["ranges"][DERIVED][:-1]
        self.ranges.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(1, D.check(), "a group copy one entry short passed as up to date")

    def test_check_reports_and_writes_nothing(self):
        # The script's own promise: `--check` must not repair behind the user's back, or a
        # verification run would silently become a build run.
        path = self._derived_corpus(".tgt")
        original = path.read_bytes()
        path.write_bytes(b"drifted\n")
        rc = D.check()
        self.assertEqual(1, rc)
        self.assertEqual(b"drifted\n", path.read_bytes(),
                         "--check rewrote the file it was only supposed to report on")
        path.write_bytes(original)


class ShippedArtefactsTests(unittest.TestCase):
    """The committed repo files, checked by the same code path -- this is the gate."""

    def test_the_shipped_derived_files_match_their_sources(self):
        # If this goes red, someone edited av-en-zh's corpus, pack or group copy by hand. The fix
        # is to re-derive, not to correct the assertion: the derived files are outputs.
        self.assertEqual(0, D.check(),
                         "a derived artefact in the repo no longer matches what it came from; "
                         "run python script/derive_av_direction.py")

    def test_the_derivation_covers_exactly_the_declared_directions(self):
        # DERIVED is the list `--check` walks. A direction added to the corpus without a derivation
        # entry would look fine here and drift forever, unnoticed; conversely an entry whose
        # forward corpus is gone would fail the check above with a confusing message.
        self.assertEqual([DERIVED], sorted(D.DERIVED))
        self.assertEqual(FORWARD, D.DERIVED[DERIVED])
        for direction in (FORWARD, DERIVED):
            prefix = "av-" + direction
            self.assertTrue((REPO / "script" / "eval-data" / prefix / (prefix + ".src")).is_file(),
                            "no corpus for %s" % direction)
            self.assertTrue((REPO / "docs" / "glossary-packs" / (prefix + ".json")).is_file(),
                            "no seed pack for %s" % direction)


if __name__ == "__main__":
    unittest.main()
