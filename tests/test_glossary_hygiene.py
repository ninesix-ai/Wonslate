# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Glossary hygiene gate: every term row must be written in the language it claims.

Why: D20. The distiller used to cut n-grams out of a source sentence without
asking whether that sentence is in the language it declares, so one mislabelled
TM row turned into glossary entries like ("cle" -> "time", 1.0) -- and the glossary
is injected into AI prompts as a term constraint. The extractor is now guarded
(see translator-engine/src/distill/term_extractor.rs), but two doors stay open
and neither is closed by a Rust-side check:

  * a shipped seed pack can itself declare the wrong language. Every install of
    that pack writes mis-annotated rows straight into a user's store, and the
    guard cannot see it because packs arrive through tt_glossary_import_pack,
    which never runs the extractor;
  * a store polluted before the guard keeps its data. Nothing rewrites history,
    so triage needs a way to ask "is this file still dirty".

So this gate asserts the rule on the assets we ship, and takes a file argument so
it can be pointed at any store for triage.

The script table mirrors lang::script_of in translator-engine/src/lang.rs. That
side is the authority (the engine enforces it at runtime); two deliberate
differences are noted where they occur.

Usage:
    python tests/test_glossary_hygiene.py                  # check shipped packs
    python tests/test_glossary_hygiene.py FILE ...         # check store/pack files
Exit code: 0 = clean, 1 = violations found.
"""
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK_DIR = os.path.join(ROOT, "docs", "glossary-packs")

# Difference 1 vs lang.rs: the Rust latin test is is_ascii_alphabetic, narrow
# because the only languages it ever judges there are zh and en. Here a pack may
# declare de/fr/pt, whose orthography carries accents, so the wider Latin block is
# used -- flagging "Über" as non-Latin would be a false positive, not a catch.
# lint-allow: unicode-tables -- script classes must be spelled out inline.
SCRIPTS = {
    "cjk": re.compile(
        r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"      # Han, ext A, compatibility
        r"\u3040-\u30ff\uac00-\ud7af\u1100-\u11ff]"      # kana, hangul
    ),
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
    "latin": re.compile(r"[A-Za-z\u00c0-\u024f]"),
}

# Same families as lang.rs::script_of.
FAMILY = {
    "zh": "cjk", "ja": "cjk", "ko": "cjk", "yue": "cjk",
    "ru": "cyrillic",
    "en": "latin", "fr": "latin", "de": "latin", "es": "latin", "pt": "latin",
    "it": "latin", "nl": "latin", "tr": "latin", "id": "latin", "vi": "latin",
}


def family_of(lang):
    """Script family of a language tag, or None when we decline to judge.

    Difference 2 vs lang.rs: Rust lowercases and matches the whole tag, so a
    region-qualified code such as pt-BR resolves to None and goes unchecked.
    Shipped packs use exactly those codes, so the gate reduces to the primary
    subtag before looking up -- being stricter than the engine is safe here
    because a false alarm on a pack is a human reading one line.
    """
    if not isinstance(lang, str):
        return None
    return FAMILY.get(lang.strip().lower().split("-")[0])


def violations(row, source_lang, target_lang, side="row"):
    """Yield a message per field whose script contradicts its declared language."""
    out = []
    for field, lang in (("source_term", source_lang), ("target_term", target_lang)):
        text = row.get(field, "")
        fam = family_of(lang)
        if fam is None or not isinstance(text, str):
            continue                       # unknown language: do not judge
        if not SCRIPTS[fam].search(text):
            out.append("%s: %s=%r holds no %s character but the pair declares %r"
                       % (side, field, text[:40], fam, lang))
    return out


def check_document(path):
    """Check one pack or store file. Returns (rows_checked, [messages])."""
    try:
        data = json.load(io.open(path, encoding="utf-8"))
    except ValueError as exc:
        return 0, ["%s: not valid JSON (%s)" % (os.path.basename(path), exc)]
    rows = data.get("entries")
    if rows is None:
        rows = data.get("records")
    if rows is None:
        return 0, ["%s: neither 'entries' nor 'records' found" % os.path.basename(path)]

    name = os.path.basename(path)
    bad = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            bad.append("%s[%d]: not an object" % (name, i))
            continue
        # A pack states the pair once at the top; a store repeats it per row.
        bad.extend(violations(row,
                              row.get("source_lang", data.get("source_lang")),
                              row.get("target_lang", data.get("target_lang")),
                              side="%s[%d]" % (name, i)))
    return len(rows), bad


def main(argv):
    targets = argv[1:]
    if not targets:
        targets = sorted(glob.glob(os.path.join(PACK_DIR, "*.json")))
        label = "shipped seed packs"
    else:
        label = "given files"

    total = 0
    problems = []
    for path in targets:
        rows, bad = check_document(path)
        total += rows
        problems.extend(bad)

    if problems:
        print("glossary hygiene gate: FAIL (%d violation(s) in %d rows, %s)"
              % (len(problems), total, label))
        for line in problems:
            print("   " + line)
        return 1
    print("glossary hygiene gate: PASS (%d rows, %d file(s), %s)"
          % (total, len(targets), label))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
