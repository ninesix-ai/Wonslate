#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""run_av_baseline.py -- one-shot AV-domain baseline runner.

Wraps the whole S12 measurement loop so a first-time user gets from
"clone + build" to "docs/av-domain-benchmark.md has real numbers"
in one command:

    1. preflight: verify cdylib, corpus, seed pack, argos/madlad models,
       ollama endpoint, and (if you want live COMET) a writable HF
       cache. Any missing item prints an actionable fix line, not a
       stack trace.
    2. start argos + madlad sidecars as subprocesses and wait on
       their /health endpoints. If already running, reuse them.
    3. install the AV seed pack (idempotent; already-installed rows
       bump confidence to 1.0, do not duplicate).
    4. run bench_domain_av.py six times: {argos, madlad, ollama-qwen}
       x {domain="", domain=av}, all zh->en over the 300-pair corpus.
    5. score_comet.py on every evidence JSON.
    6. generate docs/av-domain-benchmark.md from evidence + template,
       filling the TL;DR, section 1/2/3 tables and the four
       hypotheses. Section 4 (reproduction) and 5 (limitations) are
       written from a static block so they stay in sync.
    7. print the diff summary and the git commands to review + commit.

Modes:
    (default)              full 300-pair run + COMET + report
    --quick                first 20 pairs, no COMET; smoke-test the wiring
    --preflight-only       check env, do not run anything
    --report-only          regenerate docs/av-domain-benchmark.md from
                           whatever evidence JSONs already exist
    --only-baseline        skip the --domain av pass (useful when the
                           seed pack cannot be installed on this host)
    --skip-comet           run benches but skip COMET scoring
    --engines argos,madlad subset of the three default engines

Windows / Linux / macOS entry points: run_av_baseline.bat / .sh call
this file with the same arguments.

Exit codes:
    0  every step finished; report is on disk
    1  preflight failed (message names what to install / start)
    2  a subprocess (sidecar / bench / comet) exited non-zero
    3  report generation hit inconsistent evidence
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO = pathlib.Path(__file__).resolve().parent.parent
# One source of truth for evidence file names. The runner verifies a file appeared after each
# bench call, so a second copy of that rule here would eventually disagree with the writer --
# and disagree in the direction of aborting a multi-hour matrix run mid-way.
sys.path.insert(0, str(REPO / "script"))
from bench_domain_av import (evidence_name, ollama_model_identity,  # noqa: E402
                             DEFAULT_OLLAMA_MODEL, _ffi_id)
from sidecar_identity import SidecarIdentityError, wait_until_ours  # noqa: E402  (D73)
EVIDENCE_DIR = REPO / "docs" / "evidence"
REPORT_PATH = REPO / "docs" / "av-domain-benchmark.md"
RANGES_PATH = REPO / "script" / "av_domain_ranges.json"
PACK_PATH = REPO / "docs" / "glossary-packs" / "av-zh-en.json"
CORPUS_DIR = REPO / "script" / "eval-data" / "av-zh-en"
SIDECAR_PY = REPO / "sidecar" / "ct2_sidecar.py"
BENCH_PY = REPO / "script" / "bench_domain_av.py"
INSTALL_PY = REPO / "script" / "install_glossary_pack.py"
SCORE_PY = REPO / "script" / "score_comet.py"
# Every runner print is mirrored here so a double-click that closes its
# window on exit still leaves the full Preflight/Bench/Summary trace on
# disk. A first-time user losing the console before reading the Summary
# was the reported failure; this is the durable fix.
RUN_LOG = REPO / "run_av_baseline.log"

LIB_NAME = ("translator_engine.dll" if os.name == "nt"
            else "libtranslator_engine.dylib" if sys.platform == "darwin"
            else "libtranslator_engine.so")
CDLL_PATHS = [
    REPO / "translator-engine" / "target" / "release" / LIB_NAME,
    REPO / "translator-engine" / "target" / "debug" / LIB_NAME,
]

SIDECAR_PORTS = {"argos": 11435, "madlad": 11436}
# The bench engine id is not the same string as the sidecar's --backend
# flag: sidecar/ct2_sidecar.py uses "ct2" for the argos packages and
# "madlad" for the MADLAD checkpoint. The mapping keeps one source of truth.
SIDECAR_BACKENDS = {"argos": "ct2", "madlad": "madlad"}
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_MODEL = DEFAULT_OLLAMA_MODEL   # the shipped default; an arm may dial another
DIRECTIONS = ("zh-en", "en-zh")   # en->zh was derived under S12 T2; both corpora are in the repo
ENGINES = ("argos", "madlad", "ollama-qwen")
# Which engines can act on a glossary term context at all, keyed by the FFI id the Rust side
# knows. argos and madlad are served by sidecar.rs, which deliberately does NOT override
# `accepts_term_context`: the CT2 and MADLAD checkpoints read the term array and ignore it,
# and their docstrings say so. The published legacy artifacts measured it -- the scoped arm
# came out byte-identical to the baseline in 300/300 rows for both.
#
# tests/test_run_av_baseline.py parses engine/mod.rs and compares, so this cannot drift into
# the dangerous direction: a table that still says "not applicable" after a backend started
# applying terms would excuse the report from showing a real terminology effect.
ACCEPTS_TERM_CONTEXT = {"ollama": True, "demo": False, "argos": False, "madlad": False}


def _hr(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def preflight(args) -> list[str]:
    """Return a list of problems. Empty means ready to go."""
    problems = []
    if not any(p.exists() for p in CDLL_PATHS):
        problems.append(
            "translator_engine shared library not built. Run:\n"
            "    cargo build --release --manifest-path translator-engine/Cargo.toml\n"
            "  (or: python script/build.py)")
    for direction in getattr(args, "directions", None) or DIRECTIONS:
        prefix = "av-{}".format(direction)
        corpus = CORPUS_DIR.parent / prefix
        if not (corpus / (prefix + ".src")).is_file():
            problems.append(f"corpus missing at {corpus} (direction {direction})")
        pack = PACK_PATH.parent / "av-{}.json".format(direction)
        if not pack.is_file():
            # Without a pack for this direction there is no treatment to inject, so a
            # scoped arm would report delta 0 and read as "terminology does not help".
            problems.append(f"AV seed pack missing at {pack} (direction {direction})")
    if not SIDECAR_PY.is_file():
        problems.append(f"sidecar script missing at {SIDECAR_PY}")
    # Sidecars need their model dirs; check via default_data_dir(). Argos and
    # madlad are the two CT2 backends the bench hits; ollama is checked
    # separately below via its HTTP endpoint.
    sys.path.insert(0, str(REPO))
    try:
        from sidecar.ct2_sidecar import default_data_dir  # noqa: WPS433
        models_root = default_data_dir() / "models"
        if "argos" in args.engines and not (models_root / "argos").exists():
            problems.append(
                f"argos packages missing at {models_root / 'argos'}. Run:\n"
                "    python script/fetch_argos_models.py")
        if "madlad" in args.engines and not (models_root / "madlad").exists():
            problems.append(
                f"MADLAD-400 CT2 checkpoint missing at {models_root / 'madlad'}. Run:\n"
                "    python script/fetch_madlad_model.py")
    except ImportError:
        pass
    # Ollama check is only meaningful when we plan to bench against it.
    if "ollama-qwen" in args.engines and not _probe_url(OLLAMA_URL, timeout=3):
        problems.append(
            f"Ollama not reachable at {OLLAMA_URL}. Start it with "
            f"`ollama serve` in a separate window, and make sure "
            f"`ollama pull {ollama_model_identity()}` has finished.")
    return problems


def _probe_url(url: str, timeout: float = 2.0) -> bool:
    try:
        with urlopen(Request(url), timeout=timeout) as r:
            return 200 <= r.status < 300
    except (URLError, OSError):
        return False


def _sidecar_running(port: int) -> bool:
    """Is *something* serving this port? A preflight question, not an identity claim.

    Callers here use it to tell the operator "no engine is listening" or "one is already up".
    Deciding whether the service on a port is the one we launched is a different question and
    lives in `sidecar_identity` (defect D73); keeping the two apart is why this line carries an
    explicit annotation instead of being silently whitelisted at file level.
    """
    return _probe_url(f"http://127.0.0.1:{port}/health", timeout=1.0)  # lint-allow: liveness-only


def _start_sidecar(engine: str, port: int) -> subprocess.Popen:
    log = open(EVIDENCE_DIR / f"sidecar-{engine}.log", "ab", buffering=0)
    env = os.environ.copy()
    env.setdefault("WONSLATE_ENGINE", engine)
    backend = SIDECAR_BACKENDS[engine]
    proc = subprocess.Popen(
        [sys.executable, str(SIDECAR_PY), "--backend", backend, "--port", str(port)],
        stdout=log, stderr=log, cwd=REPO, env=env)
    # "Someone answers /health" is not "this is the service we just started": a survivor of an
    # earlier batch usually owns the port, our child cannot bind it and dies, and every number
    # then comes off a service launched with *other* arguments while the log says `ready`.
    # Defect D73; the order matters too -- polling health first used to short-circuit the
    # exit-code check below, which was written but never reached.
    try:
        label, detail = wait_until_ours("http://127.0.0.1:%d" % port, proc, timeout=120,
                                        interval=2.0)
    except SidecarIdentityError as bad:
        proc.terminate()
        raise SystemExit("%s sidecar could not be identified on port %d: %s\n"
                         "  something else owns that port -- stop it rather than waiting longer\n"
                         "  command: python %s --backend %s --port %d\n  log: %s"
                         % (engine, port, bad, SIDECAR_PY, backend, port, log.name))
    if label == "unproven":
        print("  [sidecar] %s answered on %d without a pid (pre-D72 sidecar): %s"
              % (engine, port, detail))
    else:
        print("  [sidecar] %s ready on %d (backend=%s; %s)" % (engine, port, backend, detail))
    return proc


def install_pack() -> None:
    _hr("Installing AV seed pack")
    subprocess.check_call([sys.executable, str(INSTALL_PY), "--domain", "av"])


def _corpus_rows(direction, n, av_root) -> int:
    """How many rows this run will actually read: --n, or the corpus's own length.

    The count is part of the evidence identity (T4 grows the corpus under the same
    direction), so it has to be known before the bench is called -- and counted the way the
    bench counts it, or the two disagree about which file the run just wrote.
    """
    if n:
        return n
    prefix = "av-{}".format(direction)
    roots = [r for r in (av_root, detect_av_root(), CORPUS_DIR.parent) if r]
    for root in roots:
        src = pathlib.Path(root) / prefix / (prefix + ".src")
        if src.is_file():
            return sum(1 for line in open(src, encoding="utf-8") if line.strip())
    return 0


def run_bench(engine: str, direction: str, domain: str, n: int | None,
              av_root: pathlib.Path | None = None,
              run_index: int = 1, corpus_rows: int | None = None) -> pathlib.Path:
    """Invoke bench_domain_av once; return the evidence path it wrote."""
    cmd = [sys.executable, str(BENCH_PY), "--engine", engine,
           "--direction", direction, "--run-index", str(run_index)]
    if domain:
        cmd += ["--domain", domain]
    if n:
        cmd += ["--n", str(n)]
    if av_root:
        cmd += ["--av-root", str(av_root)]
    rows = corpus_rows or _corpus_rows(direction, n, av_root)
    # The expected path has to be the path the writer will actually choose, and the model
    # is now part of that identity: with the env pointing at a candidate, preflight would
    # otherwise watch for the baseline's file and abort on "not written" after a good run.
    model = ollama_model_identity() if _ffi_id(engine) == "ollama" else None
    expected = EVIDENCE_DIR / evidence_name(engine, direction, domain, rows, model=model)
    _hr(f"Bench: engine={engine} dir={direction} domain={domain or '(unscoped)'}"
        + (f" n={n}" if n else "") + f" rows={rows}")
    subprocess.check_call(cmd)
    if not expected.exists():
        raise SystemExit(f"expected evidence file {expected} not written")
    return expected


def run_bench_with_retry(engine, direction, domain, n, av_root, procs, run_index=1,
                         corpus_rows=None):
    """Run bench_domain_av; if the sidecar crashed mid-bench, restart it and retry.

    madlad's 3B CT2 model can OOM on a 6 GB laptop GPU, especially when
    COMET loads its own model concurrently. By restarting the sidecar
    once on failure, the runner is robust to that transient condition
    without silently losing a whole engine's results.

    A row-level failure is not this path's business: bench_domain_av retries such a
    row itself and records what is left in `failed` (defect D31). Restarting the whole
    arm for one flaky sentence would hide the very signal this runner needs.
    """
    try:
        return run_bench(engine, direction, domain, n, av_root, run_index, corpus_rows)
    except subprocess.CalledProcessError:
        port = SIDECAR_PORTS.get(engine)
        if port and not _sidecar_running(port):
            print(f"  [retry] {engine} sidecar died (port {port} gone); "
                  f"restarting and retrying bench...", flush=True)
            procs.append(_start_sidecar(engine, port))
            return run_bench(engine, direction, domain, n, av_root, run_index, corpus_rows)
        # sidecar is up but bench still failed -- re-raise
        raise


def detect_av_root() -> pathlib.Path | None:
    """Return an --av-root override, or None to let bench_domain_av resolve
    normally. The runner uses the in-repo starter corpus when the default
    OS data-root has no pair files, so a first-time user gets numbers
    without a manual copy step. Users who already set WONSLATE_AV_DIR or
    --av-root themselves are respected (no override emitted)."""
    if os.environ.get("WONSLATE_AV_DIR", "").strip():
        return None
    try:
        from sidecar.ct2_sidecar import default_data_dir  # noqa: WPS433
    except ImportError:
        return None
    default_root = default_data_dir() / "benchmarks" / "av-domain"
    for prefix in ("av-zh-en", "av-en-zh"):
        for candidate in (default_root / prefix / (prefix + ".src"),
                          default_root / (prefix + ".src")):
            if candidate.is_file():
                return None
    starter = REPO / "script" / "eval-data"
    return starter if starter.is_dir() else None


def score_comet(path: pathlib.Path) -> None:
    _hr(f"COMET scoring: {path.name}")
    # score_comet.py takes evidence JSON paths as positional args, not
    # --input; nargs="+" so a batch is possible, but we call it once per
    # file so a single crash does not lose the others.
    subprocess.check_call([sys.executable, str(SCORE_PY), str(path)])


def _load_ranges(direction) -> list[dict]:
    """Sub-domain groups of ONE direction's corpus, addressed by row order.

    A group is a span of ROW NUMBERS, so it only means "these sentences" inside one corpus
    ordering. `en-zh` happens to share `zh-en`'s order because it is that same corpus with its two
    columns swapped -- an equality a test pins, and a copy `derive_av_direction.py` writes rather
    than a second thing to maintain by hand.

    A direction that is not declared gets NO groups. Falling back to another direction's list --
    which is what happened while this function took no direction at all -- prints a correct number
    under a label describing different sentences, and nothing errors: short runs get clamped, full
    runs get complete tables. That was defect D62; the report now prints an explicit "this
    direction has no group table" note instead.
    """
    data = json.loads(RANGES_PATH.read_text(encoding="utf-8"))
    groups = data.get("ranges")
    if not isinstance(groups, dict):
        raise SystemExit("%s: the 'ranges' key must map direction -> list of groups; "
                         "one shared list across directions is defect D62" % RANGES_PATH)
    return list(groups.get(direction or "", []))


def _run_direction(run) -> str:
    """Which corpus direction one run measured.

    Artifacts written before the identity reached the header carry no `direction` field; those are
    the published zh->en runs, which is also why their file names say so (see _arm_runs_by_size).
    """
    return (run or {}).get("direction") or "zh-en"


def _per_domain_means(run: dict | None, ranges: list[dict], key: str,
                      allowed=None) -> list[float]:
    """Return per-range value of `key` (chrF / BLEU / comet).

    For `comet` we mean the parallel `comet_scores_per_sample` array that
    score_comet.py attaches to a run. For `chrF` / `BLEU` we call
    bench_translation.score on the range's slice so we get a real
    corpus-level number over ~25 sentences rather than an average of
    per-sample scores (which sacrebleu does not natively expose).

    `allowed` is the set of row ids both arms translated (defect D31): without it the
    two columns of one row are means over different row sets, and the sub-domain
    table shows the same kind of artifact the headline once did. Rows the COMET pass
    skipped are None and stay out of the mean.

    Missing evidence or a slice too short for BLEU returns NaN, which the
    renderer shows as `_无数据_`."""
    if not run:
        return [float("nan")] * len(ranges)
    samples = run.get("samples") or []
    tgt_lang = (run.get("direction") or "-").split("-")[-1] or "en"
    if key == "comet":
        scores = run.get("comet_scores_per_sample")
        if not scores:
            return [float("nan")] * len(ranges)
        out = []
        for rng in ranges:
            vals = [scores[s["id"]] for s in samples
                    if rng["start"] <= s.get("id", -1) + 1 <= rng["end"]
                    and s.get("id", -1) < len(scores)
                    and (allowed is None or s.get("id") in allowed)]
            vals = [v for v in vals if v is not None]
            out.append(statistics.fmean(vals) if vals else float("nan"))
        return out
    try:
        from bench_translation import score  # noqa: WPS433 (lazy)
    except ImportError:
        return [float("nan")] * len(ranges)
    out = []
    for rng in ranges:
        subset = [s for s in samples
                  if rng["start"] <= s.get("id", -1) + 1 <= rng["end"]
                  and (allowed is None or s.get("id") in allowed)]
        if not subset:
            out.append(float("nan"))
            continue
        hyps = [s["hypothesis"] for s in subset]
        refs = [s["reference"] for s in subset]
        try:
            chrf, bleu = score(hyps, refs, tgt_lang)
        except Exception:  # noqa: BLE001 (any sacrebleu error -> NaN cell)
            out.append(float("nan"))
            continue
        out.append(chrf if key == "chrF" else bleu)
    return out


def _aggregate(path: pathlib.Path | None) -> dict | None:
    """Load one evidence JSON. Returns the sole `runs[0]` element so callers
    see a flat dict of engine/direction/domain/metrics/samples. Adds
    `comet_scores_per_sample` transparently once score_comet.py has run."""
    if path is None or not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    runs = record.get("runs") or []
    if not runs:
        # Legacy shape (pre-`runs` refactor) or the file was truncated.
        # Synthesise a run so downstream code is uniform.
        return {
            "engine": record.get("engine", "?"),
            "direction": record.get("direction", "?"),
            "domain": record.get("domain", ""),
            "metrics": record.get("metrics", {}),
            "samples": record.get("samples", []),
        }
    return runs[0]


def _run_list(path: pathlib.Path) -> list:
    """Every run inside one evidence file, in repeat order.

    `runs` is an array on purpose: bench_domain_av writes repeat slots into it
    (--run-index), so a second run of the same arm becomes a noise control instead of
    overwriting the first. The pre-`runs` shape (a bare run at the top level) still
    reads, and an unreadable file reads as "no evidence" rather than crashing the
    report."""
    if path is None or not path.exists():
        return []
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    runs = record.get("runs")
    if runs:
        return list(runs)
    if record.get("samples") or record.get("metrics"):
        return [record]
    return []


def _answered(run, index) -> bool:
    """Whether row `index` of a run actually produced a translation (defect D31)."""
    samples = run.get("samples") or []
    if index >= len(samples):
        return False
    return bool((samples[index].get("hypothesis") or "").strip())


def _mean_of(runs, pick):
    vals = [v for v in (pick(r) for r in runs) if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _build_groups(runs):
    """Split runs by the build they were measured against (defect D70).

    Three buckets, kept apart deliberately: runs sharing a recorded build id, runs whose
    build id is known to differ, and runs written before D70 that carry no id at all. The
    last one is not "the same as the others" -- it is "unknown", and reading unknown as
    equal is how S13 T3 came to average numbers taken from two different DLLs.
    """
    grouped = {}
    for run in runs:
        grouped.setdefault(run.get("engine_build_sha"), []).append(run)
    return grouped


def _spread(runs):
    """Within-arm drift across repeats **of one build** -- the noise floor for any delta.

    Max minus min over runs of two different builds measures the builds, not the sampling
    noise, so it is not returned. None here means "these runs give no floor"; `_spread_note`
    says which of the three reasons applies.
    """
    best = None
    for group in _build_groups(runs).values():
        vals = [r.get("comet") for r in group if r.get("comet") is not None]
        if len(vals) >= 2:
            spread = max(vals) - min(vals)
            best = spread if best is None else min(best, spread)
    return best


def _spread_note(runs):
    """What weakens the floor taken from these runs, or None when nothing does.

    D57/D58 applies here: a floor with several opposite readings printed as one sentence
    sends the reader to redo work that exists, or to trust a number that is really two
    builds' difference. Reported even when a floor *was* computed -- runs that carry no id
    at all are not known to share a build, and silently treating "unknown" as "same" is
    exactly the mistake D70 is about; the number stays available, the caveat travels with it.
    """
    scored = [r for r in runs if r.get("comet") is not None]
    if len(runs) < 2:
        return None                                   # genuinely not repeated
    if len(scored) < 2:
        return "repeats-unscored"
    ids = {r.get("engine_build_sha") for r in scored}
    known = {i for i in ids if i}
    if len(known) > 1 or (None in ids and known):
        return "repeats-across-builds"
    if None in ids:
        return "repeats-build-id-unknown"
    return None


def _term_context_for(engine) -> bool:
    """Whether a scoped (terminology-wired) arm could differ at all for this engine.

    Unknown ids fall on the strict side. Claiming "not applicable" for an engine whose
    capability is not in the table would excuse a genuinely missing artifact, which is the
    opposite of what this column is for.
    """
    return ACCEPTS_TERM_CONTEXT.get(_ffi_id(engine), True)


def term_context_sides():
    """(applies, ignores): the bench engine ids split by whether a scoped arm can differ.

    The report's prose about engine capability is rendered from here so it cannot disagree
    with the status column. It used to be hand-written, and it did disagree: the limitation
    section listed madlad among the engines that consume the glossary, while madlad is served
    by the same sidecar.rs as argos and keeps the trait default of not applying it (D57).
    """
    applies = [e for e in ENGINES if _term_context_for(e)]
    ignores = [e for e in ENGINES if not _term_context_for(e)]
    return applies, ignores


def _status(has_base, has_scope, failed_rows, delta, noise_floor, repeats=1,
            term_context=True, floor_note=None) -> str:
    """The verdict column, and the only place allowed to write 完整.

    D31: this used to read `has_base and has_scope` -- two files exist -- and printed
    ✅ 完整 for a run in which 19 of 300 rows had never been translated at all.

    D57: a missing scoped arm has two opposite causes as well. For an engine that cannot
    apply term context there is nothing to pair, so the arm was never run on purpose; saying
    "missing evidence" sends a reader to bench a comparison that cannot differ, and the
    0.0000 they get back then reads as "terminology does not help".
    """
    if not has_base:
        return "⚠️ 缺臂（另一轮无证据）"
    if not has_scope:
        if not term_context:
            # The repeat note still belongs here: skipping the comparison does not skip the
            # question of how often the baseline itself was sampled.
            tail = ("；未复采，无噪声地板" if repeats < 2
                    else "；基线已复采 %d 次" % repeats)
            return "ℹ️ 对照不适用（该后端不施加术语上下文），仅基线" + tail
        return "⚠️ 缺臂（另一轮无证据）"
    head = "✅ 完整" if failed_rows == 0 else \
        "⚠️ %d 行未译出（已从指标剔除）" % failed_rows
    if not term_context:
        # These backends are deterministic, so their within-arm floor comes out exactly zero,
        # and the ordinary "delta exceeds the floor" sentence would fire on any nonzero
        # difference for an engine that cannot apply the terms at all. Say what that delta is
        # instead of letting a wire format read as a terminology finding.
        tail = "；未复采，无噪声地板" if repeats < 2 else "；已复采 %d 次" % repeats
        return head + "；对照不适用（该后端不施加术语上下文），Δ 为构造使然，不作术语效果解读" + tail
    if noise_floor is None:
        # "no floor" has several opposite causes that read the same unless they are told
        # apart: a single run; repeats measured while COMET has not been scored yet; and
        # (D70) repeats taken against **different builds of the engine**, which are not
        # repeats of one measurement at all. Labelling the last one "not repeated" would
        # send someone to re-run work that exists; labelling it as a floor would let a
        # two-build difference be read as sampling noise.
        if floor_note == "repeats-across-builds":
            tail = "；已复采 %d 次但跨不同被测二进制，不得合并为噪声地板（D70）" % repeats
        elif floor_note == "repeats-build-id-unknown":
            tail = "；已复采 %d 次但部分 run 无二进制身份（D70 之前），不能假定同一批次" % repeats
        elif floor_note == "repeats-unscored" or repeats >= 2:
            tail = "；已复采 %d 次，但 COMET 未打分故无地板" % repeats
        else:
            tail = "；未复采，无噪声地板"
    elif delta is None:
        tail = "；Δ 未测（COMET 尚未打分）"
    elif abs(delta) <= noise_floor:
        tail = "；Δ 在噪声地板 ±%.4f 内，不足以归因" % noise_floor
    else:
        tail = "；Δ 超噪声地板 ±%.4f" % noise_floor
    if noise_floor is not None and floor_note == "repeats-across-builds":
        # A floor computed across two builds measures the builds; say so next to the number
        # instead of suppressing it, so the reader can still see what was collected.
        tail += "；但该地板跨不同被测二进制，不可信（D70）"
    elif noise_floor is not None and floor_note == "repeats-build-id-unknown":
        tail += "；但该地板假定重复同批（run 未记二进制身份，D70 之前）"
    return head + tail


def _pooled_adherence(runs):
    """Sum the terminology counts over repeats; None when no run carries them.

    Older evidence predates the metric, and "not measured" must stay distinguishable
    from "0% adherent" -- the latter would read as the worst possible terminology
    result rather than the absence of one.

    `opps_per_run` is what makes two arms comparable: subtracting a rate computed
    over 500 opportunities from one over 667 is the same mistake D31 was, just one
    layer over.
    """
    blocks = [r.get("term_adherence") for r in runs if r.get("term_adherence")]
    if not blocks:
        return None
    opportunities = sum(b.get("opportunities") or 0 for b in blocks)
    hits = sum(b.get("hits") or 0 for b in blocks)
    return {"opportunities": opportunities, "hits": hits,
            "rate": (hits / opportunities) if opportunities else None,
            "terms_in_pack": blocks[0].get("terms_in_pack"),
            "rows_with_terms": blocks[-1].get("rows_with_terms"),
            "opps_per_run": sorted({b.get("opportunities") or 0 for b in blocks})}


def summarize_engine(engine, base_runs, scope_runs) -> dict:
    """Compare one engine's two arms on the rows both of them translated.

    Why paired (defect D31): the two arms' aggregates used to be subtracted directly,
    so an arm that lost rows to transient engine failures had its mean taken over a
    different -- and harder -- set of rows, and the gap between the two means was read
    as a terminology effect. On 2026-10-05 that published COMET +0.0330 where the
    paired difference over the 281 rows both arms answered was +0.0058.

    Repeats pair up by index (repeat 1 against repeat 1) and the within-arm spread
    across repeats becomes the noise floor: ollama runs at temperature 0.3 with no
    seed, so a |Δ| inside that floor is not a result.
    """
    has_base, has_scope = bool(base_runs), bool(scope_runs)
    base_comet = _mean_of(base_runs, lambda r: r.get("comet"))
    scope_comet = _mean_of(scope_runs, lambda r: r.get("comet"))
    base_chrf = _mean_of(base_runs, lambda r: (r.get("metrics") or {}).get("chrF"))
    scope_chrf = _mean_of(scope_runs, lambda r: (r.get("metrics") or {}).get("chrF"))
    failed_rows = sum(len(r.get("failed") or []) for r in base_runs + scope_runs)
    floors = [f for f in (_spread(base_runs), _spread(scope_runs)) if f is not None]
    noise_floor = max(floors) if floors else None
    # Which arm's missing floor gets named, and why: the report must not have to re-derive
    # it from the run list.
    floor_note = _spread_note(base_runs) or _spread_note(scope_runs)

    per_pair, paired_n = [], None
    for b, s in zip(base_runs, scope_runs):
        ids = [i for i in range(len(b.get("samples") or []))
               if _answered(b, i) and _answered(s, i)]
        if paired_n is None:
            paired_n = len(ids)
        sb = b.get("comet_scores_per_sample")
        ss = s.get("comet_scores_per_sample")
        if sb and ss:
            diffs = [ss[i] - sb[i] for i in ids
                     if i < len(sb) and i < len(ss)
                     and sb[i] is not None and ss[i] is not None]
            if diffs:
                per_pair.append(sum(diffs) / len(diffs))
    if per_pair:
        delta = sum(per_pair) / len(per_pair)
    elif base_comet is not None and scope_comet is not None:
        delta = scope_comet - base_comet        # no per-sample scores yet
    else:
        delta = None
    base_adh = _pooled_adherence(base_runs)
    scope_adh = _pooled_adherence(scope_runs)
    adh_comparable = bool(base_adh and scope_adh
                          and base_adh["opps_per_run"] == scope_adh["opps_per_run"])
    if adh_comparable and base_adh["rate"] is not None and scope_adh["rate"] is not None:
        adh_delta = scope_adh["rate"] - base_adh["rate"]
    else:
        adh_delta = None
    return {
        "engine": engine,
        "has_base": has_base, "has_scope": has_scope,
        "base_comet": base_comet, "scope_comet": scope_comet,
        "base_chrF": base_chrf, "scope_chrF": scope_chrf,
        "delta_comet": delta, "paired_n": paired_n,
        "failed_rows": failed_rows, "noise_floor": noise_floor,
        "base_adherence": base_adh, "scope_adherence": scope_adh,
        "adherence_delta": adh_delta, "adherence_comparable": adh_comparable,
        "delta_paired": bool(per_pair),
        "repeats": max(len(base_runs), len(scope_runs)),
        "status": _status(has_base, has_scope, failed_rows, delta, noise_floor,
                          max(len(base_runs), len(scope_runs)),
                          term_context=_term_context_for(engine),
                          floor_note=floor_note),
    }


def _metric(run, key):
    if not run:
        return None
    m = run.get("metrics") or {}
    if key in m:
        return m[key]
    # COMET lives at run["comet"] (score_comet.py) rather than inside
    # run["metrics"]; normalise so _metric(run, "comet") works too.
    if key == "comet" and "comet" in run:
        return run["comet"]
    return None


def _arm_runs_by_size(engine, domain, direction) -> dict:
    """{corpus_rows: runs} for one arm of ONE direction, from named files plus the legacy name.

    The direction is part of the pattern, not decoration: sizing alone would let the en->zh
    artifact answer for zh->en, since both are named `-n300` and a dict keyed by row count
    holds only one of them -- the report would then show one direction's numbers under the
    other's heading.

    The legacy `av-domain-<engine>[-<domain>].json` artifacts are the published 300-row
    zh->en runs and carry no size in their name; their row count comes from the run's own
    `requested`. A new-style file wins when both exist for one size, because it also holds
    the repeats the noise floor needs.
    """
    tag = f"-{domain}" if domain else ""
    marker = f"av-domain-{engine}{tag}-{direction}-n"
    found = {}
    for path in sorted(EVIDENCE_DIR.glob(f"{marker}*.json")):
        size = path.name[:-len(".json")][len(marker):]
        if not size.isdigit():
            continue                    # another direction's name or a hand-made file: skip
        runs = _run_list(path)
        if runs:
            found[int(size)] = runs
    # A legacy-named file carries no direction in its name, so the run's own `direction` field
    # decides which identity it belongs to; artifacts written before that field existed are the
    # published zh->en runs (_run_direction's documented default). Reading the name alone here
    # while the headline table reads the header let one artifact be described as two directions
    # at once -- the disagreement these two lines forbid.
    legacy = _run_list(EVIDENCE_DIR / f"av-domain-{engine}{tag}.json")
    if legacy and _run_direction(legacy[0]) == direction:
        rows = next((r.get("corpus_rows") for r in legacy if r.get("corpus_rows")),
                    legacy[0].get("requested") or 0)
        found.setdefault(int(rows), legacy)
    return found


def _floor_cell(s) -> str:
    """The noise-floor column, consistent with the status column.

    A missing floor has two opposite causes; printing the single-run label for both would
    tell a reader to run a repeat that has already been run, on an arm whose only gap is that
    COMET has not scored it.
    """
    if s["noise_floor"] is not None:
        return "±%.4f" % s["noise_floor"]
    return "_单次_" if s["repeats"] < 2 else "_待 COMET_"


def _extra_identity_lines(engines, directions, primary) -> list:
    """Markdown for every measurement the main tables do not render.

    `primary` is the {(direction, rows)} the tables above already cover. Everything else on
    disk gets a row here -- a second direction (S12 T2) or a grown corpus (S12 T4) must not
    sit in docs/evidence/ with no path onto the page, because that is how a measurement
    becomes a rumor. Group tables come along, read from THAT section's own direction: a group
    is a span of corpus row numbers, and only a direction that shares another's row order may
    reuse its labels (D62). A run shorter than its group table shows fewer groups (clamped),
    never empty ones; a direction with no table at all says so.
    """
    sizes = {}
    for direction in directions:
        for engine in engines:
            for arm, domain in (("base", ""), ("scope", "av")):
                for rows in _arm_runs_by_size(engine, domain, direction):
                    sizes.setdefault((direction, rows), {"base": {}, "scope": {}})
    for direction in directions:
        for engine in engines:
            for arm, domain in (("base", ""), ("scope", "av")):
                for rows, runs in _arm_runs_by_size(engine, domain, direction).items():
                    sizes[(direction, rows)][arm][engine] = runs
    sections = []
    for (direction, rows) in sorted(sizes):
        if (direction, rows) in primary:
            continue
        base_runs = sizes[(direction, rows)]["base"]
        scope_runs = sizes[(direction, rows)]["scope"]
        present = sorted(set(base_runs) | set(scope_runs))
        if not present:
            continue
        sections += [
            "",
            f"### \u8bed\u5411 {direction} \u00b7 {rows} \u53e5",
            "",
            "| \u5f15\u64ce | \u672a\u63a5\u7ebf COMET | \u63a5\u7ebf COMET | \u0394 COMET | \u914d\u5bf9 n | \u0394 chrF | \u566a\u58f0\u5730\u677f | \u672a\u63a5\u7ebf\u9075\u5faa\u7387 | \u63a5\u7ebf\u9075\u5faa\u7387 | \u72b6\u6001 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for e in present:
            s = summarize_engine(e, base_runs.get(e, []), scope_runs.get(e, []))
            chrf_delta = (s["scope_chrF"] - s["base_chrF"]
                          if s["base_chrF"] is not None and s["scope_chrF"] is not None else None)
            floor = _floor_cell(s)
            paired_n = "_\u65e0_" if s["paired_n"] is None else str(s["paired_n"])
            rate_b = ((s["base_adherence"] or {}).get("rate"))
            rate_s = ((s["scope_adherence"] or {}).get("rate"))
            sections.append(
                f"| {e} | {_fmt(s['base_comet'])} | {_fmt(s['scope_comet'])} | "
                f"{_fmt_pct(s['delta_comet'])} | {paired_n} | {_fmt_pct(chrf_delta)} | {floor} | "
                f"{_fmt(rate_b, 3, 3)} | {_fmt(rate_s, 3, 3)} | {s['status']} |")
        groups = _load_ranges(direction)
        if not groups:
            sections += [
                "",
                "> \u65e0\u5206\u7ec4\u8868\uff1a`script/av_domain_ranges.json` \u672a\u767b\u8bb0\u8bed\u5411 `%s`\u3002"
                "\u5b50\u9886\u57df\u5206\u7ec4\u662f\u8bed\u6599\u884c\u53f7\u533a\u95f4\uff0c\u6362\u4e2a\u8bed\u5411\u5c31\u6307\u7740\u522b\u7684\u53e5\u5b50\uff0c"
                "\u56e0\u6b64\u4e0d\u5957\u7528\u5176\u4ed6\u8bed\u5411\u7684\u884c\u53f7\uff08D62\uff09\u3002" % direction,
            ]
            continue
        for e in present:
            b = (base_runs.get(e) or [None])[0]
            s = (scope_runs.get(e) or [None])[0]
            if not (b and s):
                continue          # one arm alone has nothing to pair against (D31)
            group_rows = _subdomain_rows(b, s, groups)
            if not group_rows:
                continue
            sections += [
                "",
                "#### %s \u00b7 \u5b50\u9886\u57df\uff08\u524d %d \u884c\uff09" % (e, rows),
                "",
                "| \u5b50\u9886\u57df | \u53e5\u5bf9\u6570 | \u672a\u63a5\u7ebf chrF | \u63a5\u7ebf chrF | \u672a\u63a5\u7ebf COMET | \u63a5\u7ebf COMET |",
                "|---|---|---|---|---|---|",
            ]
            for name, count, b_ch, s_ch, b_co, s_co in group_rows:
                sections.append(f"| {name} | {count} | {_fmt(b_ch, 1, 2)} | {_fmt(s_ch, 1, 2)} | "
                                f"{_fmt(b_co)} | {_fmt(s_co)} |")
    if not sections:
        return []
    return ["", "## \u5176\u4ed6\u6d4b\u91cf\u8eab\u4efd\uff08\u8de8\u8bed\u5411 / \u6269\u6837\uff09", "",
            "> \u672c\u8282\u6570\u5b57\u4e0e\u4e0a\u8868\u540c\u53e3\u5f84\uff08\u914d\u5bf9\u5dee + \u540c\u81c2\u5730\u677f\uff09\uff0c\u4f46\u6837\u672c\u96c6\u4e0d\u540c\uff0c\u4e0d\u53ef\u4e0e\u4e0a\u8868\u6df7\u7528\u3002"
            "\u5b50\u9886\u57df\u5206\u7ec4\u6309\u8bed\u6599\u884c\u5e8f\u53d6\uff0c\u53cd\u5411\u8bed\u6599\u662f\u540c\u4e00\u6279\u884c\u7684\u5217\u5bf9\u8c03\uff0c\u56e0\u6b64\u540c\u7ec4\u540d\u6307\u540c\u4e00\u6279\u53e5\u5b50\uff1b"
            "\u884c\u6570\u5c11\u4e8e\u5206\u7ec4\u8868\u7684\u8eab\u4efd\u53ea\u5217\u51fa\u8986\u76d6\u5230\u7684\u90a3\u51e0\u7ec4\u3002**",
            ] + sections


def _has_any_evidence(engine) -> bool:
    """Whether either arm of one engine is recorded on disk, under any identity."""
    for domain in ("", "av"):
        for direction in DIRECTIONS:
            if _arm_runs_by_size(engine, domain, direction):
                return True
    return bool(_run_list(EVIDENCE_DIR / f"av-domain-{engine}.json"))


def generate_report(args):
    """Read every evidence file, compute the tables, and rewrite the report.
    Returns the summary rows so main() can print a compact table too.
    An empty list means "declined because there is no evidence"; main()
    uses that as a signal to exit non-zero in --report-only mode."""
    engines = list(args.engines)
    # The report is a projection of what has been measured, not of what this invocation
    # asked for. Narrowing --engines used to delete the other engines' rows -- and their
    # sub-domain tables -- out of a published document whose evidence is still on disk:
    # reproduced by running `--report-only --engines ollama-qwen`, which dropped argos and
    # madlad. Anything with evidence is rendered; --engines only decides what gets benched.
    engines = list(dict.fromkeys([e for e in ENGINES if _has_any_evidence(e)] + engines))
    # The widening has to reach the writer too: `_write_report` builds its own engine list, and
    # keeping the CLI one there deleted the *sub-domain tables* of every engine not benched in
    # this invocation while its TL;DR row survived -- a half-fixed version of the same loss.
    args._report_engines = list(engines)
    directions = list(getattr(args, "directions", None) or DIRECTIONS)
    baseline_runs = {e: _run_list(EVIDENCE_DIR / f"av-domain-{e}.json") for e in engines}
    scoped_runs = {e: _run_list(EVIDENCE_DIR / f"av-domain-{e}-av.json") for e in engines}
    # The sub-domain tables read repeat 1 of each arm; the TL;DR averages every repeat.
    baselines = {e: (baseline_runs[e][0] if baseline_runs[e] else None) for e in engines}
    scoped = {e: (scoped_runs[e][0] if scoped_runs[e] else None) for e in engines}
    if not any(baselines.values()) and not any(scoped.values()):
        # Nothing measured yet -- refuse to overwrite the hand-authored
        # skeleton (which carries the four hypotheses, reproduction
        # commands and limitation section as static prose). A user who
        # really wants to force regeneration passes --force.
        if getattr(args, "force", False):
            print("  [warn] no evidence/*.json found; --force is set, "
                  "overwriting the skeleton with an empty table", file=sys.stderr)
        else:
            print("  [skip] no evidence/*.json under docs/evidence/av-domain-*.json; "
                  "the existing docs/av-domain-benchmark.md stays untouched.\n"
                  "         run the benches first, or pass --force to overwrite "
                  "with an empty table.")
            return []

    summary = [summarize_engine(e, baseline_runs[e], scoped_runs[e]) for e in engines]
    args._evidence_note = _compute_evidence_note(baselines, scoped)
    primary = set()
    for e in engines:                                  # what the tables above already render
        for arm, domain in (("base", ""), ("scope", "av")):
            for run in (baseline_runs if arm == "base" else scoped_runs)[e]:
                primary.add((run.get("direction") or "zh-en",
                             int(run.get("requested") or 0)))
    args._extra_lines = _extra_identity_lines(engines, directions, primary)
    _write_report(baselines, scoped, summary, args)
    return summary


def _fmt(v, width=4, digits=4):
    if v is None or v != v:  # None or NaN
        return "_无数据_"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def _fmt_pct(v):
    if v is None or v != v:
        return "_无数据_"
    return f"{v:+.4f}"


def _clamped_ranges(ranges, n_rows):
    """Ranges that overlap the first `n_rows` corpus rows, with the last one trimmed.

    The corpus grew to 1000 rows while the published runs hold 300 samples. Showing groups
    301..1000 against those runs would print empty rows that read as "these sub-domains
    scored nothing" when nothing was measured there -- so groups beyond the run are dropped
    and the boundary group is cut to the rows that actually exist.
    """
    out = []
    for rng in ranges:
        if rng["start"] > n_rows:
            continue
        item = dict(rng)
        item["end"] = min(rng["end"], n_rows)
        out.append(item)
    return out


def _subdomain_rows(base: dict | None, scope: dict | None, ranges: list[dict]) -> list:
    """Return (name, count, base_chrF, scope_chrF, base_comet, scope_comet) per range.

    Both columns of a row are restricted to the sentences both arms translated
    (defect D31); with only one arm present there is nothing to pair against. Ranges are
    clamped to what the runs actually carry, so a 300-row run never displays a 1000-row
    group table.
    """
    n_rows = max(len((base or {}).get("samples") or []),
                 len((scope or {}).get("samples") or []))
    ranges = _clamped_ranges(ranges, n_rows)
    allowed = None
    if base and scope:
        allowed = {i for i in range(len(base.get("samples") or []))
                   if _answered(base, i) and _answered(scope, i)}
    b_ch = _per_domain_means(base, ranges, "chrF", allowed)
    s_ch = _per_domain_means(scope, ranges, "chrF", allowed)
    b_co = _per_domain_means(base, ranges, "comet", allowed)
    s_co = _per_domain_means(scope, ranges, "comet", allowed)
    rows = []
    for i, rng in enumerate(ranges):
        count = rng["end"] - rng["start"] + 1
        rows.append((rng["name"], count, b_ch[i], s_ch[i], b_co[i], s_co[i]))
    return rows


def _data_note() -> str:
    """The header's 'what data is this' line, rendered from the corpus and the grouping table.

    It used to claim three hundred pairs in one direction with twelve groups, long after the
    corpus reached 1000 rows in two directions with 23 groups each -- the same stale-prose class
    as D60, and the same cure: ask the files instead of restating them. The group counts are per
    direction on purpose, since a group is a span of one corpus's row numbers (D62).
    """
    counts = []
    for direction in DIRECTIONS:
        groups = _load_ranges(direction)
        counts.append("%s %s" % (direction.replace("-", "\u2192"),
                                 "%d 组" % len(groups) if groups else "未登记分组"))
    return ("数据：`script/eval-data/`（本项目自撰，Apache-2.0，" + _corpus_size_note("对")
            + "，语向 " + _direction_note() + "；子领域分组按语向各自登记：" + "、".join(counts) + "）。")


def _write_report(baselines, scoped, summary, args) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    engines = list(getattr(args, "_report_engines", None) or args.engines)
    lines = [
        f"# AV 领域质量基准报告（{now} 自动回填）",
        "",
        "> 定位：本报告是 Wonslate **S11 领域种子包收益**的对外证据。与 flores-benchmark.md（通用域）、",
        "> translation-benchmark.md（108 句回归基线）分工不同：本报告的样本与指标专为「AV / 多媒体技术域」设计。",
        _data_note(),
        "> 指标：sacrebleu 的 chrF++ / BLEU 与 COMET-22 (Unbabel/wmt22-comet-da)。",
        f"> 生成：`python script/run_av_baseline.py`（本文件由该脚本自动写入，{_hr_line_note(args)}）。",
        "",
        "## TL;DR",
        "",
        "| 引擎 | 未接线 COMET | 接线 COMET (`--domain av`) | Δ COMET | 配对 n | Δ chrF | 噪声地板 | 状态 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        chrf_delta = (s["scope_chrF"] - s["base_chrF"]
                      if s["base_chrF"] is not None and s["scope_chrF"] is not None
                      else None)
        paired_n = "_无_" if s["paired_n"] is None else str(s["paired_n"])
        floor = _floor_cell(s)
        lines.append(f"| {s['engine']} | {_fmt(s['base_comet'])} | {_fmt(s['scope_comet'])} | "
                     f"{_fmt_pct(s['delta_comet'])} | {paired_n} | "
                     f"{_fmt_pct(chrf_delta)} | {floor} | {s['status']} |")
    lines += [
        "",
        "> **Δ COMET 是配对差**：只取两臂都译出了该句的行逐句相减再取均值（defect D31——两臂各求"
        "均值再相减，会把一臂的引擎失败算成另一臂的质量损失）。尚无逐句 COMET 分时退回两轮均值之差。",
        "> **Δ chrF 是聚合差**（各自已译出行上的 corpus 值相减），非逐句配对，只作目测，不作披露口径。",
        "> **噪声地板** = 同臂两次复采间的 COMET 均值差；|Δ| 落在地板内即不足以归因到术语。"
        "`--repeats 1` 时无地板（状态列写“未复采”）。",
        "> 子领域表读每臂**第 1 次复采**，且只统计两臂都译出的行。",
        "",
        "## 分引擎逐子领域",
        "",
    ]
    for e in engines:
        # Read the groups of the direction THIS table's runs measured, not of whichever direction
        # the ranges file happened to list first, and say which identity is being described: the
        # heading alone used to hide that these rows come from the published zh->en artifacts
        # while the identity section below holds the others (D62).
        direction = _run_direction(baselines[e] or scoped[e])
        rows = (baselines[e] or scoped[e] or {}).get("requested")
        groups = _load_ranges(direction)
        lines += [
            f"### {e}",
            "",
            "> \u8bed\u5411 `%s` \u00b7 %s \u53e5\uff08\u672c\u8868\u53d6\u8be5\u8eab\u4efd\u6bcf\u81c2\u7684\u7b2c 1 \u6b21\u590d\u91c7\uff09" % (direction, rows),
            "",
            "| \u5b50\u9886\u57df | \u53e5\u5bf9\u6570 | \u672a\u63a5\u7ebf chrF | \u63a5\u7ebf chrF | \u672a\u63a5\u7ebf COMET | \u63a5\u7ebf COMET |",
            "|---|---|---|---|---|---|",
        ]
        if not groups:
            lines.append("| _\u65e0\u5206\u7ec4\u8868\uff1a`script/av_domain_ranges.json` \u672a\u767b\u8bb0\u8be5\u8bed\u5411_ | | | | | |")
        for name, count, b_ch, s_ch, b_co, s_co in _subdomain_rows(baselines[e], scoped[e], groups):
            lines.append(f"| {name} | {count} | {_fmt(b_ch, 1, 2)} | {_fmt(s_ch, 1, 2)} | "
                         f"{_fmt(b_co)} | {_fmt(s_co)} |")
        lines.append("")
    lines += _hypothesis_block(summary, baselines, scoped)
    lines += _reproduction_block(args)
    lines += getattr(args, "_extra_lines", None) or []
    lines += _limitations_block()
    lines += ["", "---", "", "## 变更记录", "",
              f"- {now[:10]}：run_av_baseline.py 自动生成；所有数字来自 `docs/evidence/av-domain-*.json`。",
              "- 每次重跑覆盖本文件；如需保留历史版本，先 `git add docs/av-domain-benchmark.md` 再跑。",
              ""]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def _hr_line_note(args) -> str:
    # Prefer an accurate label derived from the evidence we actually
    # loaded over a nominal mode flag: --report-only can regenerate the
    # report from --quick evidence, and it would otherwise lie and say
    # "全量 300 句".
    return getattr(args, "_evidence_note", "") or "全量 300 句"


def _compute_evidence_note(baselines, scoped) -> str:
    counts = []
    for side in (baselines, scoped):
        for run in side.values():
            if run:
                counts.append(run.get("evaluated") or 0)
    if not counts:
        return "无证据"
    lo, hi = min(counts), max(counts)
    if lo == hi:
        return f"每引擎 {lo} 句" + ("（--quick 冒烟）" if lo <= 30 else "（全量）" if lo >= 300 else "")
    return f"每引擎 {lo}-{hi} 句"


def _terminology_lines(summary) -> list:
    """Terminology adherence per engine, with the caveat that makes it readable.

    The rate is measured against the whole seed pack, while only a prefix of that
    pack can ever reach a prompt (defect D32). So a positive rate here is not proof
    that injection worked, and this line has to say which set was counted -- the
    alternative is a column that quietly means something else, which is how D31 got
    published in the first place.
    """
    lines = []
    for s in summary:
        base, scope = s.get("base_adherence"), s.get("scope_adherence")
        if not base and not scope:
            continue                     # older evidence: omit rather than invent
        if base and scope and not s.get("adherence_comparable"):
            lines.append(
                f"- **术语遵循率（{s['engine']}）**：两臂统计的机会数不同"
                f"（{base['opportunities']} vs {scope['opportunities']}），"
                "**不可相减**（D31 同类错误，只是换了一层）；请核对两臂是否跑同一句集。")
            continue
        parts = []
        for label, block in (("未接线", base), ("接线", scope)):
            if block and block["rate"] is not None:
                parts.append(f"{label} {block['hits']}/{block['opportunities']}"
                             f" = {block['rate']:.3f}")
        delta = s.get("adherence_delta")
        lines.append(
            "- **术语遵循率（{}，整包口径）**：{}{}"
            "——注意：这量的是“包里规定的译法有没有被用出”，不是质量分。分母是包内"
            "出现在源文的术语，与注入集无关，所以 D32 修复前后仍可比；自注入集改为按本句"
            "命中筛选（D32 已修完）起，此 Δ 可以归因到注入。".format(
                s["engine"], "、".join(parts) if parts else "未测",
                "，Δ={:+.3f}".format(delta) if delta is not None else "，Δ 未算"))
    if not lines:
        lines.append("- **术语遵循率**：本批 evidence 未包含该字段（早于该度量），不能得出任何结论。")
    return lines


def _hypothesis_block(summary, baselines, scoped) -> list:
    lines = ["## 差值与四条待验证假设", ""]
    lines += _terminology_lines(summary)
    qwen = next((s for s in summary if s["engine"] == "ollama-qwen"), None)
    if qwen and qwen["delta_comet"] is not None:
        # The groups of the direction this comparison was actually made in, so the subset named
        # below cannot come from another corpus's row order (D62).
        groups = _load_ranges(_run_direction(baselines.get("ollama-qwen")
                                             or scoped.get("ollama-qwen")))
        rows = _subdomain_rows(baselines.get("ollama-qwen"), scoped.get("ollama-qwen"), groups)
        # Found by name, never by position: this used to read `rows[6]` on the assumption that
        # the polysemy group is the seventh range. The corpus grew and added a second
        # polysemy group, so a positional read would either fall off the end of a clamped
        # table or silently describe a different sub-domain than the one it names.
        # A row is (name, count, base_chrF, scope_chrF, base_comet, scope_comet), so the
        # subset delta subtracts index 5-4 for COMET and 3-2 for chrF (a prior bug mixed 5-3
        # and produced a nonsense ~-48 number). Guard against NaN with x == x.
        poly_rows = [r for r in rows if r[0].startswith("\u591a\u4e49\u8bcd\u8fb9\u754c")]
        if not poly_rows:
            lines.append(
                "- **\u591a\u4e49\u8bcd\u8fb9\u754c\u5b50\u96c6**\uff1a\u672c\u6279\u8bc1\u636e\u672a\u8986\u76d6\u8be5\u5b50\u9886\u57df\u5206\u7ec4\uff0c"
                "\u4e0d\u7ed9\u5b50\u96c6\u5dee\u503c\u7684\u7ed3\u8bba\u3002")
        else:
            poly_row = poly_rows[0]
            _b_ch, _s_ch, _b_co, _s_co = poly_row[2], poly_row[3], poly_row[4], poly_row[5]
            poly_comet = (_s_co - _b_co) if (_s_co == _s_co and _b_co == _b_co) else None
            poly_chrf = (_s_ch - _b_ch) if (_s_ch == _s_ch and _b_ch == _b_ch) else None
            overall_comet = qwen["delta_comet"]
            overall_chrf = (qwen["scope_chrF"] - qwen["base_chrF"]
                            if qwen["scope_chrF"] and qwen["base_chrF"] else None)
            lines += [
                f"- **\u591a\u4e49\u8bcd\u8fb9\u754c\u5b50\u96c6 \u0394 vs \u5168\u6837 \u0394\uff08ollama-qwen\uff0c\u7ec4\u540d \u300c{poly_row[0]}\u300d\uff09**\uff1a"
                f"\u5168\u6837 COMET \u0394={_fmt_pct(overall_comet)}\u3001chrF \u0394={_fmt_pct(overall_chrf)}\uff1b"
                f"\u591a\u4e49\u8bcd\u5b50\u96c6 COMET \u0394={_fmt_pct(poly_comet)}\u3001chrF \u0394={_fmt_pct(poly_chrf)}\u3002"
                + ("\u591a\u4e49\u8bcd\u5b50\u96c6\u63d0\u5347\u5927\u4e8e\u5168\u6837\uff0c\u4e0e S11 \u79cd\u5b50\u5305\u300c\u672f\u8bed\u6d88\u6b67\u300d\u8bbe\u8ba1\u76ee\u6807\u4e00\u81f4\u3002"
                   if (poly_chrf is not None and overall_chrf is not None and poly_chrf > overall_chrf)
                   else "\u591a\u4e49\u8bcd\u5b50\u96c6\u672a\u89c1\u5927\u4e8e\u5168\u6837\u7684\u63d0\u5347\uff0c\u9700\u56de\u770b\u5177\u4f53\u8bc1\u636e\u3002"),
            ]
    # A backend that drops the term context cannot show a terminology effect by construction,
    # so a zero delta there is a statement about the engine, not about the pack. The wording
    # used to read "expected if |delta| < 0.005", which leaves a reader free to take +0.0000
    # as a measurement about terminology. Rendered off the same split the status column uses.
    applies, ignores = term_context_sides()
    for s in summary:
        if s["engine"] in ignores and s["delta_comet"] is not None:
            lines.append(
                f"- **{s['engine']} Δ**：{_fmt_pct(s['delta_comet'])}——该后端不施加术语上下文"
                "（`accepts_term_context` 取 trait 默认 `false`，CT2 / MADLAD checkpoint 读到术语数组也不使用），"
                "此 Δ 为**构造使然**，不得读作“术语表在该引擎上无效”的测量结论；"
                "已发布的 legacy 证据里它的接线臂与基线臂 300/300 行逐字相同。")
    if applies:
        lines.append(f"- **可归因到术语接线的引擎**：{'、'.join(applies)}；只有它们的 Δ 能作术语效果解读。")
    lines += ["", "_(更多细节从 evidence/*.json 的 samples 数组复现)_", ""]
    return lines


def _reproduction_block(args) -> list:
    """The reproduction section.

    It used to list only four commands, none of which could reproduce the per-direction or
    per-size tables further down the same document -- a reader copying them would get one
    direction, one size and both arms. The scope knobs are now spelled out, because the
    identity of a table (D51) is decided by exactly those flags.
    """
    return [
        "## 复现",
        "",
        "```bash",
        "# Windows",
        "run_av_baseline.bat",
        "",
        "# Linux / macOS",
        "./run_av_baseline.sh",
        "",
        "# 表里每一张的身份由下面四个旋钮决定，缺一个就不是同一张表（D51）：",
        "python script/run_av_baseline.py --directions zh-en,en-zh            # 语向",
        "python script/run_av_baseline.py --n 1000                            # 句数",
        "python script/run_av_baseline.py --repeats 2                         # 每臂复采次数（给噪声地板）",
        "python script/run_av_baseline.py --engines argos,madlad --only-baseline   # 不施加术语上下文的引擎只跑基线",
        "",
        "# 其它变体",
        "python script/run_av_baseline.py --preflight-only   # 只体检",
        "python script/run_av_baseline.py --quick            # 前 20 句冒烟",
        "python script/run_av_baseline.py --skip-comet       # 不跑 COMET（快）",
        "python script/run_av_baseline.py --report-only      # 只重出报告（不动证据）",
        "```",
        "",
    ]


def _corpus_size_note(unit="对"):
    """The corpus's own current row count, phrased so this section cannot go stale.

    The limitation text used to state 300 pairs and "growing to 500-1000 sits in S12 T4" long
    after the thousandth row had landed. A dated claim in a generated document is the failure
    this report keeps being edited for: the prose stops matching the thing it describes.
    """
    rows = _corpus_rows(DIRECTIONS[0], None, None)
    return "%d %s" % (rows, unit) if rows else "（行数见 script/eval-data/）"


def _direction_note():
    """The directions the runner actually knows, read off DIRECTIONS rather than restated."""
    return "、".join(d.replace("-", "\u2192") for d in DIRECTIONS)


def _limitations_block() -> list:
    return [
        "## 局限与免责（写死，不回填）",
        "",
        "- **域覆盖窄**：AV / 多媒体技术只是众多专业域之一；本报告数字**不外推**到医疗 / 法律 / 金融等域；",
        ("- **语料自撰**：" + _corpus_size_note("对") + "由本项目工程师手写，风格偏技术描述，"
         "与真实用户素材（会议记录、视频教程、播客）在句长、口语度、噪声水平上仍有分布差；"
         "扩样按追加式进行，旧身份的行序不变，故历史数字仍可对同一子集复算；"),
        ("- **语言对覆盖**：" + _direction_note() + "；en\u2192zh 由 zh\u2192en 的语料列对调派生（S12 T2），"
         "不是独立采写的第二套句子，两语向的结论因此不完全独立；ja/ko/fr/de/es/ru/pt/it/ar 仍挂在 S12 T2；"),
        ("- **术语注入的机制依赖**：会施加术语上下文的引擎：" + ("、".join(term_context_sides()[0]) or "无")
         + "；不会施加术语上下文的引擎：" + ("、".join(term_context_sides()[1]) or "无")
         + "（来源 `translator-engine/src/engine/mod.rs` 的 `accepts_term_context` 及其测试断言，"
         "本节与表内状态列读同一张表）。后者的 Δ 恒为 0 属**构造使然**，"
         "不是“S11 对这些引擎无效”的测量结论——这是**引擎能力边界**，不是 S11 缺陷；"),
        "- **COMET 参考不完美**：在通用域与人工判断相关性高，在窄域技术文本上会下降；本报告数字作为**同引擎同批样本的相对 Δ**解读；",
        ("- **chrF 不跨语向可比**：本报告把 " + _direction_note() + " 的表并排放在同一页上，"
         "而 chrF 量的是目标侧的字符重叠，两个语向的参考文本长度不同，所以**同一引擎在不同语向的 chrF 绝对值不是同一把尺**（三个引擎均表现为 zh→en 高很多）；"
         "这**不是**“那个语向译得更差”。要跨语向比，就只比同一语向内的 Δ 与同臂地板。"),
        "- **D32 / D34 与本报告的时间关系**：种子包 266 条 av 行当时同为 confidence 0.90——成因是 **D34**："
        "导入器“缺失即 1.0”的兜底写的是 `confidence <= 0.0`，而 `GlossaryEntry::from_json` 已先把缺失键填成 0.9，"
        "该兜底从 shipped 包永远走不到（2026-10-08 已按“键是否存在”修正并重导种子包，现这些行为 1.0）；"
        "平局又原靠每次调用新建的 `HashMap` 迭代序（同进程连问 5 次，20 条窗口两两只有 3-5 条重合），"
        "而那 20 条是包前缀、与本句是否用到无关（实测固定前缀在 300 行里只让 25 行（8.3%）拿到至少一条"
        "本句相关术语，平均 0.09 条/行，而整包口径每行真有 2.22 条）。2026-10-07 同日两步修完 D32：四处 listing 加"
        "确定序 tie-break，再把 `build_context` 改为按本句命中筛选、先筛后截。**本表已是 D32 与 D34 都修完后的重跑**；"
        "D34 重导后各指标与上一轮（仅修完 D32）基线一致且在复采波动内：COMET Δ 由 +0.0030 到本表的 +0.0035、"
        "遵循率 Δ 由 +0.222 到 +0.212（同臂地板 ±0.0010 / ±0.0060），所以 confidence 修正**不改变结论**；"
        "而修复前那轮的 COMET Δ=+0.0001（落在噪声地板内）与遵循率 Δ=+0.024 **自本日起作废**——它们测的是"
        "“投一组与本句基本无关的固定术语”，不是术语包的能力；",
        "- **术语遵循率的口径**：evidence 里的 `term_adherence` 是**整包口径**（包内命中的源术语 → "
        "规定译法是否出现），英文术语按词边界匹配；它与注入集无关，所以修复前后仍可比；"
        "“注入集口径”在修复前无从定义（集合不定），现已可定义（集合由句子决定），本报告仍按整包口径披露；",
        "- **Δ 必须有噪声地板才成立**：同臂两次复采（`--repeats 2`）的均值差即地板，|Δ| 在地板内不足以归因——"
        "ollama 档以 `temperature 0.3` 且无 seed 生成，单跑一次无法区分术语效果与采样噪声。"
        "**引擎失败行已从分母剔除**（defect D31：曾把 19 行空译文当“译得差”计入，使 Δ 高估至 +0.0330）；",
        ("- **对外披露口径**：本报告的绝对数字（例如「COMET 0.87」）**不单独抽出对外宣传**，"
         "必须与「AV 域自撰 " + _corpus_size_note("句") + "、" + _direction_note() + "、未做过第三方审计」这些限定一起出现，"
         "并同报语向与句数（D51：不同测量身份的数不可混用）。"),
        "",
    ]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--quick", action="store_true",
                   help="first 20 pairs, skip COMET (smoke)")
    p.add_argument("--n", type=int, default=None,
                   help="limit to first N pairs per run (overrides --quick)")
    p.add_argument("--repeats", type=int, default=1,
                   help="run each arm this many times into the same evidence file. "
                        "ollama answers at temperature 0.3 with no seed, so a single "
                        "run per arm cannot separate a terminology effect from "
                        "sampling noise: repeats give the noise floor (defect D31)")
    p.add_argument("--preflight-only", action="store_true",
                   help="check environment, then exit")
    p.add_argument("--report-only", action="store_true",
                   help="regenerate report from existing evidence/*.json")
    p.add_argument("--only-baseline", action="store_true",
                   help="skip the --domain av pass")
    p.add_argument("--skip-comet", action="store_true",
                   help="do not run score_comet.py")
    p.add_argument("--engines", default=",".join(ENGINES),
                   help="comma-separated subset of argos,madlad,ollama-qwen")
    p.add_argument("--directions", default=",".join(DIRECTIONS),
                   help="comma-separated corpus directions, e.g. zh-en,en-zh (S12 T2)")
    p.add_argument("--force", action="store_true",
                   help="with --report-only, overwrite the skeleton even when no "
                        "evidence JSON files exist")
    return p


class _Tee:
    """Mirror everything written to one stream into a log file too.

    Keeps the interactive console live (so a foreground run still shows
    progress) while persisting the same text for a double-click whose
    window vanishes on exit. fileno is delegated so any child process
    spawned with stdout=sys.stdout still inherits the real handle."""

    def __init__(self, stream, log_handle):
        self._stream = stream
        self._log = log_handle

    def write(self, data):
        n = self._stream.write(data)
        try:
            self._log.write(data)
            self._log.flush()
        except Exception:  # noqa: BLE001 (never fail the run for logging)
            pass
        return n

    def flush(self):
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._log.flush()
        except Exception:  # noqa: BLE001
            pass

    def fileno(self):
        return self._stream.fileno()

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:  # noqa: BLE001
            return False


def log_path_for(argv) -> pathlib.Path:
    """Which durable log this invocation writes.

    A read-only call (--report-only / --preflight-only, checked before argparse so the choice
    exists before the run does) must not truncate the log of a bench that is still running.
    This session lost the argos/madlad matrix trace exactly that way: a `--report-only` opened
    the same path in "w" while the matrix was mid-flight. The bench keeps the historic name so
    anyone following the documented path still finds the run's trace.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--report-only" in argv or "--preflight-only" in argv:
        return REPO / "run_av_baseline.report.log"
    return RUN_LOG


def main(argv=None) -> int:
    """Wrap the pipeline so stdout/stderr land in run_av_baseline.log too.

    Uses line-buffered text mode and swallows any logging error -- losing
    the mirror must never abort a 1-3 hour measurement run."""
    log_path = log_path_for(argv)
    log_handle = None
    orig_out, orig_err = sys.stdout, sys.stderr
    try:
        log_handle = open(log_path, "w", encoding="utf-8")
    except OSError as exc:
        print(f"[warn] cannot open {log_path} for logging: {exc}", file=sys.stderr)
        log_handle = None
    if log_handle:
        stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        log_handle.write(f"=== run_av_baseline started {stamp} ===\n")
        log_handle.write(f"args: {argv if argv is not None else sys.argv[1:]}\n\n")
        log_handle.flush()
        sys.stdout = _Tee(orig_out, log_handle)
        sys.stderr = _Tee(orig_err, log_handle)
    try:
        rc = _run_all(argv)
    except BaseException:  # noqa: BLE001 (log then re-raise)
        if log_handle:
            import traceback
            log_handle.write("\n=== UNCAUGHT EXCEPTION ===\n")
            log_handle.write(traceback.format_exc())
            log_handle.flush()
        raise
    finally:
        sys.stdout, sys.stderr = orig_out, orig_err
        if log_handle:
            try:
                log_handle.write(f"\n=== run_av_baseline finished rc={rc} ===\n")
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass
            orig_out.write(f"(full log saved to {log_path})\n")
    return rc


def _run_all(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.engines = tuple(e.strip() for e in args.engines.split(",") if e.strip())
    args.directions = tuple(d.strip() for d in args.directions.split(",") if d.strip())
    unknown = [e for e in args.engines if e not in ENGINES]
    if unknown:
        print(f"unknown engine(s) {unknown}; valid: {ENGINES}", file=sys.stderr)
        return 1
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        summary = generate_report(args)
        if not summary:
            # generate_report declined (no evidence). Exit non-zero so a
            # caller chaining on `&&` notices.
            return 3
        print(f"report regenerated at {REPORT_PATH}")
        return 0

    _hr("Preflight")
    problems = preflight(args)
    if problems:
        print("Environment is not ready:")
        for p in problems:
            print(f"  [X] {p}")
        return 1
    print("  [OK] preflight clean")
    if args.preflight_only:
        return 0

    procs = []
    try:
        # start the sidecars we'll need and don't already have up
        for engine in ("argos", "madlad"):
            if engine not in args.engines:
                continue
            port = SIDECAR_PORTS[engine]
            if _sidecar_running(port):
                print(f"  [sidecar] {engine} already up on {port}, reusing")
                continue
            procs.append(_start_sidecar(engine, port))

        install_pack()
        n = args.n or (20 if args.quick else None)
        av_root = detect_av_root()
        if av_root:
            print(f"  [corpus] default OS root had no pair files; "
                  f"using in-repo starter at {av_root}")
        # Phase 1: run ALL benches while sidecars are alive.
        # COMET was previously scored inline after each engine (so that
        # the GPU work overlapped bench progress), but that evicts the
        # madlad CT2 model from VRAM mid-run (RTX 4050 Laptop, 6 GB).
        # Separating the phases lets the GPU stay dedicated to whichever
        # consumer is active.
        evidence_files = []
        for engine in args.engines:
            for direction in args.directions:
                # Counted once per direction and reused for every arm of this run: the
                # identity in the file name has to match what the bench actually wrote.
                rows = _corpus_rows(direction, n, av_root)
                for rep in range(1, max(1, args.repeats) + 1):
                    base = run_bench_with_retry(
                        engine, direction, "", n, av_root, procs, rep, rows)
                    evidence_files.append(base)
                    if not args.only_baseline:
                        scoped = run_bench_with_retry(
                            engine, direction, "av", n, av_root, procs, rep, rows)
                        evidence_files.append(scoped)
        # Phase 2: kill sidecars to free GPU before COMET loads.
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        procs.clear()
        # Phase 3: COMET scoring (needs GPU, now free). Repeats live inside the same
        # file, so score each artifact once -- score_comet.py walks `runs` itself.
        if not (args.skip_comet or args.quick):
            for ev in dict.fromkeys(evidence_files):
                score_comet(ev)
        # Phase 4: report.
        summary = generate_report(args)
    except subprocess.CalledProcessError as exc:
        print(f"subprocess failed: {exc.cmd} (exit {exc.returncode})", file=sys.stderr)
        return 2
    finally:
        # Safety net: if the phased approach above already terminated
        # procs normally, this second pass is a no-op. If an exception
        # hit during bench or COMET, this cleans up orphans.
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    if not summary:
        return 0

    _hr("Summary")
    for s in summary:
        print(f"  {s['engine']:<12s}  baseline COMET={_fmt(s['base_comet'])}"
              f"  scoped COMET={_fmt(s['scope_comet'])}"
              f"  delta={_fmt_pct(s['delta_comet'])}"
              f"  paired n={s['paired_n']}  repeats={s['repeats']}"
              f"  failed rows={s['failed_rows']}")
        print(f"  {'':<12s}  {s['status']}")
    print()
    print(f"report: {REPORT_PATH.relative_to(REPO.parent)}")
    print("review + commit:")
    print("  git --no-pager diff docs/av-domain-benchmark.md docs/evidence/")
    print("  git add docs/av-domain-benchmark.md docs/evidence/")
    print("  git commit -m \"docs: fill AV-domain baseline numbers from live run\"")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO / "script"))
    sys.exit(main())
