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
import os
import pathlib
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


if __name__ == "__main__":
    unittest.main()
