# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Run script/check_artifact_syntax.py, and check that it checked something.

D77 is the reason this file exists: a workflow whose YAML was invalid shipped as "delivered", and
every assertion in the repository that pointed at it passed, because they all grepped the text and
none parsed it. So the suite that names the gate also runs it, and the gate has to prove it opened
files rather than matched nothing (D65: registered-but-never-run is the same failure in a
different coat).

Two extra rules this file keeps, because both were paid for in live defects rather than thought of:

  * the audit set comes from git and its size is asserted -- "0 problems" and "0 files scanned"
    print alike otherwise;
  * a skip is a counted, printed outcome, never an absence. That covers the missing TOML parser on
    our Python floor, and a path handed to the gate that is not on disk.

The planted bad files go into the real directories and are removed in `addCleanup`, because the
only honest way to verify a structural rule is to feed it a known-broken artefact.
"""
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))
sys.path.insert(0, str(ROOT / "tests"))

import check_artifact_syntax as gate          # noqa: E402
import test_source_lint as lint               # noqa: E402

GATE = ROOT / "script" / "check_artifact_syntax.py"
WORKFLOWS = ROOT / ".github" / "workflows"

BROKEN_WORKFLOW = """name: probe
on:
  schedule:
    - cron: "1 1 * * *"
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python - <<PY
import json
PY
"""

HEALTHY_WORKFLOW = """name: probe
on:
  schedule:
    - cron: "1 1 * * *"
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python - <<PY
          import json
          PY
"""


def run_gate(*args):
    return subprocess.run([sys.executable, "-X", "utf8", str(GATE)] + list(args),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(ROOT))


class GateRunsTests(unittest.TestCase):
    def test_self_test_reports_every_seed_and_passes(self):
        proc = run_gate("--self-test")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        # The count has to be readable as a count: "11/11" is the assertion, not the word "ok".
        first = proc.stdout.splitlines()[0]              # "11/11 seeded violations detected ..."
        seen, _space, _rest = first.partition(" ")
        fired, _slash, total = seen.partition("/")
        self.assertTrue(fired.isdigit() and total.isdigit(), first)
        self.assertEqual(int(fired), int(total), "some seed did not fire: %s" % first)
        self.assertIn("structure reads", first)

    def test_the_gate_actually_audited_something(self):
        proc = run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        line = [l for l in proc.stdout.splitlines() if "artefact(s) parsed" in l]
        self.assertEqual(1, len(line), proc.stdout)
        count = int(line[0].split()[1])
        # A floor, not an expectation: the repository has 60+ structured artefacts, and a drop this
        # large can only mean the set stopped being collected.
        self.assertGreater(count, 40, line[0])

    def test_the_audit_set_is_exactly_what_git_tracks(self):
        tracked = gate.tracked_artefacts()
        listing = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(ROOT),
                                 "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                                 "--dedup"], capture_output=True).stdout.decode("utf-8").split("\0")
        want = sorted(n for n in listing if n
                      and n.lower().endswith(gate.SUFFIXES + tuple(gate.OUT_OF_SCOPE)))
        self.assertEqual(want, sorted(tracked),
                         "the gate and version control disagree about what exists")
        # Both languages must be represented, or a suffix could be "audited" by an empty list.
        self.assertTrue(any(n.endswith((".yml", ".yaml")) for n in tracked))
        self.assertTrue(any(n.endswith(".json") for n in tracked))

    def test_toml_is_a_stated_boundary_not_a_quiet_omission(self):
        # The gate does not parse TOML, and the reason matters more than the gap: `tomllib` only
        # exists on 3.11+, and the license gate decides "third-party or not" from the running
        # interpreter's stdlib list, so importing it would make this gate's verdict depend on the
        # host's Python version. What is required instead is that the boundary is spoken.
        tracked = gate.tracked_artefacts()
        tomls = [t for t in tracked if t.endswith(tuple(gate.OUT_OF_SCOPE))]
        self.assertTrue(tomls, "no TOML tracked, so this boundary proves nothing here")
        result = gate.audit(tracked)
        self.assertEqual(len(tomls), sum(result["out_of_scope"].values()), result["out_of_scope"])
        self.assertNotIn(".toml", gate.CHECKERS)
        proc = run_gate()
        self.assertIn("out of scope", proc.stdout)

    def test_quiet_still_reports_a_violation(self):
        # A tidy flag must not become a mute button -- this repository has already had a gate's
        # real defect hidden behind trimmed output.
        path = ROOT / "script" / "zz_probe_quiet.json"
        path.write_text('{"broken": ,', encoding="utf-8", newline="\n")
        self.addCleanup(path.unlink)
        proc = run_gate("--quiet", "--files", "script/zz_probe_quiet.json")
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("not valid JSON", proc.stdout)

    def test_a_path_handed_to_the_gate_that_is_not_on_disk_cannot_read_as_clean(self):
        proc = run_gate("--files", "does/not/exist.yml")
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("not on disk", proc.stdout)


class PlantedArtefactTests(unittest.TestCase):
    """Feed the gate a known-broken file in the real tree, then remove it."""

    def plant(self, name, text):
        path = WORKFLOWS / name
        path.write_text(text, encoding="utf-8", newline="\n")
        # addCleanup runs LIFO, so the assertion has to be registered first to run last: the
        # first draft registered unlink first and then "assert it is gone", which failed on every
        # case while still deleting the file -- a red suite whose redness meant the opposite of
        # what it said. Kept because a probe left behind in .github/workflows would be picked up
        # by the next real audit and read as a fresh defect.
        self.addCleanup(lambda: self.assertFalse(path.exists(),
                                                 "%s was not cleaned up" % path))
        self.addCleanup(path.unlink)
        return name

    def test_a_workflow_whose_block_body_hits_column_zero_is_caught(self):
        name = self.plant("zz_probe_broken.yml", BROKEN_WORKFLOW)
        proc = run_gate("--files", ".github/workflows/%s" % name)
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn(name, proc.stdout)
        self.assertIn("escaped its block scalar", proc.stdout)

    def test_the_same_workflow_indented_is_not_caught(self):
        # Without this twin the rule above could be firing on every workflow in the repository.
        name = self.plant("zz_probe_fine.yml", HEALTHY_WORKFLOW)
        proc = run_gate("--files", ".github/workflows/%s" % name)
        self.assertEqual(0, proc.returncode, proc.stdout)

    def test_a_broken_json_is_caught(self):
        path = ROOT / "script" / "zz_probe_broken.json"
        path.write_text('{"a": [1, 2,', encoding="utf-8", newline="\n")
        self.addCleanup(path.unlink)
        proc = run_gate("--files", "script/zz_probe_broken.json")
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("not valid JSON", proc.stdout)

    def test_json_holding_nan_is_caught_though_python_accepts_it(self):
        path = ROOT / "script" / "zz_probe_nan.json"
        path.write_text('{"latency": NaN}\n', encoding="utf-8", newline="\n")
        self.addCleanup(path.unlink)
        proc = run_gate("--files", "script/zz_probe_nan.json")
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("non-JSON constant", proc.stdout)


class RuleTests(unittest.TestCase):
    """Each rule on its own, both directions, so a widened rule cannot pass unnoticed."""

    def test_column_zero_body_is_illegal_while_column_zero_document_is_not(self):
        self.assertTrue(gate.check_yaml("a:\n  b: 1\nimport json\n"))
        self.assertEqual([], gate.check_yaml("- one\n- two\n"))
        self.assertEqual([], gate.check_yaml("---\na: 1\n...\n"))

    def test_comment_marker_at_column_zero_is_allowed(self):
        # Workflows open with prose comments; a rule that flags them gets switched off.
        self.assertEqual([], gate.check_yaml("# why this exists\non:\n  schedule: []\n"))

    def test_quoted_top_level_key_is_allowed(self):
        self.assertEqual([], gate.check_yaml('"on":\n  schedule: []\n'))

    def test_a_tab_in_indentation_is_a_finding(self):
        self.assertTrue(gate.check_yaml("a:\n\tb: 1\n"))
        self.assertEqual([], gate.check_yaml("a:\n  b: 1\n"))

    def test_duplicate_top_level_key_is_a_finding(self):
        # `on:` written twice is not a syntax error in every reader -- one simply wins silently.
        hits = gate.check_yaml("on:\n  a: 1\non:\n  b: 2\n")
        self.assertTrue(any("duplicate" in m for _n, m in hits), hits)
        self.assertEqual([], gate.check_yaml("on:\n  a: 1\njobs:\n  b: 2\n"))


class ChildrenTests(unittest.TestCase):
    def test_only_direct_children_are_returned(self):
        text = ("on:\n  schedule:\n    - cron: x\n  workflow_dispatch:\n"
                "    inputs:\n      dry_run:\n        type: boolean\n")
        self.assertEqual(["schedule", "workflow_dispatch"], gate.children(text, "on"))
        self.assertEqual([], gate.children(text, "absent"))

    def test_the_shipped_workflow_declares_no_push_trigger(self):
        # A schedule that also fires on push burns the API rate limit on every commit, and the
        # anonymous limit is what D75 turned into 403s.
        text = (WORKFLOWS / "model-updates.yml").read_text(encoding="utf-8")
        self.assertEqual(["schedule", "workflow_dispatch"], gate.children(text, "on"))


class HeaderRuleTests(unittest.TestCase):
    """D78: the license-header check the same round added, with its own two-way seeds.

    `lint-allow: no-header` must exempt a file that *declares* it as a comment, not one that
    mentions the string -- the first draft asked for a substring anywhere in the file, and the
    first file it examined (this linter) contained that exact text in its own help output, so the
    rule that documents the exemption granted itself the exemption.
    """

    def write_tmp(self, name, text):
        path = pathlib.Path(self.tmp) / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return str(path)

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = self._dir.name
        self.addCleanup(self._dir.cleanup)

    def test_mentioning_the_marker_does_not_exempt(self):
        path = self.write_tmp("mention.py", 'ALLOW = "%s"\nprint(ALLOW)\n' % lint.HEADER_ALLOW)
        self.assertEqual("missing", lint.scan_header(path)[0])

    def test_declaring_the_marker_exempts(self):
        path = self.write_tmp("declared.py", "# %s\ndef f():\n    return 1\n" % lint.HEADER_ALLOW)
        self.assertEqual("exempt", lint.scan_header(path)[0])

    def test_a_header_present_is_ok(self):
        # The mark is used verbatim. Both fixtures here first wrote `SPDX_MARK.rstrip(":")`, which
        # produces `# SPDX-License-Identifier Apache-2.0` -- no colon, so not the marker -- and the
        # "only one half missing" case below then passed for the wrong reason (both halves absent).
        # A fixture that agrees with nothing still on purpose is the trap this suite keeps hitting.
        path = self.write_tmp("headed.py", "# %s Apache-2.0\n# %s 2026 x\ndef f():\n"
                              % (lint.SPDX_MARK, lint.COPYRIGHT_MARK))
        self.assertEqual("ok", lint.scan_header(path)[0])

    def test_a_header_with_only_one_of_the_two_marks_is_missing(self):
        # Both halves are the convention: an SPDX id with no copyright line, or the reverse.
        path = self.write_tmp("half.py", "# %s Apache-2.0\ndef f():\n" % lint.SPDX_MARK)
        kind, detail = lint.scan_header(path)
        self.assertEqual("missing", kind)
        self.assertIn(lint.COPYRIGHT_MARK, detail)
        self.assertNotIn(lint.SPDX_MARK, detail, "only the absent half should be named")

    def test_absent_file_is_not_reported_as_headed(self):
        kind, detail = lint.scan_header(os.path.join(self.tmp, "nope.py"))
        self.assertEqual("missing", kind)
        self.assertIn("not on disk", detail)

    def test_the_repository_currently_needs_no_exemption(self):
        # A configuration guard: it does not prove the rule bites (the seeds above do), only that
        # nobody has quietly widened the exemption since.
        files = lint.iter_source_files(exts=lint.HEADER_EXTS)
        kinds = [lint.scan_header(p)[0] for p in files]
        self.assertEqual(0, kinds.count("missing") + kinds.count("exempt"),
                         [os.path.relpath(p, ROOT) for p, k in zip(files, kinds)
                          if k in ("missing", "exempt")])
        self.assertGreater(len(files), 100, "the scope walk found almost nothing")


class DependencyTests(unittest.TestCase):
    def test_the_gate_parses_without_importing_an_unregistered_dependency(self):
        # `import yaml` here is what made two CI jobs red at once: the component-license gate is
        # fail-closed over REGISTERED_DEPS, and a gate may not add a distribution dependency.
        for name in ("check_artifact_syntax.py", "report_model_updates.py",
                     "check_model_updates.py", "model_origin.py"):
            text = (ROOT / "script" / name).read_text(encoding="utf-8")
            offenders = [l for l in text.splitlines()
                         if l.strip().startswith("import ") and " yaml" in " " + l.split("import")[1]
                         and "yaml" in l.split()[1].split(",")]
            self.assertEqual([], offenders, "%s: %s" % (name, offenders))

    def test_every_audited_suffix_has_a_checker(self):
        for suffix in gate.SUFFIXES:
            self.assertIn(suffix, gate.CHECKERS, "%s would be collected and never checked" % suffix)

    def test_the_gate_does_not_read_the_umbrella_repository(self):
        # The one YAML outside this repo (`ci/gitee-go-workflow.yml`, a retired snapshot the
        # umbrella AGENTS forbids enabling) is deliberately out of reach: a checker must read data
        # from its own repository or it degrades into a permanent skip in CI.
        tracked = gate.tracked_artefacts()
        self.assertTrue(tracked)
        self.assertFalse([t for t in tracked if t.startswith("..") or "gitee-go-workflow" in t])
        json.dumps([t for t in tracked[:2]])      # paths must be plain repo-relative strings


class RegistrationTests(unittest.TestCase):
    """The gate exists only if something is obliged to keep running it.

    Both enumerations are asserted, not remembered: a suite in build.py but not ci.yml runs locally
    and never in CI, which is the half of D65 that registration lists do not fix by themselves. And
    an entry in AGENTS.md is what stops the next session re-inventing this as throwaway scratch.
    """

    NAME = "test_artifact_syntax.py"

    def test_the_suite_runs_locally_and_in_ci_alike(self):
        build = (ROOT / "script" / "build.py").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(self.NAME, build)
        self.assertIn(self.NAME, ci)

    def test_the_agents_table_points_at_the_gate(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("script/check_artifact_syntax.py", agents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
