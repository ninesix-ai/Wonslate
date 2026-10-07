// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Glossary-context builder (pulls term pairs for the active pair and injects them into the Translator prompt)

use crate::error::EngineError;
use crate::tm;
use crate::types::{GlossaryEntry, TmEntry};

/// Glossary context (passed to Translator::translate_with_context)
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

    // Note: there is deliberately no `top_terms(n)` here any more. Truncation belongs to
    // `build_context` (one budget, applied after filtering by the text); a second limit
    // that engines could reach for is what made `glossary_max_terms` a no-op above 20.

    /// Share of glossary entries covered by text (0.0~1.0)
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

    /// Merge few-shot TM entries into the context (level 2: few-shot example injection)
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
                source: "tm".into(),
            });
        }
        self
    }
}

/// Fetch the terms this request actually needs and build its translation context.
///
/// S11: `domain` reaches the store -- a non-empty request domain returns generic
/// rows plus that domain's rows, an empty one returns only generic rows.
/// D32: `text` is now read as well. Both halves of the seam were missing at first:
/// the field was parsed, stored and echoed back but never consumed, and even after
/// S11 wired the domain the injected set stayed a pack prefix that had nothing to
/// do with the sentence being translated.
pub fn build_context(
    text: &str,
    source_lang: &str,
    target_lang: &str,
    domain: &str,
    max_terms: usize,
) -> Result<GlossaryContext, EngineError> {
    // Take the whole candidate set, keep only the rows this request mentions, and
    // close the window last. Truncating first picks survivors out of a list built
    // without ever reading the sentence: measured on the shipped AV pack, a fixed
    // 20-of-266 prefix delivered at least one term of a sentence on just 25 of 300
    // rows (0.09 terms per row, against 2.22 the pack really holds).
    //
    // Matching is substring on the lowercased source term, the same rule
    // `coverage()` already uses, so a term written inside a longer compound still
    // counts as present. A sentence matching nothing gets nothing: an unrelated but
    // plausible list is what this fix exists to remove.
    let pool = tm::glossary_list(source_lang, target_lang, domain, usize::MAX)?;
    let lower = text.to_lowercase();
    // The store already orders rows by confidence descending with a deterministic
    // tie-break, and filtering preserves that order, so the truncation below keeps
    // the strongest matches rather than the most conveniently stored ones.
    let hits: Vec<GlossaryEntry> = pool.into_iter().filter(|e| {
        let term = e.source_term.to_lowercase();
        !term.is_empty() && lower.contains(&term)
    }).collect();
    Ok(GlossaryContext { entries: hits.into_iter().take(max_terms).collect() })
}

/// Upsert a term pair from distillation or manually
pub fn upsert(entry: &GlossaryEntry) -> Result<(), EngineError> {
    tm::glossary_upsert(entry)
}

/// Whether this language pair holds any row of `domain` at all, whatever the current
/// request says. The pipeline needs this to keep two honest states apart: a pack
/// nobody ever imported, versus a pack from which this particular sentence matches
/// nothing. Before D32 they shared one message, so a full domain could be reported
/// as missing (defect D32, rule REQ-B2).
pub fn domain_has_rows(
    source_lang: &str, target_lang: &str, domain: &str,
) -> Result<bool, EngineError> {
    if domain.is_empty() {
        return Ok(false);
    }
    // The whole candidate set, not a window: asking whether a domain has rows must
    // not itself depend on which rows a limit happens to keep.
    Ok(tm::glossary_list(source_lang, target_lang, domain, usize::MAX)?
        .iter().any(|e| e.domain == domain))
}

/// The annotation a scoped request carries, as a pure function so every branch is
/// reachable without a running engine. Three outcomes stay distinct: an engine that
/// cannot use term context at all, a domain holding no rows, and a domain whose rows
/// this sentence never mentions. The capability check comes first because it stays
/// true even after rows are loaded.
pub fn scoped_note(
    engine_name: &str,
    engine_accepts_terms: bool,
    domain: &str,
    pack_has_domain_rows: bool,
    ctx: &GlossaryContext,
) -> Option<String> {
    if !engine_accepts_terms {
        return Some(format!(
            "engine '{}' does not accept term context; domain '{}' could not be applied",
            engine_name, domain))
    }
    if !pack_has_domain_rows {
        return Some(format!(
            "domain '{}' has no specific terms loaded; served generic only", domain))
    }
    // A populated domain whose rows this sentence never mentions is neither of the
    // above: nothing is missing from the pack and the engine can use terms, yet no
    // scoped term was applied here. Calling that a missing pack would be a lie.
    if !ctx.entries.iter().any(|e| e.domain == domain) {
        return Some(format!(
            "domain '{}' holds terms, but none of them occurs in this text; no scoped \
             term applied", domain))
    }
    None
}

/// User-removed term pair
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
            source: "distill".into(),
        }
    }

    #[test]
    fn empty_context_defaults() {
        let ctx = GlossaryContext::empty();
        assert!(ctx.is_empty());
        assert!(ctx.entries.is_empty());
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
        // The text contains the first 2 terms, so coverage should be 2/4 = 0.5
        let c = ctx.coverage("人工智能和机器学习很热门");
        assert!((c - 0.5).abs() < 1e-6);
    }

    #[test]
    fn coverage_is_case_insensitive_on_source_term() {
        let ctx = GlossaryContext { entries: vec![entry("hello world", "x", 0.9)] };
        assert!(ctx.coverage("HELLO WORLD is great") > 0.9);
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
        // the merged entry is mapped from a TmEntry
        let new = &merged.entries[1];
        assert_eq!(new.source_term, "人工智能");
        assert_eq!(new.target_term, "Artificial Intelligence");
        assert_eq!(new.frequency, 42);       // hit_count → frequency
        assert!((new.confidence - 0.95).abs() < 1e-6);  // quality → confidence
        assert_eq!(new.source, "tm");        // provenance: these came from the TM, not distillation
    }

    #[test]
    fn coverage_clamped_to_one() {
        let ctx = GlossaryContext { entries: vec![entry("x", "y", 0.9)] };
        // repeated hits on one term never exceed 1.0
        let c = ctx.coverage("x x x x x");
        assert!(c <= 1.0 && c > 0.0);
    }
}
