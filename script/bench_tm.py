#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""TM hit-rate / latency benchmark for Wonslate (N-09 evidence run).

Why this exists
---------------
`docs/11` sells "the more you use it, the cheaper it gets", but every number in
its cost model used to be a directional estimate. This script produces the real
readings: how often the translation memory actually serves a request, what a hit
costs in latency, and how much of the traffic would still need an AI upgrade.

What is measured, and what is not
---------------------------------
* Only an **exact** TM hit counts as a hit. `pipeline.rs` L-1 hashes the whole
  input; the fuzzy/similar path only feeds few-shot examples, so it is not a hit.
* Requests run in **realtime** mode by default: it never attempts an AI upgrade,
  so the run stays offline and the local path is what is being measured. The
  share that *would* need an upgrade in full mode is derived from the recorded
  confidences and reported separately, clearly labelled as derived.
* Distillation is not exercised: with no AI engine in the loop the L3 path never
  runs, so the glossary does not grow. Only the TM flywheel is under test.

The repetition model is the dominant assumption
-----------------------------------------------
A TM only pays off when content repeats. Real workloads resend boilerplate,
recurring phrases and previously translated sentences, so the request stream
mixes new content with reuse. Two parameters define that mix, and both are
recorded in the report because neither is a property of the engine:

* `--new-ratio` - share of requests carrying content never seen before.
* the reuse distribution - a request that reuses picks a rank from
  Zipf(alpha = 1) over the **last `--reuse-window` distinct strings** (default
  200), so both recency and frequency bias the choice, the way re-translating a
  document or a section does. Ranks are sampled from that window's exact CDF.

`--new-ratio 1.0` is the floor case: it measures only how much the corpus itself
repeats. Note that this floor is not zero for a real corpus - Tatoeba contains
duplicate source sentences - so read it as a property of the corpus, not as an
engine result.

Usage
-----
    # smoke: 20 requests, no corpus download needed
    python script/bench_tm.py --corpus corpus.tsv --requests 20

    # the N-09 evidence run
    python script/bench_tm.py --corpus corpus.tsv --requests 10000 --new-ratio 0.30 \
        --out docs/evidence/n09-tm-bench.json

The corpus must be a TSV whose first two columns are the source and target
sentence (the Tatoeba-derived `cmn-eng.zip` layout). The corpus itself is NOT
vendored into this repository: it carries its own licence, and the benchmark only
reads it. Record its source and checksum alongside any published result.

Exit codes: 0 = ran, 1 = setup/run failure. A run always reports what it measured
even when the hit rate turns out to be zero.
"""

import argparse
import bisect
import ctypes
import hashlib
import json
import os
import pathlib
import random
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SIDECAR = ROOT / "sidecar" / "ct2_sidecar.py"

# Derived, not measured: the cloud price this project quotes for its cost model
# (docs/11 §9.3, Baidu 49 CNY per million characters). Kept here so the saving
# figure can be recomputed when the price is revised.
CLOUD_CNY_PER_MCHAR = 49.0

# Local confidence below which `router.rs` escalates to the AI engine in full mode.
FULL_MODE_UPGRADE_THRESHOLD = 0.85


def _p(*args, **kw):
    """Everything to stderr, like the rest of the repo's tooling."""
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*args, **kw)


# ---------------------------------------------------------------- corpus

def load_corpus(path: pathlib.Path) -> list[str]:
    """Read the source-side sentences (column 0) from a Tatoeba-style TSV."""
    rows: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if not parts or not parts[0].strip():
                continue
            text = parts[0].strip()
            # Skip degenerate rows: the engine must receive real sentences, and
            # a one-word input would measure something else entirely.
            if len(text) < 8 or len(text) > 200:
                continue
            rows.append(text)
    return rows


def _zipf_cdf(n: int, alpha: float) -> list[float]:
    """Exact CDF of Zipf(alpha) over ranks 0..n-1 (weight 1/(rank+1)^alpha)."""
    weights = [1.0 / ((k + 1) ** alpha) for k in range(n)]
    total = sum(weights)
    cdf, acc = [], 0.0
    for w in weights:
        acc += w / total
        cdf.append(acc)
    return cdf


def build_stream(corpus: list[str], requests: int, new_ratio: float, seed: int,
                 reuse_window: int = 200, alpha: float = 1.0) -> tuple[list[str], int, dict]:
    """Build the request stream: `1 - new_ratio` of the traffic is reuse.

    Reuse samples a rank from Zipf(alpha) over the last `reuse_window` distinct
    strings, so recently and frequently used content dominates. `reuse_window`
    bounds the tail: an unbounded Zipf over thousands of items would put almost
    every reuse on the newest string, which is not how re-translation behaves.
    Returns the stream, the count of first-time content, and the model record.
    """
    rng = random.Random(seed)
    if not corpus:
        raise SystemExit("corpus is empty after filtering")
    cdf = _zipf_cdf(reuse_window, alpha)
    pool: list[str] = []
    stream: list[str] = []
    fresh = 0
    next_corpus = 0

    for _ in range(requests):
        want_new = (not pool) or rng.random() < new_ratio
        if want_new and next_corpus >= len(corpus):
            # Corpus exhausted: fall back to reuse rather than repeating a fixed
            # sentence, which would inflate the hit rate artificially.
            want_new = False
        if want_new:
            text = corpus[next_corpus]
            next_corpus += 1
            pool.append(text)
            stream.append(text)
            fresh += 1
            continue
        if not pool:
            break
        window = pool[-reuse_window:]
        idx = min(bisect.bisect_left(cdf, rng.random()), len(window) - 1)
        stream.append(window[idx])

    model = {
        "new_ratio": new_ratio,
        "reuse_distribution": f"zipf(alpha={alpha})",
        "reuse_window": reuse_window,
        "seed": seed,
    }
    return stream, fresh, model


# ---------------------------------------------------------------- sidecar

def sidecar_healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_sidecar(args, child_env: dict) -> subprocess.Popen | None:
    """Start the ct2 sidecar unless one is already listening; return the handle.

    `child_env` must be the environment captured BEFORE the scratch data dir is
    applied: the sidecar resolves its model directory from that same variable
    (`default_model_dir` in ct2_sidecar.py), so inheriting the override would send
    it looking for models inside the scratch directory. The isolation is only
    meant to keep the benchmark out of the user's translation memory.

    A sidecar we started is ours to kill; an externally started one is left alone.
    """
    if sidecar_healthy(args.sidecar_url):
        _p(f"[bench] reusing the sidecar already listening on {args.sidecar_url}")
        return None
    if not SIDECAR.is_file():
        raise SystemExit(f"sidecar not found: {SIDECAR}")
    port = args.sidecar_url.rstrip("/").rsplit(":", 1)[-1]
    cmd = [sys.executable, str(SIDECAR), "--backend", "ct2", "--port", port]
    if args.model_dir:
        cmd += ["--model-dir", str(args.model_dir)]
    _p(f"[bench] starting sidecar: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=child_env)
    deadline = time.time() + 90
    while time.time() < deadline:
        if sidecar_healthy(args.sidecar_url):
            _p("[bench] sidecar ready")
            return proc
        if proc.poll() is not None:
            raise SystemExit(f"sidecar exited early with code {proc.returncode}")
        time.sleep(0.5)
    proc.kill()
    raise SystemExit("sidecar did not become healthy within 90s")


# ---------------------------------------------------------------- engine

def find_dll() -> pathlib.Path:
    for name in ("translator_engine.dll", "libtranslator_engine.so", "libtranslator_engine.dylib"):
        p = ROOT / "translator-engine" / "target" / "release" / name
        if p.exists():
            return p
    raise SystemExit("engine library not found; run: python script/build.py")


class Engine:
    """Minimal ctypes wrapper around the three FFI calls this benchmark needs."""

    def __init__(self, dll: pathlib.Path):
        self.lib = ctypes.cdll.LoadLibrary(str(dll))
        self.lib.tt_init.argtypes = [ctypes.c_char_p]
        self.lib.tt_init.restype = ctypes.c_void_p
        self.lib.tt_shutdown.argtypes = []
        self.lib.tt_shutdown.restype = None
        self.lib.tt_translate_full.argtypes = [ctypes.c_char_p]
        self.lib.tt_translate_full.restype = ctypes.c_void_p
        self.lib.tt_free_string.argtypes = [ctypes.c_void_p]
        self.lib.tt_free_string.restype = None

    def _take(self, ptr) -> str:
        if not ptr:
            return ""
        try:
            return ctypes.string_at(ptr).decode("utf-8", errors="replace")
        finally:
            self.lib.tt_free_string(ptr)

    def init(self, config_json: str = "{}") -> dict:
        return json.loads(self._take(self.lib.tt_init(config_json.encode("utf-8"))) or "{}")

    def shutdown(self):
        self.lib.tt_shutdown()

    def translate(self, text: str, source: str, target: str, mode: str) -> dict:
        req = {
            "input": text,
            "source_lang": source,
            "target_lang": target,
            "mode": mode,
            "privacy": False,
            "use_tm": True,
        }
        raw = self._take(self.lib.tt_translate_full(json.dumps(req).encode("utf-8")))
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": "BENCH_PARSE", "message": raw[:200]}


# ---------------------------------------------------------------- analysis

def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * q), len(ordered) - 1)
    return float(ordered[idx])


def _group(values: list[float]) -> dict:
    """Summarise one metric group; the distinct count exposes a flat score."""
    if not values:
        return {"n": 0, "mean": 0.0, "min": 0.0, "max": 0.0, "distinct_values": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "distinct_values": len({round(v, 4) for v in values}),
    }


def _latency(values: list[int]) -> dict:
    return {"n": len(values), "p50": percentile(values, 0.50), "p95": percentile(values, 0.95),
            "mean": round(statistics.fmean(values), 1) if values else 0.0}


def analyse(records: list[dict], stream_len: int, fresh: int, chars: int) -> dict:
    hits = [r for r in records if r["source"] == "tm_hit"]
    misses = [r for r in records if r["source"] != "tm_hit"]
    ok_misses = [r for r in misses if r["ok"]]
    # Misses are NOT all local: in full mode some are served by the AI engine and
    # some fall back. Labelling every miss "local" would misreport the AI path as
    # an on-device result, so each group is reported separately.
    local = [r for r in ok_misses if r["source"] == "local"]
    ai = [r for r in ok_misses if r["source"] == "ai_upgraded"]
    fallback = [r for r in ok_misses if r["source"] == "fallback"]
    by_source: dict[str, int] = {}
    for r in records:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1

    windows = 10
    curve = []
    for w in range(windows):
        lo = stream_len * w // windows
        hi = stream_len * (w + 1) // windows
        chunk = records[lo:hi]
        if not chunk:
            continue
        chunk_hits = sum(1 for r in chunk if r["source"] == "tm_hit")
        curve.append({
            "window": f"{lo + 1}-{hi}",
            "requests": len(chunk),
            "hit_rate": round(chunk_hits / len(chunk), 4),
        })

    floor_refused = sum(1 for r in misses if r.get("floor_refused"))
    hit_chars = sum(r["chars"] for r in hits)
    # Derived, not observed. Over `local`, not `misses`: an AI-served miss has
    # already escalated, so counting it again as "would escalate" would
    # double-count a decision that was actually taken.
    would_upgrade = [r for r in local if r["confidence"] < FULL_MODE_UPGRADE_THRESHOLD]

    return {
        "requests": len(records),
        "unique_content_requests": fresh,
        "reuse_share": round(1 - fresh / len(records), 4) if records else 0.0,
        "tm_hit": {
            "count": len(hits),
            "rate": round(len(hits) / len(records), 4) if records else 0.0,
            "characters_served": hit_chars,
            "characters_share": round(hit_chars / chars, 4) if chars else 0.0,
            # Not zero when the memory is full: the quality floor turned these away.
            "refused_by_quality_floor": floor_refused,
        },
        "by_source": by_source,
        "latency_ms": {
            "tm_hit": _latency([r["latency_ms"] for r in hits]),
            "local": _latency([r["latency_ms"] for r in local]),
            "ai_upgraded": _latency([r["latency_ms"] for r in ai]),
            "fallback": _latency([r["latency_ms"] for r in fallback]),
        },
        "confidence": {
            "local": _group([r["confidence"] for r in local]),
            "ai_upgraded": _group([r["confidence"] for r in ai]),
            "fallback": _group([r["confidence"] for r in fallback]),
        },
        "derived": {
            # Of the results the local engine actually produced, how many sit below
            # the threshold full mode escalates at. In realtime mode this is the
            # population that a full-mode switch would send to the AI engine.
            "local_results_below_full_mode_threshold": len(would_upgrade),
            "local_results_below_full_mode_threshold_share":
                round(len(would_upgrade) / len(local), 4) if local else 0.0,
            "cloud_cost_avoided_cny_at_49_per_mchar":
                round(hit_chars / 1_000_000 * CLOUD_CNY_PER_MCHAR, 4),
        },
        "hit_rate_curve_by_decile": curve,
    }


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Wonslate TM hit-rate benchmark (N-09).")
    ap.add_argument("--corpus", required=True, type=pathlib.Path,
                    help="TSV with source sentence in column 0")
    ap.add_argument("--requests", type=int, default=10000)
    ap.add_argument("--new-ratio", type=float, default=0.30,
                    help="share of requests carrying unseen content (rest is reuse)")
    ap.add_argument("--seed", type=int, default=20260929)
    ap.add_argument("--reuse-window", type=int, default=200,
                    help="reuse picks a Zipf rank over the last N distinct strings")
    ap.add_argument("--mode", default="realtime", choices=["realtime", "full"])
    ap.add_argument("--source-lang", default="en")
    ap.add_argument("--target-lang", default="zh")
    ap.add_argument("--sidecar-url", default="http://127.0.0.1:11435")
    ap.add_argument("--data-dir", type=pathlib.Path,
                    help="scratch TM data dir; use a fresh one so the real TM is untouched")
    ap.add_argument("--model-dir", type=pathlib.Path,
                    help="Argos package dir for the sidecar (default: the sidecar's own resolution)")
    ap.add_argument("--progress-every", type=int, default=500)
    ap.add_argument("--out", type=pathlib.Path, help="write the report JSON here")
    args = ap.parse_args()

    if not args.corpus.is_file():
        raise SystemExit(f"corpus not found: {args.corpus}")
    corpus = load_corpus(args.corpus)
    _p(f"[bench] corpus: {args.corpus.name} ({len(corpus)} usable rows, "
       f"sha256={hashlib.sha256(args.corpus.read_bytes()).hexdigest()[:16]})")

    stream, fresh, stream_model = build_stream(
        corpus, args.requests, args.new_ratio, args.seed, args.reuse_window)
    _p(f"[bench] stream: {len(stream)} requests, {fresh} first-time "
       f"(reuse {1 - fresh / len(stream):.0%})")

    # Start the sidecar with the untouched environment, then point the in-process
    # Rust core at a scratch directory below.
    base_env = dict(os.environ)
    proc = ensure_sidecar(args, base_env)

    # A scratch data dir keeps the benchmark out of the user's real TM. The Rust
    # core has no data-dir argument, so the env var is the only way in; it is read
    # when the library resolves its directory, before tt_init opens the store.
    data_dir = args.data_dir or (pathlib.Path(base_env.get("TEMP", ".")) / "wonslate-bench-data")
    for f in ("translator_tm.json", "glossary.json"):
        (data_dir / "data" / f).unlink(missing_ok=True)
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    os.environ["WONSLATE_DATA_DIR"] = str(data_dir)
    _p(f"[bench] scratch TM data dir: {data_dir}")
    records: list[dict] = []
    chars = 0
    try:
        eng = Engine(find_dll())
        ack = eng.init("{}")
        if not ack.get("ok"):
            raise SystemExit(f"tt_init failed: {ack}")
        try:
            started = time.time()
            for i, text in enumerate(stream, 1):
                r = eng.translate(text, args.source_lang, args.target_lang, args.mode)
                chars += len(text)
                records.append({
                    "source": r.get("source", "?"),
                    "ok": bool(r.get("ok")),
                    "engine": r.get("engine", ""),
                    "latency_ms": int(r.get("latency_ms", 0)),
                    "confidence": float(r.get("confidence", 0.0)),
                    "chars": len(text),
                    # A cache entry below the mode's serving floor is re-translated and the
                    # response says so. Without recording it, a zero hit rate has two very
                    # different explanations and no way to tell them apart.
                    "floor_refused": "floor" in (r.get("message") or ""),
                })
                if i % args.progress_every == 0:
                    hits = sum(1 for x in records if x["source"] == "tm_hit")
                    _p(f"[bench] {i}/{len(stream)}  hit={hits / i:.1%}  "
                       f"elapsed={time.time() - started:.0f}s")
        finally:
            eng.shutdown()
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=10)
            _p("[bench] sidecar stopped")

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "corpus": {"file": args.corpus.name, "rows": len(corpus),
                   "sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest()},
        "config": {"requests": len(stream), **stream_model,
                   "mode": args.mode, "pair": f"{args.source_lang}->{args.target_lang}",
                   "cloud_cny_per_mchar": CLOUD_CNY_PER_MCHAR},
        "result": analyse(records, len(stream), fresh, chars),
    }
    _p("")
    _p(json.dumps(report["result"], indent=2, ensure_ascii=False))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        _p(f"[bench] report written to {args.out}")

    if report["result"]["tm_hit"]["count"] == 0:
        refused = report["result"]["tm_hit"]["refused_by_quality_floor"]
        if refused:
            _p(f"[bench] note: no TM hits, but {refused} cached entries were refused by "
               "this mode's quality floor - the memory was NOT empty. Report the floor as "
               "the cause; do not attribute it to the corpus.")
        else:
            _p("[bench] note: no TM hits at all - the corpus/repetition model cannot "
               "exercise the memory. Report it as such rather than as a hit rate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
