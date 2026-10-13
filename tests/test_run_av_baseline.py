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
        text = "\n".join(run_av_baseline._hypothesis_block(summary, {}, {}, []))
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
