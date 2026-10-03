#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""bench_translation.py -- score Wonslate's engines against one reference set.

Why: "supports N languages" is a routing statement, not a quality statement.
This script sends the same 108 segments (9 pairs x both directions x 6
segments, script/bench_translation_pairs.json) to every engine that is up --
argos and madlad through their sidecar /translate endpoints, qwen through the
local Ollama /v1/chat/completions API with the exact prompt translator-engine/
src/engine/ollama.rs uses -- then scores each direction with sacrebleu
(corpus-level chrF++ and BLEU) and records per-request latency.

The engines are called over HTTP, exactly as the product calls them, so the
numbers describe the shipped system rather than a white-room harness.

Network posture: this harness talks to LOCAL translation services only. Every
URL is built from a literal 127.0.0.1 host plus a validated integer port
(1-65535, CLI-supplied); redirects to any non-loopback host are refused. No
remote endpoint is ever contacted, so the harness cannot be turned into a
proxy for arbitrary targets.

Usage:
    python script/bench_translation.py                     # all engines that answer
    python script/bench_translation.py --engines madlad,qwen
    python script/bench_translation.py --list              # show the loaded set

Output:
    * a markdown report on stdout,
    * the full per-sample record (every hypothesis, every latency) as JSON
      under docs/evidence/ -- commit it alongside the report.

Exit code: 0 = at least one engine produced scores, 1 = nothing ran.
"""
import argparse
import datetime
import json
import os
import pathlib
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_PATH = ROOT / "script" / "bench_translation_pairs.json"
EVIDENCE_DIR = ROOT / "docs" / "evidence"
CJK_TARGETS = frozenset({"zh", "ja", "ko"})
SIDECAR_TIMEOUT_S = 60
QWEN_TIMEOUT_S = 120


def loopback_url(port, path):
    """Build an http URL that can only ever point at this machine.

    The host is a literal; the port is coerced to int and range-checked, so no
    string supplied on the command line can change the destination host.
    """
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range: {}".format(port))
    return "http://127.0.0.1:{}{}".format(port, path)


class _LoopbackRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that leave loopback instead of silently following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.request.urlparse(newurl)
        if parts.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise urllib.error.URLError("redirect away from loopback: {}".format(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_LoopbackRedirect)

# Mirrors language_prompt() in translator-engine/src/engine/ollama.rs so the
# benchmark exercises the prompt the shipped engine actually sends.
def ollama_pair_phrase(source, target):
    if (source, target) == ("zh", "en"):
        return "Chinese to English"
    if (source, target) == ("en", "zh"):
        return "English to Chinese"
    return "{} to {}".format(source, target)


# Mirrors build_system_prompt() in the same file (no glossary in the benchmark).
def ollama_system_prompt(source, target):
    return (
        "You are a professional offline translation engine. Translate the user's text"
        " from {}. Output ONLY the translated text with no explanations, no quotes,"
        " no annotations.".format(ollama_pair_phrase(source, target))
    )


def clean_qwen_output(raw):
    """Mirror OllamaTranslator::clean_output plus the <think> tag qwen3 emits."""
    out = (raw or "").strip()
    for tag in ("think", "thinking"):
        start, end = "<{}>".format(tag), "</{}>".format(tag)
        if start in out and end in out:
            head = out.split(start, 1)[0]
            tail = out.rsplit(end, 1)[-1]
            out = (head + tail).strip()
    if out.startswith("```"):
        body = out.lstrip("`").lstrip()
        if "```" in body:
            body = body.split("```", 1)[0]
        out = body.strip()
    return out.strip().strip('"').strip()


class Engine:
    """One callable engine plus the metadata the report needs."""

    def __init__(self, engine_id, translate, probe):
        self.id = engine_id
        self._translate = translate
        self._probe = probe
        self.available = False

    def check(self):
        try:
            self.available = self._probe()
        except Exception:                              # noqa: BLE001 - any probe failure = unavailable
            self.available = False
        return self.available

    def translate(self, text, source, target):
        return self._translate(text, source, target)


def http_json(url, payload, timeout):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def build_engines(args):
    engines = []

    def sidecar(name, port):
        base = loopback_url(port, "")

        def probe():
            with _OPENER.open(base + "/health", timeout=3) as r:
                return r.status == 200

        def translate(text, source, target):
            out = http_json(base + "/translate",
                            {"text": text, "source": source, "target": target},
                            SIDECAR_TIMEOUT_S)
            return out.get("text", "")

        engines.append(Engine(name, translate, probe))

    def qwen(port, model):
        base = loopback_url(port, "")

        def probe():
            with _OPENER.open(base + "/api/tags", timeout=3) as r:
                return r.status == 200

        def translate(text, source, target):
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": ollama_system_prompt(source, target)},
                    {"role": "user", "content": text},
                ],
                "stream": False,
                "reasoning_effort": "none",
                "think": False,
                "temperature": 0.3,
            }
            out = http_json(base + "/v1/chat/completions", payload, QWEN_TIMEOUT_S)
            raw = out["choices"][0]["message"]["content"]
            return clean_qwen_output(raw)

        engines.append(Engine("qwen:" + model, translate, probe))

    if "argos" in args.engines:
        sidecar("argos", args.argos_port)
    if "madlad" in args.engines:
        sidecar("madlad", args.madlad_port)
    if "qwen" in args.engines:
        qwen(args.qwen_port, args.qwen_model)
    return engines


def score(hypotheses, references, target):
    import sacrebleu
    if target in CJK_TARGETS:
        # Unsegmented CJK has no whitespace "words": word_order=2 would see one
        # word per sentence and zero word n-grams, so use pure char-level chrF.
        chrf = sacrebleu.corpus_chrf(hypotheses, [references])
        bleu = sacrebleu.corpus_bleu(hypotheses, [references], tokenize="char", force=True)
    else:
        chrf = sacrebleu.corpus_chrf(hypotheses, [references], word_order=2)
        bleu = sacrebleu.corpus_bleu(hypotheses, [references], force=True)
    return chrf.score, bleu.score


def write_json(path, payload):
    """Atomic write via mkstemp + os.replace; the path is normalized and
    verified to stay inside the evidence directory before the replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    root = os.path.realpath(str(path.parent))
    final = os.path.realpath(os.path.join(root, path.name))
    if ".." in final.split(os.sep) or not final.startswith(root + os.sep):
        raise ValueError("evidence path escapes the evidence directory: {}".format(final))
    fd, tmp = tempfile.mkstemp(dir=root, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False, indent=2)
        os.replace(tmp, final)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return final


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score Wonslate engines on the reference set")
    parser.add_argument("--engines", default="argos,madlad,qwen",
                        help="comma-separated subset of argos,madlad,qwen")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--argos-port", type=int, default=11435)
    parser.add_argument("--madlad-port", type=int, default=11436)
    parser.add_argument("--qwen-port", type=int, default=11434)
    parser.add_argument("--qwen-model", default="qwen3:8b")
    parser.add_argument("--out", default=str(EVIDENCE_DIR / "translation-benchmark.json"))
    parser.add_argument("--list", action="store_true", help="print the loaded test set and exit")
    args = parser.parse_args(argv)
    args.engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    data = json.loads(pathlib.Path(args.data).read_text(encoding="utf-8"))
    directions = []
    for pair in data["pairs"]:
        a, b = pair["source"], pair["target"]
        directions.append((a, b, [(seg["a"], seg["b"]) for seg in pair["segments"]]))
        directions.append((b, a, [(seg["b"], seg["a"]) for seg in pair["segments"]]))

    if args.list:
        for src, tgt, segs in directions:
            print("{}->{}  {} segments".format(src, tgt, len(segs)))
        return 0

    engines = build_engines(args)
    for engine in engines:
        engine.check()
        print("[probe] {}: {}".format(engine.id, "up" if engine.available else "DOWN (skipped)"),
              flush=True)
    engines = [e for e in engines if e.available]
    if not engines:
        print("no engine is reachable; start the sidecars / ollama first", flush=True)
        return 1

    stamp = datetime.date.today().isoformat()
    record = {"date": stamp, "segments_per_direction": 6,
              "qwen_model": args.qwen_model, "runs": []}

    for engine in engines:
        for src, tgt, segs in directions:
            hyps, refs, lats, errors = [], [], [], []
            for source_text, reference in segs:
                t0 = time.perf_counter()
                try:
                    hyp = engine.translate(source_text, src, tgt)
                except Exception as exc:            # noqa: BLE001 - engine error per sample
                    errors.append("{}: {}".format(type(exc).__name__, exc))
                    continue
                lats.append(time.perf_counter() - t0)
                hyps.append(hyp or "")
                refs.append(reference)
            if hyps and not errors:
                chrf, bleu = score(hyps, refs, tgt)
            else:
                chrf, bleu = None, None
            run = {
                "engine": engine.id, "direction": "{}->{}".format(src, tgt),
                "chrf": None if chrf is None else round(chrf, 1),
                "bleu": None if bleu is None else round(bleu, 1),
                "latency_mean_s": None if not lats else round(statistics.mean(lats), 2),
                "latency_median_s": None if not lats else round(statistics.median(lats), 2),
                "errors": errors,
                "samples": [
                    {"source": s, "reference": r, "hypothesis": h}
                    for (s, r), h in zip(segs, hyps)
                ],
            }
            record["runs"].append(run)
            print("[{}] {}->{}  chrF={}  BLEU={}  mean={}s{}".format(
                engine.id, src, tgt,
                "-" if chrf is None else "{:.1f}".format(chrf),
                "-" if bleu is None else "{:.1f}".format(bleu),
                "-" if not lats else "{:.2f}".format(statistics.mean(lats)),
                "  ({} errors) ".format(len(errors)) if errors else ""),
                flush=True)

    out_path = write_json(pathlib.Path(args.out), record)
    print("\nevidence written: {}".format(out_path), flush=True)

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
