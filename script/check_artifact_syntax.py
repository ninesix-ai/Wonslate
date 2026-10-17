# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Gate: a structured artefact that cannot be parsed is not delivered (defect D77).

D77 was not a wrong assertion -- it was a file that no test ever *read*. The first version of
`.github/workflows/model-updates.yml` put a heredoc body back at column 0, which ends the
`run: |` block scalar and invalidates the whole document. GitHub Actions then shows a failed run
with no log, one phantom job, and the workflow "name" rendered as the file path: the timer looked
delivered and could never fire. Every existing cross-file assertion in this repo -- "each flag the
workflow names must still exist in the argparse" -- passed on that broken file, because they all
grep the text and none of them parse it.

Grep is not parse. This gate therefore reads every tracked YAML/JSON artefact as *structure*.

Why no third-party library: the component-license gate is fail-closed over `REGISTERED_DEPS`, and
`yaml` is not one -- a test may not introduce a distribution dependency by itself (that is how
`9858cc4` and `c1f0341` went red on two CI jobs at once). So YAML is checked by indentation rules
and JSON by `json.loads`.

Why no TOML either, though the standard library grew a parser for it: `tomllib` only exists on
3.11+, and the license gate decides "is this a dependency?" from `sys.stdlib_module_names` of the
interpreter running it. Importing it therefore makes the gate's verdict depend on the host's
Python version -- green here, red there, which is the D40/D45 shape this repository has already
paid for twice. `Cargo.toml` and `deny.toml` are parsed by cargo itself, which fails the build when
they are malformed, so the coverage exists; the scope line below just says so out loud rather than
leaving a silent gap between "audited" and "tracked".

One boundary worth stating because it is easy to get wrong: the same shape is not the same rule
across formats. A tab is illegal as YAML indentation and perfectly legal in TOML. Rules are
keyed by language for that reason; a shared table would either flag valid files or train people
to switch the gate off.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Artefacts that something is expected to read as data. `evidence/` JSONs are cited by the
#: ledgers, the workflow YAMLs are executed by Actions, `script/*.json` are loaded at runtime.
SUFFIXES = (".yml", ".yaml", ".json")

#: Deliberately not audited, and printed as such so the gap between "tracked" and "audited" is
#: a stated boundary rather than an accident. `.toml` is here because of the version-dependent
#: parser problem described at the top of this file.
OUT_OF_SCOPE = {".toml": "parsed by cargo, which fails the build when it is malformed"}

#: A column-0 line in a YAML file is only legal in these shapes. Everything else means a
#: block-scalar body escaped its block -- exactly what D77's heredoc did, and it invalidates the
#: whole document rather than one step. `$` must be its own alternative: writing `^(#||$|...)`
#: puts an empty pattern first, which matches the start of every line and turns the guard into a
#: no-op (the first draft did precisely that, and only a planted broken file showed it). The
#: closing quote after the key name is not optional either: `"on":` is legal YAML, and a draft
#: that matched only `"on` + `:` rejected it -- the test that planted a quoted key caught that.
TOP_LEVEL = re.compile(r'^(?:#|---|\.\.\.|- |<<:|["\']?[A-Za-z_][\w.-]*["\']?:|$)')

#: A top-level mapping key, used for the duplicate check.
MAPPING_KEY = re.compile(r'^(?:#|---|\.\.\.|- )')

#: What this gate deliberately does NOT check, and why. The first draft also tried to prove that
#: a `key: |` body is indented deeper than its key. That rule fired on
#:
#:       - run: |
#:         echo hi
#:
#: which is the most ordinary GitHub Actions indentation there is, because the baseline for a
#: mapping nested in a sequence entry is ambiguous without a real parser. A rule that cries wolf
#: gets switched off inside a week -- worse than no rule, since the switch-off is silent. Only the
#: case with no interpretation space is kept: body text returning to column 0 (D77's shape), which
#: cannot be valid YAML in any file that also has a mapping open above it.


def _indent(line):
    """Width of the leading whitespace run, tabs included -- a tab there is itself a finding."""
    return len(line) - len(line.lstrip(" \t"))


def check_yaml(text):
    """(line_number, message) for each structural violation, 1-based.

    Rules, in decreasing confidence. The first two are what D77 needed; the rest came from asking
    what else a reader could get wrong in a file that never gets opened.
    """
    problems = []
    lines = text.split("\n")
    seen = {}
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        lead = line[:_indent(line)]
        if "\t" in lead:
            problems.append((number, "tab is not YAML indentation: %s" % stripped[:48]))
        if not line[0].isspace():
            # Only column-0 lines are constrained here; anything indented is ordinary structure.
            # A rule that flags those is a rule that gets switched off within a week.
            if not TOP_LEVEL.match(line):
                problems.append((number, "line escaped its block scalar: %s" % line[:48]))
            elif not MAPPING_KEY.match(line):
                key = re.match(r'^(["\']?)([A-Za-z_][\w.-]*)\1:', line)
                if key:
                    name = key.group(2)
                    if name in seen:
                        problems.append((number, "duplicate top-level key %r (first at line %d); "
                                         "a parser keeps only one" % (name, seen[name])))
                    else:
                        seen[name] = number
    return sorted(problems)


def children(text, key):
    """The direct child keys of a top-level mapping entry, read by indentation.

    Shared on purpose: `tests/test_report_model_updates.py` used to carry its own copy of this
    walker, and two readers of the same format drift into two definitions of "valid" -- the D64
    shape. It lives here now so the gate and the test cannot disagree about `on:`.

    Only *direct* children: the first draft of this shared copy returned every deeper line, so
    `on:` came back as `schedule, cron, workflow_dispatch, inputs, dry_run, ...` -- grandchild
    keys promoted into the level above them. A reader that flattens depth is not reading the
    structure, which is the whole point of parsing instead of grepping.

    Deliberately dependency-free, for the reason in the module docstring: `yaml` is not a
    registered dependency and a gate may not add one.
    """
    lines = text.split("\n")
    for number, line in enumerate(lines):
        if line.startswith(key + ":") or line.startswith('"%s":' % key):
            indent = len(line) - len(line.lstrip())
            out = []
            child_indent = None
            for child in lines[number + 1:]:
                if not child.strip():
                    continue
                depth = _indent(child)
                if depth <= indent:
                    break                       # the mapping closed; siblings are not children
                if child_indent is None:
                    child_indent = depth
                elif depth != child_indent:
                    continue                    # deeper: a grandchild, not this level
                match = re.match(r"^\s*-?\s*([A-Za-z_][\w.-]*):", child)
                if match:
                    out.append(match.group(1))
            return out
    return []


def json_rejects(constant):
    """`parse_constant` hook: NaN/Infinity are not JSON, however lenient Python is about them.

    They are the quiet half of this gate: such a file loads happily here and fails in every
    stricter reader (Actions' expression evaluator, a browser's JSON.parse, another language).
    """
    raise ValueError("non-JSON constant %r (JSON has no NaN/Infinity)" % constant)


def check_json(text):
    """Syntax problems as (line_number, message); line is best-effort from the decoder."""
    try:
        json.loads(text, parse_constant=json_rejects)
    except ValueError as exc:
        row = getattr(exc, "lineno", 0) or 1
        return [(row, "not valid JSON: %s" % exc)]
    return []


CHECKERS = {".yml": check_yaml, ".yaml": check_yaml, ".json": check_json}


def audit(paths):
    """{audited, problems, out_of_scope} over the given repo-relative paths."""
    problems, audited, out_of_scope = [], 0, {}
    for rel in sorted(paths):
        path = ROOT / rel
        suffix = path.suffix.lower()
        if suffix in OUT_OF_SCOPE:
            out_of_scope[suffix] = out_of_scope.get(suffix, 0) + 1
            continue
        if suffix not in CHECKERS:
            continue
        if not path.is_file():
            problems.append((rel, 0, "tracked but missing from the work tree"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        audited += 1
        for number, message in CHECKERS[suffix](text):
            problems.append((rel, number, message))
    return {"audited": audited, "problems": problems, "out_of_scope": out_of_scope}


def tracked_artefacts():
    """Every tracked structured file, from git -- not from a hard-coded list.

    A list maintained by hand is how D65's "registered but never run" happens in reverse: the
    gate keeps reporting green while the file nobody added stays broken. `core.quotepath=false`
    is not cosmetic; without it any non-ASCII path comes back octal-escaped and does not exist.
    """
    out = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(ROOT), "ls-files", "-z",
                          "--cached", "--others", "--exclude-standard", "--dedup"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    names = [n for n in out.split("\0") if n]
    return [n for n in names if n.lower().endswith(SUFFIXES + tuple(OUT_OF_SCOPE))]


# Seeds are transcribed into the file, never pulled from git history: CI's checkout is shallow,
# so a `git show <broken commit>` seed would work here and silently do nothing there -- the exact
# failure this repository already documented when the name-guard gate was registered. Each seed is
# paired with a correct twin, because a rule that fires on healthy input is a rule that gets
# switched off.
SEED_BLOCK_ESCAPE_BAD = """name: demo
on:
  schedule:
    - cron: "17 3 * * *"
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python - <<PY
import json
print(json.dumps({}))
PY
"""

SEED_BLOCK_ESCAPE_GOOD = """name: demo
on:
  schedule:
    - cron: "17 3 * * *"
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python - <<PY
          import json
          print(json.dumps({}))
          PY
"""

SEED_TAB_BAD = """jobs:
  a:
	steps:
	  - run: echo hi
"""

SEED_TAB_GOOD = """jobs:
  a:
    steps:
      - run: echo hi
"""

SEED_DUPLICATE_BAD = """name: demo
on:
  schedule:
    - cron: "17 3 * * *"
on:
  push:
    branches: [main]
"""

SEED_DUPLICATE_GOOD = """name: demo
on:
  schedule:
    - cron: "17 3 * * *"
permissions:
  contents: read
"""

SEED_SHALLOW_LIST_GOOD = """- one
- two
- key: value
"""

#: `cron` and `inputs` sit one level deeper and must not be promoted into the answer.
SEED_CHILDREN_NESTED = """on:
  schedule:
    - cron: "17 3 * * *"
  workflow_dispatch:
    inputs:
      dry_run:
        type: boolean
permissions:
  contents: read
"""


def self_test():
    """Every rule must fire on its bad seed and stay quiet on its good twin."""
    cases = [
        ("D77 block body at column 0", check_yaml, SEED_BLOCK_ESCAPE_BAD, True),
        ("indented block body (the fix)", check_yaml, SEED_BLOCK_ESCAPE_GOOD, False),
        ("tab indentation", check_yaml, SEED_TAB_BAD, True),
        ("space indentation", check_yaml, SEED_TAB_GOOD, False),
        ("duplicate top-level key", check_yaml, SEED_DUPLICATE_BAD, True),
        ("distinct top-level keys", check_yaml, SEED_DUPLICATE_GOOD, False),
        ("top-level sequence, column-0 by design", check_yaml, SEED_SHALLOW_LIST_GOOD, False),
        ("prose comment at column 0", check_yaml, "# why this exists\nname: demo\n", False),
        ("quoted top-level key", check_yaml, '"on":\n  schedule: []\n', False),
        ("json syntax error", check_json, "{\"a\": }", True),
        ("json is fine", check_json, '{"a": [1, 2], "b": {"c": null}}', False),
        ("json carrying NaN", check_json, '{"latency": NaN}', True),
        ("json carrying Infinity", check_json, '{"latency": Infinity}', True),
    ]
    failures = []
    for label, checker, text, expect_hit in cases:
        hits = checker(text)
        if bool(hits) != expect_hit:
            failures.append("%s: expected %s, got %r" % (label, "a hit" if expect_hit
                                                         else "clean", hits[:2]))
    # `children` is not a violation checker, so it gets its own two-way assertion: flattening
    # depth must not be what it does, and a missing key must read as empty rather than as a hit.
    nested = children(SEED_CHILDREN_NESTED, "on")
    if nested != ["schedule", "workflow_dispatch"]:
        failures.append("children() returned %r instead of the two direct keys" % (nested,))
    if children(SEED_CHILDREN_NESTED, "nope"):
        failures.append("children() invented keys for an absent mapping")
    # The checker table must cover every suffix the auditor claims to handle, or a language can be
    # silently dropped from the audit while the gate still prints "0 problems".
    for suffix in SUFFIXES:
        if suffix not in CHECKERS:
            failures.append("suffix %s is audited but has no checker" % suffix)
    print("%d/%d seeded violations detected (+2 structure reads)" % (len(cases) - len(failures),
                                                                     len(cases)))
    for line in failures:
        print("  MISSED: %s" % line)
    return not failures


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parse every tracked YAML/JSON artefact; a file that "
                                             "cannot be parsed is not delivered (D77)")
    ap.add_argument("--self-test", action="store_true", help="prove each rule fires and its twin "
                                                             "stays quiet")
    ap.add_argument("--files", nargs="*", default=None, metavar="PATH", help="audit exactly these "
                    "paths instead of the tracked set: for a file not yet added to git, and for "
                    "the tests that plant a broken one to prove this gate bites. The tracked set "
                    "is the default because it is what CI can reproduce; the cost is that a new "
                    "workflow is only covered once it is staged, which pre-commit then catches.")
    ap.add_argument("--quiet", action="store_true", help="print only the verdict")
    args = ap.parse_args(argv)
    if args.self_test:
        return 0 if self_test() else 1
    paths = args.files if args.files else tracked_artefacts()
    if args.files:
        # A typo in --files must not read as "clean": asking about nothing and finding nothing
        # are the same output otherwise.
        missing = [p for p in paths if not (ROOT / p).is_file()]
        if missing:
            print("gate: FAIL - %d path(s) given but not on disk: %s"
                  % (len(missing), ", ".join(missing[:3])))
            return 1
    result = audit(paths)
    if not result["audited"]:
        # "0 problems" from "0 files" is the output this gate exists to make impossible.
        print("gate: FAIL - audited nothing, so this run says nothing about the repository")
        return 1
    for rel, number, message in result["problems"]:
        print("%s:%d %s" % (rel, number, message))
    for suffix, count in sorted(result["out_of_scope"].items()):
        if not args.quiet:
            print("note: %d %s file(s) are out of scope here -- %s"
                  % (count, suffix, OUT_OF_SCOPE[suffix]))
    print("gate: %d artefact(s) parsed, %d problem(s)"
          % (result["audited"], len(result["problems"])))
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
