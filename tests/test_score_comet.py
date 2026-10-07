# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Hermetic tests for the COMET aggregation step (defect D31).

`script/score_comet.py` reads the evidence JSONs the bench harnesses write and
rewrites them in place with COMET scores. torch and comet are imported inside
main(), so this suite needs no model, no GPU and no network: the aggregation is
reachable through `score_run(run, predict)` with the predictor passed in.

Run:
    python -m unittest tests.test_score_comet -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "script"))

from score_comet import score_run  # noqa: E402


def _samples(rows):
    """rows: list of (hypothesis, score) pairs; hypothesis "" means the engine failed."""
    return [{"id": i, "source": "src-%d" % i, "reference": "ref-%d" % i,
             "hypothesis": h} for i, (h, _s) in enumerate(rows)]


def _predict_factory(rows):
    """A stand-in for model.predict: records what it was asked to score.

    Scores are looked up by hypothesis text rather than by position, because the
    whole point of the test is that the caller filters rows first -- a positional
    stand-in would silently renumber them and pass for correct."""
    seen = []
    by_text = {h: s for h, s in rows}

    def predict(data):
        seen.append(data)
        return [by_text[d["mt"]] for d in data]
    return predict, seen


class FailedRowsAreNotTranslations(unittest.TestCase):
    """A row the engine never translated must not be scored as a translation.

    Measured, not assumed: in the 2026-10-05 ollama baseline arm 19 of 300
    hypotheses were empty. COMET gave them very low scores and the run mean fell to
    0.7843, so the published delta of +0.0330 was largely the reliability of an
    endpoint rather than a terminology effect -- the paired delta over the 281 rows
    both arms answered was +0.0058. The COMET pass is where that distortion is
    finally caught, because it is where the mean is formed.
    """

    def test_predict_never_sees_a_row_without_a_hypothesis(self):
        rows = [("one", 0.90), ("", 0.10), ("three", 0.80)]
        predict, seen = _predict_factory(rows)
        run = {"engine": "ollama-qwen", "samples": _samples(rows)}
        score_run(run, predict)
        self.assertEqual([d["mt"] for d in seen[0]], ["one", "three"],
                         "an empty hypothesis must not be offered to COMET")

    def test_mean_is_formed_over_answered_rows_only(self):
        rows = [("one", 0.90), ("", 0.10), ("three", 0.80)]
        predict, _seen = _predict_factory(rows)
        run = {"engine": "ollama-qwen", "samples": _samples(rows)}
        score_run(run, predict)
        # (0.90 + 0.80) / 2, not (0.90 + 0.10 + 0.80) / 3 = 0.60.
        self.assertAlmostEqual(run["comet"], 0.85, places=4)
        self.assertEqual(run["comet_scored"], 2)

    def test_per_sample_positions_are_preserved_with_a_gap_for_failures(self):
        # The two arms are compared row by row, so the per-sample array must stay
        # addressable by id; null marks "not scored", it does not shift the rest.
        rows = [("one", 0.90), ("", 0.10), ("three", 0.80)]
        predict, _seen = _predict_factory(rows)
        run = {"engine": "ollama-qwen", "samples": _samples(rows)}
        score_run(run, predict)
        self.assertEqual(run["comet_scores_per_sample"], [0.9, None, 0.8])

    def test_a_run_where_nothing_answered_has_no_mean_rather_than_a_zero(self):
        # COMET 0.000 would read as "the engine produced terrible translations";
        # the truth is "there was nothing to grade", and only null says that.
        rows = [("", 0.0), ("", 0.0)]
        predict, seen = _predict_factory(rows)
        run = {"engine": "ollama-qwen", "samples": _samples(rows)}
        score_run(run, predict)
        self.assertIsNone(run["comet"])
        self.assertEqual(run["comet_scored"], 0)
        self.assertEqual(run["comet_scores_per_sample"], [None, None])
        self.assertEqual(seen, [], "nothing to score must not call the model")

    def test_a_clean_run_is_unchanged_in_shape(self):
        # The fix must not alter what a healthy evidence file looks like, or the
        # report generator and the FLORES pass both drift.
        rows = [("a", 0.7), ("b", 0.5)]
        predict, _seen = _predict_factory(rows)
        run = {"engine": "argos", "samples": _samples(rows)}
        score_run(run, predict)
        self.assertAlmostEqual(run["comet"], 0.6, places=4)
        self.assertEqual(run["comet_scores_per_sample"], [0.7, 0.5])
        self.assertEqual(len(run["samples"]), 2)


if __name__ == "__main__":
    unittest.main()
