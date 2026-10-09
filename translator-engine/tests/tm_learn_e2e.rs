// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! The flywheel edge: a good local translation must be learned, and the next request
//! must be served from what was learned.
//!
//! Why this binary exists. `pipeline::translate_full` writes the translation memory after
//! a local success (the `req.use_tm && conf >= tm_min_quality` branch), and until 2026-10-09
//! **no test executed those lines**. The line-level coverage report showed the block as
//! never hit, and the reason is instructive: it was hit on a development machine, where a
//! real argos/madlad sidecar answers and scores above the bar, and never on CI, where
//! nothing listens and the local exit produces a demo result below it. Making the suites
//! hermetic (defect D36) removed that borrowed coverage and exposed the gap -- a number that
//! falls when you stop measuring your own machine is information, not regression.
//!
//! What is protected, in order:
//!
//! 1. A local answer that clears the bar is stored with the engine that produced it and the
//!    confidence the caller was quoted. A row disagreeing with the response would make the
//!    UI's TM panel and `/health` counts describe something nobody ever saw.
//! 2. The **next** identical request is served from the memory without asking the engine
//!    again. That is the whole economic claim ("the more you use it, the less it costs"),
//!    and it is a different assertion than "a row exists": a write into a cache nobody
//!    reads, or a row the lookup path cannot find because of hash normalisation, would
//!    satisfy the first and fail this one.
//! 3. `use_tm: false` writes nothing. The opt-out has to be real, or a private/ad-hoc
//!    request would leave residue in the user's memory.
//! 4. An answer below the quality bar writes nothing -- it must not be learned just because
//!    an engine produced it. This is the case that keeps assertion 1 honest: without it,
//!    "the row appeared" could be credited to some other write path.
//!
//! The environment is the shared hermetic `Spec` with both sidecar slots pointed at a stub
//! this file starts, and the AI slot left dead: full mode's shipped policy is "always ask
//! the AI", so with no AI reachable the request must come back as a local result carrying
//! the honest note -- which is exactly the entry condition of the branch under test.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex, OnceLock};
use translator_engine::{config, pipeline, tm, types::*};

mod hermetic;

/// A target-script answer, so the confidence gets its conformance bonus and clears the bar.
const SIDECAR_ANSWER: &str = "今天天气不错，适合出去走走。";
/// Requests carrying this marker get Latin text back: a plausible non-translation for a
/// Chinese target, which the scorer must refuse to learn.
const UNTRANSLATED_MARKER: &str = "leave-untranslated";
const LATIN_NON_ANSWER: &str = "this is clearly not Chinese";

/// Answer every `/translate` request, keep each body, and echo Latin text for a marked
/// request. Same "keep serving forever" reasoning as `ai_upgrade_e2e`: a stub that stops
/// serving turns a local success into a demo fallback, and the failure would read like a
/// product bug rather than a dead fixture.
fn spawn_stub_sidecar() -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub sidecar");
    let port = listener.local_addr().expect("stub address").port();
    let bodies: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let shared = Arc::clone(&bodies);

    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut reader = BufReader::new(stream.try_clone().expect("clone stub stream"));

            let mut content_len = 0usize;
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 {
                    break;
                }
                let trimmed = line.trim_end();
                if trimmed.is_empty() {
                    break;
                }
                if let Some(v) = trimmed
                    .to_lowercase()
                    .strip_prefix("content-length:")
                    .map(str::trim)
                {
                    content_len = v.parse().unwrap_or(0);
                }
            }

            let mut body = vec![0u8; content_len];
            if content_len > 0 {
                let _ = reader.read_exact(&mut body);
            }
            let body = String::from_utf8_lossy(&body).into_owned();
            let _ = shared.lock().map(|mut log| log.push(body.clone()));

            let answer = if body.contains(UNTRANSLATED_MARKER) {
                LATIN_NON_ANSWER
            } else {
                SIDECAR_ANSWER
            };
            let payload = format!("{{\"text\":\"{}\"}}", answer);
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                payload.len(),
                payload
            );
            let _ = stream.write_all(response.as_bytes());
            let _ = stream.shutdown(std::net::Shutdown::Write);
        }
    });

    (format!("http://127.0.0.1:{port}"), bodies)
}

/// Boot the stub, the pinned environment and the store exactly once: the config singleton
/// and the store are process-global, so initialisation has to be one guarded step.
fn ensure_init() -> Arc<Mutex<Vec<String>>> {
    static BODIES: OnceLock<Arc<Mutex<Vec<String>>>> = OnceLock::new();
    Arc::clone(BODIES.get_or_init(|| {
        let (url, log) = spawn_stub_sidecar();

        let mut dir = std::env::temp_dir();
        dir.push(format!("translator-engine-tm-learn-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));

        // Both sidecar slots on the owned stub, the AI slot left dead, and every tuning
        // knob this suite does not name cleared -- otherwise the shipped `tm_min_quality`
        // these cases reason about is whatever the host happens to override it with.
        let spec = hermetic::Spec::new(&dir).sidecar_at(&url);
        spec.apply();

        config::load();
        tm::init(&dir, 100, 10).expect("TM init failed");
        spec.verify();
        log
    }))
}

fn request(input: &str, use_tm: bool) -> TranslateRequest {
    TranslateRequest {
        engine_id: String::new(),
        input: input.into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: false,
        domain: String::new(),
        use_tm,
    }
}

/// How many recorded requests carry this case's own input. Cases share one process and one
/// stub log, so an absolute count would measure the other tests as well.
fn dials_for(bodies: &Arc<Mutex<Vec<String>>>, needle: &str) -> usize {
    bodies.lock().unwrap().iter().filter(|b| b.contains(needle)).count()
}

/// The stored row for `input`, read out of the store's index rather than the LRU cache, so
/// a write that only reached the cache cannot pass as persistence.
fn stored_row(input: &str) -> Option<TmEntry> {
    tm::list("en", "zh", 1000)
        .expect("list the memory")
        .into_iter()
        .find(|e| e.source_text == input)
}

#[test]
fn a_local_answer_above_the_bar_is_written_to_the_memory() {
    let bodies = ensure_init();
    let input = "the weather today is pleasant enough for a walk";
    let before = dials_for(&bodies, input);

    let resp = pipeline::translate_full(request(input, true)).expect("the stub should answer");

    assert_eq!("madlad", resp.engine, "the full-mode local slot is MADLAD");
    assert_eq!(TranslationSource::Local, resp.source);
    assert_eq!(Some(SIDECAR_ANSWER), resp.output.as_deref());
    let message = resp.message.clone().unwrap_or_default();
    assert!(
        message.contains("is not reachable; served locally instead"),
        "the quality mode was not delivered, and REQ-B2 says the response has to say so: {message:?}"
    );

    let bar = config::get().tm_min_quality;
    assert!(
        resp.confidence >= bar,
        "this case is only meaningful above the storage bar: conf {} bar {}",
        resp.confidence,
        bar
    );
    assert_eq!(before + 1, dials_for(&bodies, input), "exactly one engine call");

    let row = stored_row(input).expect("a local answer above the bar must be learned");
    assert_eq!(SIDECAR_ANSWER, row.target_text);
    assert_eq!("madlad", row.engine, "the row must credit the engine that produced it");
    assert_eq!(
        resp.confidence, row.quality,
        "the stored quality must be the confidence that was quoted to the caller"
    );
    assert_eq!(1, row.hit_count);
}

#[test]
fn a_repeat_of_that_sentence_is_served_from_the_memory_without_asking_again() {
    let bodies = ensure_init();
    let input = "remind me to close the window before it starts raining";

    let first = pipeline::translate_full(request(input, true)).expect("first translate");
    let after_first = dials_for(&bodies, input);
    let second = pipeline::translate_full(request(input, true)).expect("second translate");

    assert_eq!(TranslationSource::Local, first.source, "the first request must earn the row");
    assert_eq!(
        TranslationSource::TmHit,
        second.source,
        "the whole point of the memory: the repeat is answered from it"
    );
    assert_eq!(first.output, second.output);
    assert_eq!(
        first.confidence, second.confidence,
        "a cache hit has to quote the quality it actually stored"
    );
    assert_eq!(
        after_first,
        dials_for(&bodies, input),
        "the repeat must not pay for the engine again -- this is the cost claim behind G2"
    );
}

#[test]
fn a_request_that_opted_out_of_the_memory_writes_nothing() {
    let _bodies = ensure_init();
    let input = "this phrasing is only ever translated once in the whole suite";

    let resp = pipeline::translate_full(request(input, false)).expect("stub still answers");
    assert_eq!(
        TranslationSource::Local,
        resp.source,
        "the opt-out changes storage, not routing"
    );
    assert!(
        stored_row(input).is_none(),
        "use_tm=false is an opt-out: nothing may be left behind in the user's memory"
    );
}

#[test]
fn an_answer_below_the_quality_bar_writes_nothing() {
    let _bodies = ensure_init();
    let input = format!("{} and a sentence nobody stored before", UNTRANSLATED_MARKER);

    let resp = pipeline::translate_full(request(&input, true)).expect("stub still answers");

    assert_eq!("madlad", resp.engine, "same engine, so only the score can differ");
    assert_eq!(Some(LATIN_NON_ANSWER), resp.output.as_deref());
    let bar = config::get().tm_min_quality;
    assert!(
        resp.confidence < bar,
        "a Latin answer for a Chinese target must fall below the bar to test anything: conf {} bar {}",
        resp.confidence,
        bar
    );
    assert!(
        stored_row(input.as_str()).is_none(),
        "an output nobody would show the user must not become a memory row"
    );
}
