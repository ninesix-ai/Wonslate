# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Forensic probe for invisible characters in translation output, on both engine tiers.

Why this exists as a tool rather than a one-off: defect D42 (external audit item 009)
measured a Lao translation arriving with an embedded ZWSP. Nothing on screen changes, while
every string comparison downstream -- dedup, search, cache keys, localisation checks, diff
scripts -- breaks silently. The sidecar now sanitises its response, and the honest answer to
"what about the ollama tier?" has to be a measurement, not a guess.

Two modes, both reading codepoints rather than console rendering (a Lao or Khmer string
renders as garbage in many terminals, which is exactly how this class of defect hides):

  sidecar  compares what a backend emitted against what left the HTTP port, so it verifies
           the sanitiser on real model output and reports the orthography it preserved.
  ollama   asks the model with the product's own prompt shape and classifies every invisible
           codepoint as orthography or noise. The Rust tier strips thinking blocks and
           Markdown fences and nothing else, so a noise hit here would reach callers -- and,
           once a Full-mode answer clears the TM quality bar, be learned into the memory.

An invisible mark counts as *orthography* when the target script writes with it (the Persian
half-space, Khmer and Myanmar word separators, Indic conjunct control) or when the source we
sent already carried that codepoint. Everything else is noise.

Usage:
    python script/diag_invisible_chars.py sidecar [--backend madlad|ct2|mock] [--n 24]
    python script/diag_invisible_chars.py ollama [--ollama http://127.0.0.1:11434] [--n 24]
Exit code: 0 = no noise found, 1 = noise reached an output, 2 = could not run (service absent).
"""
import argparse
import collections
import http.client
import json
import pathlib
import sys
import unicodedata
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidecar import ct2_sidecar  # noqa: E402

INVISIBLE_CATEGORIES = {"Cf", "Cc", "Co", "Cs"}

# UI-shaped English sentences: the shape a batch localisation caller actually sends, and the
# shape external item 009 was found under.
EN_SOURCES = [
    "Hello", "File not found", "Save changes?", "Connecting...", "Cancel", "Retry later",
    "You have {count} files in {path}.", "No presets available", "Import", "Export as WAV",
    "The buffer is too small for one frame.", "Sample rate must match the device.",
    "Turn off noise reduction before recording.", "The subtitle file is out of sync.",
    "Choose a voice for the dubbing.", "Upload finished with warnings.",
    "Delete this project?", "Settings could not be saved.", "Microphone not detected.",
    "Reduce latency by lowering the buffer size.", "License agreement", "About",
    "Pitch and formant are both available.", "This folder contains no audio files.",
    "The cache was cleared after the crash.", "Mix the stems down to one track.",
    "A container does not guarantee a codec.", "Please rate the alignment quality.",
]
TARGETS = ["lo", "my", "km", "fa", "hi", "th", "ar", "ne", "zh", "ja"]

LANGUAGE_NAMES = {"zh": "Chinese", "en": "English", "lo": "Lao", "my": "Burmese",
                  "km": "Khmer", "fa": "Persian", "hi": "Hindi", "th": "Thai",
                  "ar": "Arabic", "ne": "Nepali", "ja": "Japanese"}

# Marks a script writes with. Restated here to classify a report, not to reimplement the
# product rule (the authoritative copy lives in ct2_sidecar.ZERO_WIDTH_SCRIPTS).
ORTHOGRAPHY = {
    "\u200b": {"km", "my"},
    "\u200c": {"fa", "ps", "ckb", "ug", "ku", "sd", "hi", "mr", "ne", "sa", "bn", "gu",
               "pa", "or", "as", "kn", "ml", "te"},
    "\u200d": {"hi", "mr", "ne", "sa", "bn", "gu", "pa", "or", "as", "kn", "ml", "te", "ar"},
}


def split_invisible(text, target, source):
    """Return (orthography, noise) counters over the codepoints a reader cannot see."""
    ok, noise = collections.Counter(), collections.Counter()
    for ch in text:
        if unicodedata.category(ch) not in INVISIBLE_CATEGORIES:
            continue
        name = "U+%04X" % ord(ch)
        allowed = ORTHOGRAPHY.get(ch)
        if allowed is not None and (target in allowed or ch in source):
            ok[name] += 1
        else:
            noise[name] += 1
    return ok, noise


def codepoints(text, limit=120):
    return " ".join("U+%04X" % ord(c) for c in text[:limit])


def char_of(name):
    """Reverse a 'U+200B' label back to the character it names."""
    return chr(int(name[2:], 16))


def post_json(host, port, path, payload, timeout):
    """One request per connection, deliberately.

    Reusing a single http.client connection looks harmless until one call stalls: after a
    socket timeout the connection is no longer usable, and every later request on it fails
    instantly -- which is how a first version of this probe reported 19 served and 197
    failures and still printed "no noise found". A measurement whose failures are invisible
    in its verdict is worse than no measurement.
    """
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", path, json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        return resp.status, body
    finally:
        conn.close()


def removed_marks(raw, wire, raw_noise):
    """How many noise marks the response layer actually deleted.

    Compared per codepoint label: what the backend emitted minus what still reached the
    wire. This is the number that makes the sanitiser's effect visible -- the Lao case was
    eighty of these, none of them reproducible from a single sentence.
    """
    gone = 0
    for name, count in raw_noise.items():
        ch = char_of(name)
        gone += max(0, count - wire.count(ch))
    return gone


def report(label, served, failed, ok_by_target, noise_by_target, examples):
    print("\n%s" % label)
    print("  served=%d failed=%d" % (served, failed))
    if served == 0 or failed * 2 > served:
        # An empty noise list over a run that mostly failed would read as "measured clean",
        # which is the one conclusion this probe must never be able to print dishonestly.
        print("  WARNING: most calls did not come back, so this run measures nothing at all")
        return -1
    print("  orthography per target: %s" % (
        {t: dict(c) for t, c in sorted(ok_by_target.items()) if c} or "none"))
    print("  NOISE per target: %s" % (
        {t: dict(c) for t, c in sorted(noise_by_target.items()) if c} or "none"))
    for target, source, out, noise in examples[:8]:
        print("   NOISE %s <- %r  %s" % (target, source[:30], dict(noise)))
        print("        codepoints: %s" % codepoints(out))
    total_noise = sum(sum(c.values()) for c in noise_by_target.values())
    print("  verdict: %d noise mark(s) over %d served calls" % (total_noise, served))
    return total_noise


def run_sidecar(args):
    """Backend output vs the bytes that leave the HTTP port, so the sanitiser is measured."""
    sources = EN_SOURCES[:args.n]
    backend = ct2_sidecar.make_backend(args.backend)
    server, port = ct2_sidecar.serve_in_thread(backend, host="127.0.0.1", port=0)
    served = failed = 0
    stripped = 0
    ok_by = collections.defaultdict(collections.Counter)
    noise_by = collections.defaultdict(collections.Counter)
    examples = []
    try:
        for target in args.langs:
            for text in sources:
                try:
                    raw = backend.translate(text, "en", target)
                except ct2_sidecar.MissingDependency as exc:
                    print("  %s: backend cannot serve this direction (%s)" % (target, exc))
                    break
                status, body = post_json("127.0.0.1", port, "/translate",
                                         {"text": text, "source": "en", "target": target},
                                         args.timeout)
                if status != 200:
                    failed += 1
                    continue
                served += 1
                ok, noise = split_invisible(body["text"], target, text)
                ok_by[target].update(ok)
                noise_by[target].update(noise)
                if noise:
                    examples.append((target, text, body["text"], noise))
                # The raw side is what the sanitiser was handed, so measuring it is the only
                # way to tell "nothing was emitted" apart from "it was emitted and removed".
                _raw_ok, raw_noise = split_invisible(raw, target, text)
                stripped += removed_marks(raw, body["text"], raw_noise)
    finally:
        server.shutdown()
        server.server_close()
    noise = report("sidecar tier: backend output compared with the HTTP response",
                   served, failed, ok_by, noise_by, examples)
    print("  noise marks the sanitiser removed before the wire: %d" % stripped)
    return noise


def ollama_prompt(pair, terms):
    """The product's own system prompt shape (engine/ollama.rs::build_system_prompt)."""
    prompt = ("You are a professional offline translation engine. Translate the user's text "
              "from %s. Output ONLY the translated text with no explanations, no quotes, "
              "no annotations." % pair)
    if terms:
        rows = "\n".join("  %s \u2192 %s" % (src, tgt) for src, tgt in terms)
        prompt += ("\n\nUse these consistent term translations:\n%s"
                   "\n\nMaintain terminology consistency with the above." % rows)
    return prompt


def load_av_terms(limit=20):
    """The real zh->en AV pack, so the injected batch is the prompt routing really sends."""
    packs = sorted((ROOT / "docs" / "glossary-packs").glob("*av*.json"))
    if not packs:
        return [], None
    data = json.loads(packs[0].read_text(encoding="utf-8"))
    rows = data.get("terms") or data.get("entries") or []
    terms = [(r["source_term"], r["target_term"]) for r in rows
             if isinstance(r, dict) and r.get("source_term") and r.get("target_term")
             and r.get("target_lang", "en") == "en"]
    return terms, packs[0].name


def load_zh_sources(n):
    src = ROOT / "script" / "eval-data" / "av-zh-en" / "av-zh-en.src"
    if not src.exists():
        return []
    return [l.strip() for l in src.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")][:n]


def run_ollama(args):
    """Ask the model the way the pipeline does, and classify what it sends back."""
    parsed = urlparse(args.ollama)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 11434
    served = failed = 0
    ok_by = collections.defaultdict(collections.Counter)
    noise_by = collections.defaultdict(collections.Counter)
    examples = []
    terms, pack_name = load_av_terms()
    plan = [(t, "en", tgt, None) for tgt in args.langs for t in EN_SOURCES[:args.n]]
    plan += [(zh, "zh", "en", [(s, r) for s, r in terms if s in zh][:20] or None)
             for zh in load_zh_sources(args.n)]
    try:
        for text, source, target, hits in plan:
            pair = "%s to %s" % (LANGUAGE_NAMES.get(source, source),
                                 LANGUAGE_NAMES.get(target, target))
            payload = {
                "model": args.model,
                "messages": [{"role": "system", "content": ollama_prompt(pair, hits)},
                             {"role": "user", "content": text}],
                "stream": False,
                "reasoning_effort": "none",
                "think": False,
                "temperature": 0.3,
            }
            try:
                status, body = post_json(host, port, "/v1/chat/completions", payload,
                                         args.timeout)
                if status != 200:
                    raise RuntimeError("answered %d" % status)
                out = ((body.get("choices") or [{}])[0]
                       .get("message", {}).get("content", "") or "").strip()
            except Exception as exc:                  # noqa: BLE001 - a lost call is reported
                failed += 1
                print("  !! %s -> %s: %s" % (text[:26], target, exc))
                continue
            served += 1
            ok, noise = split_invisible(out, target, text)
            ok_by[target].update(ok)
            noise_by[target].update(noise)
            if noise:
                examples.append((target, text, out, noise))
    except OSError as exc:
        print("cannot reach ollama at %s:%s (%s); start it or pass --ollama" % (host, port, exc))
        return -1
    label = ("ollama tier: model output for %d calls (model %s, pack %s)"
             % (served, args.model, pack_name or "not found"))
    return report(label, served, failed, ok_by, noise_by, examples)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["sidecar", "ollama", "both"])
    ap.add_argument("--backend", default="madlad", choices=["mock", "ct2", "madlad"])
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--n", type=int, default=24, help="sentences per language")
    ap.add_argument("--langs", default=",".join(TARGETS))
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args(argv)
    args.langs = [c.strip() for c in args.langs.split(",") if c.strip()]

    noise = 0
    if args.mode in ("sidecar", "both"):
        got = run_sidecar(args)
        if got < 0:
            return 2
        noise += got
    if args.mode in ("ollama", "both"):
        got = run_ollama(args)
        if got < 0:
            return 2
        noise += got
    print("\ntotal noise marks that reached an output: %d" % noise)
    return 1 if noise else 0


if __name__ == "__main__":
    sys.exit(main())
