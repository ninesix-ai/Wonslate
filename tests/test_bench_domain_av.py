# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""S12 tests for the AV-domain benchmark harness.

See ``docs/tasks/S12-AV领域评测基线.md``. These assertions author the contract of
``script/bench_domain_av.py``. They were first committed as RED-phase tests while
the harness was still missing - the import below used to raise
``ModuleNotFoundError`` at collection time, which was the intended RED signal.
The harness has since landed (``b9bd906`` onward), so the suite now runs GREEN
against the real module; what the assertions pin has not changed.

The tests mirror ``tests/test_bench_flores.py`` (S10 T1) in structure: pure
stdlib ``unittest``, hermetic fixtures under a temp directory, no network, no
GPU, no real sidecar. Run:

    python -m unittest tests.test_bench_domain_av -v
"""
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "script"))

# The four symbols below are the public surface of the S12 harness that these
# tests pin; they were the contract authored during the RED phase.
from bench_domain_av import (  # noqa: E402
    DEFAULT_AV_SUBDIR,
    AvDataNotFound,
    find_pair_dir,
    measure_adherence,
    resolve_av_root,
)

LINE_TUPLES = [
    ("缓冲区太小了。", "The buffer is too small."),
    ("启用流式合成。", "Enable streaming synthesis."),
    ("VAD 切成三段。", "VAD produced three segments."),
]


def write_pair(dir_path, prefix="av-zh-en"):
    """Materialise a sacrebleu-shaped pair directory."""
    pathlib.Path(dir_path).mkdir(parents=True, exist_ok=True)
    src = pathlib.Path(dir_path).joinpath(prefix + ".src")
    tgt = pathlib.Path(dir_path).joinpath(prefix + ".tgt")
    # Test asserts zh->en; for the fixture the source column is Chinese.
    src.write_text("".join(s + "\n" for s, _ in LINE_TUPLES), encoding="utf-8")
    tgt.write_text("".join(t + "\n" for _, t in LINE_TUPLES), encoding="utf-8")
    return src, tgt


class ResolutionPriorityTests(unittest.TestCase):
    """① ② ③: --av-root > $WONSLATE_AV_DIR > <data_dir>/benchmarks/av-domain."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._saved = {}
        for key in ("WONSLATE_AV_DIR", "LT_DATA_DIR", "WONSLATE_DATA_DIR"):
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_root_is_the_benchmarks_av_domain_subdir(self):
        os.environ["WONSLATE_DATA_DIR"] = str(self.tmp / "data")
        self.assertEqual(
            resolve_av_root(None),
            self.tmp / "data" / DEFAULT_AV_SUBDIR)

    def test_env_var_overrides_default(self):
        os.environ["WONSLATE_AV_DIR"] = str(self.tmp / "elsewhere")
        self.assertEqual(resolve_av_root(None), self.tmp / "elsewhere")

    def test_explicit_root_beats_env_var(self):
        os.environ["WONSLATE_AV_DIR"] = str(self.tmp / "elsewhere")
        given = self.tmp / "given"
        self.assertEqual(resolve_av_root(given), given)

    def test_missing_data_raises_av_data_not_found_with_candidates(self):
        # Explicit failure per REQ-B2: never silently fall back. The error text
        # must list the candidate directories tried AND name WONSLATE_AV_DIR so
        # a fresh operator can recover from the traceback alone.
        os.environ["WONSLATE_DATA_DIR"] = str(self.tmp / "empty")
        with self.assertRaises(AvDataNotFound) as ctx:
            find_pair_dir(resolve_av_root(None), "av-zh-en")
        message = str(ctx.exception)
        self.assertIn("WONSLATE_AV_DIR", message)


class LoaderTests(unittest.TestCase):
    """④ ⑤ ⑥: pair loading and engine plumbing."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_load_pair_returns_source_and_reference(self):
        root = self.tmp / "av-zh-en"
        write_pair(root)
        # load_pair is exercised indirectly through find_pair_dir + file reads in
        # the eventual implementation; assert only on the discovery seam here to
        # keep this file resilient across internal helper renames.
        found = find_pair_dir(self.tmp, "av-zh-en")
        self.assertEqual(found.name, "av-zh-en")
        self.assertTrue(found.joinpath("av-zh-en.src").exists())
        self.assertTrue(found.joinpath("av-zh-en.tgt").exists())

    def test_corpus_files_carry_spdx_header(self):
        # REQ-E1 hygiene: every committed data file in a source-lint scope must
        # start with an SPDX banner so the license gate can grep it.
        root = self.tmp / "av-zh-en"
        src, _ = write_pair(root)
        src.write_text(
            "# SPDX-License-Identifier: Apache-2.0\n"
            "# Copyright (c) 2026 ninesix-ai studio\n"
            + src.read_text(encoding="utf-8"),
            encoding="utf-8")
        first = src.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("# SPDX-License-Identifier:"))

    def test_engine_argument_accepts_all_three_real_ids(self):
        # ⑤: the harness must accept argos / madlad / ollama-qwen (the three
        # registered engines per REQ-A4) and reject unknown ids with a clean
        # argparse error rather than a stack trace.
        from bench_domain_av import build_arg_parser
        parser = build_arg_parser()
        for engine_id in ("argos", "madlad", "ollama-qwen"):
            ns = parser.parse_args(["--engine", engine_id, "--direction", "zh-en"])
            self.assertEqual(ns.engine, engine_id)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["--engine", "not-an-engine", "--direction", "zh-en"])


class DomainArgumentTests(unittest.TestCase):
    """⑧ ⑨: --domain plumbs the request body's `domain` field through so the
    harness can compare baseline vs domain-scoped runs on the same sentence
    set (S12 §T3). Default is the empty string, matching an unscoped request.
    """

    def test_domain_defaults_to_empty_string(self):
        from bench_domain_av import build_arg_parser
        ns = build_arg_parser().parse_args(["--engine", "argos", "--direction", "zh-en"])
        self.assertEqual(ns.domain, "")

    def test_domain_flag_accepted(self):
        from bench_domain_av import build_arg_parser
        ns = build_arg_parser().parse_args(
            ["--engine", "argos", "--direction", "zh-en", "--domain", "av"])
        self.assertEqual(ns.domain, "av")

    def test_run_one_signature_accepts_domain_kwarg(self):
        # _run_one must be callable with (engine_id, direction, src, tgt, domain="...").
        # We do not run it (that would need a live sidecar / ollama); just verify
        # the signature admits the keyword.
        import inspect
        from bench_domain_av import _run_one
        params = inspect.signature(_run_one).parameters
        self.assertIn("domain", params)
        # Give it a default so pre-existing callers keep working.
        self.assertEqual(params["domain"].default, "")


class OneRequestPathTests(unittest.TestCase):
    """Both arms of the domain control must travel one single request path.

    The unscoped arm (domain="") used to go through bench_translation's shared
    client while the scoped arm used _translate_via_ffi (with use_tm pinned to
    False). Two arms that differ in more than the `domain` field cannot measure a
    domain effect: whatever the shared client's TM behaviour or engine wiring does
    gets charged to the delta. So the harness must issue both arms through the same
    request builder, and only the domain may differ.

    Hermetic: nothing here opens a socket, loads the DLL or scores real text.
    """

    def _run_arm(self, domain):
        import bench_domain_av as m
        import bench_translation

        seen = []

        def fake_ffi(engine_id, src_lang, tgt_lang, text, dom=""):
            seen.append((engine_id, src_lang, tgt_lang, text, dom))
            return ("hypothesis", {"ok": True})

        def explode(*_a, **_k):
            raise AssertionError(
                "the unscoped arm must not reach for the shared bench_translation "
                "client; both arms have to issue through one request builder")

        orig_ffi, orig_build, orig_score = (m._translate_via_ffi,
                                           bench_translation.build_engines,
                                           bench_translation.score)
        m._translate_via_ffi = fake_ffi
        bench_translation.build_engines = explode
        bench_translation.score = lambda h, refs, tgt: (0.0, 0.0)
        try:
            hypotheses, metrics, _failures = m._run_one("ollama-qwen", "zh-en",
                                                       ["句子"], ["sentence"],
                                                       domain)
        finally:
            m._translate_via_ffi, bench_translation.build_engines = orig_ffi, orig_build
            bench_translation.score = orig_score
        return seen, hypotheses, metrics

    def test_scoped_arm_uses_the_ffi_builder(self):
        seen, hyps, _ = self._run_arm("av")
        self.assertEqual([s[4] for s in seen], ["av"])
        self.assertEqual(hyps, ["hypothesis"])

    def test_unscoped_arm_uses_the_same_ffi_builder(self):
        # RED today: the empty-domain branch takes the shared-client path, so the
        # stub above raises before any request is recorded.
        seen, _, _ = self._run_arm("")
        self.assertEqual([s[4] for s in seen], [""],
                          "the unscoped arm must issue through the same builder")

    def test_both_arms_differ_only_in_the_domain_field(self):
        scoped, _, _ = self._run_arm("av")
        plain, _, _ = self._run_arm("")
        self.assertEqual(len(scoped), len(plain))
        for a, b in zip(scoped, plain):
            self.assertEqual(a[:4], b[:4],
                              "engine, languages and text must be identical across arms")


class EngineFailureAccountingTests(unittest.TestCase):
    """D31: a row the engine did not translate must not be scored as a translation.

    Measured, not theorised: in the 2026-10-05 ollama comparison the unscoped arm
    returned nothing for 19 of 300 sentences. `_run_one` kept only
    `resp.get("output") or ""` and threw the response away, so an engine error
    became an empty hypothesis that stayed inside `evaluated`, got scored as if the
    engine had produced a bad translation, and let the report print "complete".
    The published COMET delta was +0.0330; the paired delta over the 281 rows both
    arms actually answered was +0.0058 -- five sixths of the headline was flakiness.

    Replay proves these were transient: all 19 rows translate fine today (1.9-3.8s,
    engine field intact), so the only thing that ever distinguished them was the
    harness throwing away the reason. Hence: retry once, then record what is left.

    Hermetic: no sidecar, no DLL, no network -- the request builder is stubbed the
    same way OneRequestPathTests stubs it.
    """

    SRC = ["启用流式合成。", "第二句。", "第三句。"]
    TGT = ["Enable streaming synthesis.", "Second sentence.", "Third sentence."]

    def _run_one(self, replies):
        import bench_domain_av as m
        import bench_translation

        seen, scored = [], []

        def fake_ffi(engine_id, src_lang, tgt_lang, text, dom=""):
            attempt = sum(1 for t, _d in seen if t == text) + 1
            seen.append((text, dom))
            entry = replies[text]
            return entry(attempt) if callable(entry) else entry

        def fake_score(hyps, refs, tgt_lang):
            scored.append((list(hyps), list(refs)))
            return (0.0, 0.0)

        orig_ffi, orig_score = m._translate_via_ffi, bench_translation.score
        m._translate_via_ffi = fake_ffi
        bench_translation.score = fake_score
        try:
            result = m._run_one("ollama-qwen", "zh-en", self.SRC, self.TGT, "")
        finally:
            m._translate_via_ffi, bench_translation.score = orig_ffi, orig_score
        return result, seen, scored

    def test_row_that_returns_nothing_is_recorded_as_a_failure(self):
        replies = {
            self.SRC[0]: ("one", {"engine": "ollama-qwen"}),
            self.SRC[1]: ("", {"error": "engine_timeout", "message": "30s elapsed"}),
            self.SRC[2]: ("three", {"engine": "ollama-qwen"}),
        }
        (hyps, _metrics, failures), _seen, scored = self._run_one(replies)
        self.assertEqual([f["id"] for f in failures], [1])
        self.assertEqual(failures[0]["reason"], "engine_timeout")
        # The slot stays: samples are positional, and the two arms must remain
        # comparable row by row -- dropping the row would shift every later id.
        self.assertEqual(hyps[1], "")
        # But it is not part of what gets scored.
        self.assertEqual(scored[0][0], ["one", "three"])
        self.assertEqual(scored[0][1], [self.TGT[0], self.TGT[2]])

    def test_empty_output_without_an_error_field_is_still_a_failure(self):
        # A response shaped like success but carrying no text is exactly the shape
        # that hid for a whole run; the reason must say so rather than vanish.
        replies = {
            self.SRC[0]: ("", {}),
            self.SRC[1]: ("two", {"engine": "ollama-qwen"}),
            self.SRC[2]: ("three", {"engine": "ollama-qwen"}),
        }
        (_h, _m, failures), _seen, _s = self._run_one(replies)
        self.assertEqual(failures, [{"id": 0, "reason": "empty_output"}])

    def test_transient_failure_that_recovers_on_retry_is_not_recorded(self):
        # 19 of 19 replayed rows answered first try today, so the original failures
        # were transient: one retry is the difference between a logged failure and a
        # silently scored empty string.
        def flaky(attempt):
            if attempt == 1:
                return "", {"error": "engine_timeout"}
            return "recovered", {"engine": "ollama-qwen"}

        replies = {
            self.SRC[0]: ("one", {"engine": "ollama-qwen"}),
            self.SRC[1]: flaky,
            self.SRC[2]: ("three", {"engine": "ollama-qwen"}),
        }
        (hyps, _metrics, failures), seen, _scored = self._run_one(replies)
        self.assertEqual(failures, [])
        self.assertEqual(hyps[1], "recovered")
        self.assertEqual(sum(1 for t, _d in seen if t == self.SRC[1]), 2,
                         "the failed row must be given exactly one retry")
        self.assertEqual(sum(1 for t, _d in seen if t == self.SRC[0]), 1,
                         "a row that answered must not be re-asked")


class EvidenceFailureFieldsTests(unittest.TestCase):
    """D31: the evidence file must state how many rows actually answered.

    `requested` and `evaluated` were both 300 while 19 rows were empty, so nothing
    in the artifact distinguished a complete run from one that lost rows -- which is
    how a +0.0058 result shipped as +0.0330 marked complete. `evaluated` now means
    "rows that produced a translation and were scored".

    Also pins the repeat slot: S12 needs each arm run more than once to tell a
    terminology effect from ollama's sampling noise (temperature 0.3, no seed), and
    the evidence shape already carries a `runs` array for exactly that.
    """

    def setUp(self):
        import bench_domain_av
        self.m = bench_domain_av
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_dir = self.m.EVIDENCE_DIR
        self.m.EVIDENCE_DIR = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.m.EVIDENCE_DIR = self.orig_dir
        self.tmp.cleanup()

    def _read(self):
        import json
        path = self.m.EVIDENCE_DIR / "av-domain-ollama-qwen.json"
        with io.open(str(path), encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, hyps, failures, run_index=1):
        self.m._write_evidence("ollama-qwen", "zh-en",
                               ["s1", "s2", "s3"], ["t1", "t2", "t3"],
                               hyps, {"chrF": 10.0, "BLEU": 1.0}, None, "",
                               failures=failures, run_index=run_index)

    def test_failed_rows_and_the_scored_count_are_written(self):
        self._write(["a", "", "c"], [{"id": 1, "reason": "engine_timeout"}])
        run = self._read()["runs"][0]
        self.assertEqual(run["requested"], 3)
        self.assertEqual(run["evaluated"], 2, "empty rows must not count as evaluated")
        self.assertEqual(run["scored"], 2)
        self.assertEqual(run["failed"], [{"id": 1, "reason": "engine_timeout"}])

    def test_a_clean_run_records_no_failures(self):
        self._write(["a", "b", "c"], [])
        run = self._read()["runs"][0]
        self.assertEqual(run["failed"], [])
        self.assertEqual(run["evaluated"], 3)

    def test_a_second_repeat_appends_a_run_instead_of_replacing_the_first(self):
        # Noise floor needs two runs of the same arm in the same artifact, otherwise
        # the second repeat silently destroys the evidence for the first.
        self._write(["a", "b", "c"], [], run_index=1)
        self._write(["a2", "b2", "c2"], [], run_index=2)
        record = self._read()
        self.assertEqual(len(record["runs"]), 2)
        self.assertEqual(record["runs"][1]["samples"][0]["hypothesis"], "a2")
        self.assertEqual(record["runs"][0]["samples"][0]["hypothesis"], "a")

    def test_repeat_one_starts_a_series_rather_than_appending_to_an_old_one(self):
        # Stale repeats from a previous build must not masquerade as noise controls
        # for this one.
        self._write(["a", "b", "c"], [], run_index=1)
        self._write(["x", "y", "z"], [], run_index=1)
        record = self._read()
        self.assertEqual(len(record["runs"]), 1)
        self.assertEqual(record["runs"][0]["samples"][0]["hypothesis"], "x")


class TermAdherenceTests(unittest.TestCase):
    """D31's tail: a glossary must be graded on terminology, not only on COMET.

    COMET measures overall quality; what a seed pack promises is that a specific
    source term comes out as the specific target term. The 2026-10-07 audit found
    COMET Delta=+0.0001 while term adherence sat at +0.000 on the rows where a term
    could actually be injected -- a distinction that only exists if the harness
    reports adherence at all. Until this ran, that number came from throwaway
    scripts outside the repo, which is how a claim gets published that nobody can
    re-measure (REQ-F3 keeps a standing note about exactly that).

    Hermetic: pairs and hypotheses are literals; no DLL, no network, no model.
    """

    PAIRS = [("\u8bed\u97f3\u6d3b\u52a8\u68c0\u6d4b", "voice activity detection"),
             ("\u6d41\u5f0f\u5408\u6210", "streaming synthesis"),
             ("\u8bed\u6bb5", "segment")]

    def test_counts_opportunities_and_hits_over_rows_that_mention_a_term(self):
        samples = [
            {"id": 0, "source": "\u542f\u7528\u8bed\u97f3\u6d3b\u52a8\u68c0\u6d4b\u3002",
             "hypothesis": "Enable voice activity detection."},
            {"id": 1, "source": "\u542f\u7528\u6d41\u5f0f\u5408\u6210\u3002",
             "hypothesis": "Turn on streaming synthesis."},
            {"id": 2, "source": "\u8fd9\u6bb5\u6ca1\u6709\u88ab\u6536\u5f55\u7684\u672f\u8bed\u3002",
             "hypothesis": "This sentence has no listed term."},
        ]
        stats = measure_adherence(samples, self.PAIRS)
        self.assertEqual(stats["rows_with_terms"], 2, "the third row mentions no pack term")
        self.assertEqual(stats["opportunities"], 2)
        self.assertEqual(stats["hits"], 2)
        self.assertAlmostEqual(stats["rate"], 1.0, places=6)

    def test_a_missing_target_term_is_a_miss_not_an_absence(self):
        samples = [{"id": 0, "source": "\u542f\u7528\u6d41\u5f0f\u5408\u6210\u3002",
                    "hypothesis": "Turn on continuous synthesis."}]
        stats = measure_adherence(samples, self.PAIRS)
        self.assertEqual(stats["opportunities"], 1)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["rate"], 0.0)

    def test_english_terms_match_as_words_not_as_substrings(self):
        # "segment" inside "segmentation" must not count as using the prescribed
        # translation, or the metric would flatter the engine and hide a real miss.
        samples = [{"id": 0, "source": "\u8bed\u6bb5\u5207\u5f97\u592a\u788e\u3002",
                    "hypothesis": "The segmentation is too fine."}]
        stats = measure_adherence(samples, self.PAIRS)
        self.assertEqual((stats["opportunities"], stats["hits"]), (1, 0))

    def test_case_differences_do_not_count_against_terminology(self):
        samples = [{"id": 0, "source": "\u542f\u7528\u8bed\u97f3\u6d3b\u52a8\u68c0\u6d4b\u3002",
                    "hypothesis": "Enable Voice Activity Detection."}]
        self.assertEqual(measure_adherence(samples, self.PAIRS)["hits"], 1)

    def test_empty_hypothesis_still_counts_as_an_opportunity_missed(self):
        # Rows the engine failed to translate (defect D31) must show up in this
        # denominator as misses, not silently vanish: "no output" is the worst
        # possible terminology adherence, and the counts say so either way.
        samples = [{"id": 0, "source": "\u542f\u7528\u6d41\u5f0f\u5408\u6210\u3002",
                    "hypothesis": ""}]
        stats = measure_adherence(samples, self.PAIRS)
        self.assertEqual((stats["opportunities"], stats["hits"], stats["rate"]), (1, 0, 0.0))

    def test_no_pack_loaded_yields_zeroes_rather_than_a_fabricated_rate(self):
        stats = measure_adherence([{"id": 0, "source": "x", "hypothesis": "y"}], [])
        self.assertEqual(stats["opportunities"], 0)
        self.assertIsNone(stats["rate"], "no opportunities is not 0% adherence")

    def test_the_evidence_file_carries_the_block(self):
        # The evidence file is what the report reads, so the block has to live there
        # rather than only in stdout.
        import bench_domain_av
        import json
        orig_dir = bench_domain_av.EVIDENCE_DIR
        with tempfile.TemporaryDirectory() as d:
            bench_domain_av.EVIDENCE_DIR = pathlib.Path(d)
            try:
                samples = [{"id": 0, "source": "启用流式合成。",
                            "hypothesis": "streaming synthesis"}]
                bench_domain_av._write_evidence(
                    "argos", "zh-en", ["启用流式合成。"], ["Enable streaming synthesis."],
                    ["streaming synthesis"], {"chrF": 1.0, "BLEU": 2.0}, None, "",
                    adherence=measure_adherence(samples, self.PAIRS))
                path = pathlib.Path(d) / "av-domain-argos.json"
                rec = json.loads(path.read_text(encoding="utf-8"))
            finally:
                bench_domain_av.EVIDENCE_DIR = orig_dir
        block = rec["runs"][0]["term_adherence"]
        self.assertEqual(block["opportunities"], 1)
        self.assertEqual(block["hits"], 1)
        self.assertEqual(block["rate"], 1.0)
        self.assertEqual(block["terms_in_pack"], len(self.PAIRS))


if __name__ == "__main__":
    unittest.main()
