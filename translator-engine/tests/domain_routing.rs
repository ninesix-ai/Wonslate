// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! S11 integration tests: domain-scoped glossary and TM routing.
//!
//! These tests author the contract for `domain` (see `docs/tasks/S11-*.md` in the
//! outer task ledger). They were first committed as RED-phase tests, when
//! `router.rs::resolve` never read `req.domain`, `glossary::build_context(_domain)`
//! dropped the field, `tm::glossary_list` had no domain argument, `gl_key` excluded
//! domain and the FFI had no `tt_glossary_import_pack` - so every assertion failed
//! for "feature missing", which was the intended RED signal. S11 landed in `95bff44`
//! and the suite now runs 8/8 GREEN against the real implementation; the assertions
//! themselves are unchanged.
//!
//! Run: `cargo test --test domain_routing` (Release profile is the project default).

use std::sync::OnceLock;
use translator_engine::{config, distill, glossary, pipeline, tm, types::*};

/// Bootstrap the global singletons against a per-run temp dir so the tests
/// neither read nor write real user data. Mirrors `pipeline_e2e.rs::ensure_init`.
fn ensure_init() {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("wonslate-domain-routing-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));
        // Deterministic, offline-only: pin the routes so the AI leg never runs.
        std::env::set_var("WONSLATE_FULL_UPGRADE_POLICY", "low_confidence");
        std::env::set_var("LT_ARGOS_URL", "http://127.0.0.1:1");
        std::env::set_var("LT_MADLAD_URL", "http://127.0.0.1:1");
        config::load();
        tm::init(&dir, 100, 10).expect("TM init failed");
        distill::init();
    });
}

fn entry(term: &str, trans: &str, domain: &str) -> GlossaryEntry {
    GlossaryEntry {
        source_term: term.into(),
        source_lang: "zh".into(),
        target_term: trans.into(),
        target_lang: "en".into(),
        confidence: 1.0,
        frequency: 1,
        domain: domain.into(),
        source: "manual".into(),
    }
}

/// ① `glossary_list(src, tgt, domain, limit)` returns **generic ∪ specific**;
/// a specific row replaces the same-source generic one; other domains stay out.
#[test]
fn glossary_list_returns_generic_union_specific_domain() {
    ensure_init();
    // Two rows share source_term "缓冲" -- the av row must replace the generic
    // one on an av request. A third generic row for a different term ("声道")
    // has no av counterpart and stays visible. A medical row for the same
    // "缓冲" must not leak.
    tm::glossary_upsert(&entry("缓冲", "buffer", "")).unwrap();
    tm::glossary_upsert(&entry("缓冲", "audio buffer", "av")).unwrap();
    tm::glossary_upsert(&entry("声道", "channel", "")).unwrap();
    tm::glossary_upsert(&entry("缓冲", "buffer solution", "medical")).unwrap();

    let av = tm::glossary_list("zh", "en", "av", 10).unwrap();
    // Generic rows for terms without an av counterpart stay visible.
    assert!(av.iter().any(|e| e.source_term == "声道" && e.domain.is_empty()),
        "generic row for a term with no av counterpart must be visible");
    // The av-specific row wins over the same-source generic row.
    assert!(av.iter().any(|e| e.source_term == "缓冲" && e.domain == "av"
        && e.target_term == "audio buffer"),
        "av-scoped row must be visible");
    // Rows from a different specific domain never appear.
    assert!(av.iter().all(|e| e.domain != "medical"),
        "medical-scoped row must NOT leak into av request");
    // Exactly one row per source_term survives (the av-vs-generic collision
    // on the shared term is resolved in favour of the specific row).
    let buf_rows: Vec<&GlossaryEntry> = av.iter()
        .filter(|e| e.source_term == "缓冲").collect();
    assert_eq!(buf_rows.len(), 1,
        "one row per source_term after generic-vs-av dedup; got {:?}",
        buf_rows.iter().map(|e| (&e.target_term, &e.domain)).collect::<Vec<_>>());
}

/// ② Same source term across generic and a specific domain must be storable as
/// two rows: `gl_key` includes domain. Today the second upsert overwrites the
/// first because the key does not carry domain.
#[test]
fn same_source_term_keeps_both_generic_and_specific_domain_rows() {
    ensure_init();
    tm::glossary_upsert(&entry("stream", "flow", "")).unwrap();
    tm::glossary_upsert(&entry("stream", "streaming", "av")).unwrap();

    let generic = tm::glossary_list("zh", "en", "", 10).unwrap();
    let av = tm::glossary_list("zh", "en", "av", 10).unwrap();
    let generic_row = generic.iter()
        .find(|e| e.source_term == "stream" && e.domain == "")
        .expect("generic row must survive the av upsert");
    assert_eq!(generic_row.target_term, "flow");
    let av_row = av.iter()
        .find(|e| e.source_term == "stream" && e.domain == "av")
        .expect("av row must exist as its own key");
    assert_eq!(av_row.target_term, "streaming");
}

/// ③ `translate_full(req{domain:"av"})` must inject the av-scoped term into the
/// prompt and must NOT inject a same-source medical term.
///
/// Verified indirectly through `glossary::build_context` (the seam the AI engine
/// actually reads from), which is the layer where `req.domain` should be consumed.
#[test]
fn build_context_honours_the_request_domain() {
    ensure_init();
    tm::glossary_upsert(&entry("缓冲区", "cache", "medical")).unwrap();
    tm::glossary_upsert(&entry("缓冲区", "buffer", "av")).unwrap();

    let ctx = glossary::build_context("请解释缓冲区", "zh", "en", "av", 10).unwrap();
    let terms: Vec<&str> = ctx.entries.iter().map(|e| e.target_term.as_str()).collect();
    assert!(terms.contains(&"buffer"), "av-scoped term must be present");
    assert!(!terms.contains(&"cache"), "medical-scoped term must NOT be present");
}

/// ④ TM hit must respect domain: a cached entry written under one specific domain
/// must not be served on a request from another specific domain.
#[test]
fn tm_lookup_refuses_cross_domain_hit() {
    ensure_init();
    // Write a TM entry under "medical".
    tm::put(&TmEntry {
        source_text: "缓冲".into(),
        source_lang: "zh".into(),
        target_text: "buffer solution".into(),
        target_lang: "en".into(),
        engine: "ollama".into(),
        quality: 0.95,
        hit_count: 1,
        domain: "medical".into(),
    }).unwrap();

    // NEW SIGNATURE: tm::lookup takes 3 args today. Adding `req.domain` must
    // compile-fail until the parameter is threaded through.
    let hit = tm::lookup_with_domain("缓冲", "zh", "en", "av").unwrap();
    assert!(hit.is_none(),
        "an entry written under medical must not serve an av request");
}

/// ⑤ Distillation must stamp its produced GlossaryEntry with the task's domain,
/// not `String::new()`. Currently `term_extractor.rs` hard-codes empty domain.
#[test]
fn distilled_entry_carries_the_task_domain() {
    ensure_init();
    // Send the pair through the public submit path and drain by flushing the
    // background worker (bounded wait).
    let task = DistillTask {
        source_text: "深度学习很好".into(),
        source_lang: "zh".into(),
        target_text: "deep learning is great".into(),
        target_lang: "en".into(),
        domain: "av".into(),
    };
    distill::submit(task);
    // Give the worker a moment to drain (unbounded mpsc + fast local work).
    std::thread::sleep(std::time::Duration::from_millis(500));

    let av = tm::glossary_list("zh", "en", "av", 100).unwrap();
    assert!(
        av.iter().any(|e| e.source_term == "深度" && e.source == "distill"),
        "at least one distilled entry must be persisted under domain=av; got {:?}",
        av.iter().map(|e| (&e.source_term, &e.domain, &e.source)).collect::<Vec<_>>()
    );
}

/// ⑥ When the request asks for a domain that has zero specific entries, the
/// response `message` must state it -- REQ-B2 "no silent degradation".
#[test]
fn empty_specific_domain_surfaces_a_note() {
    ensure_init();
    let req = TranslateRequest {
        engine_id: "demo".into(),
        input: "hello".into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: true,
        domain: "yet-unknown-domain".into(),
        use_tm: false,
    };
    let resp = pipeline::translate_full(req).expect("demo should still answer");
    let msg = resp.message.clone().unwrap_or_default();
    assert!(
        msg.contains("no specific terms") || msg.contains("yet-unknown-domain"),
        "message must mention the missing domain; got {:?}",
        resp.message
    );
}

/// ⑧ A scoped request the engine cannot honour must say so. The existing note
/// only covers "this domain has no specific rows"; when the glossary *is* populated
/// but the engine cannot act on term context at all, the response used to look
/// exactly like a successfully domain-scoped translation (REQ-B2).
#[test]
fn scoped_request_on_a_term_blind_engine_says_it_was_not_applied() {
    ensure_init();
    // The note has two possible causes and this case must rule the data one out, so the
    // store needs an av row for *this request's own pair* (en->zh). Reusing the zh->en
    // helper here proved nothing while also writing a row that case ⑦ asserts on: the
    // two cases shared one key, and whichever ran last decided whether ⑦ saw
    // source=seed:av or source=manual. "latency" belongs to no other case.
    let av_row = GlossaryEntry {
        source_term: "latency".into(),
        source_lang: "en".into(),
        target_term: "延迟".into(),
        target_lang: "zh".into(),
        confidence: 1.0,
        frequency: 1,
        domain: "av".into(),
        source: "manual".into(),
    };
    tm::glossary_upsert(&av_row).unwrap();

    let req = TranslateRequest {
        engine_id: "demo".into(),
        input: "hello".into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: true,
        domain: "av".into(),
        use_tm: false,
    };
    let resp = pipeline::translate_full(req).expect("demo must still answer");
    let msg = resp.message.clone().unwrap_or_default();
    assert!(msg.contains("does not accept term context"),
        "a term-blind engine must state that the requested domain could not be applied; got {:?}",
        resp.message);
    assert!(msg.contains("av"), "the note must name the domain involved");
}

/// ⑦ After importing the AV seed pack via the FFI `tt_glossary_import_pack`
/// (landed with S11 GREEN), the entries become immediately visible to
/// `glossary_list(domain="av")`. This test drives the Rust-side helper.
#[test]
fn import_pack_materialises_domain_entries() {
    ensure_init();
    // The pack-level source_lang / target_lang is the shipped format: a seed
    // pack is scoped to one language pair, so we do not repeat the pair on
    // every term. `docs/glossary-packs/av-zh-en.json` follows this shape.
    let pack_json = r#"{
        "domain": "av",
        "source_lang": "zh",
        "target_lang": "en",
        "version": "test-1",
        "entries": [
            {"source_term":"语段","target_term":"speech segment","confidence":1.0},
            {"source_term":"音色","target_term":"speaker timbre","confidence":1.0}
        ]
    }"#;

    translator_engine::tm::glossary_import_pack(pack_json).expect("pack must import");

    let av = tm::glossary_list("zh", "en", "av", 10).unwrap();
    assert!(av.iter().any(|e| e.source_term == "语段" && e.target_term == "speech segment"));
    assert!(av.iter().any(|e| e.source_term == "音色" && e.target_term == "speaker timbre"));
    // Every imported row carries the pack's domain and provenance marker.
    assert!(av.iter().all(|e| e.domain == "av" || e.domain.is_empty()));
    assert!(av.iter().filter(|e| e.source_term == "语段" || e.source_term == "音色")
        .all(|e| e.source == "seed:av"),
        "imported rows must carry source=seed:av so a reviewer can spot them");
}


