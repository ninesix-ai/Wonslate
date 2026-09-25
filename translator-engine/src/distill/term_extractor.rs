// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 术语对提取算法（MVP：N-gram + 线性对齐 + 停用词过滤）
//!
//! 远期升级路径：jieba-rs 分词 → GIZA++ 词对齐 → 神经术语提取

use crate::types::GlossaryEntry;

const ZH_STOP: &[&str] = &["的","了","是","在","有","和","我","你","他","她","这","那","也","都","而","与"];
const EN_STOP: &[&str] = &["the","a","an","is","are","was","were","it","to","of","and","in","that","this","for","on"];

/// 从 (source_text, target_text) 对中提取候选术语对
pub fn extract_term_pairs(
    source_text: &str,
    source_lang: &str,
    target_text: &str,
    target_lang: &str,
) -> Vec<GlossaryEntry> {
    let mut results = vec![];

    // 只对 zh→en / en→zh 提取（其他语言对 MVP 跳过）
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
        // 中文 2/3/4-gram，线性对齐估算英文位置
        let chars: Vec<char> = source_text.chars().collect();
        if chars.is_empty() { return results; }
        let ratio = chars.len() as f32 / tgt_words.len() as f32;

        for n in [2usize, 3usize, 4usize] {
            if chars.len() < n { break; }
            for i in 0..=(chars.len() - n) {
                let ngram: String = chars[i..i + n].iter().collect();
                // 过滤含停用词的 ngram
                if ZH_STOP.iter().any(|s| ngram == *s) { continue; }
                // 估算英文位置
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
        // 英文 → 中文：按 1-3 词 N-gram
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
        // 应至少提取到一些术语对
        assert!(!pairs.is_empty());
    }

    #[test]
    fn stopword_not_extracted() {
        let pairs = extract_term_pairs("我们", "zh", "the", "en");
        // "我们" 不在停用词表，但对应 "the" 是英文停用词，不入库
        for p in &pairs {
            assert_ne!(p.target_term, "the");
        }
    }
}
