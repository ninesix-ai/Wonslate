#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""D70 -- every measured run must say *when* and *against which build*.

Why these tests exist. S13 T3 repeated an arm three times to get a within-arm noise
floor, and the repeats straddled a rebuilt DLL without anything recording that. The file
header had one `generated_at`, rewritten by every append, so it re-dated the earlier
repeats instead of identifying them; and `_spread()` took max-minus-min across whatever
runs were present, which measures the two builds rather than sampling noise.

The assertions are paired on purpose. A guard that only ever reports "mixed" would pass by
being useless, so each rejection case sits next to the accepted case it must not catch
(same-build repeats still yield a floor; a same-build append prints no warning).
"""
import contextlib
import datetime
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import bench_domain_av as bench       # noqa: E402
import run_av_baseline as rab         # noqa: E402
import install_glossary_pack as bench_identity   # noqa: E402  (home of measurement_identity)


class FakeLib:
    """Stands in for a ctypes.CDLL: it only has to expose `_name`."""

    def __init__(self, path):
        self._name = str(path)


def run_at(sha, comet=None):
    run = {"engine_build_sha": sha}
    if comet is not None:
        run["comet"] = comet
    return run


class BuildIdentityTests(unittest.TestCase):
    def test_sha_is_of_the_library_actually_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = pathlib.Path(tmp) / "translator_engine.dll"
            lib.write_bytes(b"build-one")
            import hashlib
            self.assertEqual(bench.engine_build_sha(lib=FakeLib(lib)),
                             hashlib.sha256(b"build-one").hexdigest()[:12])

    def test_two_builds_of_same_size_differ(self):
        # The whole point: size or version cannot tell these apart, so the identity has to.
        with tempfile.TemporaryDirectory() as tmp:
            a, b = pathlib.Path(tmp) / "a.dll", pathlib.Path(tmp) / "b.dll"
            a.write_bytes(b"build-one")
            b.write_bytes(b"build-two")
            self.assertEqual(a.stat().st_size, b.stat().st_size)
            self.assertNotEqual(bench.engine_build_sha(FakeLib(a)),
                                bench.engine_build_sha(FakeLib(b)))

    def test_nothing_loaded_is_reported_as_unknown_not_a_guess(self):
        self.assertIsNone(bench.engine_build_sha(lib=None))
        self.assertIsNone(bench.engine_build_sha(FakeLib(pathlib.Path("/no/such/file.dll"))))


class EvidenceWritesIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._evidence_dir = bench.EVIDENCE_DIR
        bench.EVIDENCE_DIR = pathlib.Path(self.tmp.name) / "evidence"

    def tearDown(self):
        bench.EVIDENCE_DIR = self._evidence_dir

    def _write(self, sha, run_index=1, engine="argos"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = bench._write_evidence(engine, "en-zh", ["hello"], ["你好"],
                                        ["hi"], {"chrF": 40.0}, 1, domain="",
                                        run_index=run_index, build_sha=sha,
                                        now=datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc))
        return out, buf.getvalue()

    def test_each_run_records_time_and_build(self):
        out, _ = self._write("a" * 12)
        record = json.loads(out.read_text(encoding="utf-8"))
        run = record["runs"][0]
        self.assertEqual(run["engine_build_sha"], "a" * 12)
        self.assertTrue(run["generated_at"].startswith("2026-10-15"), run["generated_at"])

    def test_repeats_keep_their_own_timestamps(self):
        # The defect: one file-level timestamp rewritten on append makes every earlier
        # repeat look as if it happened at the last write.
        first = json.loads(self._write("a" * 12)[0].read_text(encoding="utf-8"))
        second = json.loads(self._write("a" * 12, run_index=2)[0].read_text(encoding="utf-8"))
        self.assertEqual(len(second["runs"]), 2)
        self.assertEqual([r["generated_at"] for r in second["runs"]],
                         [first["runs"][0]["generated_at"]] * 2)

    def test_same_build_append_is_silent(self):
        self._write("b" * 12)
        _out, printed = self._write("b" * 12, run_index=2)
        self.assertNotIn("identity", printed,
                         "a legitimate same-build repeat must not be flagged: %s" % printed)

    def test_cross_build_append_says_so_when_it_happens(self):
        self._write("c" * 12)
        _out, printed = self._write("d" * 12, run_index=2)
        self.assertIn("NOT a noise floor", printed)
        self.assertIn("d" * 12, printed)

    def test_legacy_runs_without_an_id_are_named_not_assumed(self):
        self._write(None)                       # evidence as it looked before D70
        _out, printed = self._write("e" * 12, run_index=2)
        self.assertIn("before D70", printed)

    def test_null_identity_is_written_as_null(self):
        out, _ = self._write(None)
        record = json.loads(out.read_text(encoding="utf-8"))
        self.assertIsNone(record["runs"][0]["engine_build_sha"])


class FloorRefusesToPoolBuildsTests(unittest.TestCase):
    def test_same_build_repeats_still_yield_a_floor(self):
        # Without this, "refuse to merge builds" could pass by never returning a number.
        runs = [run_at("f" * 12, 0.80), run_at("f" * 12, 0.82)]
        self.assertAlmostEqual(rab._spread(runs), 0.02)
        self.assertIsNone(rab._spread_note(runs))

    def test_cross_build_repeats_yield_no_floor_and_say_why(self):
        runs = [run_at("a" * 12, 0.80), run_at("b" * 12, 0.95)]
        self.assertIsNone(rab._spread(runs))
        self.assertEqual(rab._spread_note(runs), "repeats-across-builds")

    def test_a_real_floor_inside_one_build_survives_another_builds_single_run(self):
        runs = [run_at("a" * 12, 0.80), run_at("a" * 12, 0.83), run_at("b" * 12, 0.99)]
        self.assertAlmostEqual(rab._spread(runs), 0.03)

    def test_missing_id_keeps_the_number_but_says_it_is_assumed(self):
        # Pre-D70 evidence: a spread is still computable, but "no id recorded" is not the
        # same fact as "same build". Suppressing the number would hide what was collected;
        # printing it without the caveat would be the original mistake.
        runs = [{"comet": 0.8}, {"comet": 0.9}]
        self.assertAlmostEqual(rab._spread(runs), 0.1)
        self.assertEqual(rab._spread_note(runs), "repeats-build-id-unknown")

    def test_a_computed_floor_still_carries_the_caveat(self):
        text = rab._status(True, True, 0, 0.001, 0.1, repeats=2,
                           floor_note="repeats-build-id-unknown")
        self.assertIn("假定重复同批", text)
        self.assertIn("噪声地板", text)

    def test_mixed_ids_with_real_repeats_are_flagged_too(self):
        # Each build has its own floor, so a number exists -- and the mixture still has to
        # be named, or the report would read the two builds as one measurement.
        runs = [run_at("a" * 12, 0.80), run_at("a" * 12, 0.84),
                run_at("b" * 12, 0.90), run_at("b" * 12, 0.95)]
        self.assertAlmostEqual(rab._spread(runs), 0.04)
        self.assertEqual(rab._spread_note(runs), "repeats-across-builds")

    def test_single_run_produces_no_warning_beyond_not_repeated(self):
        self.assertIsNone(rab._spread_note([run_at("a" * 12, 0.8)]))

    def test_unscored_repeats_keep_their_old_wording(self):
        runs = [{"engine_build_sha": "a" * 12}, {"engine_build_sha": "a" * 12}]
        self.assertEqual(rab._spread_note(runs), "repeats-unscored")


class StatusColumnDistinguishesCauses(unittest.TestCase):
    """D57/D58's rule applied to the floor column: opposite causes, different sentences."""

    def _say(self, note, repeats=3):
        return rab._status(True, True, 0, 0.001, None, repeats=repeats, floor_note=note)

    def test_cross_build_is_named_as_cross_build(self):
        text = self._say("repeats-across-builds")
        self.assertIn("D70", text)
        self.assertNotIn("未复采", text)

    def test_unknown_id_is_not_silently_trusted(self):
        text = self._say("repeats-build-id-unknown")
        self.assertIn("不能假定同一批次", text)

    def test_unscored_repeats_still_say_repeated(self):
        text = self._say("repeats-unscored")
        self.assertIn("COMET 未打分", text)

    def test_a_genuine_single_run_still_says_not_repeated(self):
        text = rab._status(True, True, 0, 0.001, None, repeats=1, floor_note=None)
        self.assertIn("未复采", text)
        self.assertIn("D70", self._say("repeats-across-builds"))

    def test_a_real_floor_still_decides_the_delta(self):
        text = rab._status(True, True, 0, 0.001, 0.02, repeats=2, floor_note=None)
        self.assertIn("噪声地板", text)


class SharedIdentityTests(unittest.TestCase):
    """D71: two more harnesses stamped "when" in ways that could not be compared."""

    def test_timestamp_is_utc_with_an_offset(self):
        # A naive local timestamp cannot be ordered against another machine's run, which is
        # exactly how bench_tm's `time.strftime("%Y-%m-%dT%H:%M:%S")` failed: readable, not
        # comparable. Parsing and checking tzinfo is the difference between the two.
        fields = bench_identity.measurement_identity()
        parsed = datetime.datetime.fromisoformat(fields["generated_at"])
        self.assertIsNotNone(parsed.tzinfo, fields["generated_at"])
        self.assertEqual(fields.keys(), {"generated_at", "engine_build_sha",
                                         "engine_build_source"})

    def test_injected_time_is_still_utc_aware(self):
        # Fixing the clock in a test must not be what introduces the naive timestamp.
        fields = bench_identity.measurement_identity(
            now=datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc))
        self.assertTrue(fields["generated_at"].endswith("+00:00"), fields["generated_at"])

    def test_every_evidence_writer_uses_the_one_helper(self):
        """Wiring rule: a harness that writes evidence must stamp identity through
        `measurement_identity` (or the helpers that call it). D70 was one harness
        forgetting; D71 was two harnesses each inventing a different way to be wrong."""
        writers = ("bench_flores.py", "bench_domain_av.py", "bench_translation.py",
                   "bench_tm.py")
        for name in writers:
            with self.subTest(name):
                text = (ROOT / "script" / name).read_text(encoding="utf-8")
                self.assertTrue("measurement_identity(" in text or "identity_fields(" in text,
                                "%s writes evidence without the shared identity helper" % name)

    def test_no_naive_timestamp_expression_survives(self):
        # The shape bench_tm had. Banning it in source beats discovering it in a report.
        offenders = []
        for path in (ROOT / "script").glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text.split("\n"):
                if ("time.strftime(" in line and "%z" not in line
                        and not line.lstrip().startswith("#")):
                    offenders.append("%s: %s" % (path.name, line.strip()[:70]))
        self.assertEqual([], offenders,
                         "offset-less timestamps make runs unorderable: %s" % offenders)

    def test_av_run_records_the_stronger_source_label(self):
        # A hash of the loaded library is a stronger claim than one of the artefact on
        # disk; the field that distinguishes them must never be silently missing.
        with tempfile.TemporaryDirectory() as tmp:
            saved = bench.EVIDENCE_DIR
            bench.EVIDENCE_DIR = pathlib.Path(tmp) / "ev"
            try:
                out = bench._write_evidence("argos", "en-zh", ["x"], ["y"], ["z"],
                                            {}, 1, build_sha="a" * 12,
                                            now=datetime.datetime(2026, 10, 15,
                                                                  tzinfo=datetime.timezone.utc))
                injected = json.loads(out.read_text(encoding="utf-8"))["runs"][0]
                self.assertEqual(injected["engine_build_source"], "injected")
                for key in ("generated_at", "engine_build_sha", "engine_build_source"):
                    self.assertIn(key, injected)
            finally:
                bench.EVIDENCE_DIR = saved

    def test_av_default_path_works_without_injected_arguments(self):
        """The un-injected path is what production takes, so it must be covered too.

        The first draft of this file injected `build_sha`/`now` in every case and stayed
        green while breaking the suites that call `_write_evidence` plainly -- a helper
        imported inside another function is invisible until someone takes the other branch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            saved = bench.EVIDENCE_DIR
            bench.EVIDENCE_DIR = pathlib.Path(tmp) / "ev"
            try:
                out = bench._write_evidence("madlad", "zh-en", ["a", "b"], ["x", "y"],
                                            ["p", "q"], {}, 2)
                run = json.loads(out.read_text(encoding="utf-8"))["runs"][0]
            finally:
                bench.EVIDENCE_DIR = saved
        parsed = datetime.datetime.fromisoformat(run["generated_at"])
        self.assertIsNotNone(parsed.tzinfo, run["generated_at"])
        self.assertEqual(
            {k for k in run if k.startswith("engine_build")},
            {"engine_build_sha", "engine_build_source"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
