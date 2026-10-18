# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Gate: every convention in CONTRIBUTING.md must name the thing that enforces it (defect D79).

Why this exists. The repository requires a license header on every source file, and 121 of 133
files had one -- the twelve that did not were all created recently, nine of them across four
consecutive rounds by the same hand. Nobody noticed, because the convention lived only in prose and
`CONTRIBUTING.md` never even recorded it. That is defect D78's quieter sibling: **a convention with
no enforcement decays silently, and nothing reports the decay.**

So this gate does not check code style. It checks the *contract between prose and tooling*: each
convention must declare who enforces it, and that declarant must exist.

Three evidence grades, deliberately kept apart -- a claim about enforcement is worth only as much as
the proof behind it (same rule this repo applied to sha256 provenance in D69):

  planted      a violating file was actually fed to the named checker and it went red;
  pointer      the named checker exists and runs, but nobody has planted a violation of *this*
               convention to see it fire;
  asserted     the prose says something enforces it and nothing here was verified -- the row that
               should be upgraded, and the reason the counts are printed.

`human` is a fourth kind for conventions no machine can decide ("attach evidence to a status
change"). Those must carry a reason, and their count is printed: an exemption that is not counted
becomes the normal path (D78).

No third-party parser, for the standing reason: the component-license gate is fail-closed and a
checker may not add a dependency by itself.
"""

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRIB = ROOT / "CONTRIBUTING.md"

#: Which sections of CONTRIBUTING.md carry conventions. Everything under a heading listed here is
#: audited for top-level `- ` items, so adding a convention without registering it here fails.
SECTIONS = ("## Conventions", "## Invariants to preserve")

#: A convention is identified by its leading bold phrase, e.g. "**Line endings**".
BOLD_LEAD = re.compile(r"^- \*\*(.+?)\*\*")

EVIDENCE_KINDS = ("planted", "pointer", "asserted", "human")

#: Every convention CONTRIBUTING.md states, and what enforces it. `source` is the bold phrase as
#: written in the file -- the join key that makes the coverage check mechanical rather than a list
#: I keep in my head. `enforced_by` must be a path that exists; `evidence` is the command that
#: proves it, and `grade` says how much that command has actually been tested against this rule.
CONVENTIONS = [
    {
        "source": "Code comments in English.",
        "kind": "pointer",
        "enforced_by": "tests/test_source_lint.py",
        "evidence": "python tests/test_source_lint.py",
        "note": "check 1 of the lint; CJK test *data* is deliberately not flagged",
    },
    {
        "source": "Every source file opens with its license header",
        "kind": "planted",
        "enforced_by": "tests/test_source_lint.py",
        "evidence": "python tests/test_source_lint.py <file-without-header>",
        "note": "planted both ways: header-less file goes red, a file that merely mentions the "
                "exemption marker stays red too (that is D78's fix)",
    },
    {
        "source": "Structured artefacts must parse, not just match a pattern.",
        "kind": "planted",
        "enforced_by": "script/check_artifact_syntax.py",
        "evidence": "python script/check_artifact_syntax.py --files .github/workflows/<broken>.yml",
        "note": "planted a workflow whose block body sits at column 0 -- the exact D77 shape",
    },
    {
        # The rule about this table is itself a row in it: a self-referential convention is only
        # honest if it is held to its own standard, and dropping it would make the gate exempt from
        # the thing it enforces.
        "source": "Every convention names its enforcer",
        "kind": "planted",
        "enforced_by": "script/check_convention_coverage.py",
        "evidence": "python tests/test_convention_coverage.py",
        "note": "planted by GateRunsTests.test_an_unregistered_convention_makes_the_gate_red: a "
                "bold item is appended inside ## Conventions, the gate goes red naming it, and the "
                "file is restored byte for byte with the undo asserted",
    },
    {
        "source": "Conventional Commits",
        "kind": "asserted",
        "enforced_by": "",
        "evidence": "manual review at PR time",
        "note": "nothing in this repository rejects a non-conventional subject line; recorded as "
                "a known gap rather than claimed away",
    },
    {
        "source": "Line endings",
        "kind": "planted",
        "enforced_by": ".gitattributes",
        "evidence": "git add <a CRLF file>; git show :<path> | has no CR",
        "note": "planted a work-tree file written with CRLF, staged it, and read the index blob "
                "back: zero CRs, and git printed 'CRLF will be replaced by LF'. The enforcer is "
                "git's clean filter, not a test -- which corrects nine rounds of treating "
                "work-tree CRLF as a delivery defect. Work-tree LF still matters for tools that "
                "read files straight off disk (this gate and the linters do), so the habit stays; "
                "the *severity* was the mistake.",
    },
    {
        "source": "UTF-8 across the FFI boundary",
        "kind": "pointer",
        "enforced_by": "tests/test_phase1_ffi.py",
        "evidence": "python tests/test_phase1_ffi.py",
        "note": "the FFI suite round-trips non-ASCII through the C ABI; nobody has planted a "
                "wrong-marshalling case to see a failure land in LibraryImport specifically",
    },
    {
        "source": "Rust core stays lean",
        "kind": "pointer",
        "enforced_by": "translator-engine/src/license_gate.rs",
        "evidence": "python tests/test_component_licenses.py",
        "note": "the license gate refuses unregistered dependencies (fail-closed), which covers "
                "'minimal deps'; the 'zero proc-macro' half is not checked by anything",
    },
    {
        "source": "Environment variables",
        "kind": "pointer",
        "enforced_by": "translator-engine/src/config.rs",
        "evidence": "cargo test config:: -- --nocapture",
        "note": "the chain is `first_env(lookup, [\"LT_X\", \"WONSLATE_X\"])` at 11 sites, and three "
                "unit tests pin the order by giving both spellings of one knob *different* values "
                "(lt_prefixed_ollama_still_wins_for_backward_compat, "
                "lt_data_dir_wins_over_wonslate_for_compat, bare_ollama_used_when_no_lt_prefix); "
                "translator-engine/tests/hermetic/mod.rs pins every name in the chain. Found by "
                "this gate's first run, which flagged the row as absent -- the correct outcome was "
                "to go look, not to declare the convention unenforced.",
    },
    {
        "source": "Privacy",
        "kind": "pointer",
        "enforced_by": "translator-engine/tests/privacy_loopback_e2e.rs",
        "evidence": "cargo test --test privacy_loopback_e2e",
        "note": "binds a real listener on a non-loopback address and asserts the text never gets "
                "there (D68); runs in the gate, not planted against a broken clamp",
    },
    {
        "source": "Licensing",
        "kind": "planted",
        "enforced_by": "script/check_name_guards.py",
        "evidence": "python script/check_name_guards.py --self-test",
        "note": "planted by its own --self-test: 8/8 seeded violations fire, one of them a "
                "downloader that bypasses net_guard, plus a name registered without a digest and a "
                "pin that is not 64 hex chars",
    },
]

#: Conventions that no machine can decide. Listed rather than omitted, so "not covered" is always a
#: choice somebody wrote down.
HUMAN_ONLY = [
    {
        "source": "Pull requests",
        "reason": "template completeness and review judgement are human acts; the build/test half "
                  "is covered by the suites registered in ci.yml",
    },
]


def declared_conventions():
    """The bold phrases of every top-level item under SECTIONS, in file order.

    Structural, not keyword-based: an earlier sweep for normative words found 36 candidate lines but
    missed `Line endings`, `Conventional Commits` and the `WONSLATE_*` spelling rule, because those
    state a requirement without using 'must'. A keyword filter silently under-counts, which is the
    failure this file exists to prevent.
    """
    lines = CONTRIB.read_text(encoding="utf-8").split("\n")
    names, section = [], None
    for line in lines:
        if line.startswith("## "):
            section = line.strip() if line.strip() in SECTIONS else None
            continue
        if not section or not line.startswith("- "):
            continue
        lead = BOLD_LEAD.match(line)
        if lead:
            names.append(lead.group(1).strip())
        else:
            # An item with no bold lead cannot be joined to a row; say so loudly instead of
            # letting it fall out of the audit unnoticed.
            names.append("!UNLED:%s" % line[2:40].strip())
    return names


def check_coverage(declared, sources, humans):
    """Does every convention CONTRIBUTING.md states have a row?

    Pure and parameterised so a seed can exercise it (D78's lesson: a guard that only ever sees the
    real data has never been shown to bite).
    """
    problems = []
    for name in declared:
        if name.startswith("!UNLED:"):
            problems.append("CONTRIBUTING.md item without a bold lead: %s -- it cannot be joined "
                            "to a row, so it would be audited by nobody" % name[7:])
            continue
        if name in sources or name in humans:
            continue
        near = [known for known in sources if known[:12] and
                (name.startswith(known[:12]) or known.startswith(name[:12]))]
        if near:
            problems.append("row %r does not match CONTRIBUTING.md's %r exactly (the join key is "
                            "the bold phrase as written)" % (near[0], name))
        else:
            problems.append("convention %r has no row here: it is prose without an owner (D79)"
                            % name)
    return problems


def check_rows(rows, exists):
    """Does each row name an enforcer that is real? `exists` is injected for the same reason."""
    problems = []
    for row in rows:
        for field in ("source", "kind", "enforced_by", "evidence", "note"):
            if field not in row:
                problems.append("row %r is missing the %r field" % (row.get("source"), field))
        if row.get("kind") not in EVIDENCE_KINDS:
            problems.append("%r: unknown kind %r" % (row.get("source"), row.get("kind")))
        target = row.get("enforced_by")
        if row.get("kind") in ("planted", "pointer") and not target:
            problems.append("%r: claims %s enforcement but names no file"
                            % (row.get("source"), row.get("kind")))
        elif target and not exists(target):
            # The mistake this very round made: a docstring promised `tests/test_model_origin.py`
            # asserted something. That file has never existed. A pointer to a non-existent checker
            # is worse than no pointer, because it stops the next reader looking.
            problems.append("%r: enforcement file does not exist: %s" % (row.get("source"), target))
        if row.get("kind") == "human" and not row.get("note"):
            problems.append("%r: human-only needs a stated reason" % row.get("source"))
        # Evidence grades must not drift upward by wishful thinking. A `planted` row claims that a
        # violation was actually fed to the checker, so it has to say what was planted; an
        # `asserted` row admits nobody tested it, so naming an enforcer would contradict that.
        if row.get("kind") == "planted" and "plant" not in row.get("note", "").lower():
            problems.append("%r: graded 'planted' but the note never says what was planted"
                            % row.get("source"))
        if row.get("kind") == "asserted":
            if row.get("enforced_by"):
                problems.append("%r: graded 'asserted' yet names %s -- the grade and the pointer "
                                "disagree about whether anything checks this"
                                % (row.get("source"), row["enforced_by"]))
            if "not" not in row.get("note", "").lower() and "no" not in row.get("note", "").lower():
                problems.append("%r: an acknowledged gap must say what is missing" % row.get("source"))
    return problems


def check_humans(rows):
    """A `human` verdict is allowed; an unexplained one is not."""
    return ["human-only entry %r has no reason" % row.get("source") for row in rows
            if not row.get("reason")]


def audit():
    """(problems, counts) over the real CONTRIBUTING.md and the table above."""
    declared = declared_conventions()
    problems = check_coverage(declared, {row["source"] for row in CONVENTIONS},
                              {row["source"] for row in HUMAN_ONLY})
    problems += check_rows(CONVENTIONS, lambda target: (ROOT / target).exists())
    problems += check_humans(HUMAN_ONLY)
    counts = {kind: 0 for kind in EVIDENCE_KINDS}
    for row in CONVENTIONS:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    counts["human-listed"] = len(HUMAN_ONLY)
    counts["declared"] = len(declared)
    return problems, counts


GOOD_ROW = {"source": "Some rule", "kind": "pointer", "enforced_by": "CONTRIBUTING.md",
            "evidence": "x", "note": "y"}


def self_test():
    """Every judgement below is exercised against a constructed violation *and* its clean twin.

    An earlier draft of this function tested nothing: it looked for a non-empty string I had typed
    into the case list, so it passed by construction -- the same 'green and hollow' shape this file
    exists to catch, reproduced by its own author in the same round.
    """
    cases = [
        ("convention with no row is reported",
         check_coverage(["Brand New Rule"], set(), set()), True),
        ("registered convention is not reported",
         check_coverage(["Brand New Rule"], {"Brand New Rule"}, set()), False),
        ("human-listed convention is not reported",
         check_coverage(["Pull requests"], set(), {"Pull requests"}), False),
        ("item with no bold lead is reported",
         check_coverage(["!UNLED:Line endings"], set(), set()), True),
        ("near-miss join key is a mismatch, not coverage",
         check_coverage(["Line endings and encoding"], {"Line endings"}, set()), True),
        ("planted grade without saying what was planted is reported",
         check_rows([dict(GOOD_ROW, kind="planted",
                          note="some checker exists")], lambda t: True), True),
        ("asserted grade that still names an enforcer is reported",
         check_rows([dict(GOOD_ROW, kind="asserted", note="nothing checks this")],
                    lambda t: True), True),
        ("row pointing at a missing checker is reported",
         check_rows([GOOD_ROW], lambda target: False), True),
        ("row pointing at a real checker is quiet",
         check_rows([GOOD_ROW], lambda target: True), False),
        ("planted row naming no file is reported",
         check_rows([dict(GOOD_ROW, kind="planted", enforced_by="")], lambda t: True), True),
        ("unknown kind is reported",
         check_rows([dict(GOOD_ROW, kind="vibes")], lambda t: True), True),
        ("human row with no reason is reported",
         check_rows([dict(GOOD_ROW, kind="human", note="")], lambda t: True), True),
        ("human-only entry with no reason is reported",
         check_humans([{"source": "x"}]), True),
        ("human-only entry with a reason is quiet",
         check_humans([{"source": "x", "reason": "judgement"}]), False),
    ]
    failures = []
    for label, hits, expect_problem in cases:
        if bool(hits) != expect_problem:
            failures.append("%s: expected %s, got %r"
                            % (label, "a problem" if expect_problem else "clean", hits[:2]))
    # The declaration walk must find a plausible number of items, or "all conventions covered" may
    # just mean "the pattern stopped matching".
    declared = declared_conventions()
    if len(declared) < 6:
        failures.append("declared_conventions() found %d item(s); SECTIONS or the bold-lead "
                        "pattern changed shape -- a short list reads as everything being covered"
                        % len(declared))
    if any(name.startswith("!UNLED:") for name in declared):
        failures.append("CONTRIBUTING.md has items with no bold lead: %s"
                        % [n for n in declared if n.startswith("!UNLED:")])
    print("%d/%d seeded judgements correct (%d conventions declared)"
          % (len(cases) - len(failures), len(cases), len(declared)))
    for line in failures:
        print("  MISSED: %s" % line)
    return not failures


def main(argv=None):
    ap = argparse.ArgumentParser(description="Every convention must name its enforcer (D79)")
    ap.add_argument("--json", default=None, help="write the table as JSON (for the ledger)")
    ap.add_argument("--quiet", action="store_true", help="print only the verdict")
    ap.add_argument("--self-test", action="store_true", help="prove the checks can fail")
    args = ap.parse_args(argv)
    problems, counts = audit()
    if args.self_test:
        for line in problems:
            print("SELFTEST: the clean tree is not clean: %s" % line)
        # Both halves must hold: the judgements bite on seeds, and the real table is clean.
        return 0 if self_test() and not problems else 1
    if not args.quiet:
        for row in CONVENTIONS:
            print("%-9s %-52s <- %s" % (row["kind"], row["source"][:52],
                                        row["enforced_by"] or "nothing"))
        for row in HUMAN_ONLY:
            print("%-9s %-52s    (%s)" % ("human", row["source"][:52], row["reason"][:60]))
    print("counts: declared=%d registered=%d planted=%d pointer=%d asserted=%d human=%d"
          % (counts["declared"], len(CONVENTIONS), counts["planted"], counts["pointer"],
             counts["asserted"], counts["human-listed"]))
    for line in problems:
        print("GAP: %s" % line)
    print("gate: convention coverage -- %d problem(s)" % len(problems))
    if args.json:
        payload = {"conventions": CONVENTIONS, "human_only": HUMAN_ONLY, "counts": counts,
                   "problems": problems}
        (ROOT / args.json).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8", newline="\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
