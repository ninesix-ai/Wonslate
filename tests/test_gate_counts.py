# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for script/gate_counts.py, the tool that turns a gate log into quotable numbers.

Why this is a repo tool and not session scratch: every ledger round quotes Rust / Python / .NET
figures parsed out of `build.py --test` output, and the parsing lived in a throwaway file. That
made two documented failures reproducible at any time:

  * a host effect (PowerShell `>` writing UTF-16LE; the .NET CLI localising its summary on a
    Chinese machine) made a pattern match zero, and "matched zero" looks exactly like "that layer
    did not run" -- one round published numbers from a log the counter could not actually read;
  * the three gate scripts print no `Ran N` line, so the ledger convention counts each as one
    case. Counting occurrences instead of names inflated the Python total whenever a test calls a
    gate in process (measured: 4 PASS lines, 3 names, 389 vs a true 388).

Both are the same class as D51: a tool whose failure mode resembles success. So every case below
feeds the parser a log shape and asserts what it must NOTICE, and the fixtures are copied from a
real gate log's line shapes rather than invented -- a parser tested against an imagined format
passes while the actual log breaks it.

Run:
    python tests/test_gate_counts.py
"""
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "script"))

import gate_counts  # noqa: E402

# Line shapes taken from a real `build.py --test` log (197 lines of rust/python/dotnet output),
# reduced to one instance per shape. Nothing here is invented, including the odd spacing in the
# dotnet summary, which is what the CLI actually emits in English.
FULL_LOG = """
   Compiling translator-engine v0.1.0
test result: ok. 199 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 2.01s
test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
  -- test_phase1_ffi.py --
passed 46 / failed 0 / skipped 0 / total 46
  -- test_bench_domain_av.py --
Ran 43 tests in 1.21s

OK
  -- test_run_av_baseline.py --
Ran 44 tests in 0.98s

OK
  -- test_glossary_hygiene.py --
glossary hygiene gate: PASS (672 rows, 12 file(s), shipped seed packs)
  -- test_glossary_hygiene_rules.py --
glossary hygiene gate: PASS (672 rows, 12 file(s), shipped seed packs)
Ran 16 tests in 0.44s

OK
  -- test_component_licenses.py --
component license gate: PASS
  -- test_source_lint.py --
source lint: PASS
Passed!  - Failed:     0, Passed:   188, Skipped:     0, Total:   188, Duration: 1 s - Wonslate.UI.Tests.dll
ALL DONE, tests PASSED
"""


def without_line_starting(text, prefix):
    """Drop the lines starting with `prefix` -- removing a layer's only evidence for it.

    Written as a helper because the first version of these cases substituted part of the line
    and left the matched shape behind, which made "layer missing" tests pass against a log that
    still contained the layer. Deleting whole lines cannot do that.
    """
    return "\n".join(l for l in text.split("\n") if not l.strip().startswith(prefix)) + "\n"


class AttributionTests(unittest.TestCase):
    """The numbers must decompose, or they are not evidence."""

    def test_every_layer_is_attributed_from_one_log(self):
        s = gate_counts.parse(FULL_LOG)
        self.assertEqual([199, 8], s["rust"])
        self.assertEqual(207, s["layers"]["rust"])
        # 46 checker + 43 + 44 + 16 unittest = 149, plus the three gate scripts at one each
        self.assertEqual(149, sum(n for _name, _kind, n in s["suites"]))
        self.assertEqual(3, len(s["gate_names"]))
        self.assertEqual(152, s["layers"]["python"])
        self.assertEqual(188, s["layers"]["dotnet"])
        self.assertEqual(207 + 152 + 188, s["total"])

    def test_a_suite_is_attributed_to_the_script_that_produced_it(self):
        # A ledger line saying "+16 python" has to be traceable to a file, and a total that
        # cannot be decomposed is how an inflated number survives a review.
        s = gate_counts.parse(FULL_LOG)
        by_name = {name: (kind, n) for name, kind, n in s["suites"]}
        self.assertEqual(("unittest", 44), by_name["test_run_av_baseline.py"])
        self.assertEqual(("checker", 46), by_name["test_phase1_ffi.py"])

    def test_missing_rust_output_is_a_gap_and_not_a_zero(self):
        s = gate_counts.parse(without_line_starting(FULL_LOG, "test result: ok."))
        self.assertIn("rust", s["missing"])

    def test_missing_dotnet_summary_is_reported_as_missing(self):
        s = gate_counts.parse(without_line_starting(FULL_LOG, "Passed!"))
        self.assertIn("dotnet", s["missing"])


class GateAccountingTests(unittest.TestCase):
    """The three gate scripts count as one case each -- by name, never by occurrence."""

    def test_a_gate_called_in_process_by_a_test_does_not_inflate_the_python_total(self):
        # Measured shape: test_glossary_hygiene_rules.py prints the hygiene gate's own PASS line
        # because one of its cases calls the gate. The rules suite is already counted by its
        # `Ran 16`, so the gate adds nothing here. Counting occurrences instead gave 389 against a
        # true 388 -- a number that could not be attributed to any test.
        s = gate_counts.parse(FULL_LOG)
        self.assertEqual(4, s["gate_lines"])
        self.assertEqual(3, s["gate_names"].count("glossary hygiene gate")
                         + s["gate_names"].count("component license gate")
                         + s["gate_names"].count("source lint"))
        self.assertNotIn("test_glossary_hygiene_rules.py", s["gate_only_suites"])
        self.assertIn("test_glossary_hygiene.py", s["gate_only_suites"])
        self.assertEqual(152, s["layers"]["python"])

    def test_the_same_log_without_the_extra_call_still_totals_the_same(self):
        # The invariant the two cases above are really about: an extra in-process call must not
        # move the figure. Written as a pair comparison so it cannot be satisfied by a constant.
        with_extra = gate_counts.parse(FULL_LOG)
        without = FULL_LOG.replace(
            "  -- test_glossary_hygiene_rules.py --\nglossary hygiene gate: PASS (672 rows, "
            "12 file(s), shipped seed packs)\n", "  -- test_glossary_hygiene_rules.py --\n")
        self.assertEqual(with_extra["layers"]["python"],
                         gate_counts.parse(without)["layers"]["python"])


class HostEffectTests(unittest.TestCase):
    """Two host behaviours have already produced published-but-unreadable numbers."""

    def test_a_bom_marked_utf16_log_still_yields_the_suites(self):
        # What PowerShell's `>` actually emits: UTF-16LE carrying a BOM. The BOM has to be
        # consumed by the decoder, not left at the head of the text -- a BOM glued to the first
        # line makes an anchored pattern miss that line, which is how the first suite of a log
        # silently loses its attribution. The payload is therefore placed first here; the
        # BOM-less case below exercises the other detection.
        payload = "-- test_first.py --\nRan 1 tests in 0.01s\n\nOK\n"
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "gate.log"
            path.write_bytes((payload + FULL_LOG).encode("utf-16"))   # writes the BOM
            self.assertEqual(b"\xff\xfe", path.read_bytes()[:2])
            text = gate_counts.read_log(path)
        self.assertFalse(text.startswith("\ufeff"), "the BOM survived into the text")
        s = gate_counts.parse(text)
        self.assertIn("test_first.py", {name for name, _k, _n in s["suites"]})
        self.assertEqual(153, s["layers"]["python"])
        self.assertEqual([], s["missing"])

    def test_a_bomless_utf16_log_is_caught_by_the_nul_heuristic(self):
        # PowerShell's `>` writes UTF-16LE. Read as UTF-8 that is interleaved NULs, every pattern
        # misses, and the report looks like a run where nothing ran -- indistinguishable from a
        # real gap unless the reader checks the encoding.
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "gate.log"
            path.write_bytes(FULL_LOG.encode("utf-16-le"))          # no BOM
            text = gate_counts.read_log(path)
        s = gate_counts.parse(text)
        self.assertIn("Ran 44 tests", text)
        self.assertNotIn("dotnet", s["missing"])
        self.assertEqual(152, s["layers"]["python"])

    def test_a_localised_dotnet_summary_is_read_as_missing_not_as_zero(self):
        # The .NET CLI localises this line on a Chinese host. A pattern written against English
        # matches nothing there, and "nothing" must surface as a coverage gap rather than as a
        # layer of zero tests, which would silently drop .NET from the published total.
        localised = FULL_LOG.replace(
            "Passed!  - Failed:     0, Passed:   188, Skipped:     0, Total:   188",
            "已通过! - 失败:     0，通过:   188，已跳过:     0，总计:   188")
        s = gate_counts.parse(localised)
        self.assertIn("dotnet", s["missing"])

    def test_an_absent_verdict_line_is_missing_even_when_counts_are_present(self):
        # The log of a run that was truncated, or that died mid-phase, still contains per-layer
        # numbers. Without the closing verdict those numbers describe an unfinished run.
        s = gate_counts.parse(FULL_LOG.replace("ALL DONE, tests PASSED\n", ""))
        self.assertIn("verdict", s["missing"])

    def test_a_failed_phase_is_reported_even_with_full_counts(self):
        s = gate_counts.parse(FULL_LOG.replace("ALL DONE, tests PASSED", "1 phase(s) FAILED"))
        self.assertTrue(s["failed_phase"])
        self.assertIn("verdict", s["missing"])


class EmptyLogTests(unittest.TestCase):
    """A parser that matches nothing must say so rather than report a clean zero."""

    def test_an_unparseable_log_reports_every_layer_missing(self):
        s = gate_counts.parse("some unrelated transcript\nwith no test output\n")
        for layer in ("rust", "python-unittest", "dotnet", "verdict"):
            self.assertIn(layer, s["missing"], layer)
        self.assertEqual(0, s["total"])

    def test_the_verdict_is_not_inferred_from_a_zero_failure_count(self):
        # "Failed: 0" is not the same claim as "the gate ran and passed": the summary line can be
        # present while the verdict line never was, and reading the first as the second is what
        # lets a truncated log look green.
        s = gate_counts.parse("Passed!  - Failed:     0, Passed:   188, Skipped:     0, Total:   188\n")
        self.assertIn("verdict", s["missing"])
        self.assertFalse(s["done"])


class RunPlanTests(unittest.TestCase):
    """`run` must not inherit the machine's output language, nor reuse yesterday's log name."""

    def test_the_run_pins_the_dotnet_ui_language(self):
        plan = gate_counts.plan_run(pathlib.Path("gate-x.log"))
        self.assertEqual("en", plan["env"]["DOTNET_CLI_UI_LANGUAGE"])

    def test_the_run_does_not_override_the_python_io_encoding(self):
        # Setting only one side of the pair garbles the suites' output for build.py, which reads
        # it with the host's own decoding. Measured the hard way: the counter came back empty.
        plan = gate_counts.plan_run(pathlib.Path("gate-x.log"))
        self.assertNotIn("PYTHONIOENCODING", plan["env"])

    def test_the_run_invokes_the_authoritative_entry_point(self):
        # AGENTS.md trap 1: a bare `dotnet test` fails three cases in a fresh worktree for reasons
        # unrelated to the change. Only build.py --test has a verdict worth quoting.
        plan = gate_counts.plan_run(pathlib.Path("gate-x.log"))
        self.assertEqual(["--test"], plan["argv"][-1:])
        self.assertTrue(plan["argv"][1].endswith("build.py"), plan["argv"])

    def test_a_log_name_that_already_exists_is_not_overwritten(self):
        # D51 in the tool's own output: a fixed log path means the next round silently destroys
        # the evidence behind the previous round's published figure. It happened to this
        # session's wrapper, which is why the name is now required and collision is an error.
        with tempfile.TemporaryDirectory() as d:
            taken = pathlib.Path(d) / "gate-2026-10-14t0900.log"
            taken.write_text("previous round's evidence", encoding="utf-8")
            with self.assertRaises(gate_counts.LogNameOccupied):
                gate_counts.plan_run(taken)
            fresh = gate_counts.plan_run(pathlib.Path(d) / "gate-new.log")
            self.assertEqual("gate-new.log", fresh["log"].name)

    def test_a_default_log_name_carries_a_timestamp(self):
        # No explicit name must still mean no silent replacement: the default has to be unique.
        plan = gate_counts.plan_run(None)
        self.assertRegex(plan["log"].name, r"^gate-\d{4}-\d{2}-\d{2}T\d{4}\.log$")


class SharedRuleTests(unittest.TestCase):
    """Local and CI figures must be produced by one rule, or their difference is meaningless."""

    def test_the_ci_parser_uses_the_same_totals_as_the_local_one(self):
        # The gap quoted in the ledger (28 for several rounds running) is only a fact if both sides
        # counted the same way. One function for both, so a rule change moves both at once.
        self.assertIs(gate_counts.totals_for_ci, gate_counts.totals_for_local)

    def test_a_ci_log_with_a_repeated_gate_line_counts_it_once(self):
        ci = ("test result: ok. 305 passed; 0 failed\n"
              "Ran 16 tests in 0.4s\nOK\n"
              "glossary hygiene gate: PASS (672 rows)\n"
              "glossary hygiene gate: PASS (672 rows)\n"
              "component license gate: PASS\nsource lint: PASS\n"
              "Passed!  - Failed:     0, Passed:   188, Skipped:     0, Total:   188\n"
              "ALL DONE, tests PASSED\n")
        s = gate_counts.parse_ci_log(ci)
        # Four PASS lines in this log: the hygiene gate twice, licence and lint once each.
        self.assertEqual(4, s["gate_lines"])
        self.assertEqual(1, s["gate_names"].count("glossary hygiene gate"))
        self.assertEqual(3, len(s["gate_names"]))
        self.assertEqual(16 + 3, s["layers"]["python"])

    def test_the_ci_parser_refuses_to_report_nothing(self):
        # Same defence as D59's vacuous-compare case: an empty result set is a failure to measure,
        # not a measurement of zero.
        s = gate_counts.parse_ci_log("##[group]Run actions/checkout@v4\n")
        self.assertTrue(s["missing"])


class CILogShapeTests(unittest.TestCase):
    """GitHub's log format, taken from a live run rather than imagined.

    Every line here is a real shape captured from run 38499436512: `<job>\t<step>\t<timestamp>Z
    <text>`, a BOM sitting INSIDE the prefix of a job's first line, three separate rust legs, a
    job whose log is simply not uploaded, and no `ALL DONE` line anywhere -- that verdict belongs
    to build.py, while CI states it through job conclusions. Feeding such a log to the anchored
    parser without stripping is the vacuous case: it matches nothing and reports an empty run.
    """

    JOB = "Python FFI smoke"
    STEP = "Python smoke (FFI, sidecar e2e + unit, benchmark path + lint, hook guards)"

    def prefixed(self, step, text, stamp="2026-10-14T02:09:19.8501895Z"):
        return "%s\t%s\t%s %s" % (self.JOB, step, stamp, text)

    def test_the_anchored_parser_sees_nothing_in_a_raw_github_log(self):
        # Not a style complaint about the prefix: without stripping, every count comes back zero
        # and a zero looks like a layer nobody ran. This case is what makes the stripper
        # load-bearing rather than decorative.
        raw = "\n".join([
            self.prefixed(self.STEP, "Ran 111 tests in 41.803s"),
            self.prefixed(self.STEP, "  passed 46 / failed 0 / skipped 0 / total 46"),
            self.prefixed(self.STEP, "glossary hygiene gate: PASS (672 rows, 12 file(s))"),
            ".NET WPF (build + test)\tRun .NET tests\t2026-10-14T02:10:21.3070645Z Passed!  "
            "- Failed:     0, Passed:   188, Skipped:     0, Total:   188, Duration: 1 s",
        ])
        self.assertIn("Ran 111 tests", raw)                    # the text is there...
        self.assertEqual([], gate_counts.parse(raw)["suites"])  # ...but the parser cannot see it

    def test_a_prefixed_log_yields_the_same_counts_as_the_plain_one(self):
        raw = "\n".join([
            self.prefixed(self.STEP, "Ran 111 tests in 41.803s"),
            self.prefixed(self.STEP, "  passed 46 / failed 0 / skipped 0 / total 46"),
            self.prefixed(self.STEP, "glossary hygiene gate: PASS (672 rows, 12 file(s))"),
            self.prefixed(self.STEP, "component license gate: PASS"),
            self.prefixed(self.STEP, "source lint: PASS"),
        ])
        s = gate_counts.parse_ci_log(raw)
        self.assertEqual(111, s["layers"]["python-unittest"])
        self.assertEqual(46, s["layers"]["python-checker"])
        self.assertEqual(3, s["layers"]["python-gate"])
        self.assertEqual(160, s["layers"]["python"])

    def test_a_bom_inside_the_prefix_does_not_reach_the_text(self):
        # A real job's first line carries it between the step field and the timestamp. Left in
        # place it glues itself to the first record, which is exactly how an anchored pattern
        # silently misses.
        line = "Python FFI smoke\tSet up job\t\ufeff2026-10-14T02:08:11.4468866Z Ran 3 tests"
        s = gate_counts.parse_ci_log(line)
        self.assertEqual(3, s["layers"]["python-unittest"])

    def test_three_rust_legs_are_not_summed_into_the_total(self):
        # The ledger's CI figure is one rust leg plus python plus dotnet: the legs are the same
        # suite on three platforms, so adding them would invent a layer twice over.
        jobs = [
            {"name": "Rust engine (ubuntu-latest)", "conclusion": "success",
             "log": "test result: ok. 305 passed; 0 failed"},
            {"name": "Rust engine (macos-latest)", "conclusion": "success",
             "log": "test result: ok. 305 passed; 0 failed"},
            {"name": "Rust engine (windows-latest)", "conclusion": "success",
             "log": "test result: ok. 305 passed; 0 failed"},
            {"name": self.JOB, "conclusion": "success",
             "log": self.prefixed(self.STEP, "Ran 322 tests in 41.8s")},
            {"name": ".NET WPF (build + test)", "conclusion": "success",
             "log": "Passed!  - Failed:     0, Passed:   188, Skipped:     0, Total:   188"},
        ]
        r = gate_counts.summarize_ci(jobs)
        self.assertEqual(305, r["layers"]["rust"])         # one leg, not three
        # This fixture's python job prints a single `Ran` line and no gate verdict, so its
        # layer is 322 and nothing is added for gates -- written down because 325 was the
        # first figure that came to mind, and it was the sum of two different logs.
        self.assertEqual(322, r["layers"]["python"])
        self.assertEqual(305 + 322 + 188, r["total"])
        self.assertEqual(3, len(r["rust_legs"]))

    def test_disagreeing_rust_legs_are_a_finding_not_an_average(self):
        jobs = [
            {"name": "Rust engine (ubuntu-latest)", "conclusion": "success",
             "log": "test result: ok. 305 passed; 0 failed"},
            {"name": "Rust engine (macos-latest)", "conclusion": "success",
             "log": "test result: ok. 304 passed; 0 failed"},
            {"name": self.JOB, "conclusion": "success", "log": "Ran 1 tests in 0.1s"},
            {"name": ".NET WPF (build + test)", "conclusion": "success",
             "log": "Passed!  - Failed: 0, Passed: 1, Skipped: 0, Total: 1"},
        ]
        r = gate_counts.summarize_ci(jobs)
        self.assertIn("rust legs disagree", ", ".join(r["missing"]))

    def test_a_job_whose_log_was_never_uploaded_is_reported_not_dropped(self):
        # Measured: `.NET WPF tests` (job 115643256012) has no log on the run this fixture came
        # from, and `gh run view --log` for the whole run then fails outright -- which would read
        # as "CI reported nothing" unless the unreadable job is named.
        r = gate_counts.summarize_ci([
            {"name": "Rust engine (ubuntu-latest)", "conclusion": "success",
             "log": "test result: ok. 305 passed"},
            {"name": self.JOB, "conclusion": "success", "log": "Ran 1 tests in 0.1s"},
            {"name": ".NET WPF tests", "conclusion": None, "log": None},
        ])
        self.assertIn(".NET WPF tests", ", ".join(r["unreadable"]))
        self.assertIn("dotnet", ", ".join(r["missing"]))

    def test_the_ci_verdict_comes_from_job_conclusions(self):
        # No CI log contains build.py's ALL DONE line, so verdict must not be inferred from
        # counts -- a red job still prints every per-layer number it got before failing.
        ok = [{"name": "Rust engine (ubuntu-latest)", "conclusion": "success",
               "log": "test result: ok. 2 passed"},
              {"name": self.JOB, "conclusion": "success", "log": "Ran 1 tests in 0.1s"},
              {"name": ".NET WPF (build + test)", "conclusion": "success",
               "log": "Passed!  - Failed: 0, Passed: 1, Skipped: 0, Total: 1"}]
        self.assertEqual([], [m for m in gate_counts.summarize_ci(ok)["missing"]
                              if m.startswith("verdict")])
        red = [dict(j) for j in ok]
        red[1]["conclusion"] = "failure"
        self.assertIn("verdict", ", ".join(gate_counts.summarize_ci(red)["missing"]))

    def test_unmeasured_jobs_do_not_silently_become_a_zero_layer(self):
        # The coverage job re-runs one rust target a second way and the license gate job audits
        # manifests; counting either as a test layer would move the total without any new case.
        # Both spellings of the coverage job are checked: today's name does not contain
        # `rust engine` and is skipped by accident, so only the renamed shape proves the
        # exclusion is placed where it can do its job.
        self.assertIsNone(gate_counts.classify_job("Rust coverage baseline"))
        self.assertIsNone(gate_counts.classify_job("Rust engine coverage baseline"))
        self.assertIsNone(gate_counts.classify_job("License compliance gate"))
        r = gate_counts.summarize_ci([
            {"name": "Rust coverage baseline", "conclusion": "success",
             "log": "test result: ok. 305 passed"},
            {"name": "Rust engine coverage baseline", "conclusion": "success",
             "log": "test result: ok. 305 passed"},
            {"name": "License compliance gate", "conclusion": "success", "log": "ok"},
            {"name": self.JOB, "conclusion": "success", "log": "Ran 1 tests in 0.1s"},
            {"name": ".NET WPF (build + test)", "conclusion": "success",
             "log": "Passed!  - Failed: 0, Passed: 1, Skipped: 0, Total: 1"},
        ])
        self.assertEqual(0, r["layers"]["rust"])
        self.assertIn("rust", ", ".join(r["missing"]))


class CiFetchTests(unittest.TestCase):
    """The fetch seam is injectable, so the tolerant-per-job shape is testable offline."""

    JOBS_JSON = '{"jobs": [' \
        '{"databaseId": 1, "name": "Python FFI smoke", "conclusion": "success"},' \
        '{"databaseId": 2, "name": ".NET WPF tests", "conclusion": "success"}]}'

    def fake_gh(self, fail_id):
        """Fail exactly one job's log fetch -- the shape a real run showed.

        Keyed on the job id rather than on the `--job` flag, which is present for every job and
        would make this fake fail all of them (the first version did, and the assertion below is
        what catches that: it names which log came back)."""
        def gh(args):
            if "--json" in args:
                return 0, CiFetchTests.JOBS_JSON, ""
            if fail_id in args:
                return 1, "", "log not found: %s" % fail_id
            return 0, self.prefixed_body(), ""
        return gh

    @staticmethod
    def prefixed_body():
        return "Python FFI smoke\tRun tests\t2026-10-14T02:09:19.8501895Z Ran 7 tests in 1s\n"

    def test_a_job_without_an_uploaded_log_is_kept_and_named(self):
        jobs = gate_counts.fetch_ci_jobs("42", gh=self.fake_gh("2"), repo="o/r")
        # Both jobs survive the fetch; the unreadable one (job 2) comes back with log=None so the
        # summary can say which layer it therefore could not measure. Order follows the jobs list.
        self.assertEqual([self.prefixed_body(), None], [j["log"] for j in jobs])
        r = gate_counts.summarize_ci(jobs)
        self.assertIn(".NET WPF tests", r["unreadable"])
        self.assertIn("dotnet", ", ".join(r["missing"]))

    def test_a_failed_jobs_listing_is_an_error_not_an_empty_run(self):
        def gh(args):
            return 1, "", "HTTP 401"
        with self.assertRaises(RuntimeError):
            gate_counts.fetch_ci_jobs("42", gh=gh)


class RegistrationTests(unittest.TestCase):
    """The tool exists only if something is obliged to keep it working."""

    def test_this_suite_is_registered_in_both_places(self):
        # AGENTS.md trap 2: a suite missing from build.py and ci.yml silently never runs.
        build = (REPO / "script" / "build.py").read_text(encoding="utf-8")
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("test_gate_counts.py", build)
        self.assertIn("test_gate_counts.py", ci)

    def test_the_agents_md_verification_table_points_at_the_tool(self):
        # A tool nobody is directed to gets re-invented as throwaway scratch -- which is exactly
        # the state this round is fixing.
        agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("script/gate_counts.py", agents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
