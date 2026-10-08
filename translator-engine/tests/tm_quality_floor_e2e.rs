// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! D15: the translation memory's *serving* floor, with an upgrade engine that is up.
//!
//! A separate test binary because the floor is read from the process-global config:
//! pinning it here through the environment keeps `pipeline_e2e` (which pins the
//! shipped default) deterministic at the same time, without either file reaching into
//! the other's setup.
//!
//! What is being protected: a locally-written entry scores 0.88 and an AI-written one
//! 0.95, so without a floor the quality a caller receives on a cache hit depends on
//! whether the AI engine happened to be running the first time that sentence was seen.
//!
//! The upgrade engine is a stub listener this binary starts itself, not the developer's
//! Ollama: whether the floor may be enforced at all now depends on that engine being
//! *reachable*, and a test whose outcome changes with the machine it runs on would be
//! asserting the environment rather than the product. The complementary case - the
//! upgrade engine is down, so the floor cannot be honoured - needs the opposite
//! environment and is covered in `tests/test_phase1_ffi.py`, where each case gets its
//! own process and its own environment.

use std::sync::OnceLock;
use translator_engine::{config, pipeline, tm, types::*};

mod hermetic;

const LOCAL_QUALITY: f32 = 0.88;
const AI_QUALITY: f32 = 0.95;

/// Accept every connection and drop it immediately.
///
/// Enough for the reachability probe, which only needs a successful TCP connect, while a
/// real request against it fails at once instead of waiting out a timeout. Returning the
/// port lets the caller point the engine at it.
fn spawn_stub_upgrade_engine() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind stub engine");
    let port = listener.local_addr().expect("stub addr").port();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            drop(stream);
        }
    });
    port
}

fn ensure_init() {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("translator-engine-floor-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));

        // Quality-first, expressed the way a settings file would express it. Read at
        // load time, which is why this test binary owns its own process.
        //
        // Every other spelling of every other knob is pinned or cleared here, and the
        // file's own premise is then asserted: this suite needs an upgrade engine that is
        // *reachable but silent* (the stub below). Pinning only `WONSLATE_OLLAMA_URL` did
        // not deliver that -- a host `LT_OLLAMA_URL` outranks it and the refusal branch
        // stops being reachable at all (defect D36, second sighting: ambient shell 5
        // passed / 1.35 s, hostile shell 1 failed / 0.21 s, i.e. a different code path).
        // The sidecar slots are pinned dead too, because on a development box argos and
        // madlad are usually running and would answer as the "local" engine, which is not
        // the machine CI runs this on.
        let port = spawn_stub_upgrade_engine();
        let url = format!("http://127.0.0.1:{}", port);
        let spec = hermetic::Spec::new(&dir)
            .quality_first()
            .ai_at(&url);
        spec.apply();

        config::load();
        tm::init(&dir, 100, 10).expect("TM init failed");
        spec.verify();
        // The stub never answers a request, so a reached engine must fail fast: the 500 ms
        // the Spec pins beats the shipped 120 s, and the resolved value is what matters.
        assert_eq!(
            config::get().ollama_timeout_ms, 500,
            "a suite whose stub never answers must not wait out the shipped timeout"
        );
        assert_eq!(
            config::get().routing.full.tm_quality_floor, AI_QUALITY,
            "the floor this suite studies must be the one quality_first maps to, \
             not a host override of it"
        );
        assert_eq!(
            config::get().routing.full.upgrade_threshold,
            config::Config::default().routing.full.upgrade_threshold,
            "the upgrade threshold has to stay at the shipped value, or the refusal \
             asserted below is a different decision"
        );
    });
}

fn req(input: &str, mode: TranslationMode) -> TranslateRequest {
    TranslateRequest {
        engine_id: String::new(),
        input: input.into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode,
        privacy: false,
        domain: String::new(),
        use_tm: true,
    }
}

fn seed(input: &str, quality: f32) {
    tm::put(&TmEntry {
        source_text: input.into(),
        source_lang: "en".into(),
        target_text: "缓存的译文".into(),
        target_lang: "zh".into(),
        engine: "argos".into(),
        quality,
        hit_count: 1,
        domain: String::new(),
    })
    .expect("seed TM entry");
}

#[test]
fn quality_first_serves_an_ai_written_entry_from_cache() {
    ensure_init();
    let input = "cache hit at AI quality";
    seed(input, AI_QUALITY);

    let resp = pipeline::translate_full(req(input, TranslationMode::Full)).expect("translate");

    assert_eq!(resp.source, TranslationSource::TmHit, "an AI-quality entry may be reused");
    assert_eq!(resp.confidence, AI_QUALITY);
    assert!(resp.message.is_none(), "a clean hit needs no explanation");
}

#[test]
fn quality_first_refuses_a_locally_written_entry_while_an_upgrade_engine_is_reachable() {
    ensure_init();
    // An input the fallback chain can still produce, so the refusal is observable in a
    // *response*. A sentence nothing can translate would only prove that NO_RESULT wins,
    // which is a different claim.
    let input = "hello world";
    seed(input, LOCAL_QUALITY);

    let resp = pipeline::translate_full(req(input, TranslationMode::Full)).expect("translate");

    assert_ne!(
        resp.source,
        TranslationSource::TmHit,
        "a 0.88 entry must not satisfy a mode whose floor is 0.95 while the AI engine can do better"
    );
    let message = resp.message.unwrap_or_default();
    assert!(
        message.contains("below this mode's") && message.contains("re-translated"),
        "the refusal has to reach the caller, got: {message:?}"
    );
}

#[test]
fn a_locally_written_entry_is_still_served_when_nothing_could_clear_the_floor() {
    ensure_init();
    // Privacy forbids the AI upgrade, so no engine that is allowed to run can clear the
    // 0.95 floor. Refusing here would re-run the local engine that wrote the entry, return
    // the same text, and pay its latency for nothing - the cache would stop paying for
    // itself (docs/17 D16). The unkept promise has to be said out loud instead.
    let input = "cache hit under privacy";
    seed(input, LOCAL_QUALITY);

    let mut request = req(input, TranslationMode::Full);
    request.privacy = true;
    let resp = pipeline::translate_full(request).expect("translate");

    assert_eq!(
        resp.source,
        TranslationSource::TmHit,
        "with no engine allowed to clear the floor, the cached entry is the honest answer"
    );
    assert_eq!(resp.confidence, LOCAL_QUALITY);
    let message = resp.message.unwrap_or_default();
    assert!(
        message.contains("quality preference not honoured"),
        "serving below the floor must be stated, not silent, got: {message:?}"
    );
}

#[test]
fn realtime_still_serves_whatever_is_cached() {
    ensure_init();
    let input = "cache hit in the fast mode";
    seed(input, LOCAL_QUALITY);

    let resp = pipeline::translate_full(req(input, TranslationMode::Realtime)).expect("translate");

    assert_eq!(
        resp.source,
        TranslationSource::TmHit,
        "the fast mode is defined by latency, not by quality: it takes any cache hit"
    );
    assert_eq!(resp.confidence, LOCAL_QUALITY);
    assert!(resp.message.is_none(), "realtime has no floor, so nothing to explain");
}

#[test]
fn the_effective_config_shows_the_floor_in_force() {
    ensure_init();
    let snapshot = config::effective_routing();

    // The relaxation above is a property of one response, never of the configuration:
    // the settings page has to keep showing the floor the user asked for, or it would
    // report a value that no file contains.
    assert_eq!(snapshot["routing"]["full"]["tm_quality_floor"], AI_QUALITY);
    assert_eq!(snapshot["routing"]["realtime"]["upgrade_engine"], "");
}
