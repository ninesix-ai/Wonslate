// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Term-pair extraction with explainable per-candidate scoring.
//!
//! N-gram + linear alignment + stop-word filter. The aligner is a plain
//! character<->word projection: right often enough to be useful, wrong often
//! enough to be dangerous. Until N-08 every candidate was stamped 0.90 — the same
//! value as the store threshold — so nothing could ever be rejected and misaligned
//! pairs ended up in the glossary, which is injected into AI prompts as a term
//! constraint. Each candidate now carries a score built from named signals.
//!
//! Longer-term upgrade path: jieba-rs tokenization -> GIZA++ alignment -> neural term extraction

use crate::lang::{has_script_for, is_cjk};
use crate::types::GlossaryEntry;

const ZH_STOP: &[&str] = &["的","了","是","在","有","和","我","你","他","她","这","那","也","都","而","与"];
const EN_STOP: &[&str] = &["the","a","an","is","are","was","were","it","to","of","and","in","that","this","for","on"];

/// Weight of the graded alignment signal (span length vs the ratio estimate).
const W_ALIGNMENT: f32 = 0.55;
/// Weight of the script gate. A span written in the wrong script loses this much,
/// which drops it below the store bar unless the alignment was perfect.
const W_SCRIPT: f32 = 0.45;
/// A function word at either edge means the sentence was cut at the wrong place.
/// Halving keeps the candidate visibly weak instead of silently plausible.
const EDGE_PENALTY: f32 = 0.5;

/// Signal breakdown behind one candidate's confidence.
///
/// Kept alongside the score so a stored term can be explained after the fact
/// ("why is this pair at 0.84?"), which the previous constant could not answer.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TermScore {
    /// How closely the aligned span length matches the length the sentence-level
    /// ratio implies (0.0 / 0.7 / 1.0).
    pub alignment: f32,
    /// The aligned span is written in the target language's script (0.0 or 1.0).
    pub script: f32,
    /// Neither edge of either term is a function word.
    pub clean_edges: bool,
    /// Weighted blend stamped onto the entry.
    pub confidence: f32,
}

impl TermScore {
    pub fn blend(alignment: f32, script: f32, clean_edges: bool) -> Self {
        let base = W_ALIGNMENT * alignment + W_SCRIPT * script;
        let confidence = if clean_edges { base } else { base * EDGE_PENALTY };
        Self { alignment, script, clean_edges, confidence: confidence.clamp(0.0, 1.0) }
    }
}

/// Alignment quality: the aligned span should hold about as many units as the
/// sentence-level ratio implies. Half a unit of slack is expected from rounding up
/// or down at the span boundaries, but being off by more than one unit means the
/// projection landed somewhere else in the sentence.
fn alignment_score(span_units: usize, expected_units: f32) -> f32 {
    let diff = (span_units as f32 - expected_units).abs();
    if diff <= 0.5 { 1.0 } else if diff <= 1.0 { 0.7 } else { 0.0 }
}

/// Latin-script span: only letters and the joiners English terms actually use.
fn is_latin_span(span: &str) -> bool {
    !span.is_empty() && span.chars().all(|c| c.is_ascii_alphabetic() || c == ' ' || c == '-' || c == '\'')
}

/// CJK span: at least one ideograph and no Latin letters. A Latin-only "Chinese"
/// term means the projection crossed into the wrong side of the sentence.
fn is_cjk_span(span: &str) -> bool {
    span.chars().any(is_cjk) && !span.chars().any(|c| c.is_ascii_alphabetic())
}

/// The ZH_STOP list is entirely single characters; compare without allocating.
fn is_zh_stop_char(c: char) -> bool {
    let mut buf = [0u8; 4];
    let s: &str = c.encode_utf8(&mut buf);
    ZH_STOP.iter().any(|stop| *stop == s)
}

/// A Chinese term never starts or ends with a function word: a possessive particle
/// glued to the front of a noun is a fragment of the sentence, not a term.
fn clean_zh_edges(ngram: &str) -> bool {
    match (ngram.chars().next(), ngram.chars().last()) {
        (Some(first), Some(last)) => !is_zh_stop_char(first) && !is_zh_stop_char(last),
        _ => false,
    }
}

/// Same rule on the word side. Interior function words are fine ("state of the art"
/// is a real term); only the boundaries reveal a misaligned span.
fn clean_en_edges(span: &str) -> bool {
    let words: Vec<&str> = span.split_whitespace().collect();
    match (words.first(), words.last()) {
        (Some(first), Some(last)) => !EN_STOP.contains(first) && !EN_STOP.contains(last),
        _ => false,
    }
}

/// Extract candidate term pairs from a (source_text, target_text) pair.
///
/// Every candidate carries the score from [`TermScore`]; the caller decides what may
/// persist (see `distill::process_pairs` and `config.distill_min_conf`).
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

    // D20: the source must be written in the language it claims. Both branches below
    // cut units out of source_text and pair them with the target, while every other
    // signal only judges the *target* span - so a mislabelled sentence yields fragments
    // ("cle" -> "time", "387" -> "fine") that score up to a clean 1.0 and persist into
    // the glossary, which build_system_prompt then hands to the AI as a term
    // constraint. The predicate lives in lang::has_script_for, shared with the
    // confidence estimator precisely so the two never disagree on what "wrong script"
    // means; unclassified languages return true and are judged by the other signals.
    if !has_script_for(source_text, source_lang) {
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
        let ratio = chars.len() as f32 / tgt_words.len() as f32;   // chars per word

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
                let te_clamped = te.min(tgt_words.len());
                let en = tgt_words[ts..te_clamped].join(" ").to_lowercase();
                if en.is_empty() || EN_STOP.contains(&en.as_str()) { continue; }

                let score = TermScore::blend(
                    alignment_score(te_clamped - ts, n as f32 / ratio),
                    if is_latin_span(&en) { 1.0 } else { 0.0 },
                    clean_en_edges(&en) && clean_zh_edges(&ngram),
                );
                results.push(GlossaryEntry {
                    source_term: ngram,
                    source_lang: source_lang.into(),
                    target_term: en,
                    target_lang: target_lang.into(),
                    confidence: score.confidence,
                    frequency: 1,
                    domain: String::new(),
                    source: "distill".into(),
                });
            }
        }
    } else {
        // English -> Chinese: word N-grams of length 1-3
        let en_words: Vec<&str> = source_text.split_whitespace().collect();
        let zh_chars: Vec<char> = target_text.chars().collect();
        if en_words.is_empty() || zh_chars.is_empty() { return results; }
        let ratio = zh_chars.len() as f32 / en_words.len() as f32;   // chars per word

        for n in [1usize, 2usize, 3usize] {
            if en_words.len() < n { break; }
            for i in 0..=(en_words.len() - n) {
                let en = en_words[i..i + n].join(" ").to_lowercase();
                if EN_STOP.contains(&en.as_str()) || en.len() <= 1 { continue; }
                let ts = ((i as f32) * ratio).floor() as usize;
                let te = ((i + n) as f32 * ratio).ceil() as usize;
                if ts >= zh_chars.len() { continue; }
                let te_clamped = te.min(zh_chars.len());
                let zh: String = zh_chars[ts..te_clamped].iter().collect();
                if zh.is_empty() || ZH_STOP.iter().any(|s| zh == *s) { continue; }

                let score = TermScore::blend(
                    alignment_score(te_clamped - ts, n as f32 * ratio),
                    if is_cjk_span(&zh) { 1.0 } else { 0.0 },
                    clean_zh_edges(&zh) && clean_en_edges(&en),
                );
                results.push(GlossaryEntry {
                    source_term: en,
                    source_lang: source_lang.into(),
                    target_term: zh,
                    target_lang: target_lang.into(),
                    confidence: score.confidence,
                    frequency: 1,
                    domain: String::new(),
                    source: "distill".into(),
                });
            }
        }
    }

    results
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    /// The bar the distill worker persists at, read from the shipped default.
    fn store_bar() -> f32 {
        Config::default().distill_min_conf
    }

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

    #[test]
    fn well_aligned_pair_clears_the_store_bar() {
        let pairs = extract_term_pairs("深度学习很好", "zh", "deep learning is great", "en");
        let good = pairs
            .iter()
            .find(|p| p.source_term == "深度" && p.target_term == "deep learning")
            .expect("the aligned 深度 -> deep learning candidate should exist");
        assert!(
            good.confidence >= store_bar(),
            "a well-aligned pair must clear the bar, got {}",
            good.confidence
        );
        assert_eq!(good.source, "distill", "provenance must be recorded");
    }

    #[test]
    fn misaligned_negative_sample_never_clears_the_store_bar() {
        // The aligner heads every candidate span with the preposition "on", which means
        // the sentence was cut at the wrong place. Before N-08 all of these were stamped
        // 0.90 and persisted; the edge signal now keeps every one of them out.
        let pairs = extract_term_pairs("在桌子上", "zh", "on the table", "en");
        assert!(!pairs.is_empty(), "the negative sample must produce candidates to reject");
        let leaked: Vec<&GlossaryEntry> =
            pairs.iter().filter(|p| p.confidence >= store_bar()).collect();
        assert!(
            leaked.is_empty(),
            "expected nothing to clear the bar, but got {:?}",
            leaked.iter().map(|p| (&p.source_term, &p.target_term, p.confidence)).collect::<Vec<_>>()
        );
    }

    #[test]
    fn scores_are_graded_not_a_constant_stamp() {
        // N-08 regression guard for the defect itself: the extractor used to stamp every
        // candidate with the same 0.90, so the store threshold could reject nothing.
        let pairs = extract_term_pairs("今天天气不错", "zh", "the weather is nice today", "en");
        let distinct: std::collections::BTreeSet<u32> =
            pairs.iter().map(|p| (p.confidence * 1000.0) as u32).collect();
        assert!(distinct.len() > 1, "expected varied scores, got {:?}", distinct);
        assert!(
            pairs.iter().any(|p| p.confidence < store_bar()),
            "at least one candidate must fail the bar"
        );
    }

    #[test]
    fn latin_span_under_a_chinese_target_fails_the_script_gate() {
        // Misdeclared pair (text is English, target claimed as Chinese): every span lands
        // on Latin characters, so the script gate keeps them all out.
        let pairs = extract_term_pairs("hello world", "en", "hello world", "zh");
        assert!(!pairs.is_empty(), "the misdeclared pair must still produce candidates");
        assert!(pairs.iter().all(|p| p.confidence < store_bar()));
    }

    #[test]
    fn a_source_sentence_must_be_written_in_the_language_it_claims_zh() {
        // D20. The zh branch slides characters over whatever it is handed and never
        // asks whether the sentence is Chinese at all, while the script gate only
        // ever judges the *target* span. This is not a hypothetical input:
        // tests/test_phase1_ffi.py writes rows as
        //     source_text="tm-manager-cycle <epoch-ms>"  source_lang="zh"
        // and that mislabelled pair is exactly where the polluted glossary rows in
        // this machine's store came from. Paired with an ordinary English output - what
        // a translate call on that row returns - the target script gate passes, so the
        // only thing standing between these fragments and the glossary is a source-side
        // check, which does not exist today.
        let pairs = extract_term_pairs(
            "tm-manager-cycle 1790689387227", "zh", "the cycle time is fine", "en");
        // Either shape is a valid fix: the guard may refuse the sentence outright, or
        // emit candidates that all fail the bar. What must never happen is a storable
        // one. Red-phase measurement against this exact input returned 37 entries over
        // the bar, e.g. ("cle" -> "time", 1.0) and ("387" -> "fine", 1.0).
        // That the guard does not simply starve real traffic is pinned by
        // the_source_guard_still_admits_a_real_chinese_sentence below.
        let leaked: Vec<&GlossaryEntry> =
            pairs.iter().filter(|p| p.confidence >= store_bar()).collect();
        assert!(
            leaked.is_empty(),
            "a non-Chinese source must store nothing, but these cleared the bar: {:?}",
            leaked.iter().map(|p| (&p.source_term, &p.target_term, p.confidence))
                 .collect::<Vec<_>>()
        );
    }

    #[test]
    fn a_source_sentence_must_be_written_in_the_language_it_claims_en() {
        // The mirror case: an en-labelled source that carries no letters at all. The
        // whitespace split hands back the whole CJK string as one "word", it is not
        // in EN_STOP, the aligned Chinese span passes the target script gate and the
        // edge helpers find no function word - so it scores a clean 1.0 today, the
        // worst shape a candidate can have: maximum confidence, no real term in it.
        let pairs = extract_term_pairs(
            "深度学习很好", "en", "很不错的深度学习", "zh");
        // Same contract as the zh case - red phase this scored ("深度学习很好" ->
        // "很不错的深度学习", 1.0), a maximum-confidence entry with no term in it.
        let leaked: Vec<&GlossaryEntry> =
            pairs.iter().filter(|p| p.confidence >= store_bar()).collect();
        assert!(
            leaked.is_empty(),
            "a non-Latin source must store nothing, but these cleared the bar: {:?}",
            leaked.iter().map(|p| (&p.source_term, &p.target_term, p.confidence))
                 .collect::<Vec<_>>()
        );
    }

    #[test]
    fn the_source_guard_still_admits_a_real_chinese_sentence() {
        // The guard must not become "reject everything": a genuinely Chinese sentence
        // has to keep producing terms that clear the bar, or the distill flywheel
        // silently stops feeding the glossary.
        let pairs = extract_term_pairs("深度学习很好", "zh", "deep learning is great", "en");
        assert!(
            pairs.iter().any(|p| p.confidence >= store_bar()),
            "a real zh->en pair must still clear the bar, got {:?}",
            pairs.iter().map(|p| (&p.source_term, &p.target_term, p.confidence))
                 .collect::<Vec<_>>()
        );
    }

    #[test]
    fn en_to_zh_extracts_scored_pairs() {
        let pairs = extract_term_pairs("deep learning is great", "en", "深度学习很好", "zh");
        assert!(pairs.iter().any(|p| p.source_term == "deep" && p.target_term == "深度"));
        assert!(pairs.iter().all(|p| p.confidence > 0.0 && p.confidence <= 1.0));
    }

    #[test]
    fn edge_function_word_halves_the_blend() {
        let clean = TermScore::blend(1.0, 1.0, true);
        let dirty = TermScore::blend(1.0, 1.0, false);
        assert!((clean.confidence - 1.0).abs() < 1e-6);
        assert!((dirty.confidence - 0.5).abs() < 1e-6);
        assert!(!dirty.clean_edges);
    }

    #[test]
    fn bad_alignment_alone_cannot_clear_the_bar() {
        // Even with the right script and clean edges, an off-by-more-than-one span
        // leaves the score at the script weight (0.45), well below the bar.
        let score = TermScore::blend(0.0, 1.0, true);
        assert!(score.confidence < store_bar(), "got {}", score.confidence);
    }

    #[test]
    fn alignment_score_tolerates_rounding_but_not_drift() {
        assert_eq!(alignment_score(2, 1.6), 1.0);   // within half a unit
        assert_eq!(alignment_score(2, 1.3), 0.7);   // within one unit
        assert_eq!(alignment_score(5, 1.3), 0.0);   // far off -> the projection drifted
    }

    #[test]
    fn script_helpers_reject_the_other_script() {
        assert!(is_latin_span("deep learning"));
        assert!(!is_latin_span("深度学习"));
        assert!(is_cjk_span("深度学习"));
        assert!(!is_cjk_span("deep learning"));
        assert!(!is_cjk_span("深度learning"), "mixed script is not a clean Chinese term");
        assert!(!is_latin_span(""));
    }
}
