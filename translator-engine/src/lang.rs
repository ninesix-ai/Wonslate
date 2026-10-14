// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Small language/script predicates shared by the quality checks.
//!
//! Two independent places need to ask "is this text written in the language it
//! claims to be": the distillation term scorer (a Latin span under a Chinese target
//! means the aligner crossed the sentence) and the confidence estimator (an output in
//! the wrong script means the translation never happened). They must agree, so the
//! rule lives here once.

/// Han ideographs, including the CJK extension A and compatibility blocks.
pub fn is_cjk(c: char) -> bool {
    matches!(c as u32, 0x3400..=0x4DBF | 0x4E00..=0x9FFF | 0xF900..=0xFAFF)
}

fn is_kana(c: char) -> bool {
    matches!(c as u32, 0x3040..=0x30FF)
}

fn is_hangul(c: char) -> bool {
    matches!(c as u32, 0xAC00..=0xD7AF | 0x1100..=0x11FF)
}

fn is_cyrillic(c: char) -> bool {
    matches!(c as u32, 0x0400..=0x04FF)
}

/// Latin letters, accents included: ASCII plus Latin-1 Supplement and Latin
/// Extended-A/B. Deliberately wider than `is_ascii_alphabetic`: the Polish word for
/// "yellow" is written entirely with diacritics, and an ASCII-only test would call
/// such a correct translation a script mismatch and cost it the conformance bonus.
/// The shipped-asset gate uses the same range
/// (tests/test_glossary_hygiene.py SCRIPTS["latin"]); keeping the two equal is what
/// lets its family table be compared with `script_of` with nothing else to remember.
pub fn is_latin(c: char) -> bool {
    c.is_ascii_alphabetic() || matches!(c as u32, 0x00C0..=0x024F)
}

/// Script family a language is written in. `None` = not classified, and callers must
/// treat that as "cannot judge" rather than "does not match".
///
/// An unclassified code is exempt at runtime *and* in the shipped-asset gate, which is
/// why the exemption has to be a decision rather than an oversight:
/// docs/glossary-packs/software-ui-en-pl.json declared `pl` while this arm and the
/// gate's table both omitted it, so those 14 rows went unjudged with nothing saying so
/// (D63). tests/test_glossary_hygiene_rules.py now fails if a shipped pack declares a
/// language missing here, and compares the two tables directly.
pub fn script_of(lang: &str) -> Option<Script> {
    match lang.trim().to_lowercase().as_str() {
        "zh" | "ja" | "ko" | "yue" => Some(Script::Cjk),
        "ru" => Some(Script::Cyrillic),
        "en" | "fr" | "de" | "es" | "pt" | "it" | "nl" | "tr" | "id" | "vi" | "pl" => {
            Some(Script::Latin)
        }
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Script {
    Cjk,
    Cyrillic,
    Latin,
}

/// Whether `text` contains at least one character of `lang`'s script.
///
/// Deliberately "contains" rather than "consists only of": real output mixes in
/// numbers, punctuation, proper nouns and loanwords. The question being answered is
/// "did the engine write the target language at all", and one matching character
/// already refutes the wrong-script case.
pub fn has_script_for(text: &str, lang: &str) -> bool {
    match script_of(lang) {
        None => true,   // unclassified language: do not judge, do not penalize
        Some(Script::Cjk) => text.chars().any(|c| is_cjk(c) || is_kana(c) || is_hangul(c)),
        Some(Script::Cyrillic) => text.chars().any(is_cyrillic),
        Some(Script::Latin) => text.chars().any(is_latin),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cjk_detected_across_scripts() {
        assert!(has_script_for("深度学习", "zh"));
        assert!(has_script_for("こんにちは", "ja"));
        assert!(has_script_for("안녕하세요", "ko"));
        assert!(has_script_for("深度学习 v2.0", "zh"), "mixed content still counts");
    }

    #[test]
    fn latin_and_cyrillic_separated() {
        assert!(has_script_for("hello world", "en"));
        assert!(!has_script_for("深度学习", "en"));
        assert!(has_script_for("Привет", "ru"));
        assert!(!has_script_for("hello", "ru"));
        assert!(!has_script_for("Привет", "en"));
    }

    #[test]
    fn accented_latin_counts_as_latin() {
        // The predicate used to be is_ascii_alphabetic, which reads a diacritic-only
        // word as "not the target language at all". Polish supplies the sharpest case
        // because the word below has no ASCII letter in it, and `pl` itself was missing
        // from script_of while a shipped pack declares it (D63).
        assert!(has_script_for("\u{017C}\u{00F3}\u{0142}\u{0107}", "pl"), "yellow");
        assert!(has_script_for("\u{00DC}ber", "de"));
        assert!(has_script_for("Ustawienia", "pl"));
        // Classifying pl is only useful if a non-Latin output still fails:
        assert!(!has_script_for("\u{8A2D}\u{5B9A}", "pl"), "Han under pl is a mismatch");
    }

    #[test]
    fn unclassified_language_is_never_penalized() {
        assert!(has_script_for("anything", "sw"));
        assert!(has_script_for("", "xx"));
        assert_eq!(script_of("sw"), None);
        assert_eq!(script_of("zh"), Some(Script::Cjk));
        assert_eq!(script_of("ZH"), Some(Script::Cjk), "case-insensitive");
    }

    #[test]
    fn empty_text_has_no_script() {
        assert!(!has_script_for("", "zh"));
        assert!(!has_script_for("   ", "en"));
    }
}
