// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! The exit where the AI was asked and failed, so a local result is served instead.
//!
//! Why this binary exists. `pipeline::translate_full` has four returns, and until
//! 2026-10-09 one of them had never been executed by any test: the fallback that hands
//! back the local result after an upgrade attempt (defect G-TM-1, registered in the
//! ledger with the line range). What makes it hard to reach is not the code but the
//! environment it needs: the AI slot must be **connectable and unsuccessful**. Every
//! other suite pins it dead, because a dead endpoint short-circuits the attempt
//! upstream and produces a *different* honest answer ("served locally instead").
//! Coverage of a branch nobody can reach is a fiction, so this file owns the missing
//! shape: a listener that accepts connections -- which is all the reachability probe
//! asks -- and then answers each request with something that is not a translation.
//!
//! Two real-world failure shapes are exercised, because they fail in different layers
//! and a test that covers only one would let the other regress silently:
//!
//! * a non-2xx status, caught by the HTTP client (`engine::http::response_body_from_http`);
//! * a 200 whose `choices[0].message.content` is empty, caught by the engine's own parse.
//!
//! What is protected:
//!
//! 1. The response says it is a fallback. `source`, `engine` and `confidence` are the
//!    three fields a caller branches on, and this exit claims none of the AI's
//!    authority: the engine named is the one that actually served (D33's rule), and the
//!    confidence is the fallback's own 0.60 rather than the 0.95 an AI answer gets or
//!    the ~0.84 a normal local answer scores. Reporting a higher number here would make
//!    a failed upgrade look like a succeeded one.
//! 2. The attempt really happened. Asserting the dial count is what separates "AI failed"
//!    from "AI was never asked": both end in a local result, and only one of them is
//!    allowed to cost a request.
//! 3. A failure teaches the memory nothing, and repeats keep trying. If the fallback
//!    result were stored, every later request for that sentence would be served from the
//!    memory and the AI would never be asked again -- a one-off outage would be frozen
//!    into the user's translation memory permanently.
//! 4. The success path is reachable in this same environment. Without that control, the
//!    three assertions above could be satisfied by a fixture that can never succeed, and
//!    "no memory row was written" would be a property of the stub rather than of the code.
//!
//! Both endpoints are owned here and the environment is the shared hermetic `Spec`, so
//! none of this depends on what the developer's machine happens to be running.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex, OnceLock};
use translator_engine::{config, pipeline, tm, types::*};

mod hermetic;

/// The local answer: target-script, so it scores above the storage bar and a missing
/// memory row can only be explained by the code, not by an engine that produced nothing.
const SIDECAR_ANSWER: &str = "今天天气不错，适合出去走走。";
/// Requests whose prompt carries this marker get a 200 with an empty completion.
const EMPTY_CONTENT_MARKER: &str = "answer-with-empty-content";
/// Requests carrying this one get a working completion, for the control case.
const WORKING_MARKER: &str = "answer-properly";
const AI_ANSWER: &str = "AI produced this translation instead";

/// Accept every connection -- so the reachability probe says "up" -- then answer badly.
///
/// The reply depends on the request, which lets one listener serve the status failure,
/// the empty-content failure and the control case without any process-global juggling.
fn spawn_failing_ai() -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind failing ai");
    let port = listener.local_addr().expect("ai addr").port();
    let bodies: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let shared = Arc::clone(&bodies);

    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut reader = BufReader::new(stream.try_clone().expect("clone ai stream"));

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

            let (status, payload) = if body.contains(WORKING_MARKER) {
                ("200 OK", format!(
                    "{{\"choices\":[{{\"message\":{{\"role\":\"assistant\",\"content\":\"{AI_ANSWER}\"}}}}]}}"))
            } else if body.contains(EMPTY_CONTENT_MARKER) {
                ("200 OK", "{\"choices\":[{\"message\":{\"role\":\"assistant\",\"content\":\"\"}}]}".to_string())
            } else {
                // What an ollama that is up but cannot serve looks like: model missing,
                // GPU busy, context overflow. All of them arrive as a non-2xx.
                ("500 Internal Server Error", "{\"error\":\"no such model\"}".to_string())
            };
            let response = format!(
                "HTTP/1.1 {}\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                status,
                payload.len(),
                payload
            );
            let _ = stream.write_all(response.as_bytes());
            let _ = stream.shutdown(std::net::Shutdown::Write);
        }
    });

    (format!("http://127.0.0.1:{port}"), bodies)
}

/// The local slot, answering properly: the fallback under test only exists if the
/// escalation had something to fall back *to*.
fn spawn_sidecar_stub() -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind sidecar");
    let port = listener.local_addr().expect("sidecar addr").port();
    let bodies: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let shared = Arc::clone(&bodies);

    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut reader = BufReader::new(stream.try_clone().expect("clone sidecar stream"));

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

            let payload = format!("{{\"text\":\"{}\"}}", SIDECAR_ANSWER);
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

struct Fixtures {
    ai_bodies: Arc<Mutex<Vec<String>>>,
    sidecar_bodies: Arc<Mutex<Vec<String>>>,
}

/// One guarded boot: the config singleton, the store and both listeners are per process.
fn ensure_init() -> Fixtures {
    static FIX: OnceLock<(Arc<Mutex<Vec<String>>>, Arc<Mutex<Vec<String>>>)> = OnceLock::new();
    let (ai_bodies, sidecar_bodies) = FIX.get_or_init(|| {
        let (ai_url, ai_log) = spawn_failing_ai();
        let (sidecar_url, sidecar_log) = spawn_sidecar_stub();

        let mut dir = std::env::temp_dir();
        dir.push(format!("translator-engine-ai-failure-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));

        // `ai_at` also records the premise "the AI slot answers connections", which is the
        // difference between this suite and every other one: the endpoint must be
        // reachable, or the pipeline never attempts it and therefore never fails.
        let spec = hermetic::Spec::new(&dir)
            .ai_at(&ai_url)
            .sidecar_at(&sidecar_url);
        spec.apply();

        config::load();
        tm::init(&dir, 100, 10).expect("TM init failed");
        spec.verify();

        (ai_log, sidecar_log)
    });
    Fixtures {
        ai_bodies: Arc::clone(ai_bodies),
        sidecar_bodies: Arc::clone(sidecar_bodies),
    }
}

fn request(input: &str) -> TranslateRequest {
    TranslateRequest {
        engine_id: String::new(),
        input: input.into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: false,
        domain: String::new(),
        use_tm: true,
    }
}

/// Requests for this case's own sentence, per listener. Cases share both logs, so an
/// absolute count would measure the other tests too.
fn dials(bodies: &Arc<Mutex<Vec<String>>>, needle: &str) -> usize {
    bodies.lock().unwrap().iter().filter(|b| b.contains(needle)).count()
}

fn stored_row(input: &str) -> Option<TmEntry> {
    tm::list("en", "zh", 1000)
        .expect("list the memory")
        .into_iter()
        .find(|e| e.source_text == input)
}

/// The shared shape of both failure modes: served locally, labelled as a fallback, and
/// honest about what it is.
fn assert_fallback_response(resp: &TranslateResponse, question: &str) {
    assert!(resp.ok, "a local result is still a successful translation: {question}");
    assert_eq!("madlad", resp.engine, "the engine named must be the one that served (D33)");
    assert_eq!(
        TranslationSource::Fallback,
        resp.source,
        "an upgrade that was attempted and failed is not the same answer as one that was skipped"
    );
    assert_eq!(Some(SIDECAR_ANSWER), resp.output.as_deref());
    assert_eq!(
        0.60, resp.confidence,
        "the fallback carries its own confidence; the 0.95 an AI answer gets or the ~0.84 \
         a normal local answer scores would both overstate what happened"
    );
    let message = resp.message.clone().unwrap_or_default();
    assert!(
        message.contains("AI upgrade failed, returning local result"),
        "REQ-B2: the caller has to see that the quality mode did not materialise: {message:?}"
    );
}

#[test]
fn a_failed_upgrade_attempt_is_served_locally_and_says_so() {
    let f = ensure_init();
    let question = "please translate this while the model server is erroring";

    let resp = pipeline::translate_full(request(question)).expect("local slot answers");

    assert_fallback_response(&resp, "500 from the AI");
    assert_eq!(1, dials(&f.ai_bodies, question), "the attempt has to have been made");
    assert_eq!(1, dials(&f.sidecar_bodies, question), "and the local slot produced the text");
}

#[test]
fn a_two_hundred_with_an_empty_completion_is_the_same_failure() {
    let f = ensure_init();
    let question = format!("{EMPTY_CONTENT_MARKER} please translate this sentence");

    let resp = pipeline::translate_full(request(&question)).expect("local slot answers");

    // Same product contract, different layer: this one slips past the HTTP client and is
    // caught by the engine's own parse of choices[0].message.content.
    assert_fallback_response(&resp, "200 with an empty completion");
    assert_eq!(1, dials(&f.ai_bodies, &question), "the request was sent and answered badly");
}

#[test]
fn a_failure_teaches_the_memory_nothing_and_later_requests_try_again() {
    let f = ensure_init();
    let question = "if the AI is down now do not remember my fallback forever";

    let first = pipeline::translate_full(request(question)).expect("first translate");
    assert_eq!(TranslationSource::Fallback, first.source);
    assert!(
        stored_row(question).is_none(),
        "a result nobody chose must not become the user's memory: storing it would make \
         every later request a cache hit and freeze a momentary outage into the TM"
    );

    let second = pipeline::translate_full(request(question)).expect("second translate");
    assert_eq!(
        TranslationSource::Fallback,
        second.source,
        "the repeat must not be served from a memory row written by the failure"
    );
    assert_eq!(
        2,
        dials(&f.ai_bodies, question),
        "the upgrade is retried, which is the only way a recovered model server is used again"
    );
}

#[test]
fn when_the_same_endpoint_answers_the_other_exit_is_taken() {
    let f = ensure_init();
    let question = format!("{WORKING_MARKER} translate this one properly");

    let resp = pipeline::translate_full(request(&question)).expect("the AI answers this time");

    // The control: identical fixtures, identical environment, opposite outcome. Without
    // it the three cases above could pass because the stub can never succeed at all.
    assert!(resp.ok);
    assert_eq!(
        TranslationSource::AiUpgraded,
        resp.source,
        "a working endpoint must escalate, so the fallback cases above are about failure \
         and not about this fixture being permanently unable to serve"
    );
    assert_eq!(AI_ANSWER, resp.output.as_deref().unwrap_or_default());
    assert_eq!(0.95, resp.confidence);
    assert_eq!(1, dials(&f.ai_bodies, &question));
    let row = stored_row(&question).expect("an AI answer is learned");
    assert_eq!(AI_ANSWER, row.target_text);
    assert_eq!("ollama-qwen", row.engine, "the row credits the engine that produced it");
}
