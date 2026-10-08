// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! The AI upgrade *success* path, end to end over a real socket.
//!
//! Why this binary exists: `pipeline_e2e` has to pin
//! `WONSLATE_FULL_UPGRADE_POLICY=low_confidence`, otherwise its outcome depends on
//! whether an AI engine happens to be running on the machine. That is the right call
//! for tests about the local path, but it leaves full mode's shipped default -- "always
//! ask the AI", defect D14's whole promise -- exercised only against a dead engine. The
//! escalation *decision* was covered; what the pipeline does once the model answers was
//! not (registered as N-14 item 2, "AI upgrade success path").
//!
//! That gap matters more than a coverage number: the AI branch is the only exit that
//! writes a 0.95 memory row, the only one that persists few-shot context, and the one
//! place where D32's input-aware term selection reaches a live prompt. An unverified
//! branch of that kind is where a silent regression would cost the most.
//!
//! The engine is a stub this binary starts on its own ephemeral port -- not the
//! developer's Ollama -- so every assertion is about the product rather than the
//! environment. It speaks just enough of the OpenAI-compatible shape that
//! `engine/http.rs` accepts: a real TCP round trip, a real JSON body, and the request
//! body kept so a case can say what actually went over the wire.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex, OnceLock};
use translator_engine::{confidence, config, pipeline, tm, types::*};

/// Text the stub returns. Deliberately free of every token the cases assert on, so a
/// passing assertion cannot be satisfied by the stub's own answer.
const MODEL_ANSWER: &str = "MODEL-OUTPUT-OK";

/// Answer every request with `MODEL_ANSWER` and keep each request body.
///
/// Returns the base URL plus the shared body log. Accepts connections until the process
/// exits: the listener is intentionally leaked, because a test that stopped serving
/// would turn a dropped connection into a silent fallback to the local engine, and the
/// failure would read like a product bug.
fn spawn_stub_llm() -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub llm");
    let port = listener.local_addr().expect("stub addr").port();
    let bodies: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let shared = Arc::clone(&bodies);

    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut reader = BufReader::new(
                stream.try_clone().expect("clone stub stream"),
            );

            // Headers: read until the blank line, remembering the declared body length.
            // The first line decides whether this was a request at all: the pipeline
            // probes reachability with a bare TCP connect (engine::http::probe), which
            // reaches this socket with no bytes. Logging it as a dial would make "the
            // cache absorbed the repeat" fail for a connection that asked for nothing.
            let mut content_len = 0usize;
            let mut is_request = false;
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 {
                    break;
                }
                let trimmed = line.trim_end();
                if trimmed.is_empty() {
                    break;
                }
                is_request = true;
                if let Some(v) = trimmed
                    .to_lowercase()
                    .strip_prefix("content-length:")
                    .map(str::trim)
                {
                    content_len = v.parse().unwrap_or(0);
                }
            }
            if !is_request {
                continue;               // liveness probe: nothing was asked, nothing logged
            }

            // Body: exactly the declared number of bytes, so a keep-alive connection
            // cannot make this block waiting for a request that never comes.
            let mut body = vec![0u8; content_len];
            if content_len > 0 {
                let _ = reader.read_exact(&mut body);
            }
            let _ = shared
                .lock()
                .map(|mut log| log.push(String::from_utf8_lossy(&body).into_owned()));

            let payload = format!(
                "{{\"choices\":[{{\"message\":{{\"role\":\"assistant\",\"content\":\"{MODEL_ANSWER}\"}}}}]}}"
            );
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

/// Start the stub and pin the process-global configuration exactly once, and hand back
/// the request log. Cases in one binary run on threads that share the store, so the
/// initialisation has to be a single guarded step: `get_or_init` blocks the other
/// threads until the environment, config and TM are ready.
fn ensure_init() -> Arc<Mutex<Vec<String>>> {
    static BODIES: OnceLock<Arc<Mutex<Vec<String>>>> = OnceLock::new();
    Arc::clone(BODIES.get_or_init(|| {
        let (url, log) = spawn_stub_llm();

        let mut dir = std::env::temp_dir();
        dir.push(format!("translator-engine-ai-upgrade-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));

        // Hermetic configuration: point the data dir at the temp directory so neither a
        // deployment routes file nor a developer's settings.json can change the routing
        // these cases assume. Full mode is left at its shipped default -- policy
        // "always", floor 0.0 -- because that default is the thing under test.
        std::env::set_var("WONSLATE_DATA_DIR", &dir);
        std::env::set_var("WONSLATE_OLLAMA_URL", &url);
        // A failure must be quick and visible: if the stub ever stops answering, the case
        // should say "not AiUpgraded" within seconds rather than wait out 120 s.
        std::env::set_var("WONSLATE_OLLAMA_TIMEOUT_MS", "3000");
        // Pin the sidecars to a dead port so the local slot falls back to demo the same
        // way on every machine (same reasoning as `pipeline_e2e`).
        std::env::set_var("LT_ARGOS_URL", "http://127.0.0.1:1");
        std::env::set_var("LT_MADLAD_URL", "http://127.0.0.1:1");

        // The chain above only looks hermetic; it is not. Resolution order is
        // `LT_*` > `WONSLATE_*` > bare, so a host that exports `LT_OLLAMA_URL` -- which is
        // exactly how a developer points the engine at their own model server -- outranks
        // the stub and every assertion below starts describing that machine instead of
        // this one: on such a host the three cases here failed with `got: Fallback`,
        // looking like a product regression in the AI upgrade path (defect D36). So pin all
        // three spellings, and check below that the pin is what the engine really reads.
        for (name, value) in [("LT_OLLAMA_URL", url.as_str()), ("OLLAMA_URL", url.as_str()),
                              ("LT_DATA_DIR", dir.to_str().unwrap_or("")),
                              ("DATA_DIR", dir.to_str().unwrap_or("")),
                              ("LT_OLLAMA_TIMEOUT_MS", "3000"), ("OLLAMA_TIMEOUT_MS", "3000")] {
            std::env::set_var(name, value);
        }

        config::load();
        // The guard is the part that makes this fail loudly here rather than as a
        // confusing red suite somewhere else: if any ambient name still wins, say so now.
        let cfg = config::get();
        assert_eq!(cfg.ollama_url, url,
            "this suite must dial its own stub; an ambient endpoint is in front of it");
        assert_eq!(config::data_dir(), dir,
            "the config must resolve inside the temp dir, or a deployment routes file \
             decides the routing these cases assume");
        tm::init(&dir, 100, 10).expect("TM init failed");
        log
    }))
}

fn request(input: &str, source: &str, target: &str, domain: &str) -> TranslateRequest {
    TranslateRequest {
        engine_id: String::new(),
        input: input.into(),
        source_lang: source.into(),
        target_lang: target.into(),
        mode: TranslationMode::Full,
        privacy: false,
        domain: domain.into(),
        use_tm: true,
    }
}

/// How many recorded prompts carry `needle` -- the caller's own input text. Cases share
/// one process and run on parallel threads, so an absolute count would measure the other
/// tests as well; filtering by the sentence makes a dial count per case.
fn dials_for(bodies: &Arc<Mutex<Vec<String>>>, needle: &str) -> usize {
    bodies.lock().unwrap().iter().filter(|b| b.contains(needle)).count()
}

/// The prompt this case sent, picked out of the shared log by its own input text.
/// Reading the last entry instead would hand back whichever parallel case wrote last.
fn body_for(bodies: &Arc<Mutex<Vec<String>>>, needle: &str) -> Option<String> {
    bodies.lock().unwrap().iter().rev().find(|b| b.contains(needle)).cloned()
}

fn term(source_term: &str, target_term: &str, domain: &str) -> GlossaryEntry {
    GlossaryEntry {
        source_term: source_term.into(),
        source_lang: "zh".into(),
        target_term: target_term.into(),
        target_lang: "en".into(),
        confidence: 0.9,
        frequency: 1,
        domain: domain.into(),
        source: "seed:test".into(),
    }
}

/// The full-mode promise, verified on the side that had never been checked: the model
/// answers, and the pipeline really serves its text, scores it as an AI result and
/// absorbs it into the memory so the next identical request costs no model call.
#[test]
fn ai_upgrade_serves_the_model_output_and_the_cache_absorbs_it() {
    let bodies = ensure_init();
    let input = "a sentence only the AI path will settle";

    let first = pipeline::translate_full(request(input, "en", "zh", ""))
        .expect("translate with a reachable AI engine");
    assert_eq!(
        first.source,
        TranslationSource::AiUpgraded,
        "full mode asks the AI whenever it is reachable (D14's default), got {:?}",
        first.source
    );
    assert_eq!(first.engine, "ollama-qwen", "the serving engine is named, not the local one");
    assert_eq!(first.output.as_deref(), Some(MODEL_ANSWER), "the model's text is what the caller gets");
    assert_eq!(
        first.confidence,
        confidence::AI_UPGRADE_CONFIDENCE,
        "an AI-written result carries the AI confidence, which is what lets a later \
         quality floor accept it"
    );
    assert_eq!(dials_for(&bodies, input), 1, "one prompt per escalation");

    // The write behind the escalation, asserted at the store rather than inferred from
    // a response: quality 0.95 under the AI engine's own name. Rows written at the
    // local engine's score would be refused by a quality floor, and rows attributed to
    // the wrong engine would hide who produced them.
    let stored = tm::lookup(input, "en", "zh").expect("lookup").expect(
        "an AI result has to be persisted, or every repeat pays for the model again",
    );
    assert_eq!(stored.quality, confidence::AI_UPGRADE_CONFIDENCE);
    assert_eq!(stored.engine, "ollama-qwen");
    assert_eq!(stored.target_text, MODEL_ANSWER);

    let second = pipeline::translate_full(request(input, "en", "zh", ""))
        .expect("repeat translate");
    assert_eq!(
        second.source,
        TranslationSource::TmHit,
        "the repeat request must be served from the memory, got {:?}",
        second.source
    );
    assert_eq!(second.confidence, confidence::AI_UPGRADE_CONFIDENCE);
    assert_eq!(
        dials_for(&bodies, input),
        1,
        "the cache absorbed the repeat; the model was dialed again instead"
    );
}

/// D32 end to end: the terms reaching a live prompt are the ones this sentence uses,
/// and the TM rows the AI path folds in as few-shot arrive too.
///
/// D32 was proven at unit level only (pure-function tests). This is the first case that
/// reads the bytes a model would actually see, so it also covers the AI path's own
/// context assembly -- the few-shot branch that unit tests never reach.
#[test]
fn ai_prompt_carries_this_sentences_terms_and_its_few_shot_rows() {
    let bodies = ensure_init();
    let input = "显存占用过高";
    tm::glossary_upsert(&term("显存", "VRAM-TERM-UNIQUE", "av")).expect("upsert scoped term");
    tm::glossary_upsert(&term("声卡", "SOUNDCARD-TERM-UNIQUE", "av")).expect("upsert unused term");
    // A fuzzy row that contains the request as a substring, so it is a few-shot example
    // and not an exact hit (an exact hit would serve from the cache and skip the AI).
    tm::put(&TmEntry {
        source_text: format!("{}导致核心降频", input),
        source_lang: "zh".into(),
        target_text: "FEWSHOT-ROW-UNIQUE".into(),
        target_lang: "en".into(),
        engine: "ollama".into(),
        quality: 0.95,
        hit_count: 1,
        domain: String::new(),
    })
    .expect("seed few-shot row");

    let resp = pipeline::translate_full(request(input, "zh", "en", "av"))
        .expect("scoped translate");
    assert_eq!(resp.source, TranslationSource::AiUpgraded, "the scoped request still escalated");
    // The domain did shape this reply -- the term row proves it -- so nothing may be
    // annotated as unapplied. Before D33 this carried the *local* engine's note.
    assert!(
        resp.message.is_none(),
        "a scoped request whose term reached the prompt must not be annotated as a gap, \
         got: {:?}",
        resp.message
    );

    let body = body_for(&bodies, input).expect("the stub recorded this case's escalation request");
    assert!(
        body.contains("Use these consistent term translations"),
        "a request carrying term context must say so in the prompt, body was: {body}"
    );
    assert!(
        body.contains("VRAM-TERM-UNIQUE"),
        "the term this sentence mentions has to reach the prompt -- that is the point \
         of D32's input-aware selection"
    );
    assert!(
        !body.contains("SOUNDCARD-TERM-UNIQUE"),
        "a term of the same domain that this sentence never mentions must not be \
         injected: that was D32, and it is what made a domain comparison meaningless"
    );
    assert!(
        body.contains("FEWSHOT-ROW-UNIQUE"),
        "the AI path assembles few-shot rows from the memory (level 2 injection); an \
         empty prompt here would mean the escalation silently dropped them"
    );
}

/// The domain note travels with every exit, including the AI one (S11 / REQ-B2).
///
/// The AI branch builds its message through a different helper than the local branch,
/// so "the note is attached" is not a property the local-path tests can hand over.
#[test]
fn ai_path_reports_an_empty_domain_instead_of_passing_it_off() {
    let _bodies = ensure_init();
    let input = "a scoped sentence for an empty domain";

    let resp = pipeline::translate_full(request(input, "en", "zh", "ghostdomain"))
        .expect("translate with an unknown domain");
    assert_eq!(resp.source, TranslationSource::AiUpgraded);
    let message = resp.message.expect(
        "a request that named a domain must be told the domain did not shape the output",
    );
    assert!(
        message.contains("has no specific terms loaded"),
        "an empty domain is a data gap and has to be named as one, got: {message}"
    );
    assert!(
        !message.contains("madlad"),
        "the AI engine served this, so the note may not blame the local engine that \
         never ran: that is D33, and it told the caller the domain could not be applied \
         when it just was, got: {message}"
    );
    assert!(
        !message.contains("does not accept term context"),
        "the AI engine does accept term context, so it must not be blamed for a gap in \
         the data, got: {message}"
    );
}
