# -*- coding: utf-8 -*-
"""report_model_updates.py -- turn a version check into one announcement, never one per day.

`check_model_updates.py --baseline` answers "has an upstream moved". This decides what to do
about it, and the difficulty is not the network call, it is repetition: the baseline is
deliberately not advanced by the scheduled run (advancing it is the human decision that stops
the alert from firing again), so the same finding comes back every morning. Without a memory of
what has already been announced, a daily timer would open a fresh issue each day until someone
switched it off -- and a switched-off check is worse than none (D65's lesson).

So the announcement key is the *upstream answer itself*, not "something is new":

  * never announced            -> open an issue
  * already announced verbatim -> say nothing
  * the finding changed         -> add a comment to the existing open issue
  * nothing new                 -> say nothing

Reading the issue list is a network action that can fail, and a failure here must not look like
"nothing to announce" (D74 again, one layer further along the chain): the exit code separates
"we handled it" from "I could not tell you", and nothing else is non-zero. Announcing is success,
not a fault, so the scheduled workflow goes green when it posts -- a red timer that someone has to
click into is how timers get disabled.

    python script/report_model_updates.py --json model-updates.json --dry-run

Only public upstream facts go into the issue body: asset names, version strings, the day we
looked. Nothing from the umbrella repository, no local paths -- the same boundary as
`docs/privacy-architecture.md` draws for evidence files.
"""
import argparse
import json
import pathlib
import subprocess
import sys

TITLE_PREFIX = "[auto] Model upstream published a newer version"
MARKER = "wonslate-model-updates"
#: Everything before the finding key inside the marker comment. `open_findings` slices at this
#: exact length, so the marker text and this constant may not drift apart -- the first draft
#: sliced with the length of a *complete* marker while searching for the prefix, and quietly ate
#: the first characters of every key.
MARKER_PREFIX = "<!-- %s:" % MARKER
NEWER = "newer"


class CannotTell(RuntimeError):
    """The lookup needed to decide failed. Reported as its own outcome, never as silence."""


def newer_rows(payload):
    """The rows that say an upstream is ahead of this repository's baseline."""
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    return [row for row in (rows or []) if row.get("state") == NEWER]


def finding_key(rows):
    """A stable identity for *what* was found, so the same finding is only announced once.

    Keyed on the upstream answer rather than the baseline value we are still holding: if the
    baseline is not advanced, the next run produces the same key and stays quiet. If upstream
    moves again, the key changes and the issue gets a comment.
    """
    parts = sorted("%s=%s" % (row["checked"], row.get("upstream")) for row in rows)
    return ";".join(parts)


def open_findings(issues):
    """Every finding key already announced in these open issues, and where it lives."""
    announced, first_open = {}, None
    for issue in issues:
        text = issue.get("body") or ""
        start = text.find(MARKER_PREFIX)
        if start < 0:
            continue
        end = text.find("-->", start)
        if end < 0:
            continue                       # truncated marker: treat as unannounced, not as ""
        announced.setdefault(text[start + len(MARKER_PREFIX):end].strip(), issue.get("number"))
        if first_open is None:
            first_open = issue.get("number")
    return announced, first_open


def plan(rows, issues):
    """(action, detail) -- the whole decision, so it can be tested without GitHub.

    `action` is one of "none", "create", "comment". Commenting rather than opening a second
    issue keeps one thread per ongoing situation, which is what makes the daily run tolerable.
    """
    if not rows:
        return "none", "no upstream is ahead of the baseline"
    key = finding_key(rows)
    announced, first_open = open_findings(issues)
    if key in announced:
        return "none", "finding %s already announced in issue #%s" % (key, announced[key])
    if first_open is None:
        return "create", key
    return "comment", (key, first_open)


def body(rows, key, verdict_message=""):
    """The issue text: what moved, what we still ship, and how to accept it."""
    lines = ["Automated check found that an upstream published something newer than this "
             "repository's recorded baseline (`script/model_upstream_baseline.json`).", ""]
    if verdict_message:
        lines.append("> %s" % verdict_message)
        lines.append("")
    lines += ["| asset | we ship | upstream now says |", "|---|---|---|"]
    for row in sorted(rows, key=lambda r: r["checked"]):
        lines.append("| %s | %s | %s |" % (row["checked"], row.get("local") or "-",
                                           row.get("upstream") or "-"))
    lines += ["", "Nothing was downloaded and the baseline was not advanced: accepting a new "
              "version is a human decision, because the pins in each fetch script have to be "
              "re-verified first (D69).", "", "```",
              "python script/check_model_updates.py --baseline",
              "# and only after you have decided to accept it:",
              "python script/check_model_updates.py --baseline --record-baseline",
              "```", "", "%s %s -->" % (MARKER_PREFIX, key)]
    return "\n".join(lines) + "\n"


def _gh(args, runner=subprocess.run):
    """One `gh` call, with failures turned into `CannotTell` rather than empty results."""
    proc = runner(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                  errors="replace")
    if proc.returncode != 0:
        raise CannotTell("gh %s failed (%d): %s" % (" ".join(args[:2]), proc.returncode,
                                                    (proc.stderr or "").strip()[:200]))
    return proc.stdout


def open_issues(runner=subprocess.run):
    """Open issues whose title marks them as ours. A failed listing raises.

    Filtering by title prefix locally rather than by label: a label has to exist before
    `--label` can match it, and quietly getting an empty list here would read as "nothing
    announced yet" and open a duplicate every day.
    """
    out = _gh(["issue", "list", "--state", "open", "--limit", "200",
               "--json", "number,title,body"], runner)
    try:
        issues = json.loads(out or "[]")
    except ValueError as exc:
        raise CannotTell("unreadable issue listing: %s" % exc)
    return [issue for issue in issues if (issue.get("title") or "").startswith(TITLE_PREFIX)]


def publish(action, detail, rows, verdict_message, runner=subprocess.run, dry_run=False):
    """Carry out the plan. Returns a sentence about what happened."""
    key = detail if action == "create" else detail[0]
    text = body(rows, key, verdict_message)
    if dry_run:
        return "dry-run: would %s (%s)" % (action, key)
    if action == "create":
        _gh(["issue", "create", "--title", "%s: %s" % (TITLE_PREFIX, _short(rows)),
             "--body", text], runner)
        return "opened an issue for %s" % key
    _gh(["issue", "comment", str(detail[1]), "--body", text], runner)
    return "commented on issue #%s about %s" % (detail[1], key)


def _short(rows):
    return ", ".join(sorted(str(row["checked"]).split(":", 1)[-1] for row in rows))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Announce model upstream version findings once per "
                                             "distinct finding (0 handled, 1 could not tell)")
    ap.add_argument("--json", required=True, help="the rows file written by check_model_updates.py")
    ap.add_argument("--dry-run", action="store_true", help="decide and print, change nothing")
    args = ap.parse_args(argv)

    payload = json.loads(pathlib.Path(args.json).read_text(encoding="utf-8"))
    rows = newer_rows(payload)
    verdict_message = (payload.get("verdict") or {}).get("message", "")
    if (payload.get("verdict") or {}).get("code") == 1:
        # The check itself could not tell. Announcing that as a version alert would be wrong, and
        # staying silent would be worse: the reader would assume the check ran.
        print("cannot tell: %s" % verdict_message)
        return 1
    try:
        action, detail = plan(rows, open_issues())
    except CannotTell as problem:
        print("cannot tell: %s" % problem)
        return 1
    print(publish(action, detail, rows, verdict_message, dry_run=args.dry_run))
    # Both outcomes -- "nothing new to say" and "said it" -- are 0. Only blindness is not, so no
    # caller has to interpret an exit code that means success.
    return 0


if __name__ == "__main__":
    sys.exit(main())
