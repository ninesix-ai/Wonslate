# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Hermetic tests for the AV report generator's comparison (defect D31).

`script/run_av_baseline.py` turns the evidence files into docs/av-domain-benchmark.md.
Two of its columns decided what this repo publishes as "S11 的领域收益", and both
were wrong in the 2026-10-05 run:

  * the delta subtracted two aggregate means computed over *different* row sets --
    the unscoped arm had lost 19 rows to transient engine failures, and those rows
    scored near zero, so +0.0330 was published where the paired effect was +0.0058;
  * the status column printed "✅ 完整" whenever both arms had a file, which said
    nothing about the rows that never got translated.

These tests pin `summarize_engine` on synthetic runs: no files, no model, no GPU.

Run:
    python -m unittest tests.test_run_av_baseline -v
"""
import json
import os
import pathlib
import re
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "script"))

from run_av_baseline import summarize_engine  # noqa: E402


def _run(hyps, scores=None, failed=None, comet=None, chrF=None, engine="ollama-qwen",
         domain=""):
    """One evidence run: hypotheses per row, optional per-sample COMET, failures."""
    samples = [{"id": i, "source": "src-%d" % i, "reference": "ref-%d" % i,
                "hypothesis": h} for i, h in enumerate(hyps)]
    run = {
        "engine": engine,
        "direction": "zh-en",
        "domain": domain,
        "requested": len(hyps),
        "evaluated": len(hyps) - len(failed or []),
        "failed": failed or [],
        "metrics": {"chrF": chrF if chrF is not None else 50.0, "BLEU": 10.0},
        "samples": samples,
    }
    if comet is not None:
        run["comet"] = comet
    if scores is not None:
        run["comet_scores_per_sample"] = scores
    return run


class PairedDeltaTests(unittest.TestCase):
    """The delta must be formed on rows both arms actually translated."""

    def test_rows_one_arm_failed_do_not_enter_the_delta(self):
        # id=1 is missing from the unscoped arm: comparing mean(3 rows) against
        # mean(2 rows) is what turned flakiness into a quality claim.
        base = _run(["a", "", "c"], scores=[0.60, None, 0.80], comet=0.70,
                    failed=[{"id": 1, "reason": "engine_timeout"}])
        scope = _run(["a2", "b2", "c2"], scores=[0.70, 0.72, 0.90], comet=0.7733)
        s = summarize_engine("ollama-qwen", [base], [scope])
        self.assertEqual(s["paired_n"], 2)
        # paired: ((0.70-0.60) + (0.90-0.80)) / 2 = +0.10, not 0.7733-0.70 = +0.0733
        self.assertAlmostEqual(s["delta_comet"], 0.10, places=4)
        self.assertEqual(s["failed_rows"], 1)

    def test_the_delta_is_unavailable_before_the_comet_pass(self):
        # "Not measured yet" must not render as a number, and must not be zero.
        base = _run(["a", "b"], comet=None)
        scope = _run(["a2", "b2"], comet=None)
        s = summarize_engine("ollama-qwen", [base], [scope])
        self.assertIsNone(s["delta_comet"])
        self.assertEqual(s["paired_n"], 2, "row pairing needs no COMET, only hypotheses")

    def test_a_missing_arm_yields_no_delta_rather_than_a_zero(self):
        s = summarize_engine("argos", [_run(["a"])], [])
        self.assertIsNone(s["delta_comet"])
        self.assertFalse(s["has_scope"])


class StatusHonestyTests(unittest.TestCase):
    """"Complete" has to mean complete: every row translated, both arms present."""

    def test_a_run_that_lost_rows_is_not_reported_complete(self):
        base = _run(["a", "", "c"], comet=0.7, failed=[{"id": 1, "reason": "engine_timeout"}])
        scope = _run(["a2", "b2", "c2"], comet=0.8)
        s = summarize_engine("ollama-qwen", [base], [scope])
        self.assertNotIn("完整", s["status"])
        self.assertIn("1 行", s["status"])

    def test_a_clean_run_is_reported_complete(self):
        base = _run(["a", "b"], comet=0.70)
        scope = _run(["a2", "b2"], comet=0.80)
        s = summarize_engine("ollama-qwen", [base, _run(["a", "b"], comet=0.72)],
                            [scope, _run(["a2", "b2"], comet=0.81)])
        self.assertIn("完整", s["status"])

    def test_a_single_run_per_arm_has_no_noise_floor_and_says_so(self):
        # ollama runs at temperature 0.3 with no seed, so one run per arm cannot
        # separate a terminology effect from sampling noise. Saying "complete"
        # without that caveat is how +0.0058 got read as a proven gain.
        base = _run(["a", "b"], comet=0.70)
        scope = _run(["a2", "b2"], comet=0.80)
        s = summarize_engine("ollama-qwen", [base], [scope])
        self.assertIsNone(s["noise_floor"])
        self.assertIn("未复采", s["status"])

    def test_repeats_without_comet_are_not_labelled_unsampled(self):
        # Two causes of a missing floor read alike unless they are told apart. A fresh
        # direction is benched twice and scored for COMET later; labelling that "not repeated"
        # would delete the fact that the arm was sampled twice, and a reader would wait for a
        # repeat that already exists.
        base = _run(["a", "b"], comet=None)
        scope = _run(["a2", "b2"], comet=None)
        s = summarize_engine("ollama-qwen", [base, _run(["a", "b"], comet=None)],
                            [scope, _run(["a2", "b2"], comet=None)])
        self.assertIsNone(s["noise_floor"])
        self.assertEqual(2, s["repeats"])
        self.assertNotIn("未复采", s["status"])
        self.assertIn("已复采 2 次", s["status"])

    def test_the_floor_cell_agrees_with_the_repeat_count(self):
        # The floor column and the status column are read side by side; one of them claiming
        # a single run while the other says the arm was sampled twice makes the row unreadable.
        from run_av_baseline import _floor_cell
        self.assertEqual("_单次_", _floor_cell({"noise_floor": None, "repeats": 1}))
        self.assertEqual("_待 COMET_", _floor_cell({"noise_floor": None, "repeats": 2}))
        self.assertEqual("±0.0100", _floor_cell({"noise_floor": 0.01, "repeats": 2}))

    def test_a_backend_that_cannot_apply_terms_has_no_arm_to_miss(self):
        # argos and madlad are served by sidecar.rs, which deliberately does NOT override
        # accepts_term_context: the CT2/MADLAD checkpoints read the term array and do nothing.
        # The published legacy artifacts proved it -- the scoped arm came out identical in
        # 300/300 rows. A baseline-only identity for those engines is therefore a design
        # decision. Calling it "missing arm" tells a reader to go run a comparison that
        # cannot differ, and the 0.0000 they get back reads as a measurement of "terminology
        # does not help" rather than "nothing in this backend can apply it".
        s = summarize_engine("argos", [_run(["a", "b"], comet=0.70)], [])
        self.assertNotIn("缺臂", s["status"])
        self.assertIn("不适用", s["status"])

    def test_an_engine_that_can_apply_terms_still_reports_the_missing_arm(self):
        # The other half of the distinction. ollama's scoped arm is a real measurement, so
        # an absent one IS missing evidence; widening the "not applicable" wording to every
        # engine would quietly cover up an artifact that went missing.
        s = summarize_engine("ollama-qwen", [_run(["a", "b"], comet=0.70)], [])
        self.assertIn("缺臂", s["status"])

    def test_a_zero_floor_on_an_nmt_engine_is_not_read_as_significance(self):
        # These backends are deterministic, so the within-arm floor comes out exactly zero and
        # the ordinary sentence "the delta exceeds the floor" fires on any nonzero difference.
        # Publishing that for an engine whose backend drops the term array would turn a wire
        # format into a terminology finding -- the D51 mistake from the other side.
        base = _run(["a", "b"], scores=[0.70, 0.70], comet=0.70)
        scope = _run(["a2", "b2"], scores=[0.7001, 0.7001], comet=0.7001)
        s = summarize_engine("argos", [base, base], [scope, scope])
        self.assertEqual(0.0, s["noise_floor"])
        self.assertNotIn("超噪声地板", s["status"])
        self.assertIn("构造使然", s["status"])

    def test_the_not_applicable_row_still_reports_its_repeat_count(self):
        # "Not applicable" says something about the comparison, not about how often the
        # baseline ran. A fixed sentence claiming a single sample would be a lie the moment
        # that identity gets a second one, and the repeat count is what a reader uses to
        # decide whether the absolute metrics are worth quoting.
        one = summarize_engine("argos", [_run(["a", "b"], comet=0.70)], [])
        two = summarize_engine("argos", [_run(["a", "b"], comet=0.70),
                                         _run(["a", "b"], comet=0.71)], [])
        self.assertIn("未复采", one["status"])
        self.assertNotIn("未复采", two["status"])
        self.assertIn("已复采 2 次", two["status"])

    def test_a_delta_inside_the_noise_floor_is_not_a_result(self):
        base = _run(["a", "b"], comet=0.700)
        base2 = _run(["a", "b"], comet=0.710)          # arm drifts by 0.010 on its own
        scope = _run(["a2", "b2"], comet=0.706)
        scope2 = _run(["a2", "b2"], comet=0.716)
        s = summarize_engine("ollama-qwen", [base, base2], [scope, scope2])
        self.assertAlmostEqual(s["noise_floor"], 0.010, places=4)
        self.assertAlmostEqual(s["delta_comet"], 0.006, places=4)
        self.assertIn("噪声", s["status"])


class RepeatAveragingTests(unittest.TestCase):
    """Repeats live in the same artifact's `runs` array; aggregates must read them."""

    def test_absolute_metrics_average_across_repeats(self):
        s = summarize_engine("madlad", [_run(["a", "b"], comet=0.80),
                                        _run(["a", "b"], comet=0.82)],
                            [_run(["x", "y"], comet=0.84),
                             _run(["x", "y"], comet=0.86)])
        self.assertAlmostEqual(s["base_comet"], 0.81, places=4)
        self.assertAlmostEqual(s["scope_comet"], 0.85, places=4)

    def test_repeats_pair_by_index_not_across_the_whole_set(self):
        # run 0 of one arm with run 0 of the other, so a repeat of the baseline is
        # never compared against a different repeat of the scoped arm.
        base = _run(["a", "b"], scores=[0.60, 0.60], comet=0.60)
        base2 = _run(["a", "b"], scores=[0.80, 0.80], comet=0.80)
        scope = _run(["a2", "b2"], scores=[0.70, 0.70], comet=0.70)
        scope2 = _run(["a2", "b2"], scores=[0.75, 0.75], comet=0.75)
        s = summarize_engine("ollama-qwen", [base, base2], [scope, scope2])
        # per-pair deltas: +0.10 and -0.05 -> mean +0.025
        self.assertAlmostEqual(s["delta_comet"], 0.025, places=4)


class TermAdherenceReportingTests(unittest.TestCase):
    """The report must surface terminology adherence, and label which pack it counts.

    The whole-pack rate is not an injection rate: most of a large pack can never
    reach a prompt (defect D32), so a gain measured across the whole pack cannot be
    attributed to the terminology that was actually sent. The report therefore has to
    carry the counts, not just a percentage, and must say when the numbers cover
    different sets of rows between the arms.
    """

    @staticmethod
    def _ad(hits, opportunities, terms=266):
        return {"terms_in_pack": terms, "rows_with_terms": 270,
                "opportunities": opportunities, "hits": hits,
                "rate": round(hits / opportunities, 6) if opportunities else None}

    def test_rates_and_counts_are_pooled_across_repeats(self):
        base = _run(["a", "b"])
        base["term_adherence"] = self._ad(403, 667)
        again = _run(["a", "b"])
        again["term_adherence"] = self._ad(405, 667)
        scope = _run(["a2", "b2"])
        scope["term_adherence"] = self._ad(421, 667)
        s = summarize_engine("ollama-qwen", [base, again], [scope])
        self.assertEqual(s["base_adherence"]["opportunities"], 1334)
        self.assertEqual(s["base_adherence"]["hits"], 808)
        self.assertAlmostEqual(s["base_adherence"]["rate"], 808 / 1334, places=6)
        self.assertEqual(s["scope_adherence"]["opportunities"], 667)
        self.assertAlmostEqual(s["adherence_delta"],
                               421 / 667 - 808 / 1334, places=6)

    def test_evidence_without_the_field_reports_none_rather_than_zero(self):
        # Older evidence files predate the metric; "not measured" must not become
        # "0% adherent", which would read as the worst possible terminology result.
        s = summarize_engine("argos", [_run(["a"])], [_run(["a2"])])
        self.assertIsNone(s["base_adherence"])
        self.assertIsNone(s["scope_adherence"])
        self.assertIsNone(s["adherence_delta"])

    def test_mismatched_opportunity_counts_are_flagged_as_not_comparable(self):
        # If the two arms were graded against different row sets the delta is an
        # artifact again -- the exact mistake D31 was. Say so instead of subtracting.
        base = _run(["a", "b"])
        base["term_adherence"] = self._ad(300, 500)
        scope = _run(["a2", "b2"])
        scope["term_adherence"] = self._ad(300, 667)
        s = summarize_engine("ollama-qwen", [base], [scope])
        self.assertIsNotNone(s["base_adherence"])
        self.assertFalse(s["adherence_comparable"],
                         "rates over 500 and 667 opportunities must not be subtracted")


class ReportRenderingTests(unittest.TestCase):
    """The report is a projection of what was measured, not of what this run asked for.

    Two ways the published document lost content without losing a test: narrowing --engines
    deleted another engine's TL;DR row, and after the first fix its sub-domain tables still
    vanished -- because the writer built its own engine list from args.engines while the
    summary used the widened one. A half-fix leaves the row and drops the table, which reads
    to a passer-by as "that engine was never measured here".
    """

    def setUp(self):
        import run_av_baseline
        self.m = run_av_baseline
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_evidence = self.m.EVIDENCE_DIR
        self.orig_report = self.m.REPORT_PATH
        self.m.EVIDENCE_DIR = pathlib.Path(self.tmp.name) / "evidence"
        self.m.EVIDENCE_DIR.mkdir(parents=True)
        self.m.REPORT_PATH = pathlib.Path(self.tmp.name) / "av-domain-benchmark.md"

    def tearDown(self):
        self.m.EVIDENCE_DIR = self.orig_evidence
        self.m.REPORT_PATH = self.orig_report
        self.tmp.cleanup()

    def _put_legacy_evidence(self, engine, comet):
        run = _run(["a", "b"], scores=[comet, comet], comet=comet)
        payload = {"corpus": "av-domain", "generated_at": "x", "runs": [run]}
        (self.m.EVIDENCE_DIR / ("av-domain-%s.json" % engine)).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        scoped = dict(run, comet=comet + 0.01,
                      comet_scores_per_sample=[comet + 0.01, comet + 0.01])
        (self.m.EVIDENCE_DIR / ("av-domain-%s-av.json" % engine)).write_text(
            json.dumps(dict(payload, runs=[scoped]), ensure_ascii=False), encoding="utf-8")

    def _args(self, engines):
        return types.SimpleNamespace(
            engines=engines, directions=list(self.m.DIRECTIONS), n=None, repeats=1,
            quick=False, skip_comet=True, report_only=True, force=False,
            only_baseline=False, preflight_only=False)

    def test_an_engine_with_evidence_keeps_its_subdomain_table(self):
        self._put_legacy_evidence("argos", 0.73)
        self._put_legacy_evidence("ollama-qwen", 0.82)
        self.m.generate_report(self._args(["ollama-qwen"]))
        text = self.m.REPORT_PATH.read_text(encoding="utf-8")
        self.assertIn("### ollama-qwen", text)
        self.assertIn("### argos", text,
                      "benching one engine must not delete another engine's published tables")
        self.assertIn("| argos |", text)

    def test_a_baseline_only_identity_of_an_nmt_engine_is_not_labelled_missing_arm(self):
        # The rendered table, not just the dict: this is the row a reader meets after
        # `--only-baseline`, and "missing arm" there sends someone to re-run a comparison
        # that sidecar.rs makes impossible by construction.
        run = _run(["a", "b"], scores=[0.7, 0.7], comet=0.7, engine="argos")
        run["direction"] = "zh-en"
        name = self.m.evidence_name("argos", "zh-en", "", 2)
        (self.m.EVIDENCE_DIR / name).write_text(
            json.dumps({"corpus": "av-domain", "generated_at": "x",
                        "direction": "zh-en", "corpus_rows": 2, "runs": [run]},
                       ensure_ascii=False), encoding="utf-8")
        lines = self.m._extra_identity_lines(["argos"], ["zh-en"], set())
        row = [l for l in lines if l.startswith("| argos |")]
        self.assertEqual(1, len(row), "the new identity did not reach the table")
        self.assertIn("不适用", row[0])
        self.assertNotIn("缺臂", row[0])

    def test_the_header_describes_the_corpus_actually_on_disk(self):
        # The published header claimed three hundred pairs in one direction with twelve groups,
        # long after S12 T4 had landed the thousandth row across two directions with 23 groups
        # each. That sentence is the qualifier REQ-E2 attaches to every quoted number, so a stale
        # one mislabels the measurement it is guarding. Group counts are per direction because a
        # group is a span of one corpus's row numbers (D62), and the header has to say so too.
        import run_av_baseline
        note = run_av_baseline._data_note()
        self.assertNotIn("300 对", note, "a stale row count in a generated header")
        self.assertIn(str(run_av_baseline._corpus_rows("zh-en", None, None)), note)
        for direction in run_av_baseline.DIRECTIONS:
            self.assertIn(direction.replace("-", "\u2192"), note,
                          "%s is measured but the header does not mention it" % direction)


class PerDirectionRangesTests(unittest.TestCase):
    """A sub-domain group table belongs to ONE corpus row order, so it must be read per direction.

    `script/av_domain_ranges.json` labels spans of corpus ROWS (1..30 = "pipeline core", and
    so on). Sharing one list between directions was safe for exactly one reason: en->zh is the
    forward corpus with its two columns swapped, so row 7 is still the same sentence. A direction
    that is newly authored -- the ja/ko half of S12 T2 -- has its own row order, and borrowing
    zh-en's spans would print a real number under a group name that describes different
    sentences. Nothing errors: a 300-row run gets clamped, a 1000-row run gets full tables, and
    the labels are simply wrong. That is the same class as D55 (empty groups read as "scored
    nothing") but worse, because these groups are not empty -- they are plausible.
    """

    GROUPS = {
        "zh-en": [{"name": "\u7532\u7ec4", "start": 1, "end": 2}],
        "en-zh": [{"name": "\u4e59\u7ec4", "start": 1, "end": 2}],
    }

    def setUp(self):
        import run_av_baseline
        self.m = run_av_baseline
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_evidence = self.m.EVIDENCE_DIR
        self.orig_ranges = self.m.RANGES_PATH
        self.orig_report = self.m.REPORT_PATH
        self.m.EVIDENCE_DIR = pathlib.Path(self.tmp.name) / "evidence"
        self.m.EVIDENCE_DIR.mkdir(parents=True)
        # generate_report writes the published document; a test must never aim at the real one.
        self.m.REPORT_PATH = pathlib.Path(self.tmp.name) / "av-domain-benchmark.md"
        self.write_ranges({"comment": "fixture", "ranges": dict(self.GROUPS)})

    def tearDown(self):
        self.m.EVIDENCE_DIR = self.orig_evidence
        self.m.RANGES_PATH = self.orig_ranges
        self.m.REPORT_PATH = self.orig_report
        self.tmp.cleanup()

    def _args(self, engines):
        return types.SimpleNamespace(
            engines=engines, directions=list(self.m.DIRECTIONS), n=None, repeats=1,
            quick=False, skip_comet=True, report_only=True, force=False,
            only_baseline=False, preflight_only=False)

    def write_ranges(self, payload):
        path = pathlib.Path(self.tmp.name) / "av_domain_ranges.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.m.RANGES_PATH = path
        return path

    def _put_identity(self, engine, direction, rows=2, comet=0.70, delta=0.01):
        """Write both arms of one (engine, direction, size) identity."""
        base = _run(["a", "b"], scores=[comet, comet], comet=comet, engine=engine)
        base["direction"] = direction
        scope = _run(["a2", "b2"], scores=[comet + delta, comet + delta],
                     comet=comet + delta, engine=engine)
        scope["direction"] = direction
        for domain, run in (("", base), ("av", scope)):
            name = self.m.evidence_name(engine, direction, domain, rows)
            (self.m.EVIDENCE_DIR / name).write_text(
                json.dumps({"corpus": "av-domain", "generated_at": "x",
                            "direction": direction, "corpus_rows": rows, "runs": [run]},
                           ensure_ascii=False), encoding="utf-8")

    def test_ranges_are_read_for_one_direction_and_never_borrowed(self):
        self.assertEqual([{"name": "\u7532\u7ec4", "start": 1, "end": 2}],
                         self.m._load_ranges("zh-en"))
        self.assertEqual([{"name": "\u4e59\u7ec4", "start": 1, "end": 2}],
                         self.m._load_ranges("en-zh"))
        # An unlisted direction has NO groups. Returning another direction's list -- what
        # happens while the loader ignores direction entirely -- is the defect.
        self.assertEqual([], self.m._load_ranges("zh-ja"))

    def test_each_directions_group_table_carries_its_own_group_names(self):
        self._put_identity("ollama-qwen", "zh-en")
        self._put_identity("ollama-qwen", "en-zh")
        lines = self.m._extra_identity_lines(["ollama-qwen"], ["zh-en", "en-zh"], set())
        text = "\n".join(lines)
        section = {}
        current = None
        for line in lines:
            if line.startswith("### "):
                current = line
            if line.startswith("| ") and "\u7532\u7ec4" in line:
                section.setdefault(current, []).append("\u7532")
            if line.startswith("| ") and "\u4e59\u7ec4" in line:
                section.setdefault(current, []).append("\u4e59")
        zh = [k for k in section if "zh-en" in k]
        en = [k for k in section if "en-zh" in k]
        self.assertEqual(["\u7532"], [g for k in zh for g in section[k]],
                         "the zh-en section must show its own group only; got %s" % text[:200])
        self.assertEqual(["\u4e59"], [g for k in en for g in section[k]],
                         "the en-zh section must show its own group only; got %s" % text[:200])

    def test_a_direction_without_groups_says_so_instead_of_borrowing_anothers(self):
        # The honest degradation. A new direction lands in DIRECTIONS before anyone has grouped
        # its corpus; the report must then show no group table for it, and must SAY why, rather
        # than silently dressing its rows in zh-en's labels.
        self.write_ranges({"comment": "fixture", "ranges": {"zh-en": self.GROUPS["zh-en"]}})
        self._put_identity("ollama-qwen", "zh-ja")
        lines = self.m._extra_identity_lines(["ollama-qwen"], ["zh-ja"], set())
        text = "\n".join(lines)
        self.assertIn("zh-ja", text, "the identity itself must still be reported")
        self.assertNotIn("\u7532\u7ec4", text,
                         "zh-en's group name must not be printed over another direction's rows")
        self.assertIn("\u65e0\u5206\u7ec4\u8868", text,
                      "silence is not an option: the reader must be told no groups exist here")

    def test_the_shipped_ranges_file_declares_every_direction_the_runner_knows(self):
        # Read the real asset: adding a direction to DIRECTIONS without declaring its groups
        # must fail here, not surface later as a missing table in a published report.
        self.write_ranges(json.loads(self.orig_ranges.read_text(encoding="utf-8")))
        for direction in self.m.DIRECTIONS:
            groups = self.m._load_ranges(direction)
            self.assertTrue(groups, "direction %s has no sub-domain groups declared" % direction)
            self.assertEqual(1, groups[0]["start"],
                             "%s: groups must start at corpus row 1" % direction)

    def test_the_main_engine_table_follows_the_runs_own_direction(self):
        # The headline tables read the legacy artifacts, which are the published zh->en runs; if
        # the writer asked for a fixed direction instead of the one the run records, a future
        # headline built on another direction's artifacts would carry zh-en's group labels. This
        # is the half of D62 that --report-only cannot show on today's data, so it is pinned here.
        for engine in ("argos", "ollama-qwen"):
            base = _run(["a", "b"], scores=[0.7, 0.7], comet=0.7, engine=engine)
            base["direction"] = "en-zh"
            scope = _run(["a2", "b2"], scores=[0.71, 0.71], comet=0.71, engine=engine)
            scope["direction"] = "en-zh"
            (self.m.EVIDENCE_DIR / ("av-domain-%s.json" % engine)).write_text(
                json.dumps({"corpus": "av-domain", "generated_at": "x", "direction": "en-zh",
                            "corpus_rows": 2, "runs": [base]}, ensure_ascii=False),
                encoding="utf-8")
            (self.m.EVIDENCE_DIR / ("av-domain-%s-av.json" % engine)).write_text(
                json.dumps({"corpus": "av-domain", "generated_at": "x", "direction": "en-zh",
                            "corpus_rows": 2, "runs": [scope]}, ensure_ascii=False),
                encoding="utf-8")
        self.m.generate_report(self._args(["argos", "ollama-qwen"]))
        text = self.m.REPORT_PATH.read_text(encoding="utf-8")
        self.assertIn("\u4e59\u7ec4", text,
                      "the headline table must use the direction its runs recorded")
        self.assertNotIn("\u7532\u7ec4", text,
                         "zh-en's group name appeared in a table built from en-zh artifacts")
        self.assertIn("> \u8bed\u5411 `en-zh`", text,
                      "the table must say which identity it describes")


class DeriveCheckRangesTests(unittest.TestCase):
    """`derive_av_direction.py --check` has to notice a stale group copy, not just a stale corpus.

    The derived direction's groups are a copy, so they are the one artefact a hand edit can leave
    behind while the corpus still matches. Without this the drift path would cover two of the three
    derived files and a reader would take "derived artefacts match their sources" to mean all three.
    """

    def test_a_mismatching_group_copy_is_reported_as_drift(self):
        import derive_av_direction as D
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real = json.loads(D.RANGES.read_text(encoding="utf-8"))
        stale = json.loads(json.dumps(real))
        forward = stale["ranges"]["zh-en"]
        # A derived list that stops short of the corpus: same shape, wrong coverage.
        stale["ranges"]["en-zh"] = forward[:-1]
        path = pathlib.Path(tmp.name) / "av_domain_ranges.json"
        path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
        orig = D.RANGES
        D.RANGES = path
        try:
            self.assertEqual(1, D.check(),
                             "--check passed a derived group list that is not the forward's")
            # And the same loader is happy with the real file, so the failure above is the
            # mismatch and not an unreadable fixture.
            D.RANGES = orig
            self.assertEqual(0, D.check())
        finally:
            D.RANGES = orig


class PublishedHeaderlessArtifactsTests(unittest.TestCase):
    """Reads the committed evidence: what happens when a file name knows more than its header
    does. One case uses a fixture, because no committed file exercises the fallback it pins.

    Two published artifacts were written before the identity reached the header, so their
    top level has no `direction`/`corpus_rows` -- but every run inside them does carry
    `direction`. The runner must keep filing them under en->zh all the same, and must not let
    the same file answer for zh->en. `test_bench_domain_av` has the corpus-side version of this
    rule (CorpusIntegrityTests); this is the reading side, and it is the half that a rebuilt
    artifact with an empty header would silently break.
    """

    ENGINE = "ollama-qwen"
    ORPHAN = "en-zh"                  # direction whose 300-row artifact has no header identity
    PAIR = "zh-en"                    # whose 300-row identity lives in the legacy-named file

    def _doc(self, direction, domain):
        import run_av_baseline
        name = run_av_baseline.evidence_name(self.ENGINE, direction, domain, 300)
        path = run_av_baseline.EVIDENCE_DIR / name
        if not path.is_file():
            self.skipTest("published artifact absent in this checkout: %s" % name)
        return json.loads(path.read_text(encoding="utf-8")), name

    def test_the_published_artifact_really_lacks_its_header_identity(self):
        # Records the current state rather than idealising it: if someone ever fills the header
        # in, this test says so, and the two rules below must still hold either way.
        doc, name = self._doc(self.ORPHAN, "")
        self.assertIsNone(doc.get("direction"),
                          "%s now carries a header; update this test and the handoff note" % name)
        for run in doc["runs"]:
            self.assertEqual(self.ORPHAN, run.get("direction"),
                             "a run inside the en->zh artifact claims another direction")

    def test_the_runner_files_it_under_the_direction_of_its_name_and_records(self):
        import run_av_baseline
        by_size = run_av_baseline._arm_runs_by_size(self.ENGINE, "", self.ORPHAN)
        self.assertIn(300, by_size,
                      "the 300-row en->zh identity vanished from the report's view")
        self.assertEqual([self.ORPHAN] * len(by_size[300]),
                         [r.get("direction") for r in by_size[300]])

    def test_a_lookup_never_answers_for_another_directions_rows(self):
        # The invariant, stated for every engine and direction rather than for these two files:
        # every run a per-direction lookup returns must either record this direction or record
        # nothing at all (the pre-D51 legacy artifacts, which are the published zh->en runs by
        # rule). A lookup that keys on size alone would let whichever file sorts last answer for
        # both, and one that ignores the recorded direction would file the legacy baseline under
        # whichever direction happens to ask -- both print one direction's numbers under the
        # other's heading. Looping engines matters: for `ollama-qwen` a newer same-size artifact
        # masks the leak, so only argos and madlad expose it.
        import run_av_baseline
        for engine in run_av_baseline.ENGINES:
            for direction in run_av_baseline.DIRECTIONS:
                for domain in ("", "av"):
                    for rows, runs in run_av_baseline._arm_runs_by_size(
                            engine, domain, direction).items():
                        for run in runs:
                            recorded = run.get("direction")
                            if recorded is None:
                                self.assertEqual(300, rows,
                                                "%s/%s: an artifact with no recorded direction at "
                                                "rows=%d must only be the legacy 300-row one"
                                                % (engine, direction, rows))
                                self.assertEqual(self.PAIR, direction)
                            else:
                                self.assertEqual(
                                    direction, recorded,
                                    "%s: %s's lookup returned a %s run" % (engine, direction,
                                                                           recorded))

    def test_the_pre_identity_legacy_artifact_belongs_to_its_published_direction(self):
        # Measured, not assumed: the bench has always written `direction` into each run, so the
        # legacy-named artifacts do carry it at run level (zh-en) even though their headers predate
        # the identity. What has to hold is that they are filed under the direction they record and
        # appear nowhere else -- the lookup keying on file name plus this field is what keeps the
        # published 300-row baseline from being read as en->zh.
        import run_av_baseline
        legacy = run_av_baseline.EVIDENCE_DIR / ("av-domain-%s.json" % self.ENGINE)
        if not legacy.is_file():
            self.skipTest("no legacy artifact in this checkout")
        doc = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertIsNone(doc.get("direction"), "this case is about headers that stayed silent")
        for run in doc["runs"]:
            self.assertEqual(self.PAIR, run.get("direction"))
        base = run_av_baseline._arm_runs_by_size(self.ENGINE, "", self.PAIR)
        self.assertIn(300, base, "the published zh->en baseline vanished from the report's view")
        other = run_av_baseline._arm_runs_by_size(self.ENGINE, "", self.ORPHAN)
        self.assertEqual([], [r for r in other.get(300, []) if r.get("engine") == self.ENGINE
                              and r.get("direction") == self.PAIR],
                         "a zh->en run leaked into the en->zh identity")

    def test_an_artifact_recording_nothing_is_filed_under_the_documented_default(self):
        # The rule under test is `_run_direction`'s fallback, and no committed file exercises it
        # (the case above shows why: the bench has always written the field). So it is exercised
        # on a fixture. An artifact that records no direction must land in exactly ONE identity --
        # the published zh->en one, by written rule. A default that came out empty would file it
        # nowhere and the baseline would silently disappear from the report; a fallback keyed on
        # the loop variable would file it under both, which is D52 all over again.
        import run_av_baseline
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        evidence = pathlib.Path(tmp.name)
        run = _run(["a", "b"], scores=[0.7, 0.7], comet=0.7, engine="fakeeng")
        run.pop("direction")
        (evidence / "av-domain-fakeeng.json").write_text(
            json.dumps({"corpus": "av-domain", "generated_at": "x", "runs": [run]},
                       ensure_ascii=False), encoding="utf-8")
        orig = run_av_baseline.EVIDENCE_DIR
        run_av_baseline.EVIDENCE_DIR = evidence
        self.addCleanup(setattr, run_av_baseline, "EVIDENCE_DIR", orig)
        filed = {direction: [r.get("engine") for rows, runs in
                             run_av_baseline._arm_runs_by_size("fakeeng", "", direction).items()
                             for r in runs]
                 for direction in run_av_baseline.DIRECTIONS}
        self.assertEqual(["fakeeng"], filed["zh-en"],
                         "a directionless artifact was not filed under the published default")
        self.assertEqual([], filed["en-zh"],
                         "a directionless artifact leaked into en->zh")


class NmtTermContextWordingTests(unittest.TestCase):
    """The prose that says which engines consume a glossary must come from the table.

    The shipped report used to read "madlad / qwen consume the term list through the prompt;
    argos does not". sidecar.rs serves BOTH argos and madlad and deliberately keeps the trait
    default `accepts_term_context() == false`, and the published scoped artifact for madlad
    came out identical in 300/300 rows -- so the sentence assigned one engine to the wrong
    side, which is exactly how a limitation section ends up contradicting the table its own
    status column reads.
    """

    MARKER = "不会施加术语上下文的引擎："

    def test_the_split_itself_is_what_the_report_claims(self):
        import run_av_baseline
        applies, ignores = run_av_baseline.term_context_sides()
        self.assertEqual(["ollama-qwen"], applies)
        self.assertEqual(["argos", "madlad"], ignores)

    def test_the_limitation_line_puts_madlad_with_the_backends_that_ignore_terms(self):
        import run_av_baseline
        line = [l for l in run_av_baseline._limitations_block() if "机制依赖" in l]
        self.assertEqual(1, len(line), "the capability limitation line vanished")
        tail = line[0].split(self.MARKER)[1].split("。")[0]
        self.assertIn("argos", tail)
        self.assertIn("madlad", tail,
                      "madlad does not apply term context; listing it as a consumer is the "
                      "claim this test exists to catch")
        self.assertNotIn("ollama", tail)

    def test_the_delta_note_calls_a_zero_delta_constructual_not_measured(self):
        # Both NMT rows used to be explained as "a small delta is expected", which leaves
        # room for reading 0.0000 as a finding about terminology. It is a finding about the
        # backend, and the report has to say so.
        import run_av_baseline
        summary = [{"engine": "argos", "delta_comet": 0.0},
                   {"engine": "madlad", "delta_comet": 0.0}]
        text = "\n".join(run_av_baseline._hypothesis_block(summary, {}, {}))
        self.assertIn("构造使然", text)
        self.assertNotIn("属预期", text)

    def test_the_corpus_size_note_states_what_is_on_disk(self):
        # The limitation section claimed "300 self-authored pairs, growing to 500-1000 sits in
        # S12 T4" after T4 had already landed the thousandth row. A dated claim inside a
        # generated document is the same failure this round keeps meeting: prose that stops
        # matching the thing it describes. The size now comes from the corpus files.
        import run_av_baseline
        rows = run_av_baseline._corpus_rows("zh-en", None, None)
        self.assertGreater(rows, 300, "the in-repo corpus shrank; fix the test and the report")
        text = "\n".join(run_av_baseline._limitations_block())
        self.assertIn("%d 对" % rows, text)
        self.assertNotIn("挂在 S12 T4", text)

    def test_the_direction_note_covers_every_direction_the_runner_knows(self):
        import run_av_baseline
        text = "\n".join(run_av_baseline._limitations_block())
        self.assertNotIn("仅 zh→en", text,
                         "en->zh has been measured since S12 T2; saying zh->en only is the "
                         "stale-claim shape this test exists for")
        for direction in run_av_baseline.DIRECTIONS:
            self.assertIn(direction.replace("-", "→"), text)

    def test_the_disclosure_line_carries_the_current_size_and_directions(self):
        # REQ-E2 points at this sentence when it says an absolute number may only be quoted
        # with its qualifiers attached. Two of those qualifiers had gone stale, so a reader
        # faithfully following the rule would have published "300 句、zh→en" about a
        # thousand-row, two-direction measurement -- a limitation that no longer describes
        # the data it is supposed to constrain.
        import run_av_baseline
        rows = run_av_baseline._corpus_rows("zh-en", None, None)
        line = next(l for l in run_av_baseline._limitations_block() if "对外披露口径" in l)
        self.assertIn("AV 域自撰 %d 句" % rows, line)
        self.assertIn("zh→en", line)
        self.assertIn("en→zh", line)

    def test_the_reproduction_block_can_reproduce_what_the_report_shows(self):
        # The identity section holds per-direction and per-size tables, and two of its
        # identities have no scoped arm by design. A reader copying only the four commands
        # that used to be listed would get a single direction, one size, and both arms --
        # i.e. a document that does not reproduce the tables it prints.
        import run_av_baseline
        text = "\n".join(run_av_baseline._reproduction_block(
            types.SimpleNamespace(n=1000, repeats=1, engines="argos,madlad",
                                  directions="zh-en,en-zh")))
        for flag in ("--directions", "--only-baseline", "--repeats", "--n"):
            self.assertIn(flag, text, "%s is missing from the reproduction commands" % flag)


    def test_the_chrf_column_is_not_presented_as_cross_direction_comparable(self):
        # The identity section now puts zh->en and en->zh tables on one page, and all three
        # engines score far higher chrF on zh->en (ollama 48.90 vs 25.64). Nothing about that
        # means the en->zh output is worse: chrF counts character overlap, and the reference
        # texts have very different lengths per direction. Without the caveat, a side-by-side
        # layout invites exactly that reading.
        import run_av_baseline
        line = [l for l in run_av_baseline._limitations_block() if "chrF" in l]
        self.assertEqual(1, len(line), "the chrF caveat is missing from the limitation section")
        # Assert the claim, not merely the topic: a line that says chrF IS comparable across
        # directions would also contain "chrF" and "跨语向", so only the negation is testable.
        self.assertIn("chrF 不跨语向可比", line[0])
        self.assertIn("不是同一把尺", line[0])


class RunLogTests(unittest.TestCase):
    """A read-only invocation must not destroy the trace of a bench that is still running.

    This session hit it: while the argos/madlad 1000-row matrix was mid-flight, a
    `--report-only` call reopened run_av_baseline.log in write mode and truncated the live
    run's log. The durable log exists precisely because a console window can be closed before
    anyone reads the summary -- so a second, harmless-looking command must not be able to
    erase it. Losing it cost this round its own timing evidence (the artifact mtimes had
    already been rewritten by the COMET pass by the time it was gone).
    """

    def test_bench_and_read_only_invocations_do_not_share_one_log(self):
        import run_av_baseline
        bench = run_av_baseline.log_path_for(["--engines", "argos"])
        report = run_av_baseline.log_path_for(["--report-only"])
        preflight = run_av_baseline.log_path_for(["--preflight-only"])
        self.assertNotEqual(bench, report)
        self.assertNotEqual(bench, preflight)
        self.assertEqual("run_av_baseline.log", bench.name)

    def test_the_default_invocation_still_writes_the_durable_log(self):
        # Narrowing this wrongly would move the bench trace to a new file and leave anyone
        # reading run_av_baseline.log with nothing -- the opposite of the fix's purpose.
        import run_av_baseline
        self.assertEqual("run_av_baseline.log", run_av_baseline.log_path_for([]).name)
        self.assertEqual("run_av_baseline.log",
                         run_av_baseline.log_path_for(["--skip-comet"]).name)


class TermContextSourceTests(unittest.TestCase):
    """The runner's idea of which engine can carry a glossary comes from the Rust trait.

    A second, hand-kept list in Python would drift the moment an engine starts or stops
    honouring term context -- and the drift direction that matters is the one where the
    report claims a comparison is "not applicable" while the backend now does apply terms.
    So the expectation is parsed out of `engine/mod.rs`, from the assertions that already
    pin the trait there, in the same spirit as the FFI census reading lib.rs.
    """

    RUST = (pathlib.Path(__file__).resolve().parent.parent
            / "translator-engine" / "src" / "engine" / "mod.rs")

    def _rust_expectations(self):
        text = self.RUST.read_text(encoding="utf-8")
        yes, no = set(), set()
        for m in re.finditer(r'assert!\((!?)get_engine\("([a-z]+)"\)'
                             r'\.accepts_term_context\(\)\)', text):
            (no if m.group(1) else yes).add(m.group(2))
        return yes, no

    def test_the_extraction_is_not_silently_empty(self):
        # Without this guard the equality test below could pass against an empty set, which
        # is the shape a "vacuously green" source-check always has.
        yes, no = self._rust_expectations()
        self.assertEqual({"ollama"}, yes)
        self.assertEqual({"demo", "argos", "madlad"}, no)

    def test_the_python_table_matches_what_the_engine_asserts(self):
        from run_av_baseline import ACCEPTS_TERM_CONTEXT
        yes, no = self._rust_expectations()
        self.assertEqual(yes, {k for k, v in ACCEPTS_TERM_CONTEXT.items() if v})
        self.assertEqual(no, {k for k, v in ACCEPTS_TERM_CONTEXT.items() if not v})


if __name__ == "__main__":
    unittest.main()
