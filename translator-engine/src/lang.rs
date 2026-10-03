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

/// Script family a language is written in. `None` = not classified, and callers must
/// treat that as "cannot judge" rather than "does not match".
pub fn script_of(lang: &str) -> Option<Script> {
    match lang.trim().to_lowercase().as_str() {
        "zh" | "ja" | "ko" | "yue" => Some(Script::Cjk),
        "ru" => Some(Script::Cyrillic),
        "en" | "fr" | "de" | "es" | "pt" | "it" | "nl" | "tr" | "id" | "vi" => Some(Script::Latin),
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
        Some(Script::Latin) => text.chars().any(|c| c.is_ascii_alphabetic()),
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
