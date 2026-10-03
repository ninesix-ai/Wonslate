#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""bench_domain_av.py -- score Wonslate engines on the AV-domain sentence set.

Why: FLORES-101 (bench_flores.py) covers news/wiki and cannot back an
"we handle AV/multimedia terminology correctly" claim. S11 shipped the
domain-scoped glossary wiring, but without an AV evaluation set the
quality delta stays qualitative. This harness closes that gap: it runs
a fixed, curated zh<->en sentence pair set through each registered
engine and reports chrF++ / BLEU / COMET alongside the same evidence
JSON layout bench_flores.py writes.

Method (fixed, for reproducibility):
  * pair directory `<prefix>/` holding `<prefix>.src` (source column)
    and `<prefix>.tgt` (reference column), UTF-8, one segment per line;
  * T1 ships the `av-zh-en` starter set (in-repo under
    `script/eval-data/av-zh-en/`); T2 in the task card extends to
    ja/ko/de/fr/es/ru/pt/it/ar and grows to 500-1000 pairs;
  * engines invoked over the same path as bench_flores (sidecar
    /translate, Ollama /v1/chat/completions with the exact prompt of
    engine/ollama.rs) via bench_translation.build_engines;
  * metrics: sacrebleu chrF++ and BLEU with the CJK conventions already
    used in bench_translation.py; COMET via score_comet.py afterwards.

Root resolution, highest first: --av-root, then $WONSLATE_AV_DIR, then
<data_dir>/benchmarks/av-domain where <data_dir> is the same per-OS
user data directory the model fetchers and FLORES harness use
(sidecar/ct2_sidecar.py::default_data_dir). A root holding no pair
directory is an explicit failure (AvDataNotFound) that names every
candidate it tried and the env var to set -- never a silent fallback
to some other corpus (REQ-B2).

The in-repo starter corpus at `script/eval-data/` is a *separate*
default: to run against it, set WONSLATE_AV_DIR to that directory (or
pass --av-root). The env-var-first resolution keeps user-supplied
larger corpora from being shadowed by whatever ships with the repo.

Usage:
    python script/bench_domain_av.py --list
    python script/bench_domain_av.py --engine argos --direction zh-en
    python script/bench_domain_av.py --engine madlad --direction zh-en --n 20
"""
import argparse
import datetime
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

EVIDENCE_DIR = ROOT / "docs" / "evidence"
DEFAULT_AV_SUBDIR = pathlib.Path("benchmarks") / "av-domain"
AV_DIR_ENV = "WONSLATE_AV_DIR"

# Directory names the harness knows about, in the canonical
# `<prefix>/{prefix}.src, {prefix}.tgt` layout. T1 covers zh<->en;
# T2 (task card) extends to the other ten registered product languages.
PAIRS = ("av-zh-en",)

# Engine ids this harness accepts. Must stay in lock-step with the four
# registered ids in translator-engine/src/engine/mod.rs -- the AI leg
# is exposed as `ollama-qwen` on the FFI side (engine name as reported),
# not by the internal "ollama" id, so callers can see which model ran.
ENGINES = ("argos", "madlad", "ollama-qwen")


class AvDataNotFound(Exception):
    """The corpus is missing at every candidate path. Explicit failure
    rather than silently reading some other directory (REQ-B2)."""


def _default_data_dir():
    """Per-OS user data directory, shared with the model fetchers and
    the FLORES harness. Imported lazily so `--list` and the hermetic
    unit tests do not pay the sidecar package's import cost."""
    from sidecar.ct2_sidecar import default_data_dir  # noqa: WPS433 (lazy)
    return default_data_dir()


def resolve_av_root(explicit_root=None):
    """--av-root > $WONSLATE_AV_DIR > <data_dir>/benchmarks/av-domain.

    A blank or whitespace-only environment value counts as unset, so a
    stray `set WONSLATE_AV_DIR=` cannot resolve the current directory
    instead -- matches resolve_flores_root in bench_flores.py (S10 T1)."""
    if explicit_root:
        return pathlib.Path(explicit_root)
    from_env = os.environ.get(AV_DIR_ENV, "").strip()
    if from_env:
        return pathlib.Path(from_env)
    return _default_data_dir() / DEFAULT_AV_SUBDIR


def _pair_dir_candidates(root, prefix):
    """Directories that could hold one pair, tried in this order.

    Layout 1 (`<root>/<prefix>/<prefix>.src`) is what ships and what
    `install_glossary_pack.py` populates; layout 2 (`<root>/<prefix>.src`)
    lets a caller point --av-root straight at a directory that already
    holds the pair files, without an extra nesting level."""
    root = pathlib.Path(root)
    yield root / prefix
    yield root


def find_pair_dir(root, prefix):
    """Return the directory that holds {prefix}.src and {prefix}.tgt.

    Raises AvDataNotFound listing every candidate tried and the env var
    to set, so a fresh operator can recover from the traceback alone."""
    for candidate in _pair_dir_candidates(root, prefix):
        if (candidate / (prefix + ".src")).is_file() and \
           (candidate / (prefix + ".tgt")).is_file():
            return candidate
    raise AvDataNotFound(
        "no AV-domain pair files ({}.src/{}.tgt) under {} -- tried: {}. "
        "Copy the in-repo starter `script/eval-data/{}` there, point "
        "--av-root at a directory that already has them, or set {}.".format(
            prefix, prefix, root,
            ", ".join(str(c) for c in _pair_dir_candidates(root, prefix)),
            prefix, AV_DIR_ENV))


def load_pair(pair_dir, prefix):
    """Read source / reference columns for one pair directory.

    Both files must be UTF-8 with equal line counts after trailing
    whitespace is dropped; a length mismatch is a corpus bug and is
    reported as such rather than silently truncating to the shorter."""
    src_path = pair_dir / (prefix + ".src")
    tgt_path = pair_dir / (prefix + ".tgt")
    src = [ln.rstrip() for ln in src_path.read_text(encoding="utf-8").splitlines()
           if ln.strip()]
    tgt = [ln.rstrip() for ln in tgt_path.read_text(encoding="utf-8").splitlines()
           if ln.strip()]
    if len(src) != len(tgt):
        raise AvDataNotFound(
            "{} has {} lines but {} has {} -- they must pair 1:1".format(
                src_path, len(src), tgt_path, len(tgt)))
    return src, tgt


def build_arg_parser():
    """Expose the CLI shape as a factory so hermetic tests can construct
    a parser without running main(). Adding a new pair or engine is a
    one-line change to the constants above."""
    parser = argparse.ArgumentParser(
        description="Score Wonslate engines on the AV-domain sentence set.")
    parser.add_argument("--engine", choices=ENGINES,
                        help="one registered engine id")
    parser.add_argument("--direction", default="zh-en",
                        choices=("zh-en", "en-zh"),
                        help="translation direction (default: zh-en)")
    parser.add_argument("--av-root", default=None,
                        help="override the corpus root; highest priority")
    parser.add_argument("--n", type=int, default=None,
                        help="limit to first N pairs (smoke)")
    parser.add_argument("--list", action="store_true",
                        help="print known pairs and how many lines each side has")
    return parser


def _print_pairs(root):
    """--list mode: report each pair's directory and count, or note that
    it is not installed. Never raises -- the whole point is to run
    before setting up the corpus to see what the harness can find."""
    print("av-domain root: {}".format(root))
    for prefix in PAIRS:
        try:
            pair_dir = find_pair_dir(root, prefix)
        except AvDataNotFound:
            print("  {} : NOT INSTALLED".format(prefix))
            continue
        src, _ = load_pair(pair_dir, prefix)
        print("  {} : {} pairs at {}".format(prefix, len(src), pair_dir))


def _run_one(engine_id, direction, src, tgt):
    """Invoke one engine over the shared bench_translation client and
    return (hypotheses, metrics). Lazy-imported so the hermetic tests
    do not need a running sidecar or Ollama."""
    from bench_translation import build_engines, score  # noqa: WPS433 (lazy)
    engines = build_engines([engine_id])
    engine = engines[engine_id]
    src_lang, tgt_lang = direction.split("-")
    hypotheses = [engine.translate(line, (src_lang, tgt_lang)) for line in src]
    metrics = score(hypotheses, tgt, src_lang=src_lang, tgt_lang=tgt_lang)
    return hypotheses, metrics


def _write_evidence(engine_id, direction, src, hypotheses, metrics, n):
    """Persist one engine's run so a later COMET pass or diff report can
    pick it up. Layout mirrors docs/evidence/flores-benchmark-*.json."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / "av-domain-{}.json".format(engine_id)
    import json
    payload = {
        "engine": engine_id,
        "direction": direction,
        "corpus": "av-domain",
        "requested": n if n is not None else len(src),
        "evaluated": len(src),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "metrics": metrics,
        "samples": [
            {"id": i, "source": s, "reference": r, "hypothesis": h}
            for i, (s, r, h) in enumerate(zip(src, _read_references(direction), hypotheses))
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _read_references(direction):
    """Convenience: pull the reference column from the resolved root so
    _write_evidence does not have to thread it through call sites."""
    pair_prefix = "av-{}".format(direction)
    root = resolve_av_root(None)
    pair_dir = find_pair_dir(root, pair_prefix)
    _, refs = load_pair(pair_dir, pair_prefix)
    return refs


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    root = resolve_av_root(args.av_root)
    if args.list:
        _print_pairs(root)
        return 0
    if not args.engine:
        parser.error("--engine is required unless --list is given")
    pair_prefix = "av-{}".format(args.direction)
    pair_dir = find_pair_dir(root, pair_prefix)
    src, tgt = load_pair(pair_dir, pair_prefix)
    if args.n is not None:
        src, tgt = src[:args.n], tgt[:args.n]
    hypotheses, metrics = _run_one(args.engine, args.direction, src, tgt)
    out = _write_evidence(args.engine, args.direction, src, hypotheses, metrics,
                          args.n)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
