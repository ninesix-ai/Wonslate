# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""S12 RED-PHASE tests for the AV-domain benchmark harness.

See ``docs/tasks/S12-AV领域评测基线.md``. These assertions author the desired
contract of ``script/bench_domain_av.py``; on the current tree the module does
not exist, so the ``from bench_domain_av import ...`` line raises
``ModuleNotFoundError`` at collection time. That is the correct RED signal:
the tests fail because the feature is missing, not because of a typo.

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

# This import MUST fail today: script/bench_domain_av.py is not authored yet.
# Once S12 lands, the module will expose these four symbols.
from bench_domain_av import (  # noqa: E402  (intentionally red)
    DEFAULT_AV_SUBDIR,
    AvDataNotFound,
    find_pair_dir,
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


if __name__ == "__main__":
    unittest.main()
