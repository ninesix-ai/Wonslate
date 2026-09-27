// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Confidence estimation for translation results (heuristic, MVP version).
//!
//! The baseline score comes from the engine type; high glossary coverage adds
//! points, empty results / placeholders subtract, and an output identical to
//! the source subtracts (the language was not actually converted).

/// Baseline confidence per engine id.
const BASELINE: &[(&str, f32)] = &[
    ("ollama",          0.95),
    ("ollama-qwen",     0.95),
    ("argos",           0.72),
    ("argos_glossary",  0.75),
    ("madlad",          0.68),
    ("demo",            0.55),
    ("tm",              1.00),  // TM hits: the quality field decides the real score
];

/// Estimate the confidence of a translation result (0.0 ~ 1.0).
///
/// - `engine`: id of the engine that produced the result
/// - `glossary_coverage`: share of tokens hit by the glossary (0.0~1.0)
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

    // Glossary-coverage bonus (at most +0.15).
    score += glossary_coverage.min(1.0) * 0.15;

    // Placeholders present -> heavy penalty.
    if translated.contains("[??]") || translated.contains("???") || translated.contains("TODO") {
        score -= 0.40;
    }

    // Output identical to the source and the source contains non-ASCII
    // characters (the language was never actually converted).
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
        // baseline 0.72 - 0.40 = 0.32
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
