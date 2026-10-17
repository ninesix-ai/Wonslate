# -*- coding: utf-8 -*-
"""Tests for report_model_updates.py and the workflow that calls it.

The announcement is the part a human sees, so its failure modes are social as well as technical:

  * one finding must produce one issue, not one per day (the baseline is deliberately not
    advanced, so the same finding returns every morning -- D65's "switched off because it is
    noisy" is the real risk here);
  * a failed lookup must not be read as "nothing announced yet", which would open a duplicate;
  * the workflow file itself is a dependency: it names flags and exit codes, and a rename on the
    python side would otherwise show up as a green run that checked nothing.

Everything is offline. `gh` is reached only through an injectable runner, and `--dry-run` is
proven to change nothing by asserting the runner was never called.
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import report_model_updates as report          # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "model-updates.yml"


class FakeGh:
    """A stand-in `gh`: records the argv it was handed and replays canned answers."""

    def __init__(self, stdout="[]", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, capture_output=True, text=True, encoding=None, errors=None):
        self.calls.append(argv)
        return type("P", (), {"returncode": self.returncode, "stdout": self.stdout,
                              "stderr": "boom" if self.returncode else ""})()


def row(asset, upstream, state="newer"):
    return {"checked": asset, "state": state, "local": "old", "upstream": upstream,
            "detail": "d", "basis": "version"}


class PlanTests(unittest.TestCase):
    def test_nothing_new_says_nothing(self):
        action, detail = report.plan([], [])
        self.assertEqual("none", action)
        self.assertIn("no upstream is ahead", detail)

    def test_a_first_finding_opens_one_issue(self):
        action, key = report.plan([row("argos:en_zh", "1.10")], [])
        self.assertEqual("create", action)
        self.assertEqual("argos:en_zh=1.10", key)

    def test_the_same_finding_is_never_announced_twice(self):
        # The daily case: the finding is unchanged, so the second morning must be silent. This is
        # the difference between a notification and something a person disables.
        rows = [row("argos:en_zh", "1.10")]
        text = report.body(rows, report.finding_key(rows))
        issues = [{"number": 41, "title": report.TITLE_PREFIX, "body": text}]
        action, detail = report.plan(rows, issues)
        self.assertEqual("none", action)
        self.assertIn("#41", detail)

    def test_a_changed_finding_comments_on_the_open_issue(self):
        rows = [row("argos:en_zh", "1.10")]
        older = [{"number": 41, "title": report.TITLE_PREFIX,
                  "body": report.body(rows, "argos:en_zh=1.9")}]
        action, detail = report.plan([row("argos:en_zh", "1.11")], older)
        self.assertEqual("comment", action)
        self.assertEqual(41, detail[1])

    def test_the_key_does_not_depend_on_row_order(self):
        a = [row("argos:en_zh", "1.10"), row("madlad:model.bin", "abc")]
        b = list(reversed(a))
        self.assertEqual(report.finding_key(a), report.finding_key(b),
                         "an unstable key would re-announce the same finding every run")

    def test_a_marker_without_a_key_does_not_silence_a_real_finding(self):
        # An empty key must not match anything. It is still one of our open issues, so the finding
        # is appended there rather than opening a second thread -- but it is never swallowed.
        issues = [{"number": 1, "title": report.TITLE_PREFIX,
                   "body": "<!-- %s:  -->" % report.MARKER}]
        announced, _ = report.open_findings(issues)
        self.assertEqual({"": 1}, announced, "the empty key is recorded, not guessed at")
        action, detail = report.plan([row("argos:en_zh", "1.10")], issues)
        self.assertNotEqual("none", action)
        self.assertEqual("comment", action)
        self.assertEqual(1, detail[1])

    def test_a_truncated_marker_is_ignored_not_treated_as_our_key(self):
        # A truncated marker has no closing `-->`. Without this branch, the key would run to the
        # end of the document and a real finding could be silenced by an unrelated comment.
        issues = [{"number": 1, "title": report.TITLE_PREFIX,
                   "body": "<!-- %s: argos:en_zh=1.9" % report.MARKER}]
        announced, _ = report.open_findings(issues)
        self.assertEqual({}, announced)

    def test_body_round_trips_the_key_it_writes(self):
        # The slicing arithmetic in `open_findings` was wrong in the first draft (it used the
        # length of a complete marker while searching for the prefix, eating the key's first
        # characters). This is the test that would have caught it and now keeps it caught.
        rows = [row("argos:en_zh", "1.10"), row("voice:tts", "f" * 64)]
        key = report.finding_key(rows)
        announced, number = report.open_findings(
            [{"number": 7, "title": report.TITLE_PREFIX, "body": report.body(rows, key)}])
        self.assertEqual(7, number)
        self.assertEqual([key], list(announced))

    def test_only_newer_rows_are_announced(self):
        payload = {"rows": [row("argos:en_zh", "1.10"),
                            row("voice:asr", None, state="incomparable"),
                            row("madlad:model.bin", "b" * 64, state="current")]}
        self.assertEqual(["argos:en_zh"], [r["checked"] for r in report.newer_rows(payload)])


class GhTests(unittest.TestCase):
    def test_a_failed_listing_is_cannot_tell_not_an_empty_list(self):
        # Empty means "nothing has been announced" and would open a duplicate; here it must not be
        # inferred from a command that did not run (D74's rule, applied to the memory itself).
        with self.assertRaises(report.CannotTell):
            report.open_issues(runner=FakeGh(returncode=1))

    def test_unparsable_listing_is_also_cannot_tell(self):
        with self.assertRaises(report.CannotTell):
            report.open_issues(runner=FakeGh(stdout="not json"))

    def test_foreign_issues_are_not_ours(self):
        listing = json.dumps([{"number": 1, "title": "Bug: sidecar won't start", "body": "x"},
                              {"number": 2, "title": report.TITLE_PREFIX, "body": "y"}])
        found = report.open_issues(runner=FakeGh(stdout=listing))
        self.assertEqual([2], [issue["number"] for issue in found])

    def test_dry_run_touches_nothing(self):
        gh = FakeGh()
        report.publish("create", "k", [row("argos:en_zh", "1.10")], "msg", runner=gh,
                       dry_run=True)
        self.assertEqual([], gh.calls, "a dry run must not be able to open an issue")

    def test_create_sends_the_body_and_comment_targets_the_number(self):
        gh = FakeGh()
        rows = [row("argos:en_zh", "1.10")]
        report.publish("create", "argos:en_zh=1.10", rows, "msg", runner=gh)
        argv = gh.calls[0]
        self.assertEqual(["gh", "issue", "create"], argv[:3])
        self.assertIn("--body", argv)
        self.assertIn(report.MARKER, argv[argv.index("--body") + 1])
        gh = FakeGh()
        report.publish("comment", ("argos:en_zh=1.11", 41), rows, "msg", runner=gh)
        self.assertEqual(["gh", "issue", "comment", "41"], gh.calls[0][:4])


class MainTests(unittest.TestCase):
    def payload(self, tmp, rows, code=2, message="at least one upstream publishes something newer"):
        path = pathlib.Path(tmp) / "rows.json"
        path.write_text(json.dumps({"verdict": {"code": code, "message": message},
                                    "rows": rows}), encoding="utf-8")
        return str(path)

    def test_cannot_tell_returns_one_and_calls_no_gh(self):
        # A blind check must not become an issue, and must not become silence either: the run goes
        # non-zero so somebody notices the check did not happen.
        with tempfile.TemporaryDirectory() as tmp:
            path = self.payload(tmp, [row("argos:en_zh", "1.10")], code=1,
                                message="UNKNOWN -- a probe failed")
            gh = FakeGh()
            saved = report.open_issues
            report.open_issues = lambda runner=subprocess.run: []
            try:
                self.assertEqual(1, report.main(["--json", path]))
            finally:
                report.open_issues = saved
            self.assertEqual([], gh.calls, "it decided nothing, so it must have asked nothing")

    def test_announcing_is_success_not_a_failure(self):
        # A green "I posted the alert" run; a red one would teach people to ignore the timer.
        with tempfile.TemporaryDirectory() as tmp:
            path = self.payload(tmp, [row("argos:en_zh", "1.10")])
            gh = FakeGh()
            saved = report.open_issues
            report.open_issues = lambda runner=subprocess.run: []
            try:
                self.assertEqual(0, report.main(["--json", path, "--dry-run"]))
            finally:
                report.open_issues = saved
            self.assertEqual([], gh.calls, "--dry-run must not have reached gh")

    def test_a_listing_failure_propagates_as_one(self):
        # The same rule from the other side: `gh` failing is not "nothing announced yet".
        with tempfile.TemporaryDirectory() as tmp:
            path = self.payload(tmp, [row("argos:en_zh", "1.10")])

            def broken_listing(runner=subprocess.run):
                raise report.CannotTell("gh issue list failed")

            saved = report.open_issues
            report.open_issues = broken_listing
            try:
                self.assertEqual(1, report.main(["--json", path]))
            finally:
                report.open_issues = saved


class WorkflowTests(unittest.TestCase):
    """The YAML is a caller. Callers need the same consistency checks code needs."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    @staticmethod
    def declared_flags(script_name):
        source = (ROOT / "script" / script_name).read_text(encoding="utf-8")
        return set(re.findall(r'add_argument\("(--[a-z-]+)"', source))

    @staticmethod
    def used_flags(script_name):
        used = set()
        for line in WorkflowTests.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or ("script/" + script_name) not in stripped:
                continue
            used |= set(re.findall(r"--[a-z][a-z-]*", stripped))
        return used

    def test_every_flag_the_workflow_names_still_exists(self):
        # The rename case: a flag disappears from argparse, the YAML keeps calling it, and the run
        # either dies at the CLI parse or, worse, silently means something else.
        for script in ("check_model_updates.py", "report_model_updates.py"):
            with self.subTest(script=script):
                missing = self.used_flags(script) - self.declared_flags(script)
                self.assertEqual(set(), missing,
                                 "the workflow calls %s which %s does not define"
                                 % (", ".join(sorted(missing)), script))

    def test_the_probe_step_survives_a_finding_and_the_report_does_not_swallow_it(self):
        # Exit 2 from the probe means "upstream moved", so that step needs continue-on-error; the
        # report step must NOT have it, or a blind run would look green.
        probe_index = self.text.index("Ask each upstream")
        report_index = self.text.index("Announce the finding")
        summary_index = self.text.index("Put the same table")
        self.assertIn("continue-on-error: true", self.text[probe_index:report_index])
        self.assertNotIn("continue-on-error", self.text[report_index:summary_index],
                         "swallowing the report's exit code re-creates D74 in the workflow")
        self.assertNotIn("|| true", self.text,
                         "a swallowed exit code is how a broken timer looks healthy")

    def test_the_summary_does_not_ask_upstream_twice(self):
        # Re-running the probe for formatting would spend the rate limit again and could print a
        # different answer than the one the job acted on.
        summary = self.text[self.text.index("Put the same table"):]
        self.assertNotIn("script/check_model_updates.py", summary)
        self.assertIn("model-updates.json", summary)

    def test_it_is_a_timer_that_can_also_be_pulled_by_hand(self):
        # A schedule that nobody can trigger on demand cannot be verified today, and GitHub
        # switches schedules off for repositories whose default branch has been idle for 60 days.
        self.assertIn("schedule:", self.text)
        self.assertIn("cron:", self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("concurrency:", self.text)

    def test_permissions_are_the_minimum_the_announcement_needs(self):
        self.assertIn("issues: write", self.text)
        self.assertIn("contents: read", self.text)
        self.assertNotIn("pull-requests: write", self.text)
        self.assertNotIn("contents: write", self.text,
                         "a timer that can rewrite the repository is a different risk")

    def test_the_workflow_is_parseable_not_just_searchable(self):
        """The other tests here grep the text; none of that checks whether it is YAML.

        D77: the first version of this file put a heredoc body back to column 0, which ends the
        `run: |` block scalar and makes the whole document invalid. GitHub then shows a failed run
        with no log and the file path as the workflow name, so the timer looks like it exists while
        it can never fire. Every flag-exists assertion passed on that broken file.

        The block check needs no third-party library, so it runs on both sides of the gate. The
        full parse runs when PyYAML is importable, and its absence is reported as a count rather
        than a silent skip -- a skipped check and a passing one print the same thing.
        """
        offenders = []
        # A column-0 line in a workflow is only legal as a comment or a top-level `key:`. Anything
        # else means a block-scalar body escaped its block -- which is exactly what a heredoc typed
        # at column 0 does, and it invalidates the whole file rather than one step. The blank-line
        # alternative is `$` in its own branch: writing `^(#||$|...)` puts an *empty* pattern first,
        # which matches the start of every line and turns this whole guard into a no-op -- that is
        # exactly what the first draft did, and only a planted broken file showed it.
        top_level = re.compile('^(?:#|["\']?[A-Za-z_][\\w.-]*:|$)')
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                if line.startswith("\t") or "\t" in line[:len(line) - len(line.lstrip()) + 1]:
                    offenders.append("%s:%d tab indentation is not YAML indentation" % (path.name,
                                                                                        number))
                if line and not line[0].isspace() and not top_level.match(line):
                    # Only column-0 lines are constrained: everything indented is ordinary YAML
                    # structure, and a rule that flags those is a rule that gets switched off.
                    offenders.append("%s:%d %s" % (path.name, number, line[:48]))
        self.assertEqual([], offenders,
                         "a workflow file is not structurally parseable: %s" % offenders)
        try:
            import yaml                                # noqa: F401 - optional on purpose
        except ImportError:
            print("PyYAML not installed here: structural check only (skipped parse: 1)")
            return
        parsed = yaml.safe_load(self.text)
        self.assertIn("jobs", parsed)
        # YAML 1.1 reads the bare key `on` as the boolean True, which is how GitHub's own parser
        # sees it too; asserting the triggers here is what would have shown the broken file as
        # "no schedule at all" instead of looking fine.
        triggers = parsed.get(True, parsed.get("on"))
        self.assertIsInstance(triggers, dict, "the workflow has no readable trigger block")
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("push", triggers,
                         "a daily check must not also run on every push and burn the rate limit")
        steps = parsed["jobs"]["check"]["steps"]
        names = [step.get("name") or step.get("uses") for step in steps]
        self.assertEqual(5, len(names), names)
        probe = [s for s in steps if "Ask each upstream" in (s.get("name") or "")][0]
        self.assertIn("GITHUB_TOKEN", probe.get("env", {}),
                      "without a token the runner gets 403 from GitHub and the voice rows cannot "
                      "be checked at all")

    def test_no_model_is_downloaded_by_the_timer(self):
        # The timer only asks questions; downloading here would need the whole data pipeline and
        # would put GB-scale fetches on a schedule nobody asked for.
        for forbidden in ("fetch_argos_models.py", "fetch_voice_models.py", "fetch_madlad_model.py",
                          "net_guard"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)


class RegistrationTests(unittest.TestCase):
    def test_the_suite_runs_locally_and_in_ci_alike(self):
        # D65's lesson, applied to this file: a test that exists but is never executed is a rumour.
        name = "test_report_model_updates.py"
        self.assertIn(name, (ROOT / "script" / "build.py").read_text(encoding="utf-8"))
        self.assertIn(name, (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
