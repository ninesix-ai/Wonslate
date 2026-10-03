#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""bench_flores.py -- score Wonslate engines on the FLORES-101 devtest.

Why: the self-built 108-segment set (bench_translation_pairs.json) is a
regression baseline with self-authored references; its numbers must not back
external claims. FLORES-101 (Meta et al., CC-BY-SA) is a public, human-written
benchmark covering all eleven product languages, and IS the recognized standard
this repo's external-quality claims hang on (12 号建议任务③ / REQ-B3).

Method (fixed, for reproducibility):
  * devtest split, 1012 segments per language;
  * per direction, a seeded sample (default 100, seed 42) of devtest line ids;
  * the same seeded ids across engines, so every engine sees identical inputs;
  * engines invoked over the product path (sidecar /translate, Ollama
    /v1/chat/completions with the exact prompt of engine/ollama.rs);
  * corpus chrF++ / BLEU via sacrebleu with the CJK conventions of
    bench_translation.py; COMET (wmt22-comet-da) via score_comet.py afterwards.

Network posture: identical to bench_translation.py -- literal 127.0.0.1 hosts
plus validated integer ports; redirects off loopback refused. FLORES data is
read from a local root, never downloaded by this script. Root resolution,
highest first: --flores-root, then $WONSLATE_FLORES_DIR, then
<data_dir>/benchmarks/flores101 where <data_dir> is the same per-OS user data
directory the model fetchers use (sidecar/ct2_sidecar.py::default_data_dir). A
root that holds no split directory is an explicit failure, never a silent
fallback to some other corpus.

Usage:
    python script/bench_flores.py --engines madlad --n 100
    python script/bench_flores.py --engines qwen --n 100
    python script/bench_flores.py --list
"""
import argparse
import datetime
import os
import pathlib
import statistics
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

from bench_translation import (  # noqa: E402  (single source of engine clients)
    Engine, build_engines, score, write_json,
)
from sidecar.ct2_sidecar import default_data_dir  # noqa: E402  (shared data dir)

EVIDENCE_DIR = ROOT / "docs" / "evidence"
DEFAULT_FLORES_SUBDIR = pathlib.Path("benchmarks") / "flores101"
FLORES_DIR_ENV = "WONSLATE_FLORES_DIR"

# The shapes a public FLORES-101 checkout arrives in: the bare root, the
# `flores101_dataset` wrapper the OmniData/modelscope copy carries, and the
# `FLORES-101` wrapper of the upstream archive. The root itself is tried last,
# so pointing straight at a split directory works too.
SPLIT_CONTAINER_SUBDIRS = ("", "flores101_dataset", "FLORES-101")


class FloresDataNotFound(Exception):
    """No FLORES split directory could be located.

    The message names every candidate directory and the knob to set: a silently
    different corpus would quietly invalidate the numbers this harness produces.
    """


def resolve_flores_root(explicit_root=None):
    """--flores-root > $WONSLATE_FLORES_DIR > <data_dir>/benchmarks/flores101.

    A blank or whitespace-only environment value counts as unset, so a stray
    `set WONSLATE_FLORES_DIR=` cannot resolve the current directory instead.
    """
    if explicit_root:
        return pathlib.Path(explicit_root)
    from_env = os.environ.get(FLORES_DIR_ENV, "").strip()
    if from_env:
        return pathlib.Path(from_env)
    return default_data_dir() / DEFAULT_FLORES_SUBDIR


def _split_dir_candidates(root, split):
    """Directories that could hold one split, in the order they are tried."""
    root = pathlib.Path(root)
    for rel in SPLIT_CONTAINER_SUBDIRS:
        yield (root / rel / split) if rel else (root / split)
    yield root


def _language_file(dir_path, stem, split):
    """One language file of a split directory: the bare stem, then the
    `<stem>.<split>` spelling some mirrors use."""
    for name in (stem, "{}.{}".format(stem, split)):
        path = dir_path / name
        if path.is_file():
            return path
    return None


def find_split_dir(root, split):
    """Return the directory holding `split`'s language files, else None."""
    for candidate in _split_dir_candidates(root, split):
        if candidate.is_dir() and any(_language_file(candidate, stem, split)
                                      for stem in set(LANG_MAP.values())):
            return candidate
    return None

# Wonslate engine code -> FLORES-101 file stem (flores101 layout: devtest/<code>).
LANG_MAP = {
    "en": "eng",
    "zh": "zho_simpl",
    "ja": "jpn",
    "ko": "kor",
    "fr": "fra",
    "de": "deu",
    "es": "spa",
    "ru": "rus",
    "pt": "por",
    "it": "ita",
    "ar": "ara",   # flores101 uses ISO 639-3; the 200 edition uses arb
}

# Same nine pairs as the 108-segment regression set.
PAIRS = [("en", "zh"), ("en", "ja"), ("en", "ko"), ("en", "fr"), ("en", "de"),
         ("en", "es"), ("en", "ru"), ("en", "ar"), ("zh", "ja")]


def load_split(flores_root, split):
    """Read every language file of one split; return {stem: [lines]}.

    Raises FloresDataNotFound when the split cannot be located, rather than
    reading a different directory than the one that was asked for.
    """
    split_dir = find_split_dir(flores_root, split)
    if split_dir is None:
        raise FloresDataNotFound(
            "no '{}' split directory under {} -- tried: {}. Put the FLORES "
            "checkout there, point --flores-root at it, or set {}".format(
                split, flores_root,
                ", ".join(str(c) for c in _split_dir_candidates(flores_root, split)),
                FLORES_DIR_ENV))
    data = {}
    for stem in LANG_MAP.values():
        path = _language_file(split_dir, stem, split)
        lines = path.read_text(encoding="utf-8").splitlines()
        data[stem] = [ln.strip() for ln in lines if ln.strip()]
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score engines on FLORES-101 devtest")
    parser.add_argument("--engines", default="madlad,qwen",
                        help="comma-separated subset of argos,madlad,qwen")
    parser.add_argument("--flores-root", default=None,
                        help="FLORES checkout root (default: $WONSLATE_FLORES_DIR, "
                             "then <data_dir>/benchmarks/flores101)")
    parser.add_argument("--split", default="devtest", choices=["dev", "devtest"])
    parser.add_argument("--n", type=int, default=100, help="seeded sample per direction")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--argos-port", type=int, default=11435)
    parser.add_argument("--madlad-port", type=int, default=11436)
    parser.add_argument("--qwen-port", type=int, default=11434)
    parser.add_argument("--qwen-model", default="qwen3:8b")
    parser.add_argument("--pairs", default=None,
                        help="comma-separated pair filter, e.g. en-zh,zh-en (default: all nine pairs)")
    parser.add_argument("--out", default=None,
                        help="evidence JSON path (default: docs/evidence/flores-benchmark-<engine>).json")
    parser.add_argument("--list", action="store_true", help="print directions and sample ids, then exit")
    args = parser.parse_args(argv)
    args.engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    flores_root = resolve_flores_root(args.flores_root)
    try:
        data = load_split(flores_root, args.split)
    except FloresDataNotFound as exc:
        print("[flores] {}".format(exc), file=sys.stderr)
        return 1
    lengths = {len(v) for v in data.values()}
    if len(lengths) != 1:
        sys.exit("flores split files disagree in length: {}".format({k: len(v) for k, v in data.items()}))
    total = lengths.pop()

    directions = []
    for a, b in PAIRS:
        directions.append((a, b))
        directions.append((b, a))
    if args.pairs:
        wanted = {tuple(p.strip().split("-")) for p in args.pairs.split(",") if p.strip()}
        directions = [d for d in directions if d in wanted]

    if args.list:
        rng = __import__("random").Random(args.seed)
        ids = sorted(rng.sample(range(total), args.n))
        for src, tgt in directions:
            print("{}->{}  n={}".format(src, tgt, args.n))
        print("sample ids ({} of {}): {}".format(args.n, total, ids))
        return 0

    rng = __import__("random").Random(args.seed)
    sample_ids = sorted(rng.sample(range(total), args.n))
    print("[flores] split={} segments/split={} sample={} ids seed={}".format(
        args.split, total, args.n, args.seed), flush=True)

    engines = build_engines(args)
    for engine in engines:
        engine.check()
        print("[probe] {}: {}".format(engine.id, "up" if engine.available else "DOWN (skipped)"),
              flush=True)
    engines = [e for e in engines if e.available]
    if not engines:
        print("no engine is reachable; start the sidecars / ollama first", flush=True)
        return 1

    record = {
        "date": datetime.date.today().isoformat(),
        "benchmark": "FLORES-101 devtest",
        "sample_per_direction": args.n, "seed": args.seed,
        "sample_ids": sample_ids,
        "note": "External-facing benchmark; the 108-segment self-built set remains the regression baseline.",
        "runs": [],
    }

    for engine in engines:
        for src, tgt in directions:
            hyps, refs, lats, errors = [], [], [], []
            for idx in sample_ids:
                source_text = data[LANG_MAP[src]][idx]
                reference = data[LANG_MAP[tgt]][idx]
                t0 = time.perf_counter()
                try:
                    hyp = engine.translate(source_text, src, tgt)
                except Exception as exc:             # noqa: BLE001 - per-sample engine error
                    errors.append("{}: {}".format(type(exc).__name__, exc))
                    continue
                lats.append(time.perf_counter() - t0)
                hyps.append(hyp or "")
                refs.append(reference)
            if hyps:
                # Score whatever succeeded; per-sample errors stay recorded in the
                # run so a polluted direction is visible rather than silently mean.
                chrf, bleu = score(hyps, refs, tgt)
            else:
                chrf, bleu = None, None
            record["runs"].append({
                "engine": engine.id, "direction": "{}->{}".format(src, tgt),
                "chrf": None if chrf is None else round(chrf, 1),
                "bleu": None if bleu is None else round(bleu, 1),
                "latency_mean_s": None if not lats else round(statistics.mean(lats), 2),
                "errors": errors,
                "samples": [{"id": i, "source": data[LANG_MAP[src]][i],
                             "reference": r, "hypothesis": h}
                            for i, r, h in zip(sample_ids, refs, hyps)],
            })
            print("[{}] {}->{}  chrF={}  BLEU={}  mean={}s{}".format(
                engine.id, src, tgt,
                "-" if chrf is None else "{:.1f}".format(chrf),
                "-" if bleu is None else "{:.1f}".format(bleu),
                "-" if not lats else "{:.2f}".format(statistics.mean(lats)),
                "  ({} errors) ".format(len(errors)) if errors else ""),
                flush=True)

    for engine_id in dict.fromkeys(r["engine"] for r in record["runs"]):
        suffix = engine_id.split(":")[0] if ":" in engine_id else engine_id
        out = pathlib.Path(args.out) if args.out else \
            EVIDENCE_DIR / "flores-benchmark-{}.json".format(suffix)
        path = write_json(out, record)
        print("\nevidence written: {}".format(path), flush=True)

    print("\n## Overall (mean over available directions)\n", flush=True)
    print("| engine | directions | mean chrF++ | mean BLEU | mean latency |", flush=True)
    print("|---|---|---|---|---|", flush=True)
    for engine_id in dict.fromkeys(r["engine"] for r in record["runs"]):
        runs = [r for r in record["runs"] if r["engine"] == engine_id and r["chrf"] is not None]
        if not runs:
            continue
        print("| {} | {} | {:.1f} | {:.1f} | {:.2f}s |".format(
            engine_id, len(runs),
            statistics.mean(r["chrf"] for r in runs),
            statistics.mean(r["bleu"] for r in runs),
            statistics.mean(r["latency_mean_s"] for r in runs)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
