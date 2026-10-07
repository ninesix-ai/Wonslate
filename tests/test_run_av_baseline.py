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
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
