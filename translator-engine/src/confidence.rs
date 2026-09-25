// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 翻译结果置信度估算（启发式，MVP 版本）
//!
//! 基线分来自引擎类型，glossary 覆盖率高时加分，
//! 空结果/占位符扣分，结果与源文本相同时扣分（语言未转换）。

/// 引擎基线置信度
const BASELINE: &[(&str, f32)] = &[
    ("ollama",          0.95),
    ("ollama-qwen",     0.95),
    ("argos",           0.72),
    ("argos_glossary",  0.75),
    ("madlad",          0.68),
    ("demo",            0.55),
    ("tm",              1.00),  // TM 命中由 quality 字段决定实际分
];

/// 估算翻译结果的置信度（0.0 ~ 1.0）
///
/// - `engine`：产生结果的引擎 id
/// - `glossary_coverage`：本次翻译中命中术语表的 token 比例（0.0~1.0）
pub fn estimate(
    translated: &str,
    source_text: &str,
    engine: &str,
    glossary_coverage: f32,
) -> f32 {
    if translated.trim().is_empty() {
        return 0.0;
    }

    let mut score = BASELINE
        .iter()
        .find(|(name, _)| *name == engine)
        .map_or(0.60_f32, |(_, s)| *s);

    // 术语表覆盖加分（最多 +0.15）
    score += glossary_coverage.min(1.0) * 0.15;

    // 含占位符 → 强扣分
    if translated.contains("[??]") || translated.contains("???") || translated.contains("TODO") {
        score -= 0.40;
    }

    // 译文与源文本完全相同 且 源文本含非 ASCII 字符（语言未真正转换）
    if translated.trim() == source_text.trim()
        && source_text.chars().any(|c| !c.is_ascii())
    {
        score -= 0.30;
    }

    score.clamp(0.0, 1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ollama_high_baseline() {
        let s = estimate("Hello world", "你好世界", "ollama-qwen", 0.0);
        assert!(s >= 0.90);
    }

    #[test]
    fn empty_output_zero() {
        assert_eq!(estimate("", "你好", "ollama-qwen", 0.0), 0.0);
    }

    #[test]
    fn placeholder_penalized() {
        let s = estimate("hello [??] world", "你好世界", "argos", 0.0);
        // 基线 0.72 - 0.40 = 0.32
        assert!(s < 0.45);
    }

    #[test]
    fn same_text_non_ascii_penalized() {
        let s = estimate("你好世界", "你好世界", "demo", 0.0);
        // 0.55 - 0.30 = 0.25
        assert!(s < 0.35);
    }

    #[test]
    fn glossary_coverage_bonus() {
        let s_no = estimate("Hello world", "你好世界", "argos", 0.0);
        let s_hi = estimate("Hello world", "你好世界", "argos", 1.0);
        assert!(s_hi > s_no);
    }
}
