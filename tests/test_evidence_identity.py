#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Evidence identity: which model produced an AV arm.

Why this suite exists: switching the AI leg's model is done through the
environment (`LT_OLLAMA_MODEL` > `WONSLATE_OLLAMA_MODEL` > bare, the chain
`translator-engine/src/config.rs` resolves), which `evidence_name()` never saw. So a
candidate model run at `--n 300` computed exactly the published qwen3:8b file name and
replaced it in place, silently -- the same class of loss the direction and row count were
added to the name to stop.

Two rules these tests pin, and neither is decoration:

* a **non-default** model must get its own file, so no published evidence is rewritten;
* the **default** model must keep the historical name, otherwise renaming the baseline
  strands every artefact already recorded against it.

Like the other naming tests here, these run with no sidecar, no Ollama and no built
cdylib: naming is pure.
"""
import importlib
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "script"))

bench = importlib.import_module("bench_domain_av")
baseline = importlib.import_module("run_av_baseline")


class ModelIdentityResolutionTests(unittest.TestCase):
    """The resolver must read the same chain the product reads, both sides of each key."""

    def test_nothing_set_lands_on_the_shipped_default(self):
        model = bench.ollama_model_identity(lambda key: None)
        self.assertEqual(bench.DEFAULT_OLLAMA_MODEL, model)

    def test_prefix_order_wins_over_the_bare_name(self):
        values = {
            "LT_OLLAMA_MODEL": "lt-model:1",
            "WONSLATE_OLLAMA_MODEL": "wonslate-model:1",
            "OLLAMA_MODEL": "bare-model:1",
        }
        self.assertEqual(
            "lt-model:1", bench.ollama_model_identity(values.get)
        )
        del values["LT_OLLAMA_MODEL"]
        self.assertEqual(
            "wonslate-model:1", bench.ollama_model_identity(values.get)
        )
        del values["WONSLATE_OLLAMA_MODEL"]
        self.assertEqual("bare-model:1", bench.ollama_model_identity(values.get))

    def test_blank_or_whitespace_counts_as_unset(self):
        # Same rule `resolve_av_root` applies: an exported empty string is not a choice.
        values = {
            "LT_OLLAMA_MODEL": "   ",
            "WONSLATE_OLLAMA_MODEL": "",
            "OLLAMA_MODEL": "real-model:2",
        }
        self.assertEqual("real-model:2", bench.ollama_model_identity(values.get))

    def test_candidate_keys_are_the_ones_the_product_reads(self):
        # Drift guard: renaming a knob in config.rs without saying so here must go red.
        source = (REPO / "translator-engine" / "src" / "config.rs").read_text(
            encoding="utf-8"
        )
        for key in bench.OLLAMA_MODEL_KEYS:
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, source)
        self.assertIn("qwen3:8b", source)


class EvidenceNamingTests(unittest.TestCase):
    def test_default_model_keeps_the_published_name(self):
        name = bench.evidence_name("ollama-qwen", "en-zh", "av", 300,
                                   model=bench.DEFAULT_OLLAMA_MODEL)
        self.assertEqual("av-domain-ollama-qwen-av-en-zh-n300.json", name)

    def test_a_candidate_gets_a_file_nobody_else_owns(self):
        base = bench.evidence_name("ollama-qwen", "en-zh", "av", 300,
                                   model=bench.DEFAULT_OLLAMA_MODEL)
        hy = bench.evidence_name("ollama-qwen", "en-zh", "av", 300,
                                 model="hy-mt2-1.8b:latest")
        idx = bench.evidence_name("ollama-qwen", "en-zh", "av", 300,
                                  model="index-translate-2b:latest")
        self.assertNotEqual(base, hy)
        self.assertNotEqual(hy, idx)
        self.assertIn("hy-mt2-1.8b-latest", hy)
        # File-system safe: a tag containing ':' or '/' would not survive a checkout.
        for name in (hy, idx):
            for bad in (":", "/", "\\"):
                self.assertNotIn(bad, name)

    def test_only_the_ai_leg_carries_a_model_tag(self):
        # argos/madlad have no model to swap; tagging them would invent a second identity
        # for files that a single checkpoint always produced.
        name = bench.evidence_name("madlad", "en-zh", "av", 300,
                                   model=bench.DEFAULT_OLLAMA_MODEL)
        self.assertEqual("av-domain-madlad-av-en-zh-n300.json", name)
        other = bench.evidence_name("madlad", "en-zh", "av", 300,
                                    model="hy-mt2-1.8b:latest")
        self.assertEqual(name, other)

    def test_legacy_call_shape_still_resolves_to_the_old_name(self):
        # Every published name was built without a model argument; the default must be
        # "the shipped model", not "some tag appended".
        self.assertEqual(
            "av-domain-ollama-qwen-av-en-zh-n300.json",
            bench.evidence_name("ollama-qwen", "en-zh", "av", 300),
        )


class WriterDoesNotClobberTests(unittest.TestCase):
    """The naming rule only protects evidence if the writer honours it."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._dir.name)
        self._saved = bench.EVIDENCE_DIR
        bench.EVIDENCE_DIR = self.root

    def tearDown(self):
        bench.EVIDENCE_DIR = self._saved
        self._dir.cleanup()

    def _write(self, rows, model, run_index=1):
        src = ["source %d" % i for i in range(rows)]
        tgt = ["referencia %d" % i for i in range(rows)]
        hyp = ["translation %d" % i for i in range(rows)]
        return bench._write_evidence("ollama-qwen", "en-zh", src, tgt, hyp,
                                     {"chrF": 0.0, "BLEU": 0.0}, rows, "av",
                                     run_index=run_index, model=model)

    def test_a_candidate_run_leaves_the_published_baseline_untouched(self):
        published = self._write(3, model=bench.DEFAULT_OLLAMA_MODEL)
        sentinel = published.read_bytes()
        candidate = self._write(3, model="hy-mt2-1.8b:latest")
        self.assertNotEqual(published, candidate)
        self.assertEqual(sentinel, published.read_bytes())
        # Two shapes of the same identity, each in its own place: the file name carries the
        # path-safe tag, the JSON body carries the model string the env actually named.
        self.assertIn("hy-mt2-1.8b-latest", candidate.name)
        self.assertIn(b"hy-mt2-1.8b:latest", candidate.read_bytes())

    def test_the_file_states_which_model_answered(self):
        # An identity that lives only in the file name cannot be checked by a reader who
        # holds the JSON; the run record has to carry it (same rule as direction/rows).
        path = self._write(2, model="index-translate-2b:latest")
        text = path.read_text(encoding="utf-8")
        self.assertIn('"ollama_model"', text)
        self.assertIn("index-translate-2b:latest", text)

    def test_repeats_of_one_model_still_share_one_file(self):
        # The noise floor comes from comparing repeats of the *same* measurement; making
        # each repeat its own file would take that comparison away.
        first = self._write(2, model="hy-mt2-1.8b:latest", run_index=1)
        second = self._write(2, model="hy-mt2-1.8b:latest", run_index=2)
        self.assertEqual(first, second)


class CallSiteTests(unittest.TestCase):
    """A rule nobody calls is a rule nobody breaks."""

    def test_preflight_shares_the_naming_rule(self):
        # The runner computes the path it expects before benching; if that computation
        # ignores the model, preflight approves a path the writer then refuses to use.
        source = (REPO / "script" / "run_av_baseline.py").read_text(encoding="utf-8")
        calls = [line for line in source.splitlines() if "evidence_name(" in line
                 and "import" not in line and "def " not in line]
        self.assertTrue(calls, "no evidence_name() call site left to check")
        for line in calls:
            with self.subTest(line=line.strip()):
                self.assertIn("model", line)

    def test_writer_entrypoint_passes_the_resolved_model(self):
        source = (REPO / "script" / "bench_domain_av.py").read_text(encoding="utf-8")
        lines = source.splitlines()
        starts = [i for i, line in enumerate(lines) if line.startswith("def main(")]
        self.assertEqual(1, len(starts))
        body = "\n".join(lines[starts[0]:starts[0] + 60])
        self.assertIn("ollama_model_identity", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
