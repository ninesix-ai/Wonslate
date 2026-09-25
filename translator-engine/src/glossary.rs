// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 术语表上下文构建（从 glossary 表取相关术语对，注入 Translator prompt）

use crate::error::EngineError;
use crate::tm;
use crate::types::{GlossaryEntry, TmEntry};

/// Glossary 上下文（传递给 Translator::translate_with_context）
#[derive(Debug, Clone, Default)]
pub struct GlossaryContext {
    pub entries: Vec<GlossaryEntry>,
}

impl GlossaryContext {
    pub fn empty() -> Self {
        Self::default()
    }
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
    pub fn top_terms(&self, n: usize) -> &[GlossaryEntry] {
        &self.entries[..n.min(self.entries.len())]
    }

    /// 计算 text 被 glossary 覆盖的条目比例（0.0~1.0）
    pub fn coverage(&self, text: &str) -> f32 {
        if self.entries.is_empty() {
            return 0.0;
        }
        let lower = text.to_lowercase();
        let hit = self
            .entries
            .iter()
            .filter(|e| lower.contains(&e.source_term.to_lowercase()))
            .count();
        (hit as f32 / self.entries.len() as f32).min(1.0)
    }

    /// 将 few-shot TM 条目合并进上下文（层级 2：few-shot 示例注入）
    pub fn with_few_shot(mut self, tm_entries: Vec<TmEntry>) -> Self {
        for e in tm_entries {
            self.entries.push(GlossaryEntry {
                source_term: e.source_text,
                source_lang: e.source_lang,
                target_term: e.target_text,
                target_lang: e.target_lang,
                confidence: e.quality,
                frequency: e.hit_count,
                domain: e.domain,
            });
        }
        self
    }
}

/// 从 DB 取当前语言对最高频/最高置信的术语，构建本次翻译的上下文
pub fn build_context(
    _text: &str,
    source_lang: &str,
    target_lang: &str,
    _domain: &str,
    max_terms: usize,
) -> Result<GlossaryContext, EngineError> {
    let entries = tm::glossary_list(source_lang, target_lang, max_terms)?;
    Ok(GlossaryContext { entries })
}

/// 蒸馏/手动 upsert 术语对
pub fn upsert(entry: &GlossaryEntry) -> Result<(), EngineError> {
    tm::glossary_upsert(entry)
}

/// 用户手动删除术语对
pub fn delete(source_term: &str, source_lang: &str, target_lang: &str) -> Result<(), EngineError> {
    tm::glossary_delete(source_term, source_lang, target_lang)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{GlossaryEntry, TmEntry};

    fn entry(s: &str, t: &str, conf: f32) -> GlossaryEntry {
        GlossaryEntry {
            source_term: s.into(), source_lang: "zh".into(),
            target_term: t.into(), target_lang: "en".into(),
            confidence: conf, frequency: 1, domain: String::new(),
        }
    }

    #[test]
    fn empty_context_defaults() {
        let ctx = GlossaryContext::empty();
        assert!(ctx.is_empty());
        assert_eq!(ctx.top_terms(10).len(), 0);
        assert_eq!(ctx.coverage("任何文本"), 0.0);
    }

    #[test]
    fn coverage_measures_fraction_of_terms_present_in_text() {
        let ctx = GlossaryContext { entries: vec![
            entry("人工智能", "AI", 0.95),
            entry("机器学习", "ML", 0.95),
            entry("深度学习", "deep learning", 0.95),
            entry("神经网络", "neural network", 0.95),
        ]};
        // 文本包含前 2 个术语，coverage 应为 2/4 = 0.5
        let c = ctx.coverage("人工智能和机器学习很热门");
        assert!((c - 0.5).abs() < 1e-6);
    }

    #[test]
    fn coverage_is_case_insensitive_on_source_term() {
        let ctx = GlossaryContext { entries: vec![entry("hello world", "x", 0.9)] };
        assert!(ctx.coverage("HELLO WORLD is great") > 0.9);
    }

    #[test]
    fn top_terms_truncates_to_n() {
        let ctx = GlossaryContext { entries: (0..30).map(|i|
            entry(&format!("t{}", i), &format!("e{}", i), 0.9)).collect() };
        assert_eq!(ctx.top_terms(5).len(), 5);
        assert_eq!(ctx.top_terms(100).len(), 30);   // 超过实际数量返回全部
    }

    #[test]
    fn with_few_shot_appends_tm_entries_as_glossary() {
        let ctx = GlossaryContext { entries: vec![entry("已有", "existing", 0.9)] };
        let tm = vec![TmEntry {
            source_text: "人工智能".into(), source_lang: "zh".into(),
            target_text: "Artificial Intelligence".into(), target_lang: "en".into(),
            engine: "ollama".into(), quality: 0.95, hit_count: 42, domain: String::new(),
        }];
        let merged = ctx.with_few_shot(tm);
        assert_eq!(merged.entries.len(), 2);
        // 新增条目由 TmEntry 映射而来
        let new = &merged.entries[1];
        assert_eq!(new.source_term, "人工智能");
        assert_eq!(new.target_term, "Artificial Intelligence");
        assert_eq!(new.frequency, 42);       // hit_count → frequency
        assert!((new.confidence - 0.95).abs() < 1e-6);  // quality → confidence
    }

    #[test]
    fn coverage_clamped_to_one() {
        let ctx = GlossaryContext { entries: vec![entry("x", "y", 0.9)] };
        // 重复命中同一术语不会超过 1.0
        let c = ctx.coverage("x x x x x");
        assert!(c <= 1.0 && c > 0.0);
    }
}
