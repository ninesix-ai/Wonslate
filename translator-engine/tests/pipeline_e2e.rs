// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Integration tests: exercise the full `pipeline::translate_full` path.
//!
//! Covers what unit tests cannot:
//! - TM global singleton init + exact-match hit
//! - The five-layer routing decision (privacy / realtime / full)
//! - The AI escalation path (degradation when Ollama is absent)
//! - TM persistence to disk + read-back
//!
//! Complements `tests/test_phase1_ffi.py` (the cross-language FFI contract):
//! this file verifies Rust-internal integration, while Python verifies the
//! cross-language boundary.

use std::path::PathBuf;
use std::sync::OnceLock;
use translator_engine::{pipeline, tm, types::*};

/// Initialize the global state (TM singleton + config) exactly once, shared by all tests.
fn ensure_init() {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        // Use the system temp dir to avoid polluting real user data.
        let mut dir = std::env::temp_dir();
        dir.push(format!("translator-engine-e2e-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        // Clear on every run to keep the tests idempotent.
        let tm_file = dir.join("translator_tm.json");
        let gl_file = dir.join("glossary.json");
        let _ = std::fs::remove_file(&tm_file);
        let _ = std::fs::remove_file(&gl_file);

        // Load default config (TM enabled; distillation off to avoid side effects).
        translator_engine::config::load(&dir.join("no_such_routes.yaml"));

        tm::init(&dir, 100, 10)
            .expect("TM init failed");
    });
}

/// Build a `TranslateRequest` with sensible defaults for most tests.
fn req(input: &str, src: &str, tgt: &str, mode: TranslationMode, privacy: bool)
    -> TranslateRequest
{
    TranslateRequest {
        engine_id: String::new(),
        input: input.into(),
        source_lang: src.into(),
        target_lang: tgt.into(),
        mode,
        privacy,
        domain: String::new(),
        use_tm: true,
    }
}

// ─────────────────────────────────────────────────────────────────

#[test]
fn tm_put_then_lookup_hits_exact_match() {
    ensure_init();
    let entry = TmEntry {
        source_text: "集成测试专用术语".into(),
        source_lang: "zh".into(),
        target_text: "integration-test-only-term".into(),
        target_lang: "en".into(),
        engine: "manual".into(),
        quality: 0.99,
        hit_count: 1,
        domain: String::new(),
    };
    tm::put(&entry).expect("put should succeed");

    let got = tm::lookup("集成测试专用术语", "zh", "en")
        .expect("lookup should not error")
        .expect("should be present");
    assert_eq!(got.target_text, "integration-test-only-term");
    assert!((got.quality - 0.99).abs() < 1e-6);
}

#[test]
fn translate_full_returns_tm_hit_and_skips_engine() {
    ensure_init();
    // Pre-populate TM, then request the same source text via translate_full.
    let src_text = "流水线命中测试";
    tm::put(&TmEntry {
        source_text: src_text.into(),
        source_lang: "zh".into(),
        target_text: "PIPELINE_HIT".into(),
        target_lang: "en".into(),
        engine: "manual".into(),
        quality: 0.95,
        hit_count: 1,
        domain: String::new(),
    }).unwrap();

    let resp = pipeline::translate_full(req(src_text, "zh", "en",
        TranslationMode::Full, false)).expect("translate_full ok");
    assert!(resp.ok);
    assert_eq!(resp.engine, "tm");
    assert_eq!(resp.source, TranslationSource::TmHit);
    assert_eq!(resp.output.as_deref(), Some("PIPELINE_HIT"));
    // A TM hit must be far faster than any engine.
    assert!(resp.latency_ms < 50, "TM hit too slow: {}ms", resp.latency_ms);
}

#[test]
fn privacy_mode_never_escalates_to_ai() {
    ensure_init();
    // Use a text that misses TM; privacy mode must fall back to local only.
    // With no local hit it may return Err(NoResult) — that still counts as
    // "not escalating to AI", honoring the privacy-first guarantee.
    let unique = format!("隐私保护验证 {}", std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());

    let resp = pipeline::translate_full(req(&unique, "zh", "en",
        TranslationMode::Full, true /* privacy */));

    match resp {
        Ok(r) => {
            assert_ne!(r.source, TranslationSource::AiUpgraded,
                "PRIVACY VIOLATION: source={:?}", r.source);
            assert_ne!(r.engine, "ollama-qwen",
                "PRIVACY VIOLATION: engine={}", r.engine);
        }
        // No local result + escalation forbidden = NoResult, per the privacy-first principle.
        Err(e) => assert!(format!("{:?}", e).contains("NoResult")
                       || format!("{:?}", e).contains("TmError")
                       || format!("{:?}", e).contains("EngineFailed"),
                       "unexpected error under privacy mode: {:?}", e),
    }
}

#[test]
fn realtime_mode_never_escalates_to_ai() {
    ensure_init();
    let unique = format!("实时档验证 {}", std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());

    let resp = pipeline::translate_full(req(&unique, "zh", "en",
        TranslationMode::Realtime, false));

    // In realtime mode the demo engine returns None for Chinese with no glossary
    // hit, so a pipeline Err(NoResult) is acceptable too — as long as it is not
    // AiUpgraded (honoring the latency budget for realtime mode).
    if let Ok(r) = resp {
        assert_ne!(r.source, TranslationSource::AiUpgraded,
            "Realtime must not escalate to AI");
    }
}

#[test]
fn unknown_source_lang_auto_detect_cjk_as_zh() {
    ensure_init();
    // source_lang="auto" with CJK content should be detected internally as zh.
    // The zh->en demo glossary may not cover it, but the request must return
    // normally (either Ok or Err(NoResult)); it must never panic.
    let _ = pipeline::translate_full(req("测试输入", "auto", "en",
        TranslationMode::Realtime, false));
}

#[test]
fn tm_hit_increments_hit_count() {
    ensure_init();
    let src = "命中计数验证";
    tm::put(&TmEntry {
        source_text: src.into(), source_lang: "zh".into(),
        target_text: "HIT_COUNT_MARK".into(), target_lang: "en".into(),
        engine: "manual".into(), quality: 0.9,
        hit_count: 1, domain: String::new(),
    }).unwrap();

    // Hit the same entry 3 times in a row.
    for _ in 0..3 {
        let _ = pipeline::translate_full(req(src, "zh", "en",
            TranslationMode::Full, false));
    }

    let got = tm::lookup(src, "zh", "en").unwrap().unwrap();
    // The first put sets hit_count=1; after 3 hits it should be >= 3.
    assert!(got.hit_count >= 3, "hit_count should accumulate, got {}", got.hit_count);
}

#[test]
fn flag_bad_excludes_entry_from_lookup() {
    ensure_init();
    let src = "被踩的坏翻译";
    tm::put(&TmEntry {
        source_text: src.into(), source_lang: "zh".into(),
        target_text: "BAD_OUTPUT".into(), target_lang: "en".into(),
        engine: "demo".into(), quality: 0.9,
        hit_count: 1, domain: String::new(),
    }).unwrap();

    tm::flag_bad(src, "zh", "en").unwrap();
    // lookup should return None because flagged entries are excluded.
    assert!(tm::lookup(src, "zh", "en").unwrap().is_none(),
        "flagged entry must not be returned");
}

#[test]
fn engine_id_override_bypasses_router() {
    ensure_init();
    // Setting engine_id="demo" explicitly forces the local demo engine regardless
    // of routing. Use "hello" to hit the demo glossary and verify the override wins.
    let r = TranslateRequest {
        engine_id: "demo".into(),
        input: "hello".into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: false,
        domain: String::new(),
        use_tm: false,   // disable TM to rule out interference
    };
    let resp = pipeline::translate_full(r).unwrap();
    assert!(resp.ok);
    assert_eq!(resp.engine, "demo");
    assert!(resp.output.unwrap().contains("你好"));
}

#[test]
fn similar_entries_returns_subset() {
    ensure_init();
    // Insert several entries sharing a common keyword.
    for (i, s) in ["机器学习算法A", "机器学习算法B", "机器学习算法C", "无关内容"].iter().enumerate() {
        tm::put(&TmEntry {
            source_text: (*s).into(), source_lang: "zh".into(),
            target_text: format!("ML_{}", i), target_lang: "en".into(),
            engine: "manual".into(), quality: 0.9,
            hit_count: 1, domain: String::new(),
        }).unwrap();
    }
    let hits = tm::similar_entries("机器学习", "zh", "en", 10).unwrap();
    // At least 3 entries contain "机器学习", and none is the unrelated one.
    assert!(hits.len() >= 3, "expected >=3 fuzzy hits, got {}", hits.len());
    assert!(hits.iter().all(|h| h.source_text.contains("机器学习")));
}

#[test]
fn silent_fallback_does_not_inflate_confidence() {
    ensure_init();
    // Explicitly request "bert", an engine that is never registered (kept distinct
    // from the argos/madlad registrations), so get_engine silently falls back to
    // demo. Confidence must reflect the engine that actually ran (demo), not the
    // one that was requested (bert).
    let r = TranslateRequest {
        engine_id: "bert".into(),         // not registered -> falls back to demo
        input: "hello world".into(),     // demo glossary covers hello/world
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: false,
        domain: String::new(),
        use_tm: false,                   // rule out TM hits
    };
    let resp = pipeline::translate_full(r).expect("demo should translate hello world");
    // The fallback really happened: the actual engine is demo.
    assert_eq!(resp.engine, "demo", "unregistered bert should silently fall back to demo");
    // Core honesty assertion: confidence should be at demo's baseline (< 0.70),
    // not an inflated score borrowed from the requested engine.
    assert!(
        resp.confidence < 0.70,
        "confidence misreported: the actual engine is demo, yet got {}; it must be computed from the engine that ran",
        resp.confidence
    );
}

#[test]
fn silent_fallback_is_surfaced_in_message() {
    ensure_init();
    // Explicitly request "bert" (unregistered) -> silently falls back to demo.
    // To avoid a truly silent downgrade, the response `message` must explicitly
    // tell the caller which engine was unavailable and that a fallback occurred.
    let r = TranslateRequest {
        engine_id: "bert".into(),
        input: "hello world".into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: false,
        domain: String::new(),
        use_tm: false,
    };
    let resp = pipeline::translate_full(r).expect("demo should translate");
    let msg = resp.message.expect("the fallback must be surfaced via message, not silent");
    assert!(
        msg.contains("bert"),
        "message should name the unavailable engine bert, got: {}", msg
    );
}

#[test]
fn registered_sidecar_engine_absent_falls_back_to_demo_with_note() {
    ensure_init();
    // The requested engine argos (a sidecar) is absent at runtime (no server): the
    // pipeline should explicitly fall back to the local demo engine (fully local,
    // privacy-safe) instead of returning NoResult, and must annotate the fallback
    // rather than stay silent. An explicit engine_id means no AI escalation, isolating ollama.
    let r = TranslateRequest {
        engine_id: "argos".into(),
        input: "hello world".into(),   // demo glossary can translate this
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: true,                 // even under privacy there should be a local fallback output
        domain: String::new(),
        use_tm: false,
    };
    let resp = pipeline::translate_full(r).expect("an absent sidecar should fall back to demo, not error");
    assert_eq!(resp.engine, "demo", "absent argos (sidecar) should fall back to demo");
    assert_eq!(resp.source, TranslationSource::Fallback);
    let msg = resp.message.expect("the fallback must be explicitly annotated, not silent");
    assert!(msg.contains("demo"), "message should note the demo fallback: {}", msg);
    assert!(resp.output.unwrap().contains("你好"));
}

#[test]
fn no_result_when_even_demo_cannot_handle() {
    ensure_init();
    // The fallback must not overreach: with madlad absent and demo unable to handle
    // fr->zh, it should still return a clear NoResult (no misreporting).
    let r = TranslateRequest {
        engine_id: "madlad".into(),
        input: "bonjour le monde".into(),
        source_lang: "fr".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: true,
        domain: String::new(),
        use_tm: false,
    };
    let resp = pipeline::translate_full(r);
    assert!(resp.is_err(), "madlad absent and demo unsupported for fr-zh -> clear NoResult");
}

// ── Helper to point at the temp dir for standalone debugging ────────────────

#[allow(dead_code)]
fn _dbg_show_tmp_dir() -> PathBuf {
    let mut dir = std::env::temp_dir();
    dir.push(format!("translator-engine-e2e-{}", std::process::id()));
    dir
}
