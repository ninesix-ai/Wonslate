# -*- coding: utf-8 -*-
"""Gate: a guard that cannot be reached is not a guard (defect D73, tech/07 section 14.11 item 5).

Two findings, deliberately kept apart because they carry different evidence:

  L1  statically unreachable statement -- a statement sitting in the same list after a
      return / raise / break / continue. This is a compiler-grade fact with no interpretation
      space, the Python counterpart of Rust's `unreachable_code` and C# CS0162 (those two
      languages are covered by their own compilers, which is why this gate is Python-only).

  L2  a guard ordered behind a success branch that always leaves -- the shape that let D73
      survive review: `run_av_baseline` already had `if proc.poll() is not None: raise
      SystemExit(...)`, placed *after* `if _sidecar_running(port): return proc`. When an old
      instance owns the port that first condition is always true, so the guard below it never
      executed -- not once. Code like that reads as "already defended" in review, which is
      precisely why it needs a machine.

Run it from `tests/test_unreachable_guards.py`, which also runs the seeds; a gate that only
lives in CI's list is D65 again.
"""
import argparse
import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Predicates that mean "this branch is the happy path".
SUCCESS_WORDS = ("healthy", "running", "alive", "ready", "available", "success", "isup",
                 '"ok"', "'ok'", "is_file(", "is_dir(", "exists")
# Predicates that mean "this statement is the guard".
FAILURE_WORDS = ("poll(", "returncode", "exitcode", "exited", "failed", "error", "dead",
                 "missing", "not ok", "unhealthy")

TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)

# Lines whose L2 match is deliberate, as (file substring, line substring, reason). An entry
# here has to say why the guard is reachable anyway -- an empty list is a real assertion, so
# the gate prints how many were waived rather than leaving the reader to guess.
WAIVED = []


def statement_lists(tree):
    """Yield every statement list the AST exposes, together with its owning node."""
    for parent in ast.walk(tree):
        for _field, value in ast.iter_fields(parent):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                yield parent, value


def _condition_text(node):
    try:
        return ast.unparse(node).lower()
    except Exception:                                       # noqa: BLE001
        return ""


def _leaves(node):
    """Does this `if` block end the enclosing function on its taken branch?"""
    for stmt in node.body:
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return True
    return False


def find_unreachable(tree):
    """L1: statements after a terminator in the same list."""
    found = []
    for _parent, body in statement_lists(tree):
        for index, stmt in enumerate(body[:-1]):
            if isinstance(stmt, TERMINATORS):
                nxt = body[index + 1]
                found.append((getattr(nxt, "lineno", 0),
                              "dead code after %s" % type(stmt).__name__,
                              ast.unparse(nxt)[:70]))
    return found


def find_ordered_guards(tree):
    """L2: a success branch that returns, placed before a guard in the same statement list."""
    hits = []
    for _parent, body in statement_lists(tree):
        for index, stmt in enumerate(body[:-1]):
            if not isinstance(stmt, ast.If) or not _leaves(stmt):
                continue
            text = _condition_text(stmt.test)
            if not any(word in text for word in SUCCESS_WORDS):
                continue
            for later in body[index + 1:]:
                if not isinstance(later, ast.If) or not _leaves(later):
                    continue
                guard = _condition_text(later.test)
                if any(word in guard for word in FAILURE_WORDS):
                    hits.append((stmt.lineno, later.lineno, text[:60], guard[:60]))
                    break
    return hits


def _waived(name, hit):
    """Is this L2 match on the waiver list?

    A waiver is (path prefix, a substring of one of the two conditions involved, reason). The
    substring is matched against the condition text rather than a line number so a waiver cannot
    silently start pointing at unrelated code after an edit shifts the lines around.
    """
    _early, _guard, first, second = hit
    for prefix, needle, _reason in WAIVED:
        if name.startswith(prefix) and needle and (needle in first or needle in second):
            return True
    return False


def tracked_python():
    """The file list comes from git, not from a hand-kept roster."""
    out = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(ROOT),
                          "ls-files", "*.py"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    return [n for n in out.split("\n") if n.strip()]


def audit():
    """(violations, unparseable, audited_count). Unparseable files are a finding, not a skip."""
    violations, unparseable, audited = [], [], 0
    for name in tracked_python():
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            unparseable.append("%s: %s" % (name, exc))
            continue
        audited += 1
        for line, why, snippet in find_unreachable(tree):
            violations.append("L1 %s:%d %s -- %s" % (name, line, why, snippet))
        for early, guard, a, b in find_ordered_guards(tree):
            if _waived(name, (early, guard, a, b)):
                continue
            violations.append("L2 %s:%d returns on [%s] before the guard at :%d [%s]"
                              % (name, early, a, guard, b))
    return violations, unparseable, audited


# ---------------------------------------------------------------- seed corpus

# The D73 case, transcribed rather than read from git: CI's default checkout is shallow, so a
# `git show <pre-fix commit>` seed would work on this machine and silently do nothing in CI.
# Seeds are written as real multi-line blocks, never with \n escapes: `name:\n` reads to the
# source lint as a drive letter, which already bit this repo once while seeding another gate.
SEED_TAKEN_PORT = '''
def _start_sidecar(engine, port):
    proc = subprocess.Popen(["python", "sidecar.py", "--port", str(port)])
    deadline = time.time() + 120
    while time.time() < deadline:
        if _sidecar_running(port):
            print("ready")
            return proc
        if proc.poll() is not None:
            raise SystemExit("exited early")
        time.sleep(2)
'''

SEED_CORRECT_ORDER = '''
def _start_sidecar(engine, port):
    proc = subprocess.Popen(["python", "sidecar.py", "--port", str(port)])
    if proc.poll() is not None:
        raise SystemExit("exited early")
    if _sidecar_running(port):
        return proc
    raise SystemExit("never healthy")
'''

SEED_DEAD_CODE = """
def f():
    return 1
    print(2)
"""

SEED_LEGITIMATE_EARLY_RETURN = """
def pick(flag):
    if flag:
        return 1
    return 2
"""


def self_test():
    """Every seed must fire in the direction it claims, including the ones that must stay quiet."""
    checks = []

    tree = ast.parse(SEED_TAKEN_PORT)
    checks.append(("L2 catches the real D73 shape", bool(find_ordered_guards(tree))))

    tree = ast.parse(SEED_CORRECT_ORDER)
    checks.append(("L2 spares the fixed order", not find_ordered_guards(tree)))

    checks.append(("L1 catches a statement after return",
                   bool(find_unreachable(ast.parse(SEED_DEAD_CODE)))))
    checks.append(("L1 spares a conditional early return",
                   not find_unreachable(ast.parse(SEED_LEGITIMATE_EARLY_RETURN))))

    # The whole-repo audit must report having looked at something, or "0 violations" means
    # nothing: a pattern matching no files and a clean repository print the same number.
    violations, broken, audited = audit()
    checks.append(("audit opened a real number of files (%d)" % audited, audited > 40))
    checks.append(("audit parses everything it opens", not broken))

    # And the waiver list must not be a mute button: with it empty, the seeded violation above
    # still has to be reportable, which is what makes adding an entry a visible act.
    checks.append(("waiver list is empty, so nothing is silently excused", WAIVED == []))
    checks.append(("a waiver only silences what it names",
                   _waiver_proves_selective()))

    detected = sum(1 for _label, ok in checks if ok)
    for label, ok in checks:
        print("  %s %s" % ("OK  " if ok else "MISS", label))
    return detected, len(checks)


def _waiver_proves_selective():
    """Add a waiver that names the seeded case and require exactly that one to disappear.

    Without this, `WAIVED` could be widened until the gate says nothing at all, and the file
    would still print "0 violation(s)".
    """
    hits = find_ordered_guards(ast.parse(SEED_TAKEN_PORT))
    if len(hits) != 1:
        return False
    hit = hits[0]
    WAIVED.append(("script/check_unreachable_guards.py", hit[3][:20], "self-test"))
    try:
        silenced = _waived("script/check_unreachable_guards.py", hit)
        untouched = _waived("script/other.py", hit)
    finally:
        WAIVED.pop()
    return silenced and not untouched


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        detected, total = self_test()
        print("unreachable-guard self-test: %d/%d seeded violations detected" % (detected, total))
        return 0 if detected == total else 1

    violations, broken, audited = audit()
    for line in violations:
        print(line)
    for line in broken:
        print("UNPARSED %s" % line)
    print("unreachable-guard gate: %d file(s) audited, %d violation(s), %d waived, "
          "%d unparseable" % (audited, len(violations), len(WAIVED), len(broken)))
    return 1 if (violations or broken or audited == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
