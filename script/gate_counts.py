# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Turn a `build.py --test` log into test counts a ledger may quote -- or refuse to.

Why this is a tool and not a one-off: every round of this project quotes Rust / Python / .NET
figures parsed out of the gate's own output, and the parsing kept living in a throwaway file.
Two defects came from exactly that, both measured rather than imagined:

  * host-decided output. PowerShell's `>` writes UTF-16LE, and the .NET CLI localises its summary
    on a Chinese host (the line becomes 188-with-different-words). Either way a pattern written
    against the English shape matches zero, and "matched zero" is indistinguishable from "that
    layer never ran" -- one round published numbers taken from a log its counter could not read;
  * the counting convention. The three gate scripts print no `Ran N` line, so REQ-F3's counting
    rule gives each of them one case. Counting *occurrences* instead of *names* inflated the
    Python total the moment a test calls a gate in process (measured 4 PASS lines, 3 names, and a
    total of 389 against a true 388) -- a figure that could not be attributed to any test.

So this module does two things and shares one rule between them: `parse()` attributes every count
to the script that produced it and lists which layers it could NOT see, and `plan_run()` pins the
environment and refuses to reuse a log name, because an artefact whose name carries no identity
gets silently replaced (D51). `totals_for_ci` is `totals_for_local`: a local/CI gap is only a fact
if both sides counted the same way.

Usage:
    python script/gate_counts.py parse <log>        # report and verify coverage
    python script/gate_counts.py run [--log NAME]   # run the gate, then report
Exit code: 0 = every layer attributed and the gate says ALL DONE; 1 = a layer is missing, the
verdict is absent, or a phase failed. Never 0 merely because nothing matched.
"""
import argparse
import datetime
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# A gate prints "<name> gate: PASS (...)" or "<name> lint: PASS". Matched by shape rather than by
# a hard-coded list of three names, so a fourth gate is counted instead of silently omitted --
# the "expected value must not encode the current maximum" rule this repo already registered.
GATE_PASS = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]*(?:gate|lint)): PASS\b")
RUST_RESULT = re.compile(r"^\s*test result: ok\. (\d+) passed")
UNITTEST_RAN = re.compile(r"^Ran (\d+) tests?\b")
CHECKER_TOTAL = re.compile(r"^\s*passed (\d+) / failed \d+ / skipped \d+ / total (\d+)\s*$")
DOTNET_SUMMARY = re.compile(r"Failed:\s+(\d+), Passed:\s+(\d+), Skipped:\s+(\d+), Total:\s+(\d+)")
SUITE_MARKER = re.compile(r"^\s*-- (\S+\.py) --\s*$")
VERDICT_OK = "ALL DONE, tests PASSED"
VERDICT_BAD = "phase(s) FAILED"

LAYERS = ("rust", "python-unittest", "python-checker", "python-gate", "dotnet", "verdict")


class LogNameOccupied(Exception):
    """The requested log path already holds a previous run's evidence."""


def read_log(path):
    """Read a gate log no matter which encoding the shell handed it.

    Detecting this is not a nicety: a UTF-16 log read as UTF-8 yields interleaved NULs, every
    pattern then misses, and the report looks like a run in which nothing executed. BOM first,
    then a NUL heuristic for the BOM-less case.
    """
    raw = pathlib.Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", errors="replace")
    if b"\x00" in raw[:4096]:
        # No BOM: pick the byte order whose NULs sit where they should.
        le = raw.count(b"\x00") and raw[1::2].count(b"\x00") >= raw[0::2].count(b"\x00")
        return raw.decode("utf-16-le" if le else "utf-16-be", errors="replace")
    return raw.decode("utf-8", errors="replace")


def parse(text):
    """Attribute every count in a gate log to its producer, and name what is missing."""
    rust = []
    suites = []                       # (owning script, kind, count)
    gate_lines = 0
    gate_seen = {}                    # gate name -> set of scripts that printed it
    net = None
    done = False
    failed_phase = False
    current = None

    for line in text.splitlines():
        m = SUITE_MARKER.match(line)
        if m:
            current = m.group(1)
            continue
        m = RUST_RESULT.match(line)
        if m:
            rust.append(int(m.group(1)))
            continue
        m = UNITTEST_RAN.match(line)
        if m:
            suites.append((current or "(before first marker)", "unittest", int(m.group(1))))
            continue
        m = CHECKER_TOTAL.match(line)
        if m:
            # `total` already includes the skipped cases, which is what REQ-F3's counting rule
            # asks for; adding passed and skipped by hand would agree today and drift tomorrow.
            suites.append((current or "(before first marker)", "checker", int(m.group(2))))
            continue
        m = GATE_PASS.match(line)
        if m:
            gate_lines += 1
            gate_seen.setdefault(m.group(1).strip(), set()).add(current)
            continue
        m = DOTNET_SUMMARY.search(line)
        if m:
            net = {"failed": int(m.group(1)), "passed": int(m.group(2)),
                   "skipped": int(m.group(3)), "total": int(m.group(4))}
            continue
        if VERDICT_OK in line:
            done = True
        elif VERDICT_BAD in line:
            failed_phase = True

    counted = {name for name, _kind, _n in suites}
    # A gate adds one case only when its own script contributed nothing else. The hygiene gate is
    # printed twice because a rules test calls it in process; crediting that second line would
    # invent a case that belongs to no test.
    gate_only = sorted({s for name, scripts in gate_seen.items()
                        for s in scripts if s and s not in counted})
    gate_names = sorted(gate_seen)

    layers = {"rust": sum(rust),
              "python-unittest": sum(n for _s, k, n in suites if k == "unittest"),
              "python-checker": sum(n for _s, k, n in suites if k == "checker")}
    layers["python-gate"] = len(gate_names)
    layers["python"] = (layers["python-unittest"] + layers["python-checker"]
                        + layers["python-gate"])
    layers["dotnet"] = net["passed"] if net else 0
    total = layers["rust"] + layers["python"] + layers["dotnet"]

    missing = []
    if not rust:
        missing.append("rust")
    if not layers["python-unittest"]:
        missing.append("python-unittest")
    if not layers["python-checker"]:
        missing.append("python-checker")
    if not gate_names:
        missing.append("python-gate")
    if not net:
        missing.append("dotnet")
    if not done or failed_phase:
        # A present "Failed: 0" is not the claim "the gate ran and passed": a log truncated after
        # the summary line still shows green numbers for phases that never completed.
        missing.append("verdict")

    return {"rust": rust, "suites": suites, "gate_names": gate_names, "gate_lines": gate_lines,
            "gate_only_suites": gate_only, "dotnet": net, "done": done,
            "failed_phase": failed_phase, "layers": layers, "total": total, "missing": missing}


def totals_for_local(summary):
    """The ledger figure, plus the decomposition that makes it checkable."""
    return {"rust": summary["layers"]["rust"], "python": summary["layers"]["python"],
            "dotnet": summary["layers"]["dotnet"], "total": summary["total"]}


# One rule for both sides. The local/CI gap quoted in the ledger (28 for several rounds running)
# stops being a fact the moment the two are counted differently.
totals_for_ci = totals_for_local


def parse_ci_log(text):
    """A GitHub run log through the same parser; job headers simply produce no matches."""
    return parse(text)


def plan_run(log=None):
    """Decide how to invoke the gate: pinned environment, and a log name that identifies itself."""
    if log is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M")
        log = ROOT / ("gate-%s.log" % stamp)
    log = pathlib.Path(log)
    if not log.is_absolute():
        log = ROOT / log
    if log.exists():
        raise LogNameOccupied(
            "%s already holds a previous run's log. Its numbers may be quoted by a committed "
            "ledger entry; name the new log after what it measures." % log.name)
    env = dict(os.environ)
    env["DOTNET_CLI_UI_LANGUAGE"] = "en"      # the summary line must stay matchable
    env["DOTNET_NOLOGO"] = "1"
    # Not PYTHONIOENCODING=utf-8: build.py reads its children with the host's own decoding, so
    # overriding one side of that pair garbles their output instead of fixing it. Dropping a host
    # export keeps the run from depending on machine state in either direction.
    env.pop("PYTHONIOENCODING", None)
    return {"argv": [sys.executable or "python", str(ROOT / "script" / "build.py"), "--test"],
            "env": env, "log": log, "cwd": str(ROOT)}


def report(summary, stream=sys.stdout):
    lay = summary["layers"]
    print("Rust  targets=%d  sum=%d  detail=%s" % (len(summary["rust"]), lay["rust"], summary["rust"]),
          file=stream)
    print("Python suites=%d  unittest=%d  checker=%d  gates=%d (raw PASS lines=%d, unique names %s)"
          % (len(summary["suites"]), lay["python-unittest"], lay["python-checker"],
             lay["python-gate"], summary["gate_lines"], summary["gate_names"]), file=stream)
    for name, kind, n in summary["suites"]:
        print("   %-34s %-9s %4d" % (name, kind, n), file=stream)
    print("   gate cases credited to: %s" % (summary["gate_only_suites"] or "(none)"), file=stream)
    net = summary["dotnet"]
    print(".NET  %s" % (net if net else "(no summary line found)"), file=stream)
    print("verdict ok=%s  failed_phase=%s" % (summary["done"], summary["failed_phase"]), file=stream)
    print("TOTAL = %d + %d + %d = %d" % (lay["rust"], lay["python"], lay["dotnet"],
                                         summary["total"]), file=stream)
    if summary["missing"]:
        print("PARSE COVERAGE FAILURE: these layers were not attributed: %s"
              % ", ".join(summary["missing"]), file=stream)
        print("  do not quote these numbers -- the log encoding, the CLI language, or the "
              "patterns changed", file=stream)
    return 0 if not summary["missing"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read or produce an authoritative gate log.")
    sub = parser.add_subparsers(dest="mode", required=True)
    p_parse = sub.add_parser("parse", help="report counts from an existing log")
    p_parse.add_argument("log")
    p_run = sub.add_parser("run", help="run build.py --test with pinned output, then report")
    p_run.add_argument("--log", default=None,
                       help="path for the log; must not exist. Default: gate-<UTC>.log")
    args = parser.parse_args(argv)

    if args.mode == "parse":
        return report(parse(read_log(args.log)))

    plan = plan_run(args.log)
    with open(plan["log"], "w", encoding="utf-8", errors="replace") as sink:
        proc = subprocess.run(plan["argv"], cwd=plan["cwd"], env=plan["env"], stdout=sink,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace", timeout=3000)
    print("gate rc = %d, log = %s" % (proc.returncode, plan["log"].name))
    verdict = report(parse(read_log(plan["log"])))
    return proc.returncode or verdict


if __name__ == "__main__":
    sys.exit(main())
