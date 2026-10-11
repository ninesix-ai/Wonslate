# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Sidecar batch diagnostics -- the measurements behind defect D24 / D30.

Run from the repository root. Five modes, one variable each:

  load    N sequential requests against one backend, sampling the process while
          it runs. Answers "does latency or memory grow with request count?"
          Pass `--corpus` to drive it with distinct sentences -- the shape item 004
          measured -- and results are then also reported per pass against the first
          pass, because a ladder is one body of work done again, not one sentence
          repeated.
  rotate  one fixed source sentence across several target scripts, then revisits
          the first ones. Answers "is the ladder accumulation, or is it just what
          each script costs?" A revisited language that costs what it cost before
          is evidence against accumulation.
  hard    awkward inputs (already-target text, identifier-like keys, templates,
          zero-width, long paragraphs). Answers "does some input run the decoder
          to max_decoding_length and stall there?"
  reload  N requests with `POST /reload` every K, reporting the calls that merely
          translate separately from the one call that pays for a reset. Answers
          "does self-resetting hold latency at its baseline, and what does the reset
          cost?" That split is the whole point: averaging both together blames the
          reload for the seconds it buys deliberately, and the number a retention
          line needs is the steady drift alone, pass against pass.
  race    drive two revisions of the sidecar with a fake ctranslate2 and count
          how many translators a cold burst builds. Answers "was the reported
          slowdown a duplicated 2.95 GB checkpoint?" No real model is loaded, so
          it is fast and deterministic.
  abandon call with a deadline shorter than one decode, then watch what the service
          still spends after the caller has gone, whether that still-running work
          slows the requests that are still wanted, and what the abandoned handlers
          leave behind (defect D30 item 2 -- the half of item 004 nobody has
          measured). Needs a real backend; on mock a decode is milliseconds and the
          ratios would be noise.

Nothing here runs in CI: `load` and `reload` now state a verdict against the retention
line (exit code 0 or 1), but they need a real model and tens of minutes, so the verdict
is for whoever is deciding a release, not for a gate. `rotate`, `hard`, `abandon` and
`race` only
measure. Numbers printed by `load`, `rotate` and `reload` are environment-dependent by
design -- the ledger's counting rule keeps a runtime-precondition column for exactly that
reason (REQ-F3), and the environment a verdict was reached in belongs with the verdict.

Dependencies: psutil (process sampling) and git (only for `race --rev`).
"""
import argparse
import http.client
import json
import math
import os
import pathlib
import statistics
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sidecar import ct2_sidecar                                     # noqa: E402
from sidecar.ct2_sidecar import MadladBackend, serve_in_thread       # noqa: E402

SENTENCE = ("Hello, please import the audio file and keep the original sample "
            "rate. The cabinet simulation and the pitch shifter are both available.")

# Target scripts ordered cheap -> expensive, the order the report used.
ROTATION = ["de", "ru", "zh", "hi", "th", "my", "km", "ta"]

HARD_CASES = [
    ("short greeting", "Hello", "en", "zh"),
    ("already target language", "你好，世界。这是一个音频文件，请保持原始采样率。", "zh", "zh"),
    ("identifier-like key", "ui.MktUploadNoPresets", "en", "zh"),
    ("template placeholders", "You have {count} files in {path}.", "en", "de"),
    ("zero-width inside", "co\u200cute", "en", "zh"),
    ("uppercase shout", "PITCH AND FORMANT ARE BOTH AVAILABLE", "en", "es"),
    ("mixed script", "OK 好 done", "en", "ja"),
    ("long paragraph", " ".join([SENTENCE] * 6), "en", "zh"),
    ("long paragraph to myanmar", " ".join([SENTENCE] * 6), "en", "my"),
    ("single digit", "42", "en", "zh"),
]


def post_json(port, path, payload, timeout=120.0):
    """One request, one fresh connection: the way a batch client actually calls."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


# psutil is an operator-side diagnostic dependency (see sidecar/requirements and the ledger's
# license notes), not part of the shipped runtime. A host without it must still get the latency
# measurement -- which is the thing a retention line is read off -- so the resource columns come
# back unknown rather than killing a two-hour run at its first sample.
UNKNOWN = -1.0


def sample():
    """(rss_mb, threads, handles_or_fds) of this process; the backend runs in it.

    Unknown is printed as unknown, never as zero: a 0 MB memory column on a host that cannot
    read memory is the "stopwatch that never ran looked fast" mistake this repo has already been
    burned on (defect D30 item 1 fixed exactly that in /health).
    """
    try:
        import psutil
    except ImportError:
        return UNKNOWN, UNKNOWN, UNKNOWN
    p = psutil.Process(os.getpid())
    io = p.memory_info().rss / 1048576.0
    if hasattr(p, "num_handles"):
        third = p.num_handles()
    else:
        third = p.num_fds()
    return io, p.num_threads(), third


def fmt(value):
    """One place that decides how an unknown resource reading looks."""
    return "n/a" if value is None or value < 0 else "%d" % value


def mem(value):
    """A memory reading with its unit, or plain unknown -- "n/aMB" is what bolting the unit on
    outside this function produces, and it reads like a value someone forgot to round."""
    return "n/a" if value is None or value < 0 else "%dMB" % value


def delta(then, now):
    """A change between two samples, or unknown if either end was unreadable."""
    if then is None or now is None or then < 0 or now < 0:
        return "n/a"
    return "%+d" % (now - then)


def reclaimed(before, after):
    """How much resident memory a reset gave back, or unknown.

    Deliberately not ``before - after`` at the call site: with no psutil both ends are -1, and
    the subtraction would then print "0 MB back" -- an invented number wearing the clothes of a
    measurement, which is the mistake this file exists to avoid.
    """
    if before < 0 or after < 0:
        return "n/a"
    return "%.0f" % (before - after)


def report(tag, latencies, first_rss, last_rss):
    """Print the ratio that a ladder would show, so its absence is also evidence.

    Marked UNPAIRED because it is easy to quote by mistake: head and tail are *different*
    sentences unless --text repeats one, so this ratio mixes in what each sentence costs. On a
    300-sentence corpus today it read 1.08 while the paired pass-against-pass number from the
    same run read 0.98 -- one workload, two answers 8% apart, and only one of them measures
    drift. pass_report is the one to cite.
    """
    head = statistics.mean(latencies[:min(100, len(latencies))])
    tail = statistics.mean(latencies[-min(100, len(latencies)):])
    print("%s: n=%d  head_mean=%.2fs tail_mean=%.2fs ratio=%.2f (UNPAIRED -- different sentences"
          " in each window; read the pass report for drift)  rss %s -> %s"
          % (tag, len(latencies), head, tail, (tail / head if head else 0.0),
             mem(first_rss), mem(last_rss)))


def corpus_lines(raw):
    """The distinct source sentences a run should be driven with, or None for "repeat --text".

    Accepts a `.src`/text file, or a directory holding one (`<prefix>.src`). Reading the same
    corpus the benchmark scores against is the point: item 004 measured its ladder over 1088
    *distinct* UI keys, so repeating one sentence would measure a cache-warm path and call the
    result a batch.
    """
    if not raw:
        return None
    path = pathlib.Path(raw)
    if path.is_dir():
        candidates = sorted(path.glob("*.src")) or sorted(path.glob("*.txt"))
        if not candidates:
            raise SystemExit("no .src/.txt corpus under {}".format(path))
        path = candidates[0]
    if not path.is_file():
        raise SystemExit("corpus not found: {}".format(path))
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise SystemExit("corpus {} has no non-empty lines".format(path))
    return lines


def iter_units(args):
    """Yield (text, pass_number) for args.n calls, cycling the corpus one sentence at a time.

    Passes are counted because that is how item 004 described the symptom: the *same* body of
    work, re-run, took 130 s then 280 s then over 900 s. One request cannot be compared with
    another request whose sentence happens to cost more; one pass over the same sentences can.
    """
    lines = corpus_lines(getattr(args, "corpus", None)) or [args.text]
    for i in range(args.n):
        yield lines[i % len(lines)], i // len(lines)


# The retention line: one number, three questions that mean the same thing -- did a lap of the
# same work cost more than the lap before it (load, reload), and did a language cost more when
# revisited (rotate). Keeping it in one place is what stops those answers drifting apart.
#
# What justifies it as a line is the measured noise floor: three passes over 300 distinct
# sentences came back 1.00x / 0.97x / 0.98x, so pass-to-pass variation is about 3%, and 1.15 is
# roughly four times that -- far enough not to cry wolf on a warm cache, close enough to catch
# the 2.15x and 6.9x steps external item 004 measured. A threshold nobody can falsify is
# not a line; tests/test_diag_sidecar_batch.py feeds that ladder back in and must see a FAIL.
RETENTION_LIMIT = 1.15


def pass_report(tag, per_pass):
    """Per-pass means against the first pass -- the shape the report used.

    Every number is printed with its own sample count: a later pass with fewer entries (a run
    that ended mid-pass) is not comparable to a full one, and hiding that would make the ratio
    look firmer than it is.

    A pass of one repeated sentence is refused rather than reported: the ratio would be almost
    perfectly flat, because the work really is identical, and a flat line measured that way is
    the kind of number that gets quoted as "no drift over thousands of calls" when what was
    actually shown is one sentence translated twice.
    """
    ordered = sorted(per_pass)
    if not ordered:
        print("{}: no served requests, so nothing is measured".format(tag))
        return 1
    widest = max(len(v) for v in per_pass.values())
    baseline = statistics.mean(per_pass[ordered[0]])
    print("{}: passes={}, sentences-per-pass~{}"
          .format(tag, len(ordered), widest))
    for p in ordered:
        mean = statistics.mean(per_pass[p])
        rank = max(1, math.ceil(0.95 * len(per_pass[p])))
        print("  pass %d: n=%-4d mean=%7.3fs p95=%7.3fs vs pass1=%s"
              % (p + 1, len(per_pass[p]), mean,
                 sorted(per_pass[p])[min(rank, len(per_pass[p])) - 1],
                 "{:.2f}x".format(mean / baseline) if baseline else "n/a"))
    if widest < 2:
        print("  REFUSING to state a line: each pass here is a single repeated sentence, so the"
              " ratio compares the same work done again only in the narrowest sense -- pass"
              " `--corpus` with distinct sentences to measure a batch the way item 004 did")
        return 1
    # Only complete passes are comparable: a run that stopped mid-sentence did a different
    # amount of work than the passes around it, and comparing across that difference is how a
    # measurement quietly stops meaning what its label says. No threshold is invented here --
    # "the same sentences, all of them" is the whole condition.
    full = [p for p in ordered if len(per_pass[p]) == widest]
    dropped = [p for p in ordered if p not in full]
    if dropped:
        print("  excluded incomplete passes %s (%s sentences each, against a full pass of %d)"
              % ("".join("pass%d " % (p + 1) for p in dropped),
                 ", ".join(str(len(per_pass[p])) for p in dropped), widest))
    if len(full) < 2:
        print("  fewer than two complete passes -- a retention line needs the same body of work"
              " done at least twice, so this run cannot decide anything")
        return 1
    first = statistics.mean(per_pass[full[0]])
    last = statistics.mean(per_pass[full[-1]])
    ratio = last / first if first else 0.0
    verdict = "PASS" if ratio <= RETENTION_LIMIT else "FAIL"
    print("  ** retention line (defect D30 item 4): pass {} against pass {} = {:.2f}"
          " against a limit of {:.2f} -> {} **".format(
              full[-1] + 1, full[0] + 1, ratio, RETENTION_LIMIT, verdict))
    return 0 if verdict == "PASS" else 1


def dump_run(path, meta, records):
    """Persist every request behind a verdict, not just the verdict.

    A printed ratio cannot be re-examined by anyone: the pass means that produced it depend on
    the individual calls, on which ones failed, and on how many sentences each pass really
    held. Writing them out is what lets a number in the ledger be checked instead of trusted.

    Two things it must never do. It must not need a directory the caller forgot to make -- an
    hour-long run should not end on a typo in a path. And it must not raise: this is called from
    a finally block, so an exception here would destroy the verdict and the records it is trying
    to save, which is the opposite of why it exists.
    """
    if not path:
        return
    body = dict(meta)
    body["records"] = records
    try:
        target = pathlib.Path(path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print("  WARNING: records were NOT saved to %s (%s); the verdict above is all that is"
              " left of this run" % (path, exc))
        return
    print("  per-request records written to %s (%d calls)" % (path, len(records)))


def run_meta(args, mode):
    """What a reader needs to judge whether these numbers mean anything."""
    return {"mode": mode, "backend": args.backend, "pair": "%s->%s" % (args.src, args.tgt),
            "corpus": args.corpus or None, "requested_calls": args.n,
            "retention_limit": RETENTION_LIMIT,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(args.started_at)),
            "note": "seconds are client-side wall time around one POST, one fresh connection"
                    " per call; the service ran in this same process"}


def run_load(args):
    backend = ct2_sidecar.make_backend(args.backend)
    srv, port = serve_in_thread(backend)
    rss0 = th0 = hd0 = 0
    latencies = []
    per_pass = {}
    records = []
    verdict = 1
    try:
        for i, (sentence, passed) in enumerate(iter_units(args), 1):
            started = time.perf_counter()
            try:
                status, _ = post_json(port, "/translate",
                                      {"text": sentence, "source": args.src, "target": args.tgt})
            except OSError as exc:
                took = time.perf_counter() - started
                # The service stopped answering mid-batch. That is the failure external item 004
                # reported -- not an accident of this probe -- so it is recorded as a fact with
                # its sequence number, and the finally below writes the curve that led up to it.
                # Letting the traceback escape instead would throw away the one run worth having.
                records.append({"n": i, "pass": passed + 1, "error": type(exc).__name__,
                                "seconds": round(took, 4)})
                print("request %d (pass %d) never came back: %s -- stopping, after saving every"
                      " record measured up to here" % (i, passed + 1, exc))
                return 1
            took = time.perf_counter() - started
            records.append({"n": i, "pass": passed + 1, "status": status,
                            "seconds": round(took, 4)})
            latencies.append(took)
            per_pass.setdefault(passed, []).append(took)
            if status != 200:
                print("request %d returned %d -- stopping the run" % (i, status))
                break
            if i % args.every == 0 or i == 1:
                rss, th, hd = sample()
                if i == 1:
                    rss0, th0, hd0 = rss, th, hd
                print("  n=%-5d pass=%d rss=%s threads=%-3s handles=%-4s last=%.2fs"
                      % (i, passed + 1, mem(rss), fmt(th), fmt(hd), latencies[-1]))
        rss, th, hd = sample()
        report("load/%s %s->%s" % (args.backend, args.src, args.tgt), latencies,
               rss0, rss)
        verdict = pass_report("load/%s %s->%s" % (args.backend, args.src, args.tgt), per_pass)
        print("  deltas since first sample: threads=%s handles=%s"
              % (delta(th0, th), delta(hd0, hd)))
        return verdict
    finally:
        dump_run(args.out_json, run_meta(args, "load"), records)
        srv.shutdown()
        srv.server_close()


def run_rotate(args):
    backend = ct2_sidecar.make_backend(args.backend)
    srv, port = serve_in_thread(backend)
    try:
        costs = {}
        for tgt in args.langs.split(","):
            for _ in range(args.per):
                started = time.perf_counter()
                post_json(port, "/translate", {"text": args.text, "source": args.src,
                                               "target": tgt})
                costs.setdefault(tgt, []).append(time.perf_counter() - started)
            rss, th, hd = sample()
            print("  %-4s mean=%.2fs  rss=%s threads=%s handles=%s"
                  % (tgt, statistics.mean(costs[tgt]), mem(rss), fmt(th), fmt(hd)))
        print("revisit (accumulation would make these slower):")
        for tgt in args.langs.split(",")[:2]:
            started = time.perf_counter()
            post_json(port, "/translate", {"text": args.text, "source": args.src,
                                           "target": tgt})
            again = time.perf_counter() - started
            before = statistics.mean(costs[tgt])
            print("  %-4s first=%.2fs revisit=%.2fs ratio=%.2f%s"
                  % (tgt, before, again, again / before,
                     "  <-- slower" if again > before * RETENTION_LIMIT else ""))
        print("  spread across scripts: %.2fs -> %.2fs (%.2fx)"
              % (min(statistics.mean(v) for v in costs.values()),
                 max(statistics.mean(v) for v in costs.values()),
                 max(statistics.mean(v) for v in costs.values())
                 / min(statistics.mean(v) for v in costs.values())))
    finally:
        srv.shutdown()
        srv.server_close()


def run_hard(args):
    backend = ct2_sidecar.make_backend(args.backend)
    srv, port = serve_in_thread(backend)
    try:
        for label, text, src, tgt in HARD_CASES:
            started = time.perf_counter()
            status, body = post_json(port, "/translate",
                                     {"text": text, "source": src, "target": tgt})
            took = time.perf_counter() - started
            out = (body.get("text") or body.get("error") or "")[:44]
            print("  %-26s %-4s->%-3s %6.2fs %4d chars  %s"
                  % (label, src, tgt, took, len(body.get("text") or ""), out))
            if took >= 25.0:
                print("      ^ close to a stuck decode; check max_decoding_length")
        rss, th, hd = sample()
        print("after all cases: rss=%s threads=%s handles=%s" % (mem(rss), fmt(th), fmt(hd)))
    finally:
        srv.shutdown()
        srv.server_close()


def run_reload(args):
    """A long batch that resets itself, split into what drifts and what pays for the reset."""
    backend = ct2_sidecar.make_backend(args.backend)
    srv, port = serve_in_thread(backend)
    steady = []
    steady_by_pass = {}
    after_reset = []
    resets = []
    records = []
    rss0 = last_rss = None
    verdict = 1
    try:
        pending = False                      # the next call inherits a cold engine
        for i, (sentence, passed) in enumerate(iter_units(args), 1):
            if args.every > 0 and i > 1 and (i - 1) % args.every == 0:
                before = sample()[0]
                started = time.perf_counter()
                try:
                    status, body = post_json(port, "/reload", {}, timeout=300.0)
                except OSError as exc:
                    print("  /reload at n=%d never came back: %s -- the service died on a reset,"
                          " which is exactly what this mode is here to test" % (i - 1, exc))
                    records.append({"n": i - 1, "kind": "reload", "error": type(exc).__name__})
                    return 1
                took = time.perf_counter() - started
                after = sample()[0]
                if status != 200:
                    print("  /reload at n=%d returned %d: %s -- stopping, because a run that never"
                          " reset anything cannot say what a reset costs" % (i - 1, status, body))
                    if body.get("error") == "reload_unsupported":
                        print("      this backend holds nothing to reload (mock has no engine);"
                              " this mode needs --backend madlad or --backend ct2")
                    break
                resets.append((i - 1, took, before, after, len(body.get("discarded") or [])))
                records.append({"n": i - 1, "kind": "reload", "status": status,
                                "seconds": round(took, 4),
                                "rss_before_mb": round(before) if before >= 0 else None,
                                "rss_after_mb": round(after) if after >= 0 else None})
                print("  reset after n=%-4d rss %s -> %s (%s back), endpoint %.2fs, "
                      "discarded %d"
                      % (i - 1, mem(before), mem(after), reclaimed(before, after), took,
                         len(body.get("discarded") or [])))
                last_rss = after
                pending = True
            started = time.perf_counter()
            try:
                status, _ = post_json(port, "/translate",
                                      {"text": sentence, "source": args.src, "target": args.tgt})
            except OSError as exc:
                took = time.perf_counter() - started
                records.append({"n": i, "kind": "translate", "pass": passed + 1,
                                "error": type(exc).__name__, "seconds": round(took, 4)})
                print("request %d (pass %d) never came back: %s -- stopping, after saving every"
                      " record measured up to here" % (i, passed + 1, exc))
                return 1
            took = time.perf_counter() - started
            records.append({"n": i, "kind": "translate", "pass": passed + 1,
                            "status": status, "seconds": round(took, 4)})
            if status != 200:
                print("request %d returned %d -- stopping the run" % (i, status))
                break
            if pending:
                after_reset.append(took)
            else:
                steady.append(took)
                steady_by_pass.setdefault(passed, []).append(took)
            pending = False
            if rss0 is None:
                rss0 = sample()[0]
            last_rss = sample()[0]

        print("\nreload/%s %s->%s: n=%d  steady=%d  calls right after a reset=%d"
              % (args.backend, args.src, args.tgt, len(steady) + len(after_reset),
                 len(steady), len(after_reset)))
        # Passes rather than halves: the first half of a mixed stream against its second half
        # compares different sentences as well as different times, while pass N against pass 1 is
        # the paired comparison item 004's ladder actually describes -- the same body of work
        # done again.
        verdict = pass_report("reload/%s %s->%s steady by pass"
                              % (args.backend, args.src, args.tgt), steady_by_pass)
        if after_reset:
            print("  first call after a reset: mean=%.2fs max=%.2fs "
                  "(the price of the reset, not drift)"
                  % (statistics.mean(after_reset), max(after_reset)))
        if resets:
            got_back = [reclaimed(r[2], r[3]) for r in resets
                        if r[2] >= 0 and r[3] >= 0]
            print("  resets=%d  endpoint mean=%.2fs  memory returned %s"
                  % (len(resets), statistics.mean(r[1] for r in resets),
                     ("mean=%s MB" % statistics.mean([int(g) for g in got_back])
                      if got_back else "unknown (no psutil on this host)")))
        print("  rss across the run: %s -> %s" % (mem(rss0), mem(last_rss)))
        return verdict
    finally:
        dump_run(args.out_json, run_meta(args, "reload"), records)
        srv.shutdown()
        srv.server_close()


def run_abandon(args):
    """What a request nobody is waiting for still costs, and who else it costs it on.

    D30 item 2 is the part of item 004 nobody has measured yet: a client that times out has
    not stopped the work. `ThreadingHTTPServer` gives every connection its own thread, and
    ctranslate2's decode call is C++ with no Python-visible way to interrupt it, so the
    handler runs to completion and then discovers the caller is gone. This measures three
    things a design decision needs: how much is spent after the caller leaves, whether that
    overlapping work slows the requests that are still wanted, and whether the abandoned
    handlers leave anything behind.

    Needs a real backend. On mock the whole decode is milliseconds, so the numbers would be
    a measurement of scheduling noise -- and a number that looks like a measurement but is
    not one is the thing this file keeps having to protect against.
    """
    if args.backend == "mock":
        # Before building anything: refusing a mode is no reason to load a checkpoint.
        print("  abandon measures a decode that takes seconds; on mock it takes milliseconds,"
              " so the ratios would be noise. Run --backend madlad or --backend ct2.")
        return 1
    inner = ct2_sidecar.make_backend(args.backend)

    class _Timestamped:
        """Records what each decode was, and when it actually started and finished."""

        def __init__(self, wrapped):
            self._inner = wrapped
            self._lock = threading.Lock()
            self.spans = []
            self.errors = []

        def translate(self, text, source, target, glossary=None):
            started = time.perf_counter()
            try:
                out = self._inner.translate(text, source, target, glossary)
            except BaseException as exc:          # noqa: BLE001 - recorded, then re-raised
                with self._lock:
                    self.errors.append(type(exc).__name__)
                raise
            with self._lock:
                self.spans.append((text[:24], started, time.perf_counter()))
            return out

        def finished_of(self, key, at_least):
            """The end clock-reading of the newest decode of this sentence, or None.

            Matched by sentence rather than taking the last entry: several handlers can be
            decoding at once, so "the newest span" belongs to whichever finished first, not to
            the call this loop is asking about.
            """
            with self._lock:
                hits = [s for s in self.spans if s[0] == key]
            if len(hits) < at_least:
                return None
            return hits[-1][1], hits[-1][2], len(hits)

        def __getattr__(self, name):        # the rest of the duck-typed backend contract
            return getattr(self._inner, name)

    backend = _Timestamped(inner)
    srv, port = serve_in_thread(backend)

    def call(timeout, sentence):
        """One request with a caller-side deadline.

        Returns (gave_up, seconds_spent_waiting, moment_the_caller_left_as_a_clock_reading).
        The last one is an absolute reading on purpose: a duration and a clock reading are not
        the same kind of number, and subtracting one from the other produced "908850 s still
        spent" on the first run of this probe -- a figure absurd enough to be caught by eye,
        which is exactly why the units are named in the signature.
        """
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        started = time.perf_counter()
        try:
            conn.request("POST", "/translate",
                         json.dumps({"text": sentence, "source": args.src, "target": args.tgt}),
                         headers={"Content-Type": "application/json"})
            conn.getresponse().read()
            return False, time.perf_counter() - started, time.perf_counter()
        except OSError:
            # The caller is gone at this instant; the service may still be working.
            return True, time.perf_counter() - started, time.perf_counter()
        finally:
            conn.close()

    sentences = corpus_lines(args.corpus) or [args.text]
    try:
        print("  warm-up (also loads the checkpoint)")
        gave_up, warm_t, _left = call(600.0, sentences[0])
        print("  first call %.2fs, gave up=%s" % (warm_t, gave_up))

        base = []
        base_gave_up = 0
        for i in range(args.per):
            gave_up, took, _left = call(600.0, sentences[i % len(sentences)])
            base_gave_up += 1 if gave_up else 0
            base.append(took)
        quiet = statistics.mean(base)
        print("\n  baseline: %d patient calls, mean %.2fs, gave up on %d"
              % (len(base), quiet, base_gave_up))
        if base_gave_up:
            # A baseline containing abandoned calls is not a baseline: every ratio below would
            # be computed against a number that already includes the thing being measured.
            print("  the baseline itself timed out, so these ratios mean nothing; raise"
                  " --timeout or run on a quieter machine")
            return 1

        # The waste: give up mid-decode and watch what the service still spends.
        print("\n  abandoning %d calls at a %.2fs deadline (a decode costs ~%.1fs)"
              % (args.n, args.timeout, quiet))
        wasted = []
        decode_times = []
        for i in range(args.n):
            sentence = sentences[i % len(sentences)]
            key = sentence[:24]
            seen = len([s for s in backend.spans if s[0] == key])
            gave_up, took, left = call(args.timeout, sentence)
            if not gave_up:
                print("    call %d answered within the deadline; lower --timeout to abandon"
                      % (i + 1))
                continue
            deadline = time.perf_counter() + 600.0
            span = None
            while time.perf_counter() < deadline:
                span = backend.finished_of(key, seen + 1)
                if span:
                    break
                time.sleep(0.05)
            if not span:
                print("    call %d never finished inside 600 s -- that is itself the finding"
                      % (i + 1))
                continue
            started, finished = span[0], span[1]
            spent_after = finished - left          # both are clock readings, not durations
            wasted.append(max(0.0, spent_after))
            decode_times.append(finished - started)
            print("    abandoned call %d: caller left at %.2fs, service decoded %.2fs in total"
                  " and kept going %.2fs after the caller was gone"
                  % (i + 1, took, finished - started, max(0.0, spent_after)))

        # Does work nobody wants slow the work somebody does? Measured as a dose-response
        # curve with a k=0 control, because a single reading of "the next call was slower" is
        # an anecdote -- this repo has already been burned once by treating one observation as
        # a reproducible effect. Same run, same machine, same baseline: only k changes.
        print("\n  interference: how much does work still in flight slow the calls that are")
        print("    wanted? k = abandoned calls launched just before a group of patient ones")
        curve = []
        for k in sorted({0, 1, 2, min(4, max(1, args.burst)), max(4, args.burst)}):
            for i in range(k):
                call(args.timeout, sentences[(i + 11) % len(sentences)])
            group = []
            for i in range(args.per):
                gave_up, took, _l = call(600.0, sentences[(i + 23 + k) % len(sentences)])
                group.append(took)
            mean = statistics.mean(group)
            worst = max(group)
            curve.append((k, mean, worst))
            print("    k=%-2d mean=%5.2fs worst=%5.2fs  ratio mean=%.2f worst=%.2f%s"
                  % (k, mean, worst, mean / quiet, worst / quiet,
                     "   <-- past the line" if mean > quiet * RETENTION_LIMIT else ""))

        if len(curve) > 1:
            first, last = curve[0], curve[-1]
            print("  dose response: k=%d mean %.2fs -> k=%d mean %.2fs (%.2fx the control)"
                  % (first[0], first[1], last[0], last[1],
                     last[1] / first[1] if first[1] else 0.0))

        rss, th, hd = sample()
        print("\n  after the run: rss=%s threads=%s handles=%s; handler errors recorded=%s"
              % (mem(rss), fmt(th), fmt(hd), sorted(set(backend.errors)) or "none"))
        if wasted:
            print("  compute spent on callers who had left: mean %.2fs of the %.2fs decode,"
                  " i.e. %.0f%% of a translation thrown away per abandoned call"
                  % (statistics.mean(wasted), statistics.mean(decode_times),
                     100.0 * statistics.mean(wasted) / statistics.mean(decode_times)))
        print("  note: a handler that finished writing to a closed socket is the normal end"
              " state here -- the decode is C++ and has no Python-visible interrupt, so the"
              " disconnect only becomes visible afterwards.")
        return 0
    finally:
        srv.shutdown()
        srv.server_close()


class _Result:
    """A ctranslate2 batch result: this path only reads .hypotheses[0]."""

    def __init__(self, hypothesis):
        self.hypotheses = [hypothesis]


class _CountingCt2:
    """Fake ctranslate2: slow constructions, counted, never loaded from disk."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.built = 0
        self.live = 0
        self.peak = 0

    class _Translator:
        def __init__(self, owner):
            owner.live += 1
            owner.peak = max(owner.peak, owner.live)
            time.sleep(owner.seconds)
            owner.built += 1
            owner.in_flight_done()

        def translate_batch(self, tokens, **kwargs):
            return [_Result(["<2zh>", "ok"])]

    def in_flight_done(self):
        self.live -= 1

    def Translator(self, path, **kwargs):
        return self._Translator(self)


class _FakeSp:
    """sentencepiece stand-in: enough for the vocabulary probe and one decode."""

    def SentencePieceProcessor(self, model_file=None):
        return self._Proc()

    class _Proc:
        def encode(self, text, out_type=str):
            head = text.split(" ", 1)[0]
            if head.startswith("<2") and head.endswith(">"):
                return [head] + text.split(" ", 1)[1:]
            return text.split()

        def eos_id(self):
            return None

        def id_to_piece(self, piece_id):
            return "</s>"

        def decode(self, pieces):
            return " ".join(pieces)


def load_sidecar_rev(rev):
    """Import sidecar/ct2_sidecar.py as it stood at `rev`, as its own module."""
    src = subprocess.run(["git", "show", "%s:sidecar/ct2_sidecar.py" % rev],
                         capture_output=True, check=True).stdout
    tmp = pathlib.Path(tempfile.mkdtemp()) / ("ct2_sidecar_%s.py" % rev)
    tmp.write_bytes(src)
    import importlib.util
    spec = importlib.util.spec_from_file_location("ct2_sidecar_%s" % rev, str(tmp))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_race(args):
    """How many checkpoints does one cold burst build, in each revision?

    This is the D24 evidence: the revision the report measured built one 2.95 GB
    MADLAD checkpoint per in-flight handler, because _engine() was a lockless
    check-then-act and a client that times out has not stopped the handler it is
    waiting on.
    """
    import threading
    mods = {}
    for rev in args.rev.split(","):
        mods[rev] = load_sidecar_rev(rev)
    payload = {"text": SENTENCE, "source": "en", "target": "zh"}
    for name, mod in mods.items():
        for attempts, sequential in ((args.burst, False), (args.burst, True)):
            backend = mod.MadladBackend.__new__(mod.MadladBackend)   # no model on disk
            counter = _CountingCt2(args.load_seconds)
            sp = _FakeSp()
            backend._ct2 = counter
            backend._spm = sp
            backend._processor = sp.SentencePieceProcessor()
            backend._known_targets = set()
            backend._root = pathlib.Path(".")
            backend._translator = None
            srv, port = mod.serve_in_thread(backend)
            srv.handle_error = lambda *_: None       # clients here hang up on purpose
            statuses = {}
            try:
                def ask(timeout):
                    try:
                        code, _ = post_json(port, "/translate", payload, timeout=timeout)
                    except (TimeoutError, OSError):
                        code = "timeout"
                    statuses[code] = statuses.get(code, 0) + 1

                if sequential:
                    # Strictly sequential with a deadline shorter than the load:
                    # every abandoned handler is still working when the next arrives.
                    for _ in range(attempts):
                        ask(0.2)
                    ask(args.load_seconds + 10)
                else:
                    gate = threading.Barrier(attempts)

                    def worker():
                        gate.wait()
                        try:
                            post_json(port, "/translate", payload)
                            statuses[200] = statuses.get(200, 0) + 1
                        except OSError as exc:
                            statuses[repr(exc)] = statuses.get(repr(exc), 0) + 1

                    threads = [threading.Thread(target=worker) for _ in range(attempts)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
                # Constructors still asleep must finish before the count is read,
                # otherwise an overlap would be reported as a single build.
                time.sleep(args.load_seconds + 0.3)
                print("%s %s: requests=%d built=%d peak=%d  (2.95 GB each -> %.1f GB worst case)"
                      "  responses=%s"
                      % (name, "single-flight+timeout" if sequential else "concurrent",
                         attempts, counter.built, counter.peak, counter.built * 2.95, statuses))
            finally:
                srv.shutdown()
                srv.server_close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["load", "rotate", "hard", "reload", "race", "abandon"])
    ap.add_argument("--backend", default="madlad", choices=["mock", "ct2", "madlad"])
    ap.add_argument("--text", default=SENTENCE)
    ap.add_argument("--src", default="en")
    ap.add_argument("--tgt", default="zh")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--every", type=int, default=50)
    ap.add_argument("--langs", default=",".join(ROTATION))
    ap.add_argument("--per", type=int, default=3)
    ap.add_argument("--rev", default="02dc2bd,HEAD", help="comma-separated revisions for race")
    ap.add_argument("--burst", type=int, default=4)
    ap.add_argument("--load-seconds", type=float, default=0.5)
    ap.add_argument("--corpus", default=None,
                    help="a .src/.txt file, or a directory holding one; its sentences are used"
                         " in order and cycled, so a run measures a batch of distinct work like"
                         " item 004 did instead of one warm sentence")
    ap.add_argument("--out-json", default=None,
                    help="write every per-request record here, so a verdict can be re-checked"
                         " rather than trusted")
    ap.add_argument("--timeout", type=float, default=1.0,
                    help="abandon: caller-side deadline shorter than one decode, so the"
                         " client gives up while the service is still working")
    args = ap.parse_args()
    args.started_at = time.time()          # stamped here so a record says when the run began
    # A verdict, not just a printout: a caller scripting a release decision needs an exit code
    # to branch on, and modes that only measure return None, which becomes 0.
    sys.exit({"load": run_load, "rotate": run_rotate, "hard": run_hard,
              "reload": run_reload, "race": run_race, "abandon": run_abandon}[args.mode](args)
              or 0)


if __name__ == "__main__":
    main()
