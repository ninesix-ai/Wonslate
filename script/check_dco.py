#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""check_dco.py -- require a Developer Certificate of Origin sign-off on every new commit.

Wonslate uses DCO 1.1 instead of a CLA: no separate signing flow, no legal entity to stand up, and
the statement travels with the commit (`git commit -s`). The policy lives in CONTRIBUTING.md; this
checker is what makes it real.

Scope is deliberately incremental. The history before 2026-10-21 has no sign-offs, and rewriting
published history to add them would be a far bigger risk than the gap; so the gate checks the range
it is given (CI passes the pushed or PR range) and says how many commits it examined. A range that
cannot be resolved is an error (exit 2), never a silent pass.

Usage:
    python script/check_dco.py                          # HEAD^..HEAD
    python script/check_dco.py --range A..B
    python script/check_dco.py --repo path --range HEAD~3..HEAD

Exit codes: 0 = every non-merge commit in range is signed; 1 = some are not; 2 = cannot check.
"""
import argparse
import re
import subprocess
import sys

#: DCO requires the exact trailer; a name with no address, or an address with no name, does not
#: count. Case-sensitive on purpose: `signed-off-by:` is a different trailer and some tools ignore it.
SIGNOFF = re.compile(r"^Signed-off-by:[ \t]+\S.*[ \t]+<[^<>@\s]+@[^<>\s]+>[ \t]*$", re.MULTILINE)


class DcoError(RuntimeError):
    """The range could not be examined (bad revision, not a repo, git missing)."""


class Missing:
    __slots__ = ("sha", "subject")

    def __init__(self, sha, subject):
        self.sha = sha
        self.subject = subject

    def __repr__(self):
        return "Missing(%s, %r)" % (self.sha, self.subject)


def _git(repo, *args):
    try:
        result = subprocess.run(["git", "-C", str(repo)] + list(args),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace")
    except OSError as exc:
        raise DcoError("cannot run git: %s" % exc)
    if result.returncode != 0:
        raise DcoError("git %s failed: %s" % (" ".join(args), result.stderr.strip()))
    return result.stdout


def commits(repo, rev_range):
    """Shas in the range, oldest first, merge commits excluded."""
    output = _git(repo, "rev-list", "--no-merges", "--reverse", rev_range)
    return [line.strip() for line in output.splitlines() if line.strip()]


def check(repo, rev_range):
    """Return the non-merge commits in `rev_range` without a well-formed sign-off."""
    missing = []
    for sha in commits(repo, rev_range):
        body = _git(repo, "show", "-s", "--format=%B", sha)
        if SIGNOFF.search(body):
            continue
        subject = _git(repo, "show", "-s", "--format=%s", sha).strip()
        missing.append(Missing(sha, subject))
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description="Require DCO sign-off on every new commit")
    parser.add_argument("--range", default="HEAD^..HEAD",
                        help="git revision range (default: HEAD^..HEAD)")
    parser.add_argument("--repo", default=".", help="repository path (default: .)")
    args = parser.parse_args(argv)

    try:
        shas = commits(args.repo, args.range)
        missing = check(args.repo, args.range)
    except DcoError as exc:
        print("DCO: cannot check %s: %s" % (args.range, exc), file=sys.stderr)
        return 2

    if not shas:
        print("DCO: no commits in %s -- nothing to sign" % args.range)
        return 0
    if not missing:
        print("DCO: OK (%d commit%s signed)" % (len(shas), "" if len(shas) == 1 else "s"))
        return 0

    print("DCO: %d of %d commit(s) in %s have no Signed-off-by:"
          % (len(missing), len(shas), args.range), file=sys.stderr)
    for item in missing:
        print("  %s  %s" % (item.sha[:12], item.subject), file=sys.stderr)
    print("Fix: amend or rebase with `git commit -s` (see CONTRIBUTING.md).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
