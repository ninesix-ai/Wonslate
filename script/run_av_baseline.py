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
EVIDENCE_DIR = REPO / "docs" / "evidence"
REPORT_PATH = REPO / "docs" / "av-domain-benchmark.md"
RANGES_PATH = REPO / "script" / "av_domain_ranges.json"
PACK_PATH = REPO / "docs" / "glossary-packs" / "av-zh-en.json"
CORPUS_DIR = REPO / "script" / "eval-data" / "av-zh-en"
SIDECAR_PY = REPO / "sidecar" / "ct2_sidecar.py"
BENCH_PY = REPO / "script" / "bench_domain_av.py"
INSTALL_PY = REPO / "script" / "install_glossary_pack.py"
SCORE_PY = REPO / "script" / "score_comet.py"

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
OLLAMA_MODEL = "qwen3:8b"
DIRECTIONS = ("zh-en",)   # the starter corpus is zh->en; en->zh is S12 T2
ENGINES = ("argos", "madlad", "ollama-qwen")


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
    if not CORPUS_DIR.joinpath("av-zh-en.src").is_file():
        problems.append(f"corpus missing at {CORPUS_DIR}")
    if not PACK_PATH.is_file():
        problems.append(f"AV seed pack missing at {PACK_PATH}")
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
            f"`ollama pull {OLLAMA_MODEL}` has finished.")
    return problems


def _probe_url(url: str, timeout: float = 2.0) -> bool:
    try:
        with urlopen(Request(url), timeout=timeout) as r:
            return 200 <= r.status < 300
    except (URLError, OSError):
        return False


def _sidecar_running(port: int) -> bool:
    return _probe_url(f"http://127.0.0.1:{port}/health", timeout=1.0)


def _start_sidecar(engine: str, port: int) -> subprocess.Popen:
    log = open(EVIDENCE_DIR / f"sidecar-{engine}.log", "ab", buffering=0)
    env = os.environ.copy()
    env.setdefault("WONSLATE_ENGINE", engine)
    backend = SIDECAR_BACKENDS[engine]
    proc = subprocess.Popen(
        [sys.executable, str(SIDECAR_PY), "--backend", backend, "--port", str(port)],
        stdout=log, stderr=log, cwd=REPO, env=env)
    deadline = time.time() + 120
    while time.time() < deadline:
        if _sidecar_running(port):
            print(f"  [sidecar] {engine} ready on {port} (backend={backend})")
            return proc
        if proc.poll() is not None:
            raise SystemExit(
                f"{engine} sidecar exited early (code {proc.returncode})\n"
                f"  command: python {SIDECAR_PY} --backend {backend} --port {port}\n"
                f"  log:     {log.name}")
        time.sleep(2)
    proc.terminate()
    raise SystemExit(f"{engine} sidecar did not become healthy on {port} in 120s")


def install_pack() -> None:
    _hr("Installing AV seed pack")
    subprocess.check_call([sys.executable, str(INSTALL_PY), "--domain", "av"])


def run_bench(engine: str, direction: str, domain: str, n: int | None,
              av_root: pathlib.Path | None = None) -> pathlib.Path:
    """Invoke bench_domain_av once; return the evidence path it wrote."""
    cmd = [sys.executable, str(BENCH_PY), "--engine", engine,
           "--direction", direction]
    if domain:
        cmd += ["--domain", domain]
    if n:
        cmd += ["--n", str(n)]
    if av_root:
        cmd += ["--av-root", str(av_root)]
    tag = f"-{domain}" if domain else ""
    expected = EVIDENCE_DIR / f"av-domain-{engine}{tag}.json"
    _hr(f"Bench: engine={engine} dir={direction} domain={domain or '(unscoped)'}"
        + (f" n={n}" if n else ""))
    subprocess.check_call(cmd)
    if not expected.exists():
        raise SystemExit(f"expected evidence file {expected} not written")
    return expected


def run_bench_with_retry(engine, direction, domain, n, av_root, procs):
    """Run bench_domain_av; if the sidecar crashed mid-bench, restart it and retry.

    madlad's 3B CT2 model can OOM on a 6 GB laptop GPU, especially when
    COMET loads its own model concurrently. By restarting the sidecar
    once on failure, the runner is robust to that transient condition
    without silently losing a whole engine's results.
    """
    try:
        return run_bench(engine, direction, domain, n, av_root)
    except subprocess.CalledProcessError:
        port = SIDECAR_PORTS.get(engine)
        if port and not _sidecar_running(port):
            print(f"  [retry] {engine} sidecar died (port {port} gone); "
                  f"restarting and retrying bench...", flush=True)
            procs.append(_start_sidecar(engine, port))
            return run_bench(engine, direction, domain, n, av_root)
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


def _load_ranges() -> list[dict]:
    data = json.loads(RANGES_PATH.read_text(encoding="utf-8"))
    return data["ranges"]


def _per_domain_means(run: dict | None, ranges: list[dict], key: str) -> list[float]:
    """Return per-range value of `key` (chrF / BLEU / comet).

    For `comet` we mean the parallel `comet_scores_per_sample` array that
    score_comet.py attaches to a run. For `chrF` / `BLEU` we call
    bench_translation.score on the range's slice so we get a real
    corpus-level number over ~25 sentences rather than an average of
    per-sample scores (which sacrebleu does not natively expose).

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
                    and s.get("id", -1) < len(scores)]
            out.append(statistics.fmean(vals) if vals else float("nan"))
        return out
    try:
        from bench_translation import score  # noqa: WPS433 (lazy)
    except ImportError:
        return [float("nan")] * len(ranges)
    out = []
    for rng in ranges:
        subset = [s for s in samples
                  if rng["start"] <= s.get("id", -1) + 1 <= rng["end"]]
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


def generate_report(args):
    """Read every evidence file, compute the tables, and rewrite the report.
    Returns the summary rows so main() can print a compact table too.
    An empty list means "declined because there is no evidence"; main()
    uses that as a signal to exit non-zero in --report-only mode."""
    ranges = _load_ranges()
    engines = list(args.engines)
    baselines = {e: _aggregate(EVIDENCE_DIR / f"av-domain-{e}.json") for e in engines}
    scoped = {e: _aggregate(EVIDENCE_DIR / f"av-domain-{e}-av.json") for e in engines}
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

    summary = []
    for e in engines:
        base, scope = baselines[e], scoped[e]
        b_comet, s_comet = _metric(base, "comet"), _metric(scope, "comet")
        b_chrf, s_chrf = _metric(base, "chrF"), _metric(scope, "chrF")
        delta = (s_comet - b_comet) if (b_comet is not None and s_comet is not None) else None
        summary.append({
            "engine": e,
            "has_base": bool(base), "has_scope": bool(scope),
            "base_comet": b_comet, "scope_comet": s_comet,
            "base_chrF": b_chrf, "scope_chrF": s_chrf,
            "delta_comet": delta,
        })
    args._evidence_note = _compute_evidence_note(baselines, scoped)
    _write_report(ranges, baselines, scoped, summary, args)
    return summary


def _fmt(v, width=4, digits=4):
    if v is None or v != v:  # None or NaN
        return "_无数据_"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def _fmt_pct(v):
    if v is None or v != v:
        return "_无数据_"
    return f"{v:+.4f}"


def _subdomain_rows(base: dict | None, scope: dict | None, ranges: list[dict]) -> list:
    """Return (name, count, base_chrF, scope_chrF, base_comet, scope_comet) per range."""
    b_ch = _per_domain_means(base, ranges, "chrF")
    s_ch = _per_domain_means(scope, ranges, "chrF")
    b_co = _per_domain_means(base, ranges, "comet")
    s_co = _per_domain_means(scope, ranges, "comet")
    rows = []
    for i, rng in enumerate(ranges):
        count = rng["end"] - rng["start"] + 1
        rows.append((rng["name"], count, b_ch[i], s_ch[i], b_co[i], s_co[i]))
    return rows


def _write_report(ranges, baselines, scoped, summary, args) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    engines = list(args.engines)
    lines = [
        f"# AV 领域质量基准报告（{now} 自动回填）",
        "",
        "> 定位：本报告是 Wonslate **S11 领域种子包收益**的对外证据。与 flores-benchmark.md（通用域）、",
        "> translation-benchmark.md（108 句回归基线）分工不同：本报告的样本与指标专为「AV / 多媒体技术域」设计。",
        "> 数据：`script/eval-data/av-zh-en/`（本项目自撰，Apache-2.0，300 对 zh→en，12 类子领域分组）。",
        "> 指标：sacrebleu 的 chrF++ / BLEU 与 COMET-22 (Unbabel/wmt22-comet-da)。",
        f"> 生成：`python script/run_av_baseline.py`（本文件由该脚本自动写入，{_hr_line_note(args)}）。",
        "",
        "## TL;DR",
        "",
        "| 引擎 | 未接线 COMET | 接线 COMET (`--domain av`) | Δ | 未接线 chrF | 接线 chrF | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        status = "✅ 完整" if (s["has_base"] and s["has_scope"]) else "⚠️ 部分"
        lines.append(f"| {s['engine']} | {_fmt(s['base_comet'])} | {_fmt(s['scope_comet'])} | "
                     f"{_fmt_pct(s['delta_comet'])} | {_fmt(s['base_chrF'], 1, 2)} | "
                     f"{_fmt(s['scope_chrF'], 1, 2)} | {status} |")
    lines += ["", "## 分引擎逐子领域", ""]
    for e in engines:
        lines += [
            f"### {e}",
            "",
            "| 子领域 | 句对数 | 未接线 chrF | 接线 chrF | 未接线 COMET | 接线 COMET |",
            "|---|---|---|---|---|---|",
        ]
        for name, count, b_ch, s_ch, b_co, s_co in _subdomain_rows(baselines[e], scoped[e], ranges):
            lines.append(f"| {name} | {count} | {_fmt(b_ch, 1, 2)} | {_fmt(s_ch, 1, 2)} | "
                         f"{_fmt(b_co)} | {_fmt(s_co)} |")
        lines.append("")
    lines += _hypothesis_block(summary, baselines, scoped, ranges)
    lines += _reproduction_block(args)
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


def _hypothesis_block(summary, baselines, scoped, ranges) -> list:
    lines = ["## 差值与四条待验证假设", ""]
    qwen = next((s for s in summary if s["engine"] == "ollama-qwen"), None)
    if qwen and qwen["delta_comet"] is not None:
        poly = ranges[6]  # "多义词边界" is index 6
        rows = _subdomain_rows(baselines.get("ollama-qwen"), scoped.get("ollama-qwen"), ranges)
        poly_row = rows[6]
        # poly_row = (name, count, b_ch, s_ch, b_co, s_co)
        poly_delta = (poly_row[5] - poly_row[3]) if (poly_row[3] == poly_row[3] and poly_row[5] == poly_row[5]) else None
        overall_delta = qwen["delta_comet"]
        lines += [
            f"- **多义词边界子集 Δ vs 全样 Δ**：全样 Δ={_fmt_pct(overall_delta)}，"
            f"多义词 Δ={_fmt_pct(poly_delta)}。"
            + ("多义词子集 Δ 更大，与 S11 种子包设计目标一致。"
               if (poly_delta is not None and overall_delta is not None and poly_delta > overall_delta)
               else "差值不显著或反向，需查看具体证据。"),
        ]
    if any(s["engine"] == "madlad" and s["delta_comet"] is not None for s in summary):
        m = next(s for s in summary if s["engine"] == "madlad")
        lines.append(f"- **madlad Δ**：{_fmt_pct(m['delta_comet'])}——madlad 是 Marian 系，"
                     "上下文注入路径与 LLM 不同，Δ 通常小于 ollama-qwen。")
    if any(s["engine"] == "argos" and s["delta_comet"] is not None for s in summary):
        a = next(s for s in summary if s["engine"] == "argos")
        lines.append(f"- **argos Δ**：{_fmt_pct(a['delta_comet'])}。若 |Δ| < 0.005 属预期："
                     "argos 走 Marian，不消费 prompt 术语。非零时须单独解释。")
    lines += ["", "_(更多细节从 evidence/*.json 的 samples 数组复现)_", ""]
    return lines


def _reproduction_block(args) -> list:
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
        "# 常用变体",
        "python script/run_av_baseline.py --preflight-only   # 只体检",
        "python script/run_av_baseline.py --quick            # 前 20 句冒烟",
        "python script/run_av_baseline.py --skip-comet       # 不跑 COMET（快）",
        "python script/run_av_baseline.py --report-only      # 只重出报告",
        "```",
        "",
    ]


def _limitations_block() -> list:
    return [
        "## 局限与免责（写死，不回填）",
        "",
        "- **域覆盖窄**：AV / 多媒体技术只是众多专业域之一；本报告数字**不外推**到医疗 / 法律 / 金融等域；",
        "- **语料自撰**：300 对由本项目工程师手写，风格偏技术描述，与真实用户素材（会议记录、视频教程、播客）在句长、口语度、噪声水平上仍有分布差；扩到 500-1000 对挂在 S12 T4；",
        "- **单语言对**：仅 zh→en；ja/ko/fr/de/es/ru/pt/it/ar 与 en→zh 挂在 S12 T2；",
        "- **术语注入的机制依赖**：madlad / qwen 通过 prompt 消费术语表；argos（Marian 系）不吃这个上下文，Δ 可能为 0——这是**引擎能力边界**，不是 S11 缺陷；",
        "- **COMET 参考不完美**：在通用域与人工判断相关性高，在窄域技术文本上会下降；本报告数字作为**同引擎同批样本的相对 Δ**解读；",
        "- **一次运行 vs 波动**：延迟类指标受本机并发负载影响；质量类指标理论稳定但依赖模型端点稳定；跨日重跑出现偏差时以 evidence/*.json 为准；",
        "- **对外披露口径**：本报告的绝对数字（例如「COMET 0.87」）**不单独抽出对外宣传**，必须与「AV 域自撰 300 句、zh→en、未做过第三方审计」三个限定一起出现。",
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
    p.add_argument("--force", action="store_true",
                   help="with --report-only, overwrite the skeleton even when no "
                        "evidence JSON files exist")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.engines = tuple(e.strip() for e in args.engines.split(",") if e.strip())
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
            for direction in DIRECTIONS:
                base = run_bench_with_retry(
                    engine, direction, "", n, av_root, procs)
                evidence_files.append(base)
                if not args.only_baseline:
                    scoped = run_bench_with_retry(
                        engine, direction, "av", n, av_root, procs)
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
        # Phase 3: COMET scoring (needs GPU, now free).
        if not (args.skip_comet or args.quick):
            for ev in evidence_files:
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
              f"  delta={_fmt_pct(s['delta_comet'])}")
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
