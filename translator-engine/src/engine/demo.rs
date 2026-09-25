// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 演示引擎：内置中英词表与短语表，做词级双向替换翻译。
//!
//! 定位：P0 阶段用于跑通「.NET -> FFI -> Rust 引擎」整条链路，
//! 不追求翻译质量。真实引擎接入后，本引擎作为 fallback 保留。

use super::Translator;
use std::collections::HashMap;

/// 英文 -> 中文 常用词表（P0 演示子集）
const EN_ZH: &[(&str, &str)] = &[
    ("hello there", "你好"),
    ("good morning", "早上好"),
    ("good night", "晚安"),
    ("thank you", "谢谢"),
    ("hello", "你好"),
    ("world", "世界"),
    ("good", "好的"),
    ("morning", "早晨"),
    ("night", "夜晚"),
    ("thank", "谢谢"),
    ("you", "你"),
    ("today", "今天"),
    ("tomorrow", "明天"),
    ("software", "软件"),
    ("translation", "翻译"),
    ("voice", "语音"),
    ("the", "这个"),
    ("dog", "狗"),
    ("cat", "猫"),
];

/// 中文 -> 英文 反向词表
const ZH_EN: &[(&str, &str)] = &[
    ("你好", "hello"),
    ("早上好", "good morning"),
    ("晚安", "good night"),
    ("谢谢", "thank you"),
    ("世界", "world"),
    ("你好世界", "hello world"),
    ("好的", "good"),
    ("今天", "today"),
    ("明天", "tomorrow"),
    ("软件", "software"),
    ("翻译", "translation"),
    ("语音", "voice"),
    ("这个", "the"),
    ("狗", "dog"),
    ("猫", "cat"),
];

pub struct DemoTranslator {
    en_zh_long: HashMap<String, String>, // 先匹配长短语
    en_zh_word: HashMap<String, String>,
    zh_en_long: HashMap<String, String>,
    zh_en_word: HashMap<String, String>,
}

impl Default for DemoTranslator {
    fn default() -> Self {
        let mut en_zh_long = HashMap::new();
        let mut en_zh_word = HashMap::new();
        let mut zh_en_long = HashMap::new();
        let mut zh_en_word = HashMap::new();

        // 短语优先：词组在词表，先按词序插入长表
        for (en, zh) in EN_ZH {
            if en.split_whitespace().count() > 1 {
                en_zh_long.insert(en.to_string(), zh.to_string());
            } else {
                en_zh_word.insert(en.to_string(), zh.to_string());
            }
        }
        for (zh, en) in ZH_EN {
            if zh.chars().count() > 2 {
                zh_en_long.insert(zh.to_string(), en.to_string());
            } else {
                zh_en_word.insert(zh.to_string(), en.to_string());
            }
        }

        Self {
            en_zh_long,
            en_zh_word,
            zh_en_long,
            zh_en_word,
        }
    }
}

impl DemoTranslator {
    fn translate_en_zh(&self, text: &str) -> Option<String> {
        let lower = text.to_lowercase();
        // 1) 整句/长短语命中
        if let Some(hit) = self.en_zh_long.get(&lower) {
            return Some(hit.clone());
        }
        // 2) 词级替换
        let words: Vec<&str> = lower.split_whitespace().collect();
        let mut out = Vec::new();
        let mut hit = 0;
        for w in words {
            if let Some(zh) = self.en_zh_word.get(w) {
                out.push(zh.clone());
                hit += 1;
            } else {
                out.push(w.to_string());
            }
        }
        if hit == 0 {
            None
        } else {
            Some(out.join(" "))
        }
    }

    fn translate_zh_en(&self, text: &str) -> Option<String> {
        // 1) 长短语（无空格分词，直接查整串）
        let trimmed = text.trim();
        if let Some(hit) = self.zh_en_long.get(trimmed) {
            return Some(hit.clone());
        }
        // 2) 逐字符扫描 2 字词
        let mut out = Vec::new();
        let mut hit = 0;
        let mut i = 0;
        let chars: Vec<char> = trimmed.chars().collect();
        while i < chars.len() {
            // 优先匹配 2 字词
            if i + 1 < chars.len() {
                let two: String = chars[i..=i + 1].iter().collect();
                if let Some(en) = self.zh_en_word.get(&two) {
                    out.push(en.clone());
                    hit += 1;
                    i += 2;
                    continue;
                }
            }
            out.push(chars[i].to_string());
            i += 1;
        }
        if hit == 0 {
            None
        } else {
            Some(out.join(" "))
        }
    }
}

impl Translator for DemoTranslator {
    fn name(&self) -> &'static str {
        "demo"
    }

    fn translate(&self, text: &str, source: &str, target: &str) -> Option<String> {
        let text = text.trim();
        if text.is_empty() {
            return None;
        }
        match (source, target) {
            ("en", "zh") => self.translate_en_zh(text),
            ("zh", "en") => self.translate_zh_en(text),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn en_zh_phrase() {
        let e = DemoTranslator::default();
        assert_eq!(e.translate("Hello there", "en", "zh").as_deref(), Some("你好"));
    }

    #[test]
    fn en_zh_word_sub() {
        let e = DemoTranslator::default();
        let r = e.translate("i love the world", "en", "zh").unwrap();
        assert!(r.contains("世界"));
    }

    #[test]
    fn zh_en_word() {
        let e = DemoTranslator::default();
        assert_eq!(e.translate("你好", "zh", "en").as_deref(), Some("hello"));
    }

    #[test]
    fn unsupported_pair() {
        let e = DemoTranslator::default();
        assert_eq!(e.translate("hi", "en", "fr"), None);
    }
}
