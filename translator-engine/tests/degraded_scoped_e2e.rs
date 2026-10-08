// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! What a scoped request is told when nothing but the demo vocabulary answered.
//!
//! The environment is pinned dead on purpose -- both sidecars and the AI endpoint point at
//! a port nothing listens on -- because that is the only way to land on the pipeline's
//! last exit. It is a separate binary for the same reason as the other process-global
//! cases: one live engine anywhere in the process would take the request instead, and the
//! assertions would quietly stop describing anything.
//!
//! What is protected here is the wording of the answer (defect D33). When demo serves, the
//! response must say that *demo* could not act on the term context. Naming the engine that
//! was routed but never ran describes a different engine's capability, and a caller reading
//! "domain 'av' could not be applied" has no way to tell whether that is a fact about the
//! reply it is holding or a leftover from a slot that was skipped.

use std::path::PathBuf;
use std::sync::OnceLock;
use translator_engine::{config, pipeline, tm, types::*};

fn ensure_init() {
    static INIT: OnceLock<PathBuf> = OnceLock::new();
    INIT.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("translator-engine-degraded-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));

        // Every engine that could take this request is unreachable, so the demo fallback
        // is the only thing that can answer. A closed port fails the connect immediately,
        // which keeps the case fast instead of waiting out three timeouts.
        std::env::set_var("WONSLATE_DATA_DIR", &dir);
        std::env::set_var("WONSLATE_OLLAMA_URL", "http://127.0.0.1:1");
        std::env::set_var("WONSLATE_OLLAMA_TIMEOUT_MS", "500");
        std::env::set_var("LT_ARGOS_URL", "http://127.0.0.1:1");
        std::env::set_var("LT_MADLAD_URL", "http://127.0.0.1:1");

        // Same trap as `ai_upgrade_e2e` (defect D36): `LT_*` beats `WONSLATE_*`, so on a
        // host that exports its own AI endpoint the "nothing is reachable" premise above
        // silently stops being true and demo never gets the request -- this suite's whole
        // subject. Pin every spelling, then assert the engine actually reads the dead one.
        let dead = "http://127.0.0.1:1";
        for name in ["LT_OLLAMA_URL", "OLLAMA_URL"] {
            std::env::set_var(name, dead);
        }
        for name in ["LT_DATA_DIR", "DATA_DIR"] {
            std::env::set_var(name, dir.to_str().unwrap_or(""));
        }
        for name in ["LT_OLLAMA_TIMEOUT_MS", "OLLAMA_TIMEOUT_MS"] {
            std::env::set_var(name, "500");
        }

        config::load();
        assert_eq!(config::get().ollama_url, dead,
            "an ambient AI endpoint must not sit in front of the dead pin");
        assert_eq!(config::data_dir(), dir,
            "the routing these cases assume must come from the temp dir");
        tm::init(&dir, 100, 10).expect("TM init failed");
        dir
    });
}

fn request(input: &str, domain: &str) -> TranslateRequest {
    TranslateRequest {
        engine_id: String::new(),
        input: input.into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: false,
        domain: domain.into(),
        use_tm: true,
    }
}

/// The serving engine owns the term-context sentence, even on the last exit.
#[test]
fn the_demo_fallback_names_demo_for_the_term_context_it_dropped() {
    ensure_init();
    let resp = pipeline::translate_full(request("hello", "av"))
        .expect("demo covers en->zh hello, so something must answer");

    assert_eq!(resp.engine, "demo", "the fallback vocabulary is what served");
    assert_eq!(resp.source, TranslationSource::Fallback);
    let message = resp.message.expect("a scoped request must be told the domain did not apply");
    assert!(
        message.contains("engine 'demo' does not accept term context"),
        "the note has to describe the engine that answered, got: {message}"
    );
    assert!(
        !message.contains("engine 'madlad' does not accept term context"),
        "naming the routed-but-never-run engine as the term-blind one is D33: it reads as \
         a property of this reply, and this reply was not madlad's, got: {message}"
    );
    // The madlad failure itself stays visible: it explains why the answer is demo's.
    assert!(
        message.contains("fell back to local demo"),
        "the escalation chain has to be narrated, got: {message}"
    );
}

/// An unscoped request pays nothing for the domain judgement, on this exit included.
#[test]
fn an_unscoped_request_gets_no_term_context_clause() {
    ensure_init();
    let resp = pipeline::translate_full(request("hello", ""))
        .expect("demo answers the fallback vocabulary");

    assert_eq!(resp.engine, "demo");
    match resp.message.as_deref() {
        // The chain note may stay (it explains who ran), the domain judgement may not:
        // there was no domain, so there is nothing to report about one.
        Some(text) => {
            assert!(
                !text.contains("term context") && !text.contains("domain"),
                "an unscoped request must not be told about domains, got: {text}"
            );
        }
        None => {}
    }
}
