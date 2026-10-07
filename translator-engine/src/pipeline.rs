// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Full translation pipeline (L-1 TM -> L0 privacy -> L1 coverage -> L2 local -> L3 AI)
//!
//! Every FFI / REST / CLI entry funnels into translate_full(); one shared logic.

use std::time::Instant;
use crate::config::UpgradePolicy;
use crate::{confidence, config, distill, engine, glossary, router, tm};
use crate::error::EngineError;
use crate::types::*;

/// Attach a refused-cache note to whatever message a path already carries.
///
/// Every exit path has to carry it: re-translating a sentence whose cache entry was
/// refused is a deliberate quality decision, and a caller that cannot see it would not
/// know why the same input suddenly cost a round trip.
fn with_refusal(refused: &Option<String>, message: Option<String>) -> Option<String> {
    match (refused, message) {
        (None, m) => m,
        (Some(r), Some(m)) => Some(format!("{}; {}", r, m)),
        (Some(r), None) => Some(r.clone()),
    }
}

/// Chain a domain-scope note into the same message slot the refusal helper uses.
///
/// Ordering: refusal (cost of a re-translation) reads first, then the domain note
/// (why the glossary was thinner than the request implied), then any path-specific
/// message. All three are optional; empty slots do not produce stray separators.
fn with_domain_note(
    refused: &Option<String>,
    domain: &Option<String>,
    message: Option<String>,
) -> Option<String> {
    with_refusal(refused, with_refusal(domain, message))
}

/// Whether the requested domain shaped the reply, judged against **the engine that is
/// about to serve it**.
///
/// D33: this judgement used to be taken once, from the local engine, and then re-used by
/// the AI exit -- so a full-mode request that the model answered was reported as
/// "engine 'madlad' does not accept term context; domain could not be applied" while the
/// domain's terms were in fact sitting in the prompt that produced the text. A note about
/// an engine that never ran is not a note about this response (REQ-B2), which is why every
/// exit path asks for its own.
fn scope_note_for(
    req: &TranslateRequest,
    plan: &router::RoutePlan,
    engine_name: &str,
    engine_accepts_terms: bool,
    ctx: &glossary::GlossaryContext,
) -> Option<String> {
    if req.domain.is_empty() || !plan.use_glossary || !config::get().glossary_enabled {
        return None;
    }
    // The row census behind the note is only taken for scoped requests, so an unscoped
    // translate pays nothing for it.
    glossary::scoped_note(
        engine_name,
        engine_accepts_terms,
        &req.domain,
        glossary::domain_has_rows(&req.source_lang, &req.target_lang, &req.domain)
            .unwrap_or(false),
        ctx,
    )
}

/// Full-featured translation (TM + routing + distillation; recommended entry).
pub fn translate_full(mut req: TranslateRequest) -> Result<TranslateResponse, EngineError> {
    let t0 = Instant::now();

    // When source_lang == "auto", the MVP heuristic says: any CJK char -> zh, otherwise en
    if req.source_lang == "auto" || req.source_lang.is_empty() {
        req.source_lang = detect_lang(&req.input);
    }

    // ---- Routing decision --------------------------------------------------------
    // Resolved before the TM lookup because the rule decides the threshold a cached
    // entry has to clear to be served. Routing itself is pure and constructs no engines,
    // so moving it up costs nothing.
    // An explicit engine_id acts as an agent passthrough lock: run that engine, never upgrade (respect the choice)
    let mut plan = if !req.engine_id.is_empty() {
        router::RoutePlan {
            local_engine_id: req.engine_id.clone(),
            use_glossary: true,
            upgrade_engine_id: None,
            confidence_threshold: 0.0,
            upgrade_policy: UpgradePolicy::LowConfidence,
            tm_quality_floor: 0.0,
        }
    } else {
        router::resolve(&req)
    };

    // L0 hard invariant, enforced here rather than only in the router: under
    // privacy the engine that actually runs must be local, even when an
    // explicit engine_id bypassed router::resolve. Fail-closed -- any id not
    // known to be local (including future cloud engines) is clamped to demo
    // and the clamp is surfaced in `message`, never silent.
    let mut privacy_clamp: Option<String> = None;
    if req.privacy && !engine::is_local_engine(&plan.local_engine_id) {
        privacy_clamp = Some(format!(
            "privacy mode: non-local engine '{}' clamped to local 'demo'",
            plan.local_engine_id
        ));
        plan.local_engine_id = "demo".into();
        plan.upgrade_engine_id = None;
        plan.confidence_threshold = 0.0;
    }

    // ---- L-1: exact TM hit, subject to the rule's serving floor ----------------
    //
    // The floor is what makes a mode's quality claim hold on cached content too: a
    // locally-written entry scores 0.88 while an AI-written one scores 0.95, so serving
    // everything regardless of quality means the quality a caller gets depends on
    // whether the AI engine happened to be up the first time that sentence was seen.
    //
    // The floor is only worth enforcing while something can clear it. Refusing an entry
    // costs a full re-translation by the *same* local engine that wrote it, so with no
    // upgrade engine configured - or the configured one down - the refusal returns the
    // same text at that engine's whole latency instead of zero, and the memory stops
    // paying for itself (measured offline in full mode: 0 of 3000 repeated requests were
    // served from the cache, docs/17 D16). In that case the entry is served and the
    // unkept promise is stated in `message` rather than degraded in silence.
    //
    // Note this relaxes only what a *cache hit* is allowed to return. The reported
    // configuration is untouched: `effective_routing()` keeps showing the floor the user
    // asked for, and this response says why it was not met.
    let mut refused_cache: Option<String> = None;
    if req.use_tm && config::get().tm_enabled {
        if let Ok(Some(entry)) = tm::lookup_with_domain(
            &req.input, &req.source_lang, &req.target_lang, &req.domain) {
            let below_floor = entry.quality < plan.tm_quality_floor;
            // Probed only where it can change the outcome, and only once a cached entry is
            // in hand: the check opens a connection, and a miss, or an entry that already
            // clears the floor, must not pay for it.
            let upgrade_reachable = below_floor
                && plan.upgrade_engine_id.as_ref().is_some_and(|id| engine::is_reachable(id));

            let mut refuse = false;
            let mut served_note: Option<String> = None;
            if below_floor {
                if upgrade_reachable {
                    refuse = true;
                    refused_cache = Some(format!(
                        "cached result quality {:.2} is below this mode's {:.2} floor; re-translated",
                        entry.quality, plan.tm_quality_floor
                    ));
                } else {
                    // Nothing the engine is allowed to run can clear the floor, so refusing
                    // would pay full price for an identical result.
                    let why = match (plan.upgrade_engine_id.as_deref(), req.privacy) {
                        (Some(id), _) => format!("AI upgrade engine '{}' is not reachable", id),
                        (None, true) => "privacy mode forbids the AI upgrade".to_string(),
                        (None, false) => "no AI upgrade engine is configured for this mode".to_string(),
                    };
                    served_note = Some(format!(
                        "quality preference not honoured: {} to clear this mode's {:.2} quality floor; \
                         served the cached result ({:.2})",
                        why, plan.tm_quality_floor, entry.quality
                    ));
                }
            }

            if !refuse {
                let _ = tm::increment_hit_count(&entry);
                return Ok(TranslateResponse {
                    ok: true,
                    engine: "tm".into(),
                    source_lang: req.source_lang.clone(),
                    target_lang: req.target_lang.clone(),
                    input: req.input.clone(),
                    output: Some(entry.target_text),
                    source: TranslationSource::TmHit,
                    latency_ms: t0.elapsed().as_millis() as u64,
                    confidence: entry.quality,
                    error: None,
                    message: served_note,
                });
            }
        }
    }

    // ---- L2: local engine + glossary ----------------------------------------
    let local_engine = engine::get_engine(&plan.local_engine_id);
    let glossary_ctx = if plan.use_glossary && config::get().glossary_enabled {
        glossary::build_context(
            &req.input, &req.source_lang, &req.target_lang,
            &req.domain, config::get().glossary_max_terms,
        ).unwrap_or_default()
    } else {
        glossary::GlossaryContext::empty()
    };

    // S11 (REQ-B2): a caller that asked for a specific domain must be able to tell, from
    // the response alone, whether that domain actually shaped the output. Three honest
    // outcomes stay distinct: an engine that cannot act on term context is a capability
    // gap, a domain with no rows of its own is a data gap, and a domain whose rows this
    // sentence never mentions is neither of those (defect D32). The judgement lives in
    // `glossary::scoped_note`, a pure function, so all three are reachable without a
    // running engine. This is the local exit's copy; the AI exit takes its own (D33).
    let domain_note: Option<String> = scope_note_for(
        &req, &plan, local_engine.name(), local_engine.accepts_term_context(), &glossary_ctx,
    );

    let local_output = local_engine.translate_with_context(
        &req.input, &req.source_lang, &req.target_lang, &glossary_ctx,
    );

    if let Some(ref output) = local_output {
        let coverage = glossary_ctx.coverage(&req.input);
        // Confidence is computed from the engine that ACTUALLY ran, not the one routing CLAIMED to use.
        // When the claimed local_engine_id is unregistered, get_engine silently falls back to demo;
        // scoring it as the claimed engine (e.g. argos 0.72) would lie, so use local_engine.name().
        let conf = confidence::estimate(output, &req.input, local_engine.name(), coverage, &req.target_lang);

        // Whether to escalate: the rule's policy decides, then the hard preconditions.
        //
        // The reachability check is what keeps "always" honest. Without it, a full-mode
        // user with no AI engine running would pay the engine's whole request timeout on
        // every miss and - because the failed-upgrade branch does not write the memory -
        // would learn nothing from the traffic (measured: 0.00% TM hits, ~1 s wasted per
        // request). A service that is down cannot serve, so it must not be attempted.
        let policy_says_upgrade = match plan.upgrade_policy {
            UpgradePolicy::Always => true,
            UpgradePolicy::LowConfidence => conf < plan.confidence_threshold,
        };
        let upgrade_allowed = !req.privacy && req.mode == TranslationMode::Full;
        let wanted_upgrade = policy_says_upgrade && upgrade_allowed;
        // Probed only when the policy wants the upgrade: the check opens a connection,
        // and the common case (a local result that clears the bar) must not pay for it.
        let upgrade_blocked_by_reachability = wanted_upgrade
            && !plan.upgrade_engine_id.as_ref().is_some_and(|id| engine::is_reachable(id));
        let should_upgrade = wanted_upgrade && !upgrade_blocked_by_reachability;

        // When the claimed engine was never registered (get_engine fell back to demo silently), record why,
        // honoring "no silent degradation": the caller sees in message that X was asked but Y ran.
        // The privacy clamp (L0 hard invariant above) takes priority; both follow the same no-silence rule.
        let fallback_note: Option<String> = privacy_clamp.clone().or_else(|| {
            if !engine::is_available(&plan.local_engine_id) {
                Some(format!(
                    "requested engine '{}' unavailable, fell back to '{}'",
                    plan.local_engine_id, local_engine.name()
                ))
            } else {
                None
            }
        });

        if !should_upgrade {
            let mut notes: Vec<String> = Vec::new();

            if let Some(n) = refused_cache.clone() {
                notes.push(n);
            }
            if req.privacy && conf < 0.70 {
                notes.push("privacy mode: low confidence, AI upgrade prevented".to_string());
            }
            if let Some(n) = fallback_note.clone() {
                notes.push(n);
            }
            // The user asked for the quality mode and did not get it. Saying nothing
            // would present a local result as the intended outcome.
            if upgrade_blocked_by_reachability {
                if let Some(id) = plan.upgrade_engine_id.as_ref() {
                    notes.push(format!(
                        "AI upgrade engine '{}' is not reachable; served locally instead",
                        id
                    ));
                }
            }
            // S11: domain scope travels with every return path so a caller
            // looking at the response alone can tell whether the requested
            // domain actually influenced the glossary context.
            if let Some(n) = domain_note.clone() { notes.push(n); }

            let message = if notes.is_empty() { None } else { Some(notes.join("; ")) };
            let resp = TranslateResponse {
                ok: true,
                engine: local_engine.name().into(),
                source_lang: req.source_lang.clone(),
                target_lang: req.target_lang.clone(),
                input: req.input.clone(),
                output: Some(output.clone()),
                source: if req.privacy { TranslationSource::Fallback } else { TranslationSource::Local },
                latency_ms: t0.elapsed().as_millis() as u64,
                confidence: conf,
                error: None,
                message,
            };
            // write to the TM (only when quality passes the bar)
            let min_q = config::get().tm_min_quality;
            if req.use_tm && conf >= min_q {
                let _ = tm::put(&TmEntry {
                    source_text: req.input.clone(),
                    source_lang: req.source_lang.clone(),
                    target_text: output.clone(),
                    target_lang: req.target_lang.clone(),
                    engine: local_engine.name().into(),
                    quality: conf,
                    hit_count: 1,
                    domain: req.domain.clone(),
                });
            }
            return Ok(resp);
        }
    }

    // ---- L3: escalate to the AI engine ----------------------------------------------
    if let Some(ref ai_id) = plan.upgrade_engine_id {
        let ai_engine = engine::get_engine(ai_id);

        // few-shot injection (level 2)
        let ai_ctx = if req.use_tm {
            let similar = tm::similar_entries(&req.input, &req.source_lang, &req.target_lang, 5)
                .unwrap_or_default();
            glossary_ctx.clone().with_few_shot(similar)
        } else {
            glossary_ctx.clone()
        };

        let ai_output = ai_engine.translate_with_context(
            &req.input, &req.source_lang, &req.target_lang, &ai_ctx,
        );

        if let Some(output) = ai_output {
            // D33: the note is taken from this engine and the context actually sent to
            // it (few-shot rows included), never from the local engine that escalated
            // past. `domain_note` above still describes the local exit.
            let ai_note = scope_note_for(
                &req, &plan, ai_engine.name(), ai_engine.accepts_term_context(), &ai_ctx,
            );
            let resp = TranslateResponse {
                ok: true,
                engine: ai_engine.name().into(),
                source_lang: req.source_lang.clone(),
                target_lang: req.target_lang.clone(),
                input: req.input.clone(),
                output: Some(output.clone()),
                source: TranslationSource::AiUpgraded,
                latency_ms: t0.elapsed().as_millis() as u64,
                confidence: confidence::AI_UPGRADE_CONFIDENCE,
                error: None,
                message: with_domain_note(&refused_cache, &ai_note, None),
            };
            // write to the TM
            if req.use_tm {
                let _ = tm::put(&TmEntry {
                    source_text: req.input.clone(),
                    source_lang: req.source_lang.clone(),
                    target_text: output.clone(),
                    target_lang: req.target_lang.clone(),
                    engine: ai_engine.name().into(),
                    quality: 0.95,
                    hit_count: 1,
                    domain: req.domain.clone(),
                });
            }
            // async distillation (level 1)
            if config::get().distill_enabled {
                distill::submit(DistillTask {
                    source_text: req.input.clone(),
                    source_lang: req.source_lang.clone(),
                    target_text: output.clone(),
                    target_lang: req.target_lang.clone(),
                    domain: req.domain.clone(),
                });
            }
            return Ok(resp);
        }
    }

    // ---- Fallback: return any local result, otherwise report NO_RESULT ------------------
    if let Some(output) = local_output {
        Ok(TranslateResponse {
            ok: true,
            engine: local_engine.name().into(),
            source_lang: req.source_lang,
            target_lang: req.target_lang,
            input: req.input,
            output: Some(output),
            source: TranslationSource::Fallback,
            latency_ms: t0.elapsed().as_millis() as u64,
            confidence: 0.60,
            error: None,
            message: with_domain_note(
                &refused_cache, &domain_note,
                Some("AI upgrade failed, returning local result".into())),
        })
    } else {
        // Final local fallback: when every engine returned nothing (e.g. sidecar absent), try demo explicitly
        // (fully local, never networks, privacy-safe) with an explicit annotation; only if demo also fails is it NO_RESULT.
        let demo = engine::get_engine("demo");
        if let Some(out) =
            demo.translate_with_context(&req.input, &req.source_lang, &req.target_lang, &glossary_ctx)
        {
            let conf = confidence::estimate(&out, &req.input, "demo", 0.0, &req.target_lang);
            // D33, second exit of it: demo is what answered, so demo is what the note has
            // to describe. Re-using the local engine's judgement here blamed 'madlad' for
            // a term context that 'demo' was handed and dropped on the floor.
            let demo_note = scope_note_for(
                &req, &plan, "demo", demo.accepts_term_context(), &glossary_ctx,
            );
            return Ok(TranslateResponse {
                ok: true,
                engine: "demo".into(),
                source_lang: req.source_lang,
                target_lang: req.target_lang,
                input: req.input,
                output: Some(out),
                source: TranslationSource::Fallback,
                latency_ms: t0.elapsed().as_millis() as u64,
                confidence: conf,
                error: None,
                message: with_domain_note(&refused_cache, &demo_note, Some(format!(
                    "engine '{}' produced no result, fell back to local demo",
                    plan.local_engine_id
                ))),
            });
        }
        Err(EngineError::NoResult {
            source_lang: req.source_lang.clone(),
            target_lang: req.target_lang.clone(),
        })
    }
}

/// Backward-compatible implementation of the legacy FFI `tt_translate(engine_id, input, lang_pair)`
pub fn translate_simple(
    engine_id: &str,
    input: &str,
    lang_pair: &str,
) -> Result<TranslateResponse, EngineError> {
    let mut iter = lang_pair.split('-');
    let source = iter.next().unwrap_or("en").trim().to_string();
    let target = iter.next().unwrap_or("zh").trim().to_string();

    let req = TranslateRequest {
        engine_id: engine_id.to_string(),
        input: input.to_string(),
        source_lang: source,
        target_lang: target,
        mode: TranslationMode::Full,
        privacy: false,
        domain: String::new(),
        use_tm: false, // the legacy path never touches the TM; behavior stays consistent
    };
    translate_full(req)
}

/// Simple language detection (MVP heuristic: CJK char -> zh, otherwise -> en)
fn detect_lang(text: &str) -> String {
    if text.chars().any(|c| c >= '\u{4E00}' && c <= '\u{9FFF}') {
        "zh".into()
    } else {
        "en".into()
    }
}

// ---- Unit tests (only pure functions and simple paths that avoid the global singletons)----------------
//
// translate_full runs the whole pipeline (tm/config/engine/distill singletons);
// it is covered in the tests/pipeline_e2e.rs integration layer instead.

#[cfg(test)]
mod tests {
    use super::*;

    // ---- detect_lang ------------------------------------------------

    #[test]
    fn detect_lang_pure_chinese_returns_zh() {
        assert_eq!(detect_lang("你好世界"), "zh");
    }

    #[test]
    fn detect_lang_pure_english_returns_en() {
        assert_eq!(detect_lang("Hello, world!"), "en");
    }

    #[test]
    fn detect_lang_mixed_with_any_cjk_returns_zh() {
        // Mixed CJK/Latin text: a single CJK char tips it to zh (matches the ASR layer default)
        assert_eq!(detect_lang("Hello 世界"), "zh");
    }

    #[test]
    fn detect_lang_empty_returns_en() {
        assert_eq!(detect_lang(""), "en");
    }

    #[test]
    fn detect_lang_punctuation_only_returns_en() {
        assert_eq!(detect_lang("。！，、"), "en");   // Chinese punctuation is not in the CJK Unified Ideographs block
    }

    // ---- translate_simple (demo engine, TM untouched)------------------

    #[test]
    fn simple_en_zh_hello_returns_some() {
        // the demo vocabulary covers basic words like hello
        let r = translate_simple("demo", "hello", "en-zh").unwrap();
        assert!(r.ok);
        assert_eq!(r.engine, "demo");
        assert_eq!(r.source_lang, "en");
        assert_eq!(r.target_lang, "zh");
        let out = r.output.expect("demo hello should translate");
        assert!(out.contains("你好"), "expected 你好, got: {}", out);
    }

    #[test]
    fn simple_unknown_lang_pair_returns_no_result() {
        // demo only supports en<->zh; other pairs return None.
        // privacy=true blocks the AI upgrade so the pipeline bottoms out at Err(NoResult).
        let req = TranslateRequest {
            engine_id: "demo".into(),
            input: "bonjour".into(),
            source_lang: "fr".into(),
            target_lang: "zh".into(),
            mode: TranslationMode::Full,
            privacy: true,
            domain: String::new(),
            use_tm: false,
        };
        let r = translate_full(req);
        assert!(r.is_err(), "privacy + demo 无法翻 fr-zh，应 NoResult，got: {:?}", r.ok());
    }

    #[test]
    fn simple_lang_pair_parsing() {
        // lang_pair parsing check: zh-en must split into source/target correctly
        let r = translate_simple("demo", "你好", "zh-en").unwrap();
        assert_eq!(r.source_lang, "zh");
        assert_eq!(r.target_lang, "en");
    }

    #[test]
    fn simple_empty_input_is_no_result() {
        // demo returns None for an empty string -> pipeline fallback -> Err(NoResult)
        let r = translate_simple("demo", "", "en-zh");
        assert!(r.is_err());
    }

    #[test]
    fn simple_response_has_latency_field() {
        // latency_ms must exist so the UI can show performance
        let r = translate_simple("demo", "hello", "en-zh").unwrap();
        // 0 is allowed (same-CPU-cycle fast), but the field must parse
        let _ = r.latency_ms;
    }
}
