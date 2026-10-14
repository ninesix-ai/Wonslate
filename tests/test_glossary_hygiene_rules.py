# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Unit rules for the glossary hygiene gate (D63): in-family language blindness.

The gate (tests/test_glossary_hygiene.py) judges rows by SCRIPT FAMILY, and the
family table puts zh / ja / ko / yue all in `cjk` with Han, kana and hangul in one
character class. So it can see "Chinese written in Latin letters" but not "Chinese
passed off as Japanese", which is precisely the mistake a hand-authored ja seed
pack makes: the terms are copied from the Chinese pack and read plausibly.

Measured before these rules were written, against the assets we ship:

  * docs/glossary-packs/software-ui-en-ja.json has 14 rows, of which 6 hold NO kana
    (U+8A2D U+5B9A "Settings", U+8A00 U+8A9E "Language", U+4FDD U+5B58 "Save",
    U+524A U+9664 "Delete", U+6A19 U+6E96 "Default"). Those are correct Japanese --
    technical nouns are routinely kanji-only. A per-row "ja must contain kana" rule
    would therefore flag 6 of 14 shipped, correct rows.
  * the same pack has 8 rows WITH kana, so a document-level "at least one row shows
    the language's own script" catches a wholly mislabelled pack without touching a
    legitimate kanji-only term.
  * docs/glossary-packs/software-ui-en-pl.json declares `pl`, which is in neither
    the gate's FAMILY table nor lang.rs: those 14 rows are silently exempt, and no
    output says so.

Hence three rules, each with a case below, and each with the case that keeps it
from over-reaching:

  1. row-level EXCLUSION: a row may not use a script exclusive to a sibling
     language of the same family (zh/yue: no kana, no hangul; ja: no hangul;
     ko: no kana);
  2. document-level PRESENCE, only for pack-shaped files (the document itself
     declares the pair): a pack declaring ja must show kana somewhere, ko hangul.
     A store is a mix of many imports, so absence there is not evidence;
  3. table COMPLETENESS: every language a shipped pack declares must be judged.

Rule 1 and 2 are deliberately NOT ported to lang.rs::has_script_for, whose question
is "did the engine write the target language at all" at runtime; requiring kana for
ja there would penalise the kanji-only translations listed above. That difference is
recorded as Difference 2 in the gate's docstring, and GateAgreementTests pins the
part that must stay equal -- the family mapping -- with a parse that proves it parsed.

`pl` is the third finding of the same round and it did reach both sides: a shipped
pack declared it while neither table knew it, so its 14 rows were exempt. Adding it
to lang.rs also required widening that side's Latin test past ASCII, because Polish
has words with no ASCII letter at all; the Rust test
accented_latin_counts_as_latin carries that case.
"""
import glob
import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_glossary_hygiene as H  # noqa: E402  (the gate under test is a CLI tool)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK_DIR = os.path.join(ROOT, "docs", "glossary-packs")
LANG_RS = os.path.join(ROOT, "translator-engine", "src/lang.rs")


def read_json(path):
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_text(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def pack_doc(source_lang, target_lang, pairs):
    """A pack-shaped document: pair declared once at the top, rows under entries."""
    return {"version": 1, "domain": "fixture", "license": "Apache-2.0",
            "attribution": "test", "notes": "test",
            "source_lang": source_lang, "target_lang": target_lang,
            "entries": [{"source_term": s, "target_term": t} for s, t in pairs]}


class RuleTests(unittest.TestCase):
    """The two row-level directions the family check cannot see (rule 1)."""

    def test_zh_row_written_in_kana_is_flagged(self):
        # U+30B3 U+30CE U+30EF U+30C1 U+30F2 is Japanese katakana; no Han at all. Today the
        # family check waves it through because kana is inside the `cjk` class, which is how
        # a Chinese row can silently arrive carrying Japanese.
        msgs = H.violations({"source_term": u"\u30b3\u30ce\u30ef\u30c1\u30f2",
                             "target_term": "buffer"}, "zh", "en", side="row")
        self.assertTrue(any("kana" in m for m in msgs),
                        "a zh row written in kana must be a violation, got %r" % (msgs,))

    def test_zh_row_written_in_hangul_is_flagged(self):
        # U+C548 U+C6B4 U+C7AC U+B298 is Korean. Same blindness, other sibling.
        msgs = H.violations({"source_term": u"\uc548\ub155\ud558\uc138\uc694",
                             "target_term": "buffer"}, "zh", "en", side="row")
        self.assertTrue(any("hangul" in m for m in msgs),
                        "a zh row written in hangul must be a violation, got %r" % (msgs,))

    def test_ja_row_written_in_hangul_is_flagged(self):
        msgs = H.violations({"source_term": u"\uc548\ub155\ud558\uc138\uc694",
                             "target_term": u"\u30a8\u30a8\u30e9"}, "ja", "ko", side="row")
        self.assertTrue(any("hangul" in m and "ja" in m for m in msgs),
                        "a ja row written in hangul must be a violation, got %r" % (msgs,))

    def test_ko_row_written_in_kana_is_flagged(self):
        msgs = H.violations({"source_term": u"\u3053\u306e\u30a2\u30d7\u30ea",
                             "target_term": u"\ubc88\uc5ed"}, "ko", "en", side="row")
        self.assertTrue(any("kana" in m and "ko" in m for m in msgs),
                        "a ko row written in kana must be a violation, got %r" % (msgs,))

    def test_the_target_side_is_judged_by_its_own_language(self):
        # Both fields must be judged against the language each one declares -- a rule that
        # only ever looked at the source side would leave the whole target column exempt.
        msgs = H.violations({"source_term": "buffer",
                             "target_term": u"\u30d0\u30c3\u30d5\u30a1"}, "en", "zh", side="row")
        self.assertTrue(any("kana" in m for m in msgs),
                        "a zh target holding kana must be a violation, got %r" % (msgs,))

    def test_correct_cjk_rows_are_left_alone(self):
        # The exclusion must not become a general "cjk rows are suspicious" filter: real Han,
        # real kana-mixed Japanese and real hangul Korean all pass, including each one's own
        # family characters.
        for lang, text in (("zh", u"\u97f3\u9891\u7f13\u51b2\u533a"),
                           ("ja", u"\u30aa\u30fc\u30c7\u30a3\u30aa\u30ad\u30e3\u30c3\u30b7\u30e5"),
                           ("ko", u"\uc624\ub514\uc624 \uce90\uc2dc")):
            msgs = H.violations({"source_term": text, "target_term": "buffer"},
                                lang, "en", side="row")
            self.assertEqual([], msgs, "%s row %r should be clean" % (lang, text))


class DocumentPresenceTests(unittest.TestCase):
    """Rule 2: a pack must show its language's own script at least once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, doc, name="pack.json"):
        path = os.path.join(self.tmp.name, name)
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False)
        return path

    def _msgs(self, doc):
        _rows, bad = H.check_document(self._write(doc))
        return bad

    def test_ja_pack_whose_rows_are_all_han_is_flagged(self):
        # This is the D63 probe that used to pass: three terms copied out of the Chinese
        # pack under a ja declaration. Zero rows show kana, so the file is not Japanese --
        # while every single row would satisfy the family check forever.
        doc = pack_doc("ja", "en", [(u"\u97f3\u9891\u7f13\u51b2\u533a", "audio buffer"),
                                    (u"\u6d41\u5f0f\u5408\u6210", "streaming synthesis"),
                                    (u"\u7f16\u8f91\u5668", "editor")])
        bad = self._msgs(doc)
        self.assertTrue(any("no row" in m and "kana" in m for m in bad),
                        "a ja pack with zero kana rows must fail, got %r" % (bad,))

    def test_ko_pack_whose_rows_are_all_han_is_flagged(self):
        doc = pack_doc("ko", "en", [(u"\uc548\ub155", "hello"), (u"\ud3b8\uc9d1\uae30", "editor")])
        self.assertEqual([], self._msgs(doc), "hangul rows under a ko declaration are fine")
        doc["entries"].append({"source_term": u"\ubc88\uc5ed", "target_term": "translation"})
        doc["entries"][0]["source_term"] = u"\uae00\uc9c0"
        self.assertEqual([], self._msgs(doc))
        other = pack_doc("ko", "en", [(u"\u7ffb\u8bd1", "translation")])   # Chinese, labelled ko
        bad = self._msgs(other)
        self.assertTrue(any("no row" in m and "hangul" in m for m in bad),
                        "a ko pack with zero hangul rows must fail, got %r" % (bad,))

    def test_kanji_only_japanese_rows_still_pass_when_the_pack_shows_kana(self):
        # The false-positive guard, and the reason rule 2 is document-level rather than
        # row-level: the shipped en-ja pack has 6 of 14 rows with no kana at all, all of
        # them correct Japanese nouns. Requiring kana per row would fail real product data.
        doc = pack_doc("en", "ja", [("Settings", u"\u8a2d\u5b9a"),
                                    ("Save", u"\u4fdd\u5b58"),
                                    ("Default", u"\u6a19\u6e96"),
                                    ("Import", u"\u30a4\u30f3\u30dd\u30fc\u30c8")])
        self.assertEqual([], self._msgs(doc),
                         "kanji-only Japanese terms are legitimate; only the absence of kana "
                         "across the whole pack is evidence of a mislabelled file")

    def test_a_store_is_not_judged_for_presence(self):
        # Store-shaped: no document-level pair, the language repeats per row, and rows come
        # from whichever packs this user installed. A handful of ja rows without kana there
        # is a partial import, not a mislabelled file -- flagging it would be a false alarm
        # aimed at a user's data rather than at an asset we ship.
        store = {"version": 1, "records": [
            {"source_term": u"\u8a2d\u5b9a", "target_term": "Settings",
             "source_lang": "ja", "target_lang": "en"},
            {"source_term": u"\u4fdd\u5b58", "target_term": "Save",
             "source_lang": "ja", "target_lang": "en"}]}
        _rows, bad = H.check_document(self._write(store, "glossary.json"))
        self.assertEqual([], [m for m in bad if "no row" in m],
                         "presence is a pack property; a store mixes imports")

    def test_presence_still_applies_when_exclusions_already_fire(self):
        # Both halves must be reportable together, and on the same side. Target declared
        # ko but written in katakana: that row uses a sibling's exclusive script AND the
        # file shows no hangul anywhere. A report that stops at the first message would
        # let a file be "fixed" by swapping in one hangul row while staying kana-filled.
        doc = pack_doc("ja", "ko", [(u"\u30a8\u30e9\u30fc", u"\u30c6\u30b9\u30c8")])
        bad = self._msgs(doc)
        self.assertTrue(any("kana" in m and "'ko'" in m for m in bad), bad)
        self.assertTrue(any("no row" in m and "hangul" in m for m in bad), bad)

    def test_a_path_that_cannot_be_read_is_reported_not_raised(self):
        # The tool is pointed at stores by hand during triage. A traceback stops the run and
        # tells the reader nothing about the other files; a finding line does. Pinned because
        # this round added that behaviour, so it must not be an untested difference.
        _rows, bad = H.check_document(os.path.join(self.tmp.name, "absent.json"))
        self.assertEqual(1, len(bad))
        self.assertIn("cannot be read", bad[0])


class ShippedAssetTests(unittest.TestCase):
    """Rules 3 and 4 run on the committed packs, not on fixtures."""

    def test_every_language_a_shipped_pack_declares_is_judged(self):
        # An absent code is not "unchecked for now", it is exempt forever and silently:
        # violations() returns no message because family_of() is None, and PASS is printed.
        # `pl` was in exactly that state -- 14 rows in software-ui-en-pl.json.
        judged, declared = [], set()
        for path in sorted(glob.glob(os.path.join(PACK_DIR, "*.json"))):
            doc = read_json(path)
            for key in ("source_lang", "target_lang"):
                lang = doc.get(key)
                if isinstance(lang, str):
                    declared.add(lang)
        for lang in sorted(declared):
            judged.append((lang, H.family_of(lang) is not None))
        exempt = [lang for lang, ok in judged if not ok]
        self.assertEqual([], exempt,
                         "shipped packs declare languages the gate does not judge: %s" % exempt)

    def test_the_shipped_packs_still_pass_the_gate(self):
        # The new rules must not put a red flag on data that was correct before them:
        # `pl` aside, no shipped row uses a sibling's exclusive script and both cjk packs
        # show their own script. Measured: en-ja has 8/14 kana rows, en-ko 14/14 hangul.
        self.assertEqual(0, H.main([sys.argv[0]]),
                         "a rule that fails our own correct assets is the wrong granularity")

    def test_undeclared_language_is_still_not_judged(self):
        # Difference from rule 3: unknown codes in a USER's store stay exempt -- we decline
        # to guess a script for a language we do not know. Only shipped declarations are
        # required to be covered.
        msgs = H.violations({"source_term": "anything", "target_term": "choses"},
                            "sw", "fr", side="row")
        self.assertEqual([], msgs)


class GateAgreementTests(unittest.TestCase):
    """The gate's claim that it mirrors lang.rs must itself be checked."""

    def test_gate_agreement_with_rust(self):
        body = read_text(LANG_RS)
        # `=> Some(...)` or `=> { Some(...) }`: rustfmt wraps a long arm into a block, and
        # a parser that only accepts the inline form quietly returns a partial table --
        # which this test then reports as "too little parsed" instead of comparing 5 keys
        # against 16 and calling the difference agreement.
        arms = re.findall(r'((?:"[a-z-]+"\s*\|?\s*)+)=>\s*\{?\s*Some\(Script::(\w+)\)', body)
        table = {}
        for codes, family in arms:
            for code in re.findall(r'"([a-z-]+)"', codes):
                table[code] = family.lower()
        # Anti-vacuous assertion first: a parse that finds nothing would make every
        # comparison below trivially true, which is the failure mode this repo already
        # recorded once (two empty sides reading as agreement).
        self.assertGreaterEqual(len(table), 15, "lang.rs parse found too little: %r" % table)
        self.assertEqual({"cjk", "cyrillic", "latin"}, set(table.values()),
                         "lang.rs families drifted from the three the gate models")
        self.assertEqual(table, H.FAMILY,
                         "the gate's family table no longer mirrors lang.rs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
