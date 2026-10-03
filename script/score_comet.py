#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""score_comet.py -- add COMET scores to translation evidence JSONs.

COMET (Rei et al., 2020/2022; Unbabel) is the neural evaluation metric used as
the main automatic measure at recent WMT editions; it correlates with human
judgment far better than chrF/BLEU and is the recognized external-facing
measure this repo's quality claims hang on (12 号建议任务③ / REQ-B3).

Model: Unbabel/wmt22-comet-da (COMET-22), downloaded through the HuggingFace
hub; $HF_ENDPOINT is honoured, so the hf-mirror proxy works where
huggingface.co is unreachable. The model is cached by huggingface_hub outside
the repository.

Reads one or more evidence JSONs (as produced by bench_flores.py /
bench_translation.py), predicts a COMET score per sample, and rewrites each
file in place with per-direction corpus means plus the model name -- the
hypotheses and references stay intact, so the pass is repeatable.

Usage:
    set HF_ENDPOINT=https://hf-mirror.com
    python script/score_comet.py docs/evidence/flores-benchmark-madlad.json
"""
import argparse
import json
import os
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "script"))

# Must precede the comet import: huggingface_hub reads HF_ENDPOINT at import.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# Windows system proxies (registry-level) break the TLS handshake to the
# mirror on /resolve paths; bypass the proxy for the mirror host entirely.
os.environ.setdefault("NO_PROXY", "hf-mirror.com")
os.environ.setdefault("no_proxy", "hf-mirror.com")

DEFAULT_MODEL = "Unbabel/wmt22-comet-da"


def _fetch_checkpoint(model_name):
    """Locate the COMET checkpoint, tolerating comet>=2.2 registries that no
    longer list the 22-models: try download_model first, then a plain
    huggingface_hub snapshot (honouring $HF_ENDPOINT for mirrors)."""
    from comet import download_model
    try:
        return download_model(model_name)
    except Exception as exc:                              # noqa: BLE001 - registry mismatch fallback
        print("[comet] download_model failed ({}); falling back to snapshot_download".format(exc),
              flush=True)
    from huggingface_hub import snapshot_download
    local = snapshot_download(model_name)
    ckpt = pathlib.Path(local) / "checkpoints" / "model.ckpt"
    if not ckpt.is_file():
        candidates = sorted(pathlib.Path(local).rglob("*.ckpt"))
        if not candidates:
            raise SystemExit("no .ckpt found under {}".format(local))
        ckpt = candidates[0]
    return str(ckpt)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score COMET on evidence JSONs")
    parser.add_argument("evidence", nargs="+", help="evidence JSON file(s)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ckpt", default=None,
                        help="direct path to a model.ckpt (skips hub download; hparams.yaml must sit beside it)")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--cpu", action="store_true", help="force CPU even if CUDA is available")
    args = parser.parse_args(argv)

    import torch
    from comet import load_from_checkpoint

    gpus = 0 if args.cpu or not torch.cuda.is_available() else 1
    print("[comet] model={} gpus={}".format(args.ckpt or args.model, gpus), flush=True)
    ckpt = args.ckpt or _fetch_checkpoint(args.model)
    model = load_from_checkpoint(ckpt)

    for evidence in args.evidence:
        path = pathlib.Path(evidence)
        record = json.loads(path.read_text(encoding="utf-8"))
        scored_directions = 0
        for run in record.get("runs", []):
            samples = run.get("samples") or []
            if not samples:
                continue
            data = [{"src": s["source"], "mt": s["hypothesis"], "ref": s["reference"]}
                    for s in samples]
            result = model.predict(data, batch_size=args.batch_size, gpus=gpus,
                                   progress_bar=False)
            scores = result.scores
            run["comet"] = round(statistics.mean(scores), 4)
            run["comet_scores_per_sample"] = [round(s, 4) for s in scores]
            scored_directions += 1
            print("[comet] {} {}  COMET={:.4f}  n={}".format(
                run.get("engine", "?"), run.get("direction", "?"),
                run["comet"], len(scores)), flush=True)
        record["comet_model"] = args.model
        # Reuse the atomic writer from bench_translation for the in-place update.
        from bench_translation import write_json
        final = write_json(path, record)
        print("[comet] {} updated ({} directions scored)".format(final, scored_directions),
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
