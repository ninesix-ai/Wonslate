#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""measure_latency.py -- idle-machine latency and memory for one engine.

Why: the FLORES report quotes latency numbers that were measured while
other heavy jobs competed for the same6 GB of VRAM and CPU, so they run
2-3x high. A routing decision ("madlad is as good as qwen, so route to
madlad") needs the other half of the ledger: how much wall time and how
much RAM each engine actually costs when the machine is otherwise idle.

What it measures, per engine:
  model_load_s  server up -> first sentence translated (the model-load cost)
  steady        per-sentence wall time: median / mean / p90 / min / max
  peak_rss_mb   the sidecar process's working set, via netstat + tasklist

It does NOT score quality. Quality comes from bench_flores.py +
score_comet.py; this script only fills in the cost axis, and it never
writes into an evidence file -- the numbers belong in the report's TL;DR
footnote, which is prose, not a measured artefact.

Usage:
  # start the sidecar yourself, then:
  python script/measure_latency.py --engine madlad --port 11436 --n 30
  python script/measure_latency.py --engine argos --port 11435 --n 30

Windows-only for the memory read (tasklist); the latency part is
portable. Kept dependency-free on purpose: this machine has no working
package index (see docs/tasks/S10), so adding psutil is not an option.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

# FLORES-style sentences, deliberately mixed length: a batch tool's cost
# is dominated by long sentences, a subtitle tool's by short ones.
SENTENCES = [
    "He built a WiFi door bell, he said.",
    "The committee will meet again on Thursday to discuss the budget.",
    "She said the new law takes effect on the first of January, unless the parliament amends it before then.",
    "It rained.",
    "Researchers published a study showing that the vaccine reduced severe cases by eighty percent.",
    "The president of the company said that the new factory would create five thousand jobs over the next three years.",
]


def loopback(port: int, path: str) -> str:
    # Loopback only, never a configurable host: this benchmark must not be
    # able to send product data anywhere.
    return "http://127.0.0.1:{}{}".format(port, path)


def http_json(url: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_healthy(port: int, budget_s: float) -> float:
    """Seconds until /health answers200. Raises if it never does."""
    start = time.perf_counter()
    deadline = start + budget_s
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(loopback(port, "/health"), timeout=3) as r:
                if r.status == 200:
                    return time.perf_counter() - start
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    raise SystemExit(
        "sidecar on port {} did not become healthy within {}s".format(port, budget_s)
    )


def one_sentence(port: int, text: str, source: str, target: str, timeout: float) -> str:
    out = http_json(
        loopback(port, "/translate"),
        {"text": text, "source": source, "target": target},
        timeout,
    )
    return out.get("text", "")


def _run_console(argv, env):
    """Run a Win32 console tool and return its stdout asASCII text.

    Windows consoles emit the active OEM code page, which is GBK on a
    Chinese-locale machine -- netstat's "LISTENING" column alone contains
    bytes that are not valid UTF-8. Decoding as bytes and then as latin-1
    keeps the ASCII digits and port numbers readable no matter what the
    console encoding happens to be. Returns "" when the tool is missing.
    """
    try:
        done = subprocess.run(argv, capture_output=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("ascii", errors="replace")


def peak_rss_mb(port: int) -> float:
    """Working set of the sidecar serving `port`, in MB.

    The pid comes from netstat's LISTENING row for the port, not from matching
    the command line: two sidecars can be up at once (ct2 on 11435, madlad on
    11436) and both command lines contain "ct2_sidecar", so a name-only match
    reports the wrong process -- it once attributed madlad's 2.9 GB to argos.

    Powershell's CIM provider would also work, but shelling out to it from
    Python is blocked by this workstation's shell policy, and its console code
    page (GBK here) breaks text-mode decoding besides. netstat plus tasklist
    are both plain Win32 executables that answer in the active code page.

    Returns 0.0 when it cannot be determined; the caller reports that as
    unknown rather than as "no memory used".
    """
    if sys.platform != "win32":
        return 0.0
    # MSYS/Git Bash rewrites a leading "/" into a Windows path, which turns
    # "/FI" and "/AN" into filenames; these flags have to survive intact.
    env = dict(os.environ, MSYS_NO_PATHCONV="1")

    listening = []
    for shell in (["netstat", "-ano", "-p", "TCP"], ["netstat", "-ano"]):
        out = _run_console(shell, env)
        if not out:
            continue
        for line in out.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            # The state column reads "LISTENING" in English but is localised on
            # a Chinese Windows, so it cannot be matched as text. What holds on
            # every locale: the local address is field 1, and the owning pid is
            # the last field, never 0 for a listening socket.
            if not fields[1].startswith("127.0.0.1:"):
                continue
            if fields[1].rsplit(":", 1)[-1] != str(port):
                continue
            pid_field = fields[-1]
            if not pid_field.isdigit() or pid_field == "0":
                continue
            if pid_field not in listening:
                listening.append(pid_field)
        if listening:
            break
    if not listening:
        return 0.0

    # More than one process can hold the same listening port: SO_REUSEADDR lets
    # a stale sidecar and the live one share it, and a batch that was restarted
    # without reaping leaves exactly that. The serving process is the one whose
    # command line carries this script, and among those the largest working set
    # -- a model-backed sidecar is hundreds of MB while a leftover thread-shared
    # handle holder can be tens of MB. Picking the first pid instead reported a
    # 33 MB "madlad" and hid the real 1.6 GB.
    candidates = []
    for pid in listening:
        row = _run_console(
            ["tasklist", "/FI", "PID eq {}".format(pid), "/FO", "CSV", "/NH"], env
        )
        for line in row.splitlines():
            cells = [c.strip('" ') for c in line.split('","')]
            if len(cells) < 5 or cells[1].strip('"') != pid:
                continue
            digits = "".join(ch for ch in cells[4] if ch.isdigit())
            if digits:
                candidates.append((int(digits), pid))
    if not candidates:
        return 0.0
    return round(max(candidates)[0] / 1024.0, 1)


def summarise(samples):
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "median_s": round(statistics.median(ordered), 3),
        "mean_s": round(statistics.fmean(ordered), 3),
        "p90_s": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))], 3),
        "min_s": round(ordered[0], 3),
        "max_s": round(ordered[-1], 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", required=True, choices=["madlad", "argos"])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--n", type=int, default=30,
                    help="measured sentences after the warm-up (default 30)")
    ap.add_argument("--source", default="en")
    ap.add_argument("--target", default="zh")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--already-running", action="store_true",
                    help="skip the cold-start measurement (server is up)")
    args = ap.parse_args()

    print("[measure] engine={} port={} n={}".format(args.engine, args.port, args.n))

    cold = None
    if args.already_running:
        t0 = time.perf_counter()
        first = one_sentence(args.port, SENTENCES[0], args.source, args.target,
                             args.timeout)
        warm_first = time.perf_counter() - t0
        print("[measure] warm_first_s {:.2f} ({} chars out)".format(
            warm_first, len(first)))
    else:
        # The sidecar is expected to be started separately, often asynchronously,
        # so probing /health right away can find it already warm and report a
        # cold start of ~0s. What is actually being asked is "how long until the
        # first sentence can be translated", so time from the first successful
        # health check to the first real request and report that as
        # model_load_s -- a lower bound on start-up, not a pretend cold start.
        wait_healthy(args.port, budget_s=600.0)
        t0 = time.perf_counter()
        first = one_sentence(args.port, SENTENCES[0], args.source, args.target,
                             args.timeout)
        cold = time.perf_counter() - t0
        print("[measure] model_load_s {:.2f} (server up -> first sentence out)"
              .format(cold))

    # Warm-up: the first few calls pay for lazy allocation and thread-pool
    # spin-up, so they are excluded from the steady-state numbers.
    for text in SENTENCES[1:4]:
        one_sentence(args.port, text, args.source, args.target, args.timeout)

    timings = []
    chars = 0
    for i in range(args.n):
        text = SENTENCES[i % len(SENTENCES)]
        t0 = time.perf_counter()
        out = one_sentence(args.port, text, args.source, args.target, args.timeout)
        timings.append(time.perf_counter() - t0)
        chars += len(text)
        print("[measure]   {}/{} {:.2f}s".format(i + 1, args.n, timings[-1]))

    rss = peak_rss_mb(args.port)
    steady = summarise(timings)
    steady["chars"] = chars
    steady["chars_per_s"] = round(chars / sum(timings), 1) if timings else 0.0
    steady["peak_rss_mb"] = rss

    record = {
        "engine": args.engine,
        "source": args.source,
        "target": args.target,
        "machine_state": "idle (no ollama request in flight, no COMET scoring)",
        "model_load_s": None if cold is None else round(cold, 2),
        "warm_first_s": None if args.already_running else None,
        "steady": steady,
    }
    print("[measure] RESULT " + json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
