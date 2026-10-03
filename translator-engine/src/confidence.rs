// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Confidence estimation for translation results (heuristic).
//!
//! The score starts from the engine's reputation and is then adjusted by signals
//! read off the actual output: whether it is written in the target language's
//! script, whether its length is in a sane band, how much of the glossary it covers,
//! and whether it echoes the source or contains placeholders.
//!
//! Why the signals exist (D11): with a per-engine constant alone every argos result
//! scored exactly 0.72, while full mode escalates below 0.85 - so argos never cleared
//! the bar and *every* miss was sent to the AI engine (measured: 797/797). With no AI
//! engine reachable that meant a wasted ~1 s per request and, because the fallback
//! branch does not write the translation memory, a flywheel that never turned
//! (measured: 0.00% TM hits over 300 requests with 47% reuse). A confidence that
//! cannot separate a good result from a bad one is not a confidence.

use crate::lang;

/// Confidence reported for a result the AI engine produced. A single source of truth
/// because it is also the quality a TM entry written from that result carries, and the
/// "quality first" preference sets the cache floor to exactly this value - the two must
/// not drift apart.
pub const AI_UPGRADE_CONFIDENCE: f32 = 0.95;

/// Baseline confidence per engine id: what this engine is worth *before* looking at
/// the output. Deliberately a prior, not the answer.
const BASELINE: &[(&str, f32)] = &[
    ("ollama",          0.95),
    ("ollama-qwen",     0.95),
    ("argos",           0.72),
    ("argos_glossary",  0.75),
    ("madlad",          0.68),
    ("demo",            0.55),
    ("tm",              1.00),  // TM hits: the quality field decides the real score
];

/// Credit for an output that looks like a translation: target script present and a
/// plausible length. This is what lifts a good argos result over the 0.85 gate.
const CONFORMANCE_BONUS: f32 = 0.16;
/// The output is not in the target language at all - the translation did not happen.
const SCRIPT_MISMATCH_PENALTY: f32 = 0.35;
/// Placeholders mean the engine skipped a token; severe enough to outweigh conformance.
const PLACEHOLDER_PENALTY: f32 = 0.55;
/// Output identical to a non-ASCII source (the language was never converted).
const ECHO_PENALTY: f32 = 0.30;
/// Max glossary-coverage bonus.
const COVERAGE_WEIGHT: f32 = 0.15;

/// Characters of target per character of source outside which the output is not a
/// translation of the input: a fragment of it, or a runaway. Kept loose on purpose -
/// the script check carries the discrimination, this only catches the absurd.
/// The upper bound has to clear zh->en, where a short Chinese sentence routinely
/// expands past 6x ("九点开饭" -> "Dinner is served at nine." is 6.25x).
const MIN_LENGTH_RATIO: f32 = 0.15;
const MAX_LENGTH_RATIO: f32 = 8.0;

/// Estimate the confidence of a translation result (0.0 ~ 1.0).
///
/// - `engine`: id of the engine that produced the result
/// - `glossary_coverage`: share of tokens hit by the glossary (0.0~1.0)
/// - `target_lang`: language the output is supposed to be in; drives the script check
pub fn estimate(
    translated: &str,
    source_text: &str,
    engine: &str,
    glossary_coverage: f32,
    target_lang: &str,
) -> f32 {
    let out = translated.trim();
    if out.is_empty() {
        return 0.0;
    }

    let mut score = BASELINE
        .iter()
        .find(|(name, _)| *name == engine)
        .map_or(0.60_f32, |(_, s)| *s);

    // Target-script and length conformance: the two signals that actually describe
    // this output rather than the engine that made it.
    if !lang::has_script_for(out, target_lang) {
        score -= SCRIPT_MISMATCH_PENALTY;
    } else if length_is_plausible(out, source_text) {
        score += CONFORMANCE_BONUS;
    }

    score += glossary_coverage.min(1.0) * COVERAGE_WEIGHT;

    if out.contains("[??]") || out.contains("???") || out.contains("TODO") {
        score -= PLACEHOLDER_PENALTY;
    }

    if out == source_text.trim() && source_text.chars().any(|c| !c.is_ascii()) {
        score -= ECHO_PENALTY;
    }

    score.clamp(0.0, 1.0)
}

/// Length sanity: catches a one-word answer to a paragraph and runaway output.
fn length_is_plausible(translated: &str, source_text: &str) -> bool {
    let src = source_text.trim().chars().count();
    if src == 0 {
        return true;   // nothing to compare against
    }
    let ratio = translated.chars().count() as f32 / src as f32;
    (MIN_LENGTH_RATIO..=MAX_LENGTH_RATIO).contains(&ratio)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ollama_high_baseline() {
        let s = estimate("Hello world", "你好世界", "ollama-qwen", 0.0, "en");
        assert!(s >= 0.90, "got {s}");
    }

    #[test]
    fn empty_output_zero() {
        assert_eq!(estimate("", "你好", "ollama-qwen", 0.0, "en"), 0.0);
        assert_eq!(estimate("   ", "你好", "argos", 0.0, "en"), 0.0);
    }

    #[test]
    fn placeholder_penalized() {
        let s = estimate("hello [??] world", "你好世界", "argos", 0.0, "en");
        // 0.72 + 0.16 conformance - 0.55 placeholder = 0.33
        assert!(s < 0.45, "got {s}");
    }

    #[test]
    fn same_text_non_ascii_penalized() {
        let s = estimate("你好世界", "你好世界", "demo", 0.0, "en");
        // 0.55 - 0.35 wrong script - 0.30 echo -> 0.0
        assert!(s < 0.35, "got {s}");
    }

    #[test]
    fn glossary_coverage_bonus() {
        let no = estimate("Hello world", "你好世界", "argos", 0.0, "en");
        let hi = estimate("Hello world", "你好世界", "argos", 1.0, "en");
        assert!(hi > no, "{hi} should beat {no}");
    }

    // ---- D11: the score has to separate a good result from a bad one ----------

    #[test]
    fn good_local_result_clears_the_full_mode_gate() {
        // The defect: every argos result scored 0.72 < 0.85, so full mode escalated
        // 100% of misses. A well-formed output must now clear the gate.
        let s = estimate("Dinner is from 9 a.m. to 5 p.m.", "开饭时间早上9点至下午5点", "argos", 0.0, "en");
        assert!(s >= 0.85, "a good argos result must not be escalated, got {s}");
    }

    #[test]
    fn wrong_script_result_stays_below_the_gate() {
        // Same engine, same pair, but the engine returned the source unchanged in
        // script terms - that must escalate.
        let s = estimate("开饭时间早上9点至下午5点", "开饭时间早上9点至下午5点。", "argos", 0.0, "en");
        assert!(s < 0.85, "an unconverted result must escalate, got {s}");
    }

    #[test]
    fn demo_still_escalates() {
        // The fix must not make the placeholder engine look trustworthy.
        let s = estimate("Dinner is served.", "开饭时间到了", "demo", 0.0, "en");
        assert!(s < 0.85, "demo must stay below the gate, got {s}");
    }

    #[test]
    fn baseline_ordering_is_preserved_for_identical_output() {
        // The engine that ran must still decide the ranking: scoring a demo result as
        // if a better engine produced it is the defect this ordering guards, and it
        // must survive the addition of output-derived signals.
        let out = "Dinner is from 9 a.m. to 5 p.m.";
        let src = "开饭时间早上9点至下午5点";
        let demo = estimate(out, src, "demo", 0.0, "en");
        let madlad = estimate(out, src, "madlad", 0.0, "en");
        let argos = estimate(out, src, "argos", 0.0, "en");
        let ollama = estimate(out, src, "ollama", 0.0, "en");

        assert!(demo < madlad, "demo {demo} should trail madlad {madlad}");
        assert!(madlad < argos, "madlad {madlad} should trail argos {argos}");
        assert!(argos < ollama, "argos {argos} should trail ollama {ollama}");
    }

    #[test]
    fn unclassified_target_language_is_not_penalized_for_script() {
        let s = estimate("habari yako", "你好", "argos", 0.0, "sw");
        assert!(s >= 0.85, "cannot judge the script, so must not penalize, got {s}");
    }

    #[test]
    fn length_sanity_catches_fragments_and_runaways() {
        assert!(length_is_plausible("Dinner is served at nine.", "九点开饭"));
        assert!(!length_is_plausible("OK", "九点开饭，请准时到场，谢谢配合"), "fragment");
        assert!(!length_is_plausible(&"x".repeat(300), "九点开饭"), "runaway");
    }

    #[test]
    fn fragment_stays_below_the_gate_even_with_the_right_script() {
        // Right script but far too short to be a translation of the whole sentence.
        let s = estimate("OK", "九点开饭，请准时到场，谢谢配合", "argos", 0.0, "en");
        assert!(s < 0.85, "got {s}");
    }
}
