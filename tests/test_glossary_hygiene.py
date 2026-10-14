# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Glossary hygiene gate: every term row must be written in the language it claims.

Why: the distiller used to cut n-grams out of a source sentence without
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

Family judging alone is blind inside a family (D63): zh, ja, ko and yue all map to
cjk, and Han, kana and hangul sit in one character class, so "Chinese copied into a
file that declares ja" satisfies every family check in this file. Two further rules
close that, measured against the shipped packs before being written:

  * EXCLUSION (row level): a row may not use a script exclusive to a sibling
    language -- zh/yue never write kana or hangul, ja never writes hangul, ko never
    writes kana. Zero hits across the 12 shipped packs.
  * PRESENCE (document level, and only for a file that declares its own pair):
    a pack declaring ja must show kana in at least one row, ko must show hangul.
    It cannot be a per-row rule: docs/glossary-packs/software-ui-en-ja.json has 6
    of 14 rows with no kana at all (kanji-only nouns, all of them correct
    Japanese), so requiring kana per row would fail real product data.

Difference 2 vs lang.rs: the two rules above are NOT ported to has_script_for. Its
question at runtime is "did the engine write the target language at all", and
requiring kana for ja there would penalise a kanji-only Japanese translation --
which is what the shipped en-ja pack's 6 kana-free rows are. The family mapping that
both sides DO share is pinned by
tests/test_glossary_hygiene_rules.py::GateAgreementTests.

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

# The Latin class is deliberately wider than ASCII: de/fr/pl carry accents and Polish
# has words written entirely with diacritics, so an ASCII-only test would report a
# correct term as holding no Latin character. lang.rs::is_latin uses this same range
# (they used to differ, which was Difference 1 of the two above; unified under D63 so
# the two family tables can be compared with nothing else to remember).
# lint-allow: unicode-tables -- script classes must be spelled out inline.
SCRIPTS = {
    "cjk": re.compile(
        r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"      # Han, ext A, compatibility
        r"\u3040-\u30ff\uac00-\ud7af\u1100-\u11ff]"      # kana, hangul
    ),
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
    "latin": re.compile(r"[A-Za-z\u00c0-\u024f]"),
}

# The cjk family's three writing systems, named, because the family check above
# cannot tell them apart and the rules below must.
SUBSCRIPTS = {
    "Han": re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]"),
    "kana": re.compile(r"[\u3040-\u30ff]"),
    "hangul": re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"),
}

# Scripts a declared language never writes. CJK siblings only: a Latin row under a
# cjk declaration is already caught by the family check.
BANS = {
    "zh": ("kana", "hangul"),
    "yue": ("kana", "hangul"),
    "ja": ("hangul",),
    "ko": ("kana",),
}

# The script each language has to show *somewhere in the file* for the declaration to
# be believable. Absence across a whole pack is the signature of a copied sibling;
# presence in one row is enough, because technical Japanese is often kanji-only.
DISTINGUISHING = {"ja": "kana", "ko": "hangul", "zh": "Han", "yue": "Han"}

# Same families as lang.rs::script_of.
# `pl` was missing from both tables while docs/glossary-packs/software-ui-en-pl.json
# declares it: an unknown code is exempt by design, so 14 shipped rows went unjudged
# with no line of output saying so (D63). A shipped language must be a judged one --
# pinned by ShippedAssetTests.
FAMILY = {
    "zh": "cjk", "ja": "cjk", "ko": "cjk", "yue": "cjk",
    "ru": "cyrillic",
    "en": "latin", "fr": "latin", "de": "latin", "es": "latin", "pt": "latin",
    "it": "latin", "nl": "latin", "tr": "latin", "id": "latin", "vi": "latin",
    "pl": "latin",
}


def primary_subtag(lang):
    """The primary subtag, lower-cased: pt-BR is judged as pt (see family_of)."""
    return lang.strip().lower().split("-")[0]


def family_of(lang):
    """Script family of a language tag, or None when we decline to judge.

    Difference 1 vs lang.rs: Rust lowercases and matches the whole tag, so a
    region-qualified code such as pt-BR resolves to None and goes unchecked.
    Shipped packs use exactly those codes, so the gate reduces to the primary
    subtag before looking up -- being stricter than the engine is safe here
    because a false alarm on a pack is a human reading one line.
    """
    if not isinstance(lang, str):
        return None
    return FAMILY.get(primary_subtag(lang))


def violations(row, source_lang, target_lang, side="row"):
    """Yield a message per field whose script contradicts its declared language.

    Two questions, in this order: does the field show its family at all (the check
    lang.rs shares), and does it show a script a sibling language of the same family
    owns exclusively (D63, this file only).
    """
    out = []
    for field, lang in (("source_term", source_lang), ("target_term", target_lang)):
        text = row.get(field, "")
        fam = family_of(lang)
        if fam is None or not isinstance(text, str):
            continue                       # unknown language: do not judge
        if not SCRIPTS[fam].search(text):
            out.append("%s: %s=%r holds no %s character but the pair declares %r"
                       % (side, field, text[:40], fam, lang))
            continue                       # wrong family: that is the finding
        tag = primary_subtag(lang) if isinstance(lang, str) else ""
        for script in BANS.get(tag, ()):
            if SUBSCRIPTS[script].search(text):
                # Names exactly one script, the offending one. The explanation must not
                # list the other writing systems: tests assert on these words, so a
                # message that mentions every script would make such an assertion pass
                # while reporting a different script than the case is about.
                out.append("%s: %s=%r uses %s, which %r never writes (one cjk family, "
                           "three scripts -- the family check cannot tell them apart)"
                           % (side, field, text[:40], script, lang))
    return out


def document_violations(name, rows, data):
    """PRESENCE: a pack must show its declared language's own script at least once.

    Applies only to a file that declares its pair at the top -- a pack is asserting
    "this file is Japanese", and no kana row anywhere refutes that. A store asserts
    nothing about its provenance and accumulates rows from whichever packs a user
    installed, so two kanji-only ja rows in one are a partial import, not a mislabel.

    Grouped by the effective language of each side, so a pack whose rows override the
    pair gets the same treatment as one that does not.
    """
    if not any(isinstance(data.get(key), str) for key in ("source_lang", "target_lang")):
        return []
    out = []
    for field, key, label in (("source_term", "source_lang", "source"),
                              ("target_term", "target_lang", "target")):
        doc_lang = data.get(key)
        by_lang = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get(field)
            if not isinstance(text, str):
                continue
            lang = row.get(key, doc_lang)
            if isinstance(lang, str):
                by_lang.setdefault(primary_subtag(lang), []).append(text)
        for tag, texts in sorted(by_lang.items()):
            script = DISTINGUISHING.get(tag)
            if script is None or not texts:
                continue
            if not any(SUBSCRIPTS[script].search(t) for t in texts):
                out.append("%s: the %s side declares %r across %d row(s) but no row shows "
                           "%s, the script that language writes with -- the file looks like "
                           "a sibling of the same family"
                           % (name, label, tag, len(texts), script))
    return out


def check_document(path):
    """Check one pack or store file. Returns (rows_checked, [messages])."""
    try:
        with io.open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as exc:
        return 0, ["%s: not valid JSON (%s)" % (os.path.basename(path), exc)]
    except OSError as exc:
        # A path that cannot be opened is a finding, not a crash: this tool is pointed at
        # stores by hand during triage, and "cannot read" has to be reported the same way
        # "not clean" is.
        return 0, ["%s: cannot be read (%s)" % (os.path.basename(path), exc)]
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
    bad.extend(document_violations(name, rows, data))
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
