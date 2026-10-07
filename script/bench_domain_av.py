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
import ctypes
import datetime
import json
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
# is exposed as `ollama-qwen` on the FFI side (the reported Engine.name()),
# not by the internal "ollama" id, so callers can see which model ran.
ENGINES = ("argos", "madlad", "ollama-qwen")

# The FFI's get_engine looks up "ollama" while the engine reports itself as
# "ollama-qwen" (Engine.name()), so the user-facing id is translated once here.
# bench_translation's short ids no longer need a mapping: every arm is
# issued through the FFI builder, so the shared client is out of the picture.
_FFI_ID = {"ollama-qwen": "ollama"}


def _ffi_id(engine_id):
    return _FFI_ID.get(engine_id, engine_id)


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
    instead -- matches resolve_flores_root in bench_flores.py (S10 T1).

    This function is strict: it never silently points elsewhere just
    because the resolved default is empty. run_av_baseline.py owns the
    "fall back to the in-repo starter corpus" decision so this harness
    stays honest when a user has set WONSLATE_DATA_DIR / a custom root
    that they expect to be reported back verbatim."""
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
    parser.add_argument("--domain", default="",
                        help="pack-level domain to send in the request body "
                             "(empty = unscoped baseline; 'av' = the seed-pack "
                             "scope added by S11)")
    parser.add_argument("--n", type=int, default=None,
                        help="limit to first N pairs (smoke)")
    parser.add_argument("--run-index", type=int, default=1,
                        help="repeat slot inside this arm's `runs` array: 1 starts a "
                             "fresh series, 2 and above append. S12 uses two runs per "
                             "arm to tell a terminology effect from ollama's sampling "
                             "noise (temperature 0.3, no seed)")
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


def _translate_via_ffi(engine_id, src_lang, tgt_lang, text, domain=""):
    """Call tt_translate_full directly so the request can carry `domain`.

    The bench_translation.Engine wrapper takes only (text, source, target)
    today; rather than fork that shared client (which sits in the concurrent
    session's WIP), the AV-domain harness builds its own request here. The
    engine_id field locks the run to a specific engine, matching what the
    UI does when the user picks one explicitly."""
    lib = _load_engine_for_translate()
    body = json.dumps({
        "engine_id": _ffi_id(engine_id),
        "input": text,
        "source_lang": src_lang,
        "target_lang": tgt_lang,
        "mode": "full",
        "privacy": False,
        # use_tm must be False in a bench run. The pipeline's L-1 is a
        # TM lookup keyed by (text, src, tgt, domain) and it *does* match
        # generic TM entries (domain="") against a scoped query, so if a
        # prior bench stored argos output under the generic key, every
        # later scoped pass (madlad / ollama) would hit that same cache
        # and return argos's hypothesis verbatim. That is exactly what
        # happened on the first 300-sentence run: madlad-av and
        # ollama-qwen-av produced 300/300 identical hypotheses to argos.
        # For S12 we measure engine + terminology, not TM.
        "use_tm": False,
        "domain": domain,
    }, ensure_ascii=False).encode("utf-8")
    ptr = lib.tt_translate_full(body)
    raw = ctypes.c_char_p(ptr).value or b"{}"
    lib.tt_free_string(ptr)
    resp = json.loads(raw.decode("utf-8", errors="replace"))
    return resp.get("output") or "", resp


def _translate_row(engine_id, src_lang, tgt_lang, text, domain, attempts=2):
    """Translate one row; return (hypothesis, failure_reason_or_None).

    Defect D31: the response used to be discarded by the caller, so any engine
    error collapsed into `resp.get("output") or ""` -- an empty hypothesis that
    stayed inside `evaluated`, was scored as a bad translation, and let the report
    call the run complete. On 2026-10-05 that turned 19 transient ollama failures
    into five sixths of a published +0.0330 delta.

    An empty output is treated as a failure whatever the response looks like, since
    "the engine answered but produced nothing" is not a translation. Transient is
    the common case (all 19 rows replay clean), so the row gets one retry before it
    is recorded; the reason is taken from the response when it has one.
    """
    reason = "empty_output"
    for _ in range(max(1, attempts)):
        hyp, resp = _translate_via_ffi(engine_id, src_lang, tgt_lang, text, domain)
        if hyp.strip():
            return hyp, None
        reason = (resp.get("error") or resp.get("status") or resp.get("reason")
                  or "empty_output")
    return "", reason


# Reuse the CDLL loader without duplicating the argtypes dance: the shared
# library is opened once per process and cached here so a whole corpus run
# does not repeatedly dlopen.
_LIB_CACHE = {"lib": None}


def _load_engine_for_translate():
    lib = _LIB_CACHE["lib"]
    if lib is not None:
        return lib
    from install_glossary_pack import _load_engine  # noqa: WPS433 (lazy)
    # install_glossary_pack already knows how to locate and configure the
    # CDLL; set the two extra argtypes tt_translate_full needs on top.
    lib = _load_engine()
    lib.tt_translate_full.argtypes = [ctypes.c_char_p]
    lib.tt_translate_full.restype = ctypes.c_void_p
    _LIB_CACHE["lib"] = lib
    # tt_init must run before translate_full works (TM singleton etc.).
    ack = lib.tt_init(b"{}")
    if ack:
        lib.tt_free_string(ack)
    return lib


def _run_one(engine_id, direction, src, tgt, domain=""):
    """Translate the sentence set with one engine.

    Returns (hypotheses, metrics, failures). `hypotheses` keeps one slot per source
    row -- positions are how the two arms are compared row by row, so a row that
    failed stays in place as an empty string rather than shifting every later id.
    `failures` names those rows and why (defect D31), and `metrics` is computed over
    the rows that actually answered: scoring an engine failure as a translation
    measures the reliability of the endpoint, not the quality of the engine.

    Both arms of the domain control issue through the same request builder
    (_translate_via_ffi), so `domain` is the only thing that differs between a
    baseline run and a scoped one. The unscoped arm used to
    take bench_translation's shared client, which meant the measured delta also
    carried a different TM policy (the shared client may read and write the
    memory, this path pins use_tm=False) and a different call stack. Two arms
    that differ in more than the field under study cannot measure that field.

    `score` stays a lazy import so the hermetic tests need no sidecar, no Ollama
    and no built DLL."""
    src_lang, tgt_lang = direction.split("-")
    from bench_translation import score  # noqa: WPS433 (lazy)
    hypotheses, failures = [], []
    for index, line in enumerate(src):
        hyp, reason = _translate_row(engine_id, src_lang, tgt_lang, line, domain)
        hypotheses.append(hyp)
        if reason is not None:
            failures.append({"id": index, "reason": reason})
    answered = [i for i, h in enumerate(hypotheses) if h.strip()]
    # bench_translation.score returns a (chrf, bleu) tuple, not a dict;
    # normalise it here so the evidence JSON has a stable shape that the
    # report generator can read. score_comet.py adds "comet" to the same
    # metrics dict after a live run.
    chrf, bleu = score([hypotheses[i] for i in answered], [tgt[i] for i in answered],
                       tgt_lang)
    metrics = {"chrF": chrf, "BLEU": bleu}
    return hypotheses, metrics, failures


def _write_evidence(engine_id, direction, src, tgt, hypotheses, metrics, n, domain="",
                    failures=None, run_index=1):
    """Persist one engine's run so a later COMET pass or diff report can
    pick it up. Layout mirrors docs/evidence/flores-benchmark-*.json.

    The domain is folded into the filename (empty -> `av-domain-<engine>.json`,
    non-empty -> `av-domain-<engine>-<domain>.json`) so a baseline run and a
    scoped run of the same engine coexist for the S12 T3 comparison pass.

    `evaluated`/`scored` count the rows that produced a translation, not the rows
    that were asked for (defect D31: 300/300 was reported while 19 were empty).
    `run_index` is the repeat slot: 1 starts a fresh series -- stale repeats from an
    older build must not pose as noise controls for this one -- while 2..n append to
    `runs`, which is the array score_comet.py already iterates and the report can
    average to separate a terminology effect from ollama's sampling noise."""
    import json

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    tag = "-{}".format(domain) if domain else ""
    out = EVIDENCE_DIR / "av-domain-{}{}.json".format(engine_id, tag)
    answered = sum(1 for h in hypotheses if h.strip())
    run = {
        "engine": engine_id,
        "direction": direction,
        "domain": domain,
        "requested": n if n is not None else len(src),
        "evaluated": answered,
        "scored": answered,
        "failed": list(failures or []),
        "metrics": metrics,
        "samples": [
            {"id": i, "source": s, "reference": r, "hypothesis": h}
            for i, (s, r, h) in enumerate(zip(src, tgt, hypotheses))
        ],
    }
    # The evidence is wrapped in a top-level `runs` array so score_comet.py
    # (which iterates record.get("runs", []) and adds run["comet"] plus
    # run["comet_scores_per_sample"]) can score it. Same shape as
    # docs/evidence/flores-benchmark-*.json.
    earlier = []
    if run_index > 1 and out.exists():
        try:
            previous = json.loads(out.read_text(encoding="utf-8"))
            earlier = list(previous.get("runs") or [])
        except (ValueError, OSError):
            earlier = []          # unreadable: start clean rather than lose the run
    payload = {
        "corpus": "av-domain",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "runs": earlier + [run],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


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
    hypotheses, metrics, failures = _run_one(args.engine, args.direction, src, tgt,
                                    args.domain)
    out = _write_evidence(args.engine, args.direction, src, tgt,
                          hypotheses, metrics, args.n, args.domain,
                          failures=failures, run_index=args.run_index)
    print("wrote", out)
    if failures:
        # Loud on purpose (D31): these rows are out of the denominators, so this run
        # is not comparable to a clean one until the report says how many answered.
        print("  %d of %d rows produced no translation and are excluded from the "
              "metrics; ids=%s reasons=%s"
              % (len(failures), len(src), [f["id"] for f in failures][:20],
                 sorted({f["reason"] for f in failures})), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
