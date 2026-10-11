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

# The module the probe actually calls to build a backend. Patching this object rather than a
# re-imported copy is the difference between replacing what runs and replacing something nothing
# reads.
ct2_sidecar = d.ct2_sidecar


def args(**over):
    """A run's arguments, with the defaults the modes expect."""
    base = dict(backend="mock", text="hello", src="en", tgt="zh", n=60, every=30,
                corpus=None, out_json=None, started_at=time.time(),
                # gate-mode knobs; other modes ignore them. Small values keep the fake run
                # seconds rather than minutes -- the fake is not a measurement (see GateModeTests).
                per=2, burst=1, timeout=0.02, deadline=5.0, slots="1", queue_wait_ms=0)
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


class GateModeTests(unittest.TestCase):
    """The `gate` mode compares settings honestly, on a fake that is not a measurement.

    What is pinned is the shape of the comparison, never a ratio: a fake decode of 0.05 s makes
    every millisecond figure below meaningless, the same way the mock made `load`'s ratio
    meaningless (EvidenceKeepingTests says why). The claims that matter here hold regardless of
    how fast the decode is -- that a control dose refuses nobody, that the gate refuses people
    the control does not, that a retrying caller is still always served, and that the summary a
    reader sees can be re-derived from the per-call records beside it.
    """

    DECODE_SECONDS = 0.05
    _CACHE = {}
    _dir = None
    _corpus = None

    class _Fake:
        """A backend with a decode long enough to be abandoned on purpose."""

        name = "fakedecode"

        def translate(self, text, source, target, glossary=None):
            time.sleep(GateModeTests.DECODE_SECONDS)
            return "ok: " + target

        def readiness(self):
            return {"ready": True, "reason": None, "loaded": ["fake"]}

    @classmethod
    def setUpClass(cls):
        # Class scope on purpose. A per-instance cache would be rebuilt for every test method --
        # unittest constructs a fresh instance per test -- and four identical 6 s runs bought no
        # extra evidence over one. The corpus and the patched factory are the run's inputs, so
        # they live where the cached result lives.
        cls._dir = pathlib.Path(tempfile.mkdtemp(prefix="diag-gate-"))
        corpus = cls._dir / "pair.src"
        # Distinct sentences: a run that repeats one makes "which decode was mine" ambiguous, and
        # an ambiguous match would quietly credit the wrong call with the wasted seconds.
        corpus.write_text("\n".join("sentence number %d" % i for i in range(60)) + "\n",
                          encoding="utf-8")
        cls._corpus = str(corpus)
        cls._real_factory = ct2_sidecar.make_backend
        ct2_sidecar.make_backend = lambda kind, model_dir=None: GateModeTests._Fake()

    @classmethod
    def tearDownClass(cls):
        ct2_sidecar.make_backend = cls._real_factory
        shutil.rmtree(cls._dir, ignore_errors=True)
        cls._CACHE = {}

    def setUp(self):
        self.captured = io.StringIO()
        redirect = contextlib.redirect_stdout(self.captured)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def _run(self, **over):
        """One run per distinct configuration, shared by the tests asking the same question."""
        key = repr(sorted(over.items()))
        cached = self._CACHE.get(key)
        if cached is not None:
            return cached
        out = self._dir / ("gate-%d.json" % len(self._CACHE))
        merged = dict(backend="ct2", corpus=self._corpus, out_json=str(out),
                      per=2, burst=1, timeout=0.01, deadline=5.0, slots="1",
                      queue_wait_ms=0)
        merged.update(over)
        rc = d.run_gate(args(**merged))
        self.assertEqual(0, rc, self.captured.getvalue()[-400:])
        doc = json.loads(out.read_text(encoding="utf-8"))
        self._CACHE[key] = doc
        return doc

    def test_the_control_dose_refuses_nobody_under_any_setting(self):
        doc = self._run()
        # The per-call records are the evidence; the summary beside them is what a reader quotes,
        # so the control row has to be provably clean in the summary, not merely look plausible.
        by_name = {s["setting"]: s for s in doc["settings"]}
        self.assertIn("off", by_name)
        self.assertIn("slots=1", by_name)
        self.assertEqual(["off", "slots=1"], [s["setting"] for s in doc["settings"]])
        for name, rec in by_name.items():
            zero = next(dose for dose in rec["doses"] if dose["k"] == 0)
            self.assertEqual(0, zero["refusals"],
                             "%s k=0 was refused: groups are bleeding into each other" % name)
            self.assertEqual(0, zero["lost_if_no_retry"], name)

    def test_a_gate_refuses_work_the_ungated_service_would_decode(self):
        doc = self._run()
        by_name = {s["setting"]: s for s in doc["settings"]}
        off = by_name["off"]
        gated = by_name["slots=1"]
        self.assertEqual(0, sum(d["never_started"] for d in off["doses"]),
                         "the ungated service must never turn work away")
        self.assertEqual(0, sum(d["refusals"] for d in off["doses"]),
                         "the off row is the control; a refusal there invalidates every ratio")
        self.assertGreater(sum(d["never_started"] for d in gated["doses"]), 0,
                           "with one slot and abandoned decodes in flight, something must be "
                           "refused -- if nothing is, the gate did not engage")
        # The warning column has to say something, or it reads as reassurance.
        self.assertGreater(sum(d["lost_if_no_retry"] for d in gated["doses"]), 0,
                           "a one-slot gate does cost a client that never retries; if the "
                           "column is always zero, no reader learns that today's Rust client "
                           "would degrade instead of waiting")
        self.assertEqual(0, sum(d["lost_if_no_retry"] for d in off["doses"]),
                         "the control must show slowdowns only, never lost calls")
        # the compute a gate saves is the point of the whole column
        self.assertLess(sum(d["wasted_decode_s"] for d in gated["doses"]),
                        sum(d["wasted_decode_s"] for d in off["doses"]),
                        "a gate that wastes as much compute as no gate is not a gate")

    def test_a_retrying_caller_is_always_served_honouring_the_hint(self):
        doc = self._run()
        for rec in doc["settings"]:
            for dose in rec["doses"]:
                self.assertEqual(0, dose["unserved"],
                                 "%s k=%d left a caller unserved: the retry loop gave up"
                                 % (rec["setting"], dose["k"]))
        refused = [a for call in doc["records"] if call["role"] == "wanted"
                   for a in call["attempts"] if a.get("slept_s")]
        self.assertTrue(refused, "no refused call slept on a Retry-After, so the slept column "
                                 "is zero by construction rather than by measurement")
        for attempt in refused:
            self.assertGreaterEqual(attempt["retry_after_s"], 1,
                                    "a hint of 0 seconds would read as 'hammer me'")

    def test_the_summary_can_be_rederived_from_the_per_call_records(self):
        doc = self._run()
        wanted = [c for c in doc["records"] if c["role"] == "wanted"]
        self.assertTrue(wanted)
        for rec in doc["settings"]:
            for dose in rec["doses"]:
                group = [c for c in wanted if c["setting"] == rec["setting"]
                         and c["k"] == dose["k"]]
                served = [c["seconds"] for c in group if c["served"]]
                if not served:
                    self.assertEqual(0, dose["mean_s"])
                    continue
                again = sum(served) / len(served)
                self.assertAlmostEqual(again, dose["mean_s"], places=3,
                                       msg="%s k=%d: the printed mean does not come from the "
                                           "records beside it" % (rec["setting"], dose["k"]))

    def test_the_record_file_describes_the_calls_it_actually_made(self):
        doc = self._run()
        # A relationship pin, not a restatement: `run_meta` fills fields that describe a `--n`
        # driven mode, and the first version of `gate` shipped `requested_calls: 60` while having
        # made some other number of calls. Reverting that line would fail here rather than quietly
        # publish a header that contradicts its own records.
        self.assertEqual(len(doc["records"]), doc["requested_calls"])
        self.assertNotEqual(60, doc["requested_calls"], "the header came back from --n")
        self.assertEqual("gate", doc["mode"])
        self.assertEqual([0, 1, 2, 4], sorted({c["k"] for c in doc["records"] if c["k"] is not None}),
                         "the doses in the header should match the doses in the records")
        for rec in doc["settings"]:
            for dose in rec["doses"]:
                self.assertIn(dose["k"], [0, 1, 2, 4])

    def test_mock_is_refused_before_a_checkpoint_is_wasted(self):
        # A fake decode makes the structural assertions above meaningful and nothing else; the
        # mode must say so rather than print millisecond ratios that look like measurements.
        self.assertEqual(1, d.run_gate(args(backend="mock", corpus=self._corpus)))
        self.assertIn("scheduling noise", self.captured.getvalue())

    def test_no_candidate_slots_is_refused_rather_than_comparing_nothing(self):
        self.assertEqual(1, d.run_gate(args(backend="ct2", corpus=self._corpus, slots="")))
        self.assertIn("nothing to compare", self.captured.getvalue())


if __name__ == "__main__":
    unittest.main()
