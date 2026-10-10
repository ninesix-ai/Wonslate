# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Hermetic tests for the batch probe that sets the retention line (defect D30 items 3-4).

`script/diag_sidecar_batch.py` is the tool a release decision is read off, so its judgement has
to be pinned rather than trusted: a line nobody can fail is decoration, and a run that loses its
own records when the service dies mid-batch is a run that never happened -- which is exactly the
failure external item 004 described.

No model is loaded and nothing crosses the network. The pass logic is fed constructed numbers;
the two end-to-end cases drive the mock backend, which answers in milliseconds.

Run:
    python -m unittest tests.test_diag_sidecar_batch -v
"""
import contextlib
import inspect
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "script"))

import diag_sidecar_batch as d  # noqa: E402


def args(**over):
    """A run's arguments, with the defaults the modes expect."""
    base = dict(backend="mock", text="hello", src="en", tgt="zh", n=60, every=30,
                corpus=None, out_json=None, started_at=time.time())
    base.update(over)
    return types.SimpleNamespace(**base)


def passes(*means, each=10):
    """Constructed per-pass samples: one value per pass, replicated to look like a full lap."""
    return {i: [value] * each for i, value in enumerate(means)}


class CorpusTests(unittest.TestCase):
    """The load has to be distinct sentences, or the line measures nothing.

    Item 004's ladder appeared over 1088 different UI keys repeated, so a probe that silently
    falls back to one sentence would report a flat ratio for the wrong reason.
    """

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="diag-corpus-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_directory_resolves_to_the_source_column_it_holds(self):
        (self.dir / "pair.src").write_text("one\ntwo\n\nthree\n", encoding="utf-8")
        self.assertEqual(["one", "two", "three"], d.corpus_lines(str(self.dir)),
                         "blank lines are not sentences, and trailing whitespace is not content")

    def test_a_plain_file_is_read_as_it_is(self):
        path = self.dir / "sentences.txt"
        path.write_text("alpha\nbeta\n", encoding="utf-8")
        self.assertEqual(["alpha", "beta"], d.corpus_lines(str(path)))

    def test_a_missing_corpus_stops_rather_than_repeating_one_sentence(self):
        with self.assertRaises(SystemExit):
            d.corpus_lines(str(self.dir / "absent"))

    def test_an_empty_corpus_is_an_error_not_a_zero_length_cycle(self):
        empty = self.dir / "empty.src"
        empty.write_text("\n \n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            d.corpus_lines(str(empty))


class PassNumberingTests(unittest.TestCase):
    """A pass is one complete lap of the corpus, because that is the paired unit."""

    def test_a_corpus_cycles_and_each_lap_is_one_pass(self):
        src = self._corpus(["a", "b", "c"])
        units = list(d.iter_units(args(corpus=str(src), n=7)))
        self.assertEqual([("a", 0), ("b", 0), ("c", 0), ("a", 1), ("b", 1), ("c", 1), ("a", 2)],
                         units, "lap two must start where lap one started, with the same sentences")

    def test_without_a_corpus_every_call_is_its_own_lap(self):
        units = list(d.iter_units(args(corpus=None, text="solo", n=4)))
        self.assertEqual([("solo", 0), ("solo", 1), ("solo", 2), ("solo", 3)], units)
        # And the verdict refuses this shape -- asserted where it is decided, below.

    def _corpus(self, lines):
        path = pathlib.Path(tempfile.mkdtemp(prefix="diag-laps-")) / "x.src"
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class RetentionVerdictTests(unittest.TestCase):
    """The line has to be able to fail, and has to refuse when it cannot decide."""

    def report(self, per_pass):
        return d.pass_report("under test", per_pass)

    def test_a_batch_that_does_not_drift_passes(self):
        self.assertEqual(0, self.report(passes(4.8, 4.7, 4.7)))

    def test_the_ladder_item_004_reported_is_caught(self):
        # 130 s -> 280 s -> over 900 s for the same work, the numbers in the original report.
        self.assertEqual(1, self.report(passes(1.3, 2.8, 9.0)),
                         "a line that lets the reported ladder through is not a line")

    def test_a_judgement_boundary_is_a_boundary_not_a_vibe(self):
        self.assertEqual(0, self.report(passes(4.00, 4.56)), "1.14x is inside the line")
        self.assertEqual(1, self.report(passes(4.00, 4.64)), "1.16x crosses it")

    def test_a_single_repeated_sentence_refuses_to_state_a_line(self):
        # Identical work by construction, so the ratio would be flat for a reason that has
        # nothing to do with the engine -- and a flat number is what gets quoted later.
        self.assertEqual(1, self.report({i: [3.0] for i in range(5)}))

    def test_one_lap_cannot_be_compared_with_itself(self):
        self.assertEqual(1, self.report({0: [3.0] * 20}))

    def test_a_lap_that_stopped_early_is_excluded_rather_than_compared(self):
        # Two complete laps remain, so the run still decides -- on the laps that did the same
        # amount of work. Comparing a full lap with a truncated one compares different jobs.
        self.assertEqual(0, self.report({0: [4.0] * 20, 1: [3.0] * 5, 2: [4.05] * 20}))
        self.assertEqual(1, self.report({0: [4.0] * 20, 1: [4.1] * 5}),
                         "one complete lap left over is not a trend")

    def test_nothing_served_is_reported_as_no_measurement(self):
        self.assertEqual(1, self.report({}))

    def test_the_line_and_the_revisit_criterion_are_one_number(self):
        # run_rotate asks "did the same language cost more when revisited" -- the same question
        # as the retention line, at a smaller scale. Two literals would drift apart.
        self.assertEqual(1.15, d.RETENTION_LIMIT)
        rotate = inspect.getsource(d.run_rotate)
        self.assertIn("RETENTION_LIMIT", rotate, "rotate must read the shared constant")
        self.assertNotIn("1.15", rotate, "rotate must not keep its own copy of the threshold")


class EvidenceKeepingTests(unittest.TestCase):
    """Whatever the run learns, it has to survive its own last step.

    dump_run is called from a finally block, so an exception there would destroy both the
    verdict and the records it was meant to save -- and the interesting failure is the one
    where the service dies at call 900 of 1500.

    Note what is NOT asserted here: no test in this file judges a ratio measured through the
    mock backend. A mock answers in milliseconds, so its pass-to-pass ratio is scheduling
    noise -- on one run during a busy machine it read 1.89x, which would have made a green
    test depend on what else the host was doing. The ratio itself is pinned by constructed
    numbers in RetentionVerdictTests; these tests pin only that the evidence survives.
    """

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="diag-evidence-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.real_post = d.post_json
        self.addCleanup(setattr, d, "post_json", self.real_post)
        # A corpus, so a lap really is several sentences rather than one call per lap.
        self.corpus = self.dir / "lap.src"
        self.corpus.write_text("\n".join("sentence %d" % i for i in range(20)) + "\n",
                               encoding="utf-8")
        self.captured = io.StringIO()
        redirect = contextlib.redirect_stdout(self.captured)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_every_call_is_written_out(self):
        out = self.dir / "run.json"
        d.run_load(args(n=40, every=20, corpus=str(self.corpus), out_json=str(out)))
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(40, len(doc["records"]))
        self.assertEqual(40, sum(1 for r in doc["records"] if r.get("status") == 200))
        self.assertEqual(d.RETENTION_LIMIT, doc["retention_limit"],
                         "a verdict without the threshold it was judged by cannot be re-checked")
        self.assertEqual(20, len([r for r in doc["records"] if r["pass"] == 1]),
                         "two laps of twenty sentences, recorded as such")
        self.assertEqual(20, len([r for r in doc["records"] if r["pass"] == 2]))

    def test_records_survive_a_service_that_dies_mid_batch(self):
        calls = {"n": 0}

        def dying(port, path, payload, timeout=120.0):
            if calls["n"] >= 25:
                raise ConnectionResetError(104, "simulated death")
            calls["n"] += 1
            return self.real_post(port, path, payload, timeout)

        d.post_json = dying
        out = self.dir / "death.json"
        verdict = d.run_load(args(n=200, every=50, corpus=str(self.corpus), out_json=str(out)))
        self.assertEqual(1, verdict, "a dead service must never yield a passing verdict")
        doc = json.loads(out.read_text(encoding="utf-8"))
        errors = [r for r in doc["records"] if "error" in r]
        self.assertEqual(1, len(errors))
        self.assertEqual("ConnectionResetError", errors[0]["error"])
        self.assertEqual(26, errors[0]["n"], "the finding is WHICH call broke the run")
        self.assertEqual(25, sum(1 for r in doc["records"] if "status" in r),
                         "the curve that led up to the death is the evidence")
        self.assertIn("never came back", self.captured.getvalue())

    def test_a_missing_directory_is_created_instead_of_losing_the_run(self):
        out = self.dir / "not" / "made" / "yet.json"
        d.run_load(args(n=20, every=10, corpus=str(self.corpus), out_json=str(out)))
        self.assertTrue(out.is_file(), "an hour-long run must not end on a directory typo")
        self.assertEqual(20, len(json.loads(out.read_text(encoding="utf-8"))["records"]))

    def test_a_write_that_cannot_happen_warns_and_keeps_the_verdict(self):
        blocker = self.dir / "occupied"
        blocker.write_text("this name is taken", encoding="utf-8")
        # The parent path cannot be created because a file owns the name. Before dump_run
        # learned to fail quietly, this raised out of a finally block and took the verdict,
        # the records and the shutdown of the service down with it.
        verdict = d.run_load(args(n=20, every=10, corpus=str(self.corpus),
                                  out_json=str(blocker / "x.json")))
        self.assertIn(verdict, (0, 1), "the run still reported a verdict")
        text = self.captured.getvalue()
        self.assertIn("WARNING", text, "a lost run has to say so, not fail silently")
        self.assertIn("NOT saved", text)
        self.assertFalse((blocker / "x.json").exists())


class ResourceSamplingTests(unittest.TestCase):
    """A host without psutil still gets the measurement, and never an invented number.

    The absence is simulated rather than waited for. A test that only exercises its failure path
    on machines lacking an optional dependency is the next D29: green here, red there, and
    silent about which fact it was checking.
    """

    def setUp(self):
        self.had_psutil = "psutil" in sys.modules
        self.had_value = sys.modules.get("psutil")
        self.captured = io.StringIO()
        redirect = contextlib.redirect_stdout(self.captured)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.addCleanup(self.restore)

    def restore(self):
        if self.had_psutil:
            sys.modules["psutil"] = self.had_value
        else:
            sys.modules.pop("psutil", None)

    def test_no_psutil_gives_unknown_samples_rather_than_a_crash(self):
        sys.modules["psutil"] = None        # importing a None entry raises ImportError
        rss, threads, handles = d.sample()
        self.assertEqual((d.UNKNOWN, d.UNKNOWN, d.UNKNOWN), (rss, threads, handles),
                         "the run must survive a host that cannot read its own memory")

    def test_unknown_is_never_printed_as_zero(self):
        # Two unknowns subtracted would print "0 MB back", which is a measurement-shaped lie.
        self.assertEqual("n/a", d.fmt(d.UNKNOWN))
        self.assertEqual("n/a", d.fmt(None))
        self.assertEqual("n/a", d.reclaimed(d.UNKNOWN, d.UNKNOWN))
        self.assertEqual("n/a", d.delta(d.UNKNOWN, d.UNKNOWN))
        self.assertEqual("2850", d.reclaimed(2936.0, 86.0))
        self.assertEqual("-3", d.delta(10, 7))


class RegistrationTests(unittest.TestCase):
    """The two enumerations of Python suites have to agree.

    A suite listed in only one of them silently stops running somewhere: this one was added to
    build.py and ci.yml together, and the rule is asserted rather than remembered. Counted by
    unique name, because ci.yml references the license audit in two jobs on purpose.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def suites(self):
        build = (self.ROOT / "script" / "build.py").read_text(encoding="utf-8")
        ci = (self.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        start = build.index("def run_ffi_smoke")
        # From the runner on, so the skip set above it is not mistaken for the list itself.
        return (set(re.findall(r'"(test_\w+\.py)"', build[start:])),
                set(re.findall(r"python tests/(test_\w+\.py)", ci)))

    def test_build_py_and_ci_yml_list_the_same_suites(self):
        build_suites, ci_suites = self.suites()
        self.assertEqual(build_suites - ci_suites, set(),
                         "registered in build.py, so it runs locally, but CI never runs it")
        self.assertEqual(ci_suites - build_suites, set(),
                         "named in CI but the local gate would not run it")

    def test_this_suite_is_registered_in_both_places(self):
        build_suites, ci_suites = self.suites()
        self.assertIn("test_diag_sidecar_batch.py", build_suites)
        self.assertIn("test_diag_sidecar_batch.py", ci_suites)


if __name__ == "__main__":
    unittest.main()
