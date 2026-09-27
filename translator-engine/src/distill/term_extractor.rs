// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Term-pair extraction algorithm (MVP: N-gram + linear alignment + stop-word filter)
//!
//! Longer-term upgrade path: jieba-rs tokenization -> GIZA++ alignment -> neural term extraction

use crate::types::GlossaryEntry;

const ZH_STOP: &[&str] = &["的","了","是","在","有","和","我","你","他","她","这","那","也","都","而","与"];
const EN_STOP: &[&str] = &["the","a","an","is","are","was","were","it","to","of","and","in","that","this","for","on"];

/// Extract candidate term pairs from a (source_text, target_text) pair
pub fn extract_term_pairs(
    source_text: &str,
    source_lang: &str,
    target_text: &str,
    target_lang: &str,
) -> Vec<GlossaryEntry> {
    let mut results = vec![];

    // Extract only for zh->en / en->zh (other pairs are skipped in the MVP)
    if source_lang != "zh" && source_lang != "en" {
        return results;
    }
    let tgt_words: Vec<&str> = target_text
        .split_whitespace()
        .map(|w| w.trim_matches(|c: char| !c.is_alphanumeric()))
        .filter(|w| !w.is_empty())
        .collect();
    if tgt_words.is_empty() {
        return results;
    }

    if source_lang == "zh" {
        // Chinese 2/3/4-grams; estimate the English span by linear alignment
        let chars: Vec<char> = source_text.chars().collect();
        if chars.is_empty() { return results; }
        let ratio = chars.len() as f32 / tgt_words.len() as f32;

        for n in [2usize, 3usize, 4usize] {
            if chars.len() < n { break; }
            for i in 0..=(chars.len() - n) {
                let ngram: String = chars[i..i + n].iter().collect();
                // drop n-grams containing stop words
                if ZH_STOP.iter().any(|s| ngram == *s) { continue; }
                // estimate the English position
                let ts = ((i as f32) / ratio).floor() as usize;
                let te = ((i + n) as f32 / ratio).ceil() as usize;
                if ts >= tgt_words.len() { continue; }
                let en = tgt_words[ts.min(tgt_words.len())..te.min(tgt_words.len())].join(" ").to_lowercase();
                if en.is_empty() || EN_STOP.contains(&en.as_str()) { continue; }
                results.push(GlossaryEntry {
                    source_term: ngram,
                    source_lang: source_lang.into(),
                    target_term: en,
                    target_lang: target_lang.into(),
                    confidence: 0.90,
                    frequency: 1,
                    domain: String::new(),
                });
            }
        }
    } else {
        // English -> Chinese: word N-grams of length 1-3
        let en_words: Vec<&str> = source_text.split_whitespace().collect();
        let zh_chars: Vec<char> = target_text.chars().collect();
        if en_words.is_empty() || zh_chars.is_empty() { return results; }
        let ratio = zh_chars.len() as f32 / en_words.len() as f32;

        for n in [1usize, 2usize, 3usize] {
            if en_words.len() < n { break; }
            for i in 0..=(en_words.len() - n) {
                let en = en_words[i..i + n].join(" ").to_lowercase();
                if EN_STOP.contains(&en.as_str()) || en.len() <= 1 { continue; }
                let ts = ((i as f32) * ratio).floor() as usize;
                let te = ((i + n) as f32 * ratio).ceil() as usize;
                if ts >= zh_chars.len() { continue; }
                let zh: String = zh_chars[ts.min(zh_chars.len())..te.min(zh_chars.len())].iter().collect();
                if zh.is_empty() || ZH_STOP.iter().any(|s| zh == *s) { continue; }
                results.push(GlossaryEntry {
                    source_term: en,
                    source_lang: source_lang.into(),
                    target_term: zh,
                    target_lang: target_lang.into(),
                    confidence: 0.90,
                    frequency: 1,
                    domain: String::new(),
                });
            }
        }
    }

    results
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zh_en_basic_extraction() {
        let pairs = extract_term_pairs("深度学习很好", "zh", "deep learning is great", "en");
        // at least some term pairs should be extracted
        assert!(!pairs.is_empty());
    }

    #[test]
    fn stopword_not_extracted() {
        let pairs = extract_term_pairs("我们", "zh", "the", "en");
        // "我们" is not a stop word, but its aligned English word "the" is one,
        // so nothing gets stored for it.
        for p in &pairs {
            assert_ne!(p.target_term, "the");
        }
    }
}
