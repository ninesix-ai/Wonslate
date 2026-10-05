# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Path-resolution tests for the FLORES-101 benchmark harness.

Pure stdlib unittest, hermetic: the dataset layout is fabricated under a temp
directory and the environment is patched, so nothing here reads a developer
machine, touches the network or needs a GPU.

What is pinned:
  * the resolution priority -- --flores-root > $WONSLATE_FLORES_DIR >
    <data_dir>/benchmarks/flores101 (the same data_dir the model fetchers use);
  * layout discovery, so a checkout of the dataset in any of the shapes real
    mirrors ship is found without editing code;
  * the explicit-failure path -- missing data must exit non-zero and name both
    the candidates tried and the variable to set (REQ-B2: never fall back
    silently to some other directory).

Run:  python -m unittest tests.test_bench_flores -v
"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "script"))

from bench_flores import (  # noqa: E402
    DEFAULT_FLORES_SUBDIR, LANG_MAP, FloresDataNotFound, find_split_dir,
    load_split, resolve_flores_root,
)

LINES = ("first sentence\n", "second sentence\n", "third sentence\n")


def write_layout(dir_path, names, suffix=""):
    """Create one file per language stem under dir_path."""
    pathlib.Path(dir_path).mkdir(parents=True, exist_ok=True)
    for stem in names:
        pathlib.Path(dir_path).joinpath(stem + suffix).write_text("".join(LINES), encoding="utf-8")


class ResolutionPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self_saved = {}
        for key in ("WONSLATE_FLORES_DIR", "LT_DATA_DIR", "WONSLATE_DATA_DIR"):
            self_saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        self._saved = self_saved

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_is_the_benchmarks_subdir_of_the_shared_data_dir(self):
        os.environ["WONSLATE_DATA_DIR"] = str(self.tmp / "data")
        self.assertEqual(
            resolve_flores_root(None),
            self.tmp / "data" / DEFAULT_FLORES_SUBDIR)

    def test_the_environment_variable_overrides_the_default(self):
        os.environ["WONSLATE_DATA_DIR"] = str(self.tmp / "data")
        os.environ["WONSLATE_FLORES_DIR"] = str(self.tmp / "elsewhere")
        self.assertEqual(resolve_flores_root(None), self.tmp / "elsewhere")

    def test_an_explicit_root_beats_the_environment_variable(self):
        os.environ["WONSLATE_FLORES_DIR"] = str(self.tmp / "elsewhere")
        explicit = self.tmp / "given"
        self.assertEqual(resolve_flores_root(explicit), explicit)

    def test_blank_environment_value_is_not_treated_as_a_setting(self):
        # A stray `set WONSLATE_FLORES_DIR=` must not resolve to the cwd.
        os.environ["WONSLATE_FLORES_DIR"] = "   "
        os.environ["WONSLATE_DATA_DIR"] = str(self.tmp / "data")
        self.assertEqual(
            resolve_flores_root(None),
            self.tmp / "data" / DEFAULT_FLORES_SUBDIR)


class SplitDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.stems = sorted(set(LANG_MAP.values()))

    def test_flat_layout_with_bare_language_files(self):
        split_dir = self.tmp / "devtest"
        write_layout(split_dir, self.stems)
        self.assertEqual(find_split_dir(self.tmp, "devtest"), split_dir)

    def test_wrapped_layout_with_suffixed_language_files(self):
        split_dir = self.tmp / "flores101_dataset" / "devtest"
        write_layout(split_dir, self.stems, suffix=".devtest")
        self.assertEqual(find_split_dir(self.tmp, "devtest"), split_dir)

    def test_a_root_pointed_strictly_at_the_split_directory(self):
        write_layout(self.tmp, self.stems, suffix=".devtest")
        self.assertEqual(find_split_dir(self.tmp, "devtest"), self.tmp)

    def test_the_dev_split_is_found_without_touching_devtest(self):
        split_dir = self.tmp / "FLORES-101" / "dev"
        write_layout(split_dir, self.stems)
        self.assertEqual(find_split_dir(self.tmp, "dev"), split_dir)

    def test_a_directory_without_the_requested_split_is_not_found(self):
        write_layout(self.tmp / "dev", self.stems)
        self.assertIsNone(find_split_dir(self.tmp, "devtest"))


class LoadSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.stems = sorted(set(LANG_MAP.values()))

    def test_reads_every_product_language_from_a_wrapped_layout(self):
        write_layout(self.tmp / "flores101_dataset" / "devtest", self.stems,
                     suffix=".devtest")
        data = load_split(self.tmp, "devtest")
        self.assertEqual(sorted(data), self.stems)
        self.assertEqual(data["eng"], [line.rstrip("\n") for line in LINES])

    def test_missing_data_names_the_candidates_and_the_variable_to_set(self):
        empty = self.tmp / "nothing-here"
        empty.mkdir()
        with self.assertRaises(FloresDataNotFound) as caught:
            load_split(empty, "devtest")
        message = str(caught.exception)
        self.assertIn("WONSLATE_FLORES_DIR", message)
        self.assertIn(str(empty / "flores101_dataset" / "devtest"), message)

    def test_main_reports_missing_data_and_exits_nonzero(self):
        empty = self.tmp / "nothing-here"
        empty.mkdir()
        from bench_flores import main
        # The report goes to stdout; keep the suite output pristine and assert
        # only on the exit code (the message itself is pinned by the test above).
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--list", "--flores-root", str(empty)]), 1)


class ResumeTests(unittest.TestCase):
    """--resume only reads the evidence file when it already exists, so the
    json import it needs was never exercised by a first (fresh) run. These
    tests drive the branch a real interrupted collection takes."""

    # --list samples 100 ids by default, so the fixture corpus must exceed that
    # (the shared LINES constant is only three lines and other tests pin it).
    BIG_LINES = tuple("sentence {}\n".format(i) for i in range(120))

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.flores = self.tmp / "flores"
        split = self.flores / "devtest"
        split.mkdir(parents=True)
        for stem in sorted(set(LANG_MAP.values())):
            (split / stem).write_text("".join(self.BIG_LINES), encoding="utf-8")
        self.evidence = self.tmp / "evidence.json"

    def test_a_missing_evidence_file_is_not_an_error(self):
        """A first run has nothing to resume and must not try to read the file."""
        from bench_flores import main
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["--list", "--flores-root", str(self.flores),
                      "--resume", "--out", str(self.evidence)]),
                0)
        self.assertFalse(self.evidence.exists())

    def test_resume_reads_an_existing_evidence_file(self):
        """The stored-samples branch parses JSON; it must not raise NameError."""
        self.evidence.write_text(
            json.dumps({"sample_ids": [1, 2, 3], "runs": []}), encoding="utf-8")
        from bench_flores import main
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["--list", "--flores-root", str(self.flores),
                      "--resume", "--out", str(self.evidence)]),
                0)

    def test_a_corrupt_evidence_file_is_not_silently_ignored(self):
        # REQ-B2: never quietly switch inputs. A damaged file must surface
        # rather than being swallowed into an empty resume set.
        self.evidence.write_text("{not json", encoding="utf-8")
        import bench_flores
        with self.assertRaises(ValueError):
            bench_flores._load_resumable(self.evidence, [1, 2, 3])

    def _ids_for(self, n, seed=42, total=120):
        """The sample ids main() draws for this fixture corpus, same rule."""
        rng = __import__("random").Random(seed)
        return sorted(rng.sample(range(total), n))

    def _stored_file(self, runs, n=2):
        self.evidence.write_text(
            json.dumps({"sample_ids": self._ids_for(n), "seed": 42,
                        "sample_per_direction": n, "runs": runs}), encoding="utf-8")

    def _fake_engine(self, calls, engine_id):
        """A countable stand-in for a live engine: it records every request."""
        import bench_flores

        def translate(text, src, tgt):
            calls.append((src, tgt))
            return "hyp-" + tgt

        return bench_flores.Engine(engine_id, translate, lambda: True)

    def _collect(self, patch_to, extra=()):
        import bench_flores
        original = bench_flores.build_engines
        bench_flores.build_engines = lambda args: patch_to
        self.addCleanup(setattr, bench_flores, "build_engines", original)
        argv = ["--flores-root", str(self.flores), "--engines", "fake",
                "--n", "2", "--pairs", "en-zh", "--out", str(self.evidence)] + list(extra)
        with contextlib.redirect_stdout(io.StringIO()):
            code = bench_flores.main(argv)
        self.assertEqual(code, 0)
        return json.loads(self.evidence.read_text(encoding="utf-8"))

    def test_resume_redoes_no_request_for_samples_already_stored(self):
        # The branch the existing --list tests never reach: a resumed direction
        # must reuse stored hypotheses instead of paying for them twice.
        ids = self._ids_for(2)
        stored = [{"id": i, "source": "s", "reference": "r", "hypothesis": "old"
                   } for i in ids]
        self._stored_file([{"engine": "fake:one", "direction": "en->zh",
                            "chrf": 10.0, "bleu": 5.0, "latency_mean_s": 1.0,
                            "samples": stored}])
        calls = []
        record = self._collect([self._fake_engine(calls, "fake:one")], extra=["--resume"])
        self.assertEqual(calls, [], "resume must not re-request stored samples")
        run = [r for r in record["runs"] if r["engine"] == "fake:one"][0]
        self.assertEqual([s["hypothesis"] for s in run["samples"]], ["old", "old"])

    def test_resuming_one_engine_keeps_another_engines_evidence(self):
        # A rewrite that rebuilds runs only for this invocation would silently
        # delete the scores of every engine not being collected right now.
        ids = self._ids_for(2)
        stored = [{"id": i, "source": "s", "reference": "r", "hypothesis": "old"
                   } for i in ids]
        self._stored_file([
            {"engine": "fake:one", "direction": "en->zh", "chrf": 10.0,
             "bleu": 5.0, "latency_mean_s": 1.0, "samples": stored},
            {"engine": "qwen:qwen3:8b", "direction": "en->zh", "chrf": 37.1,
             "bleu": 42.7, "comet": 0.888, "latency_mean_s": 3.2, "samples": stored},
        ])
        calls = []
        record = self._collect([self._fake_engine(calls, "fake:one")], extra=["--resume"])
        engines = sorted({r["engine"] for r in record["runs"]})
        self.assertIn("qwen:qwen3:8b", engines,
                      "resume dropped another engine's collected evidence")
        qwen = [r for r in record["runs"] if r["engine"] == "qwen:qwen3:8b"][0]
        self.assertEqual(qwen.get("comet"), 0.888, "its COMET score must survive")

    def test_resuming_onto_a_different_sample_set_refuses_to_overwrite(self):
        # The stored file was collected over another sample: reusing it would be
        # incomparable, but quietly replacing it would destroy hours of evidence.
        # The honest third option is to stop and say so.
        prior = json.dumps({"sample_ids": [7, 8, 9], "seed": 42,
                            "runs": [{"engine": "qwen:qwen3:8b", "direction": "en->zh",
                                      "chrf": 37.1, "bleu": 42.7,
                                      "samples": [{"id": 7, "source": "a",
                                                   "reference": "b", "hypothesis": "c"}]}]})
        self.evidence.write_text(prior, encoding="utf-8")
        calls = []
        import bench_flores
        original = bench_flores.build_engines
        bench_flores.build_engines = lambda args: [self._fake_engine(calls, "fake:one")]
        self.addCleanup(setattr, bench_flores, "build_engines", original)
        argv = ["--flores-root", str(self.flores), "--engines", "fake", "--n", "2",
                "--pairs", "en-zh", "--resume", "--out", str(self.evidence)]
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = bench_flores.main(argv)
        self.assertEqual(code, 1, "must refuse rather than replace incomparable evidence")
        self.assertIn("--out", err.getvalue())
        self.assertEqual(self.evidence.read_text(encoding="utf-8"), prior,
                         "the stored file must be left byte-identical")
        self.assertEqual(calls, [], "the refusal must happen before any request")

    def test_the_summary_tolerates_a_direction_without_latency(self):
        # A resumed direction keeps its chrF but has no latency of its own;
        # averaging None used to raise TypeError and lose the whole report.
        import bench_flores
        record = {"runs": [
            {"engine": "e", "direction": "a->b", "chrf": 10.0, "bleu": 5.0,
             "latency_mean_s": 1.0, "samples": []},
            {"engine": "e", "direction": "b->a", "chrf": 20.0, "bleu": 6.0,
             "latency_mean_s": None, "samples": []},
        ]}
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            bench_flores._print_overall(record)
        self.assertIn("| e | 2 | 15.0 | 5.5 | 1.00s |", printed.getvalue())


    def test_a_share_violation_on_replace_is_retried(self):
        # Windows scanners can hold the destination for a moment; a long run
        # replaces the same file thousands of times and used to die on it.
        import bench_translation
        target = self.tmp / "retry.json"
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "simulated sharing violation")
            return real_replace(src, dst)

        original = bench_translation.os.replace
        bench_translation.os.replace = flaky
        self.addCleanup(setattr, bench_translation.os, "replace", original)
        bench_translation.write_json(target, {"runs": []})
        self.assertEqual(calls["n"], 3)
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
