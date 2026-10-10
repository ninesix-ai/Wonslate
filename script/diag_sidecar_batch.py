# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Sidecar batch diagnostics -- the measurements behind defect D24 / D30.

Run from the repository root. Five modes, one variable each:

  load    N sequential requests against one backend, sampling the process while
          it runs. Answers "does latency or memory grow with request count?"
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
          line needs is the steady drift alone.
  race    drive two revisions of the sidecar with a fake ctranslate2 and count
          how many translators a cold burst builds. Answers "was the reported
          slowdown a duplicated 2.95 GB checkpoint?" No real model is loaded, so
          it is fast and deterministic.

Nothing here is a gate: it measures, it does not assert. Numbers printed by
`load`, `rotate` and `reload` are environment-dependent by design -- the ledger's counting
rule keeps a runtime-precondition column for exactly that reason (REQ-F3).

Dependencies: psutil (process sampling) and git (only for `race --rev`).
"""
import argparse
import http.client
import json
import os
import pathlib
import statistics
import subprocess
import sys
import tempfile
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


def sample():
    """(rss_mb, threads, handles_or_fds) of this process; the backend runs in it."""
    import psutil
    p = psutil.Process(os.getpid())
    io = p.memory_info().rss / 1048576.0
    if hasattr(p, "num_handles"):
        third = p.num_handles()
    else:
        third = p.num_fds()
    return io, p.num_threads(), third


def report(tag, latencies, first_rss, last_rss):
    """Print the ratio that a ladder would show, so its absence is also evidence."""
    head = statistics.mean(latencies[:min(100, len(latencies))])
    tail = statistics.mean(latencies[-min(100, len(latencies)):])
    print("%s: n=%d  head_mean=%.2fs tail_mean=%.2fs ratio=%.2f  rss %.0f->%.0f MB"
          % (tag, len(latencies), head, tail, (tail / head if head else 0.0),
             first_rss, last_rss))


def run_load(args):
    backend = ct2_sidecar.make_backend(args.backend)
    srv, port = serve_in_thread(backend)
    try:
        rss0 = th0 = hd0 = 0
        latencies = []
        for i in range(1, args.n + 1):
            started = time.perf_counter()
            status, _ = post_json(port, "/translate",
                                  {"text": args.text, "source": args.src, "target": args.tgt})
            latencies.append(time.perf_counter() - started)
            if status != 200:
                print("request %d returned %d -- stopping the run" % (i, status))
                break
            if i % args.every == 0 or i == 1:
                rss, th, hd = sample()
                if i == 1:
                    rss0, th0, hd0 = rss, th, hd
                print("  n=%-5d rss=%6.0fMB threads=%-3d handles=%-4d last=%.2fs"
                      % (i, rss, th, hd, latencies[-1]))
        rss, th, hd = sample()
        report("load/%s %s->%s" % (args.backend, args.src, args.tgt), latencies,
               rss0, rss)
        print("  deltas since first sample: threads+%d handles+%d"
              % (th - th0, hd - hd0))
    finally:
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
            print("  %-4s mean=%.2fs  rss=%6.0fMB threads=%d handles=%d"
                  % (tgt, statistics.mean(costs[tgt]), rss, th, hd))
        print("revisit (accumulation would make these slower):")
        for tgt in args.langs.split(",")[:2]:
            started = time.perf_counter()
            post_json(port, "/translate", {"text": args.text, "source": args.src,
                                           "target": tgt})
            again = time.perf_counter() - started
            before = statistics.mean(costs[tgt])
            print("  %-4s first=%.2fs revisit=%.2fs ratio=%.2f%s"
                  % (tgt, before, again, again / before,
                     "  <-- slower" if again > before * 1.15 else ""))
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
        print("after all cases: rss=%.0fMB threads=%d handles=%d" % (rss, th, hd))
    finally:
        srv.shutdown()
        srv.server_close()


def run_reload(args):
    """A long batch that resets itself, split into what drifts and what pays for the reset."""
    backend = ct2_sidecar.make_backend(args.backend)
    srv, port = serve_in_thread(backend)
    try:
        steady = []
        after_reset = []
        resets = []
        rss0 = last_rss = None
        pending = False                      # the next call inherits a cold engine
        for i in range(1, args.n + 1):
            if args.every > 0 and i > 1 and (i - 1) % args.every == 0:
                before = sample()[0]
                started = time.perf_counter()
                status, body = post_json(port, "/reload", {}, timeout=300.0)
                took = time.perf_counter() - started
                after = sample()[0]
                if status != 200:
                    print("  /reload at n=%d returned %d: %s -- stopping"
                          % (i - 1, status, body))
                    break
                resets.append((i - 1, took, before, after, len(body.get("discarded") or [])))
                print("  reset after n=%-4d rss %6.0f -> %6.0f MB (%5.0f back), endpoint %.2fs, "
                      "discarded %d"
                      % (i - 1, before, after, before - after, took,
                         len(body.get("discarded") or [])))
                last_rss = after
                pending = True
            started = time.perf_counter()
            status, _ = post_json(port, "/translate",
                                  {"text": args.text, "source": args.src, "target": args.tgt})
            took = time.perf_counter() - started
            if status != 200:
                print("request %d returned %d -- stopping the run" % (i, status))
                break
            (after_reset if pending else steady).append(took)
            pending = False
            if rss0 is None:
                rss0 = sample()[0]
            last_rss = sample()[0]

        print("\nreload/%s %s->%s: n=%d  steady=%d  calls right after a reset=%d"
              % (args.backend, args.src, args.tgt, len(steady) + len(after_reset),
                 len(steady), len(after_reset)))
        if len(steady) >= 4:
            # First half against second half, and said out loud when the run is too short for
            # that comparison to mean anything: a retention line needs drift, and drift needs
            # enough calls to have a second half at all.
            half = len(steady) // 2
            first_mean = statistics.mean(steady[:half])
            second_mean = statistics.mean(steady[half:])
            print("  steady drift: first_half_mean=%.3fs second_half_mean=%.3fs ratio=%.2f%s"
                  % (first_mean, second_mean, second_mean / first_mean if first_mean else 0.0,
                     "" if len(steady) >= 100 else "   <-- %d samples, too few for a line"
                     % len(steady)))
        if after_reset:
            print("  first call after a reset: mean=%.2fs max=%.2fs "
                  "(the price of the reset, not drift)"
                  % (statistics.mean(after_reset), max(after_reset)))
        if resets:
            print("  resets=%d  endpoint mean=%.2fs  memory returned mean=%.0f MB"
                  % (len(resets), statistics.mean(r[1] for r in resets),
                     statistics.mean(r[2] - r[3] for r in resets)))
        print("  rss across the run: %.0f -> %.0f MB" % (rss0 or 0.0, last_rss or 0.0))
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
    ap.add_argument("mode", choices=["load", "rotate", "hard", "reload", "race"])
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
    args = ap.parse_args()
    {"load": run_load, "rotate": run_rotate, "hard": run_hard,
     "reload": run_reload, "race": run_race}[args.mode](args)


if __name__ == "__main__":
    main()
