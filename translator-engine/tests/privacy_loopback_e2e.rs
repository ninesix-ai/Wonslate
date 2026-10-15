// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Privacy invariant: under `privacy=true` the engine that runs must not dial an
//! address outside this machine, whatever that engine's id is.
//!
//! Why this suite exists. The clamp in `pipeline.rs` decides "local" from the engine
//! *id* (`engine::is_local_engine`, a whitelist of `demo` / `argos` / `madlad`), and the
//! comment next to it justifies excluding ollama on the grounds that "its URL is
//! env-overridable". The same is true of the two whitelisted sidecars: `ARGOS_URL` and
//! `MADLAD_URL` are resolved through the identical prefix chain, so the criterion in the
//! comment and the criterion in the code disagree. Worse, the shipped routing table puts
//! `madlad` in the full-mode local slot and the router's privacy branch returns exactly
//! `routing.full.local_engine`, so no explicit bypass is needed -- a default privacy
//! request dials whatever address the environment names.
//!
//! The premise is measured over the wire, not by reading the whitelist: the listener
//! binds a NON-loopback address of this host and reports what it received. Text arriving
//! there is the defect; zero connections is the invariant.
//!
//! Host requirement: the machine must own a non-loopback IPv4. That is asserted out loud
//! rather than skipped, because a silently-skipped privacy proof is how the guarantee
//! comes to look covered while nothing was measured.

mod hermetic;

use std::io::{Read, Write};
use std::net::{IpAddr, TcpListener, UdpSocket};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use translator_engine::{pipeline, types::*};

/// Bootstrap the singletons against a per-run temp dir: nothing here may touch real
/// user data, and every knob the suite does not name is cleared (`tests/hermetic`).
fn data_dir() -> PathBuf {
    static DIR: OnceLock<PathBuf> = OnceLock::new();
    DIR.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("privacy-loopback-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        dir
    })
    .clone()
}

/// A non-loopback IPv4 this host owns, picked the way any client would pick it: ask the
/// routing table which source address a default-route packet would use. No packet is
/// sent -- 203.0.113.0/24 is TEST-NET-1 (RFC 5737) and unreachable by construction.
fn non_loopback_ipv4() -> IpAddr {
    let sock = UdpSocket::bind("0.0.0.0:0").expect("udp bind for route probe");
    sock.connect("203.0.113.1:53")
        .expect("route probe needs an interface with a default route");
    let ip = sock.local_addr().expect("local_addr").ip();
    match ip {
        IpAddr::V4(v4) if !v4.is_loopback() => IpAddr::V4(v4),
        other => panic!(
            "cannot prove the privacy invariant on this host: the default-route address \
             is {other}, which is not a non-loopback IPv4. A skipped proof is not a pass, \
             so this test fails instead of silently passing."
        ),
    }
}

/// Both cases below write the same process-wide environment with opposite contents, so
/// they have to run one at a time. Cargo parallelises tests inside one binary by default;
/// without this lock the second case's loopback pin can land while the first is still
/// dialing, and the "nothing was dialed" assertion then passes for the wrong reason -- a
/// green that means nothing. (Same treatment as `tests/store_dirty.rs`, which has one
/// counter for the same reason.)
static ENV: Mutex<()> = Mutex::new(());

/// Keeps the lock usable if another case panicked while holding it: the environment is
/// rewritten by every Spec::apply anyway, so a panic in an unrelated test must not turn
/// this invariant's own proof into a cascade of lock errors.
fn env_guard() -> std::sync::MutexGuard<'static, ()> {
    ENV.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// One listener per suite, answering like a sidecar would and keeping every body it saw.
struct Interceptor {
    url: String,
    hits: Arc<AtomicUsize>,
    bodies: Arc<Mutex<Vec<String>>>,
}

impl Interceptor {
    fn spawn(addr: IpAddr) -> Self {
        let listener = TcpListener::bind((addr, 0))
            .unwrap_or_else(|e| panic!("cannot listen on {addr}: {e}"));
        let port = listener.local_addr().unwrap().port();
        let hits = Arc::new(AtomicUsize::new(0));
        let bodies = Arc::new(Mutex::new(Vec::new()));
        let (h, b) = (hits.clone(), bodies.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { continue };
                h.fetch_add(1, Ordering::SeqCst);
                // Read the headers, then exactly Content-Length bytes: a body can be split
                // across reads, and a truncated capture would understate the leak.
                let mut buf = Vec::new();
                let mut chunk = [0u8; 1024];
                loop {
                    let Ok(n) = s.read(&mut chunk) else { break };
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    let text = String::from_utf8_lossy(&buf).to_string();
                    let Some(head) = text.find("\r\n\r\n") else { continue };
                    let want = text[..head]
                        .to_lowercase()
                        .lines()
                        .find_map(|l| {
                            l.strip_prefix("content-length:")
                                .and_then(|v| v.trim().parse::<usize>().ok())
                        })
                        .unwrap_or(0);
                    if buf.len() >= head + 4 + want {
                        b.lock().unwrap().push(text[head + 4..].to_string());
                        break;
                    }
                }
                let body = "{\"text\":\"intercepted\"}";
                let reply = format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = s.write_all(reply.as_bytes());
                let _ = s.flush();
            }
        });
        Self {
            url: format!("http://{}:{}", addr, port),
            hits,
            bodies,
        }
    }

    fn saw_text(&self, needle: &str) -> bool {
        self.bodies
            .lock()
            .unwrap()
            .iter()
            .any(|b| b.contains(needle))
    }
}

/// The invariant, in its two reachable forms. Both are the product's own entry point --
/// no internal helper is called directly, so a fix cannot satisfy the test by moving the
/// check somewhere the pipeline never goes.
#[test]
fn privacy_mode_never_sends_source_text_to_a_non_loopback_address() {
    let _guard = env_guard();
    let interceptor = Interceptor::spawn(non_loopback_ipv4());

    // One process-wide environment, applied once: both cases below dial the same address.
    // `sidecar_at` writes every spelling of ARGOS_URL/MADLAD_URL, which is exactly the
    // legitimate deployment shape being probed (the sidecar runs in a container or under
    // WSL, so it is reachable on the host's LAN address rather than on 127.0.0.1).
    {
        let spec = hermetic::Spec::new(&data_dir())
            .low_confidence_upgrade()
            .sidecar_at(&interceptor.url);
        spec.apply();
    }

    // Premise check, on the product's own resolution rather than on this file's string:
    // if a host export or a racing case outranked the pin (D36's shape), the dial counts
    // below would be measuring some other service and "zero connections" would mean
    // nothing. Say so before the invariant assertion does.
    let pinned_engine = translator_engine::engine::sidecar::SidecarTranslator::madlad();
    let resolved = pinned_engine.endpoint();
    assert_eq!(
        resolved, interceptor.url,
        "the suite's own sidecar pin did not win: the engine resolves to {resolved:?}, \
         not {}; nothing below proves anything",
        interceptor.url
    );

    // Case 1: the default path. No engine_id, full mode -- the router's privacy branch
    // picks `routing.full.local_engine`, which ships as "madlad". The input is one the
    // dictionary engine covers, so the clamped request still answers: that is what lets
    // the assertions below check the *explanation*, not just the absence of a dial.
    let routed = pipeline::translate_full(TranslateRequest {
        engine_id: String::new(),
        input: "hello world".into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: true,
        domain: String::new(),
        use_tm: false,
    });

    // Case 2: the explicit bypass an agent would use. "argos" is whitelisted as local,
    // so the clamp lets it run against whatever address the environment named.
    let pinned = pipeline::translate_full(TranslateRequest {
        engine_id: "argos".into(),
        input: "please expedite the customs paperwork".into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: true,
        domain: String::new(),
        use_tm: false,
    });

    let leaked_routed = interceptor.saw_text("hello world");
    let leaked_pinned = interceptor.saw_text("customs paperwork");
    let dialed = interceptor.hits.load(Ordering::SeqCst);

    // Report what actually happened before asserting, so a red run is evidence and not a
    // bare panic. Both responses are printed too: under a correct clamp the message should
    // name the clamp instead of pretending the requested engine answered.
    // `as_ref().ok()` keeps the error type out of this file -- the suite is about where
    // text went, not about naming what refused.
    let describe = |r: &Result<TranslateResponse, _>| match r.as_ref().ok() {
        Some(v) => format!("engine={} source={:?} message={:?}", v.engine, v.source, v.message),
        None => "Err(no response)".to_string(),
    };
    println!(
        "dialed={} leaked_routed={} leaked_pinned={}\n  routed: {}\n  pinned: {}",
        dialed,
        leaked_routed,
        leaked_pinned,
        describe(&routed),
        describe(&pinned)
    );

    assert!(
        !leaked_routed,
        "PRIVACY VIOLATION: a privacy=true request with no engine_id sent its source text \
         to {} (a non-loopback address). The clamp decides \"local\" from the engine id, \
         but the id's URL is env-overridable -- see MADLAD_URL/ARGOS_URL.",
        interceptor.url
    );
    assert!(
        !leaked_pinned,
        "PRIVACY VIOLATION: privacy=true with an explicit engine_id=\"argos\" sent its \
         source text to {} (a non-loopback address).",
        interceptor.url
    );
    assert_eq!(
        dialed, 0,
        "PRIVACY VIOLATION: {} connection(s) reached a non-loopback address under \
         privacy mode; the invariant is about the address, not the engine id.",
        dialed
    );

    // A refusal that does not explain itself is the other half of this bug: an operator
    // who set WONSLATE_MADLAD_URL and watched the engine vanish would read a silent
    // downgrade to the dictionary engine as a quality regression (REQ-B2). So the message
    // must name the axis that refused and carry the address it refused.
    let msg = routed
        .as_ref()
        .ok()
        .and_then(|v| v.message.clone())
        .unwrap_or_else(|| panic!("the clamped request should still answer from demo, got {:?}", routed));
    assert!(
        msg.contains("loopback") && msg.contains(&interceptor.url),
        "the clamp did not say which address it refused and why: {}",
        msg
    );
    assert_eq!(
        routed.as_ref().map(|v| v.engine.clone()).unwrap_or_default(),
        "demo",
        "an off-device address must fall back to the in-process engine"
    );
}

/// The other half of the fix. Refusing off-device addresses is only correct if the
/// shipped deployment -- a sidecar on 127.0.0.1, which is what the docs tell users to run
/// -- still gets its real engine. A clamp that fires on loopback too would quietly reduce
/// privacy mode to the dictionary engine, and the loss would look like a quality problem
/// rather than a broken guard.
#[test]
fn a_loopback_sidecar_still_answers_under_privacy_mode() {
    let _guard = env_guard();
    // Target-script answer, so the confidence gate has something to score and the request
    // cannot pass on a fallback that never reached the engine at all.
    const LOCAL_ANSWER: &str = "合同交付日期延误了";
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut s) = stream else { continue };
            let mut buf = Vec::new();
            let mut chunk = [0u8; 1024];
            loop {
                let Ok(n) = s.read(&mut chunk) else { break };
                if n == 0 {
                    break;
                }
                buf.extend_from_slice(&chunk[..n]);
                let text = String::from_utf8_lossy(&buf).to_string();
                let Some(head) = text.find("\r\n\r\n") else { continue };
                let want = text[..head].to_lowercase().lines()
                    .find_map(|l| l.strip_prefix("content-length:")
                        .and_then(|v| v.trim().parse::<usize>().ok()))
                    .unwrap_or(0);
                if buf.len() >= head + 4 + want {
                    let _ = tx.send(text[head + 4..].to_string());
                    break;
                }
            }
            let body = format!("{{\"text\":\"{LOCAL_ANSWER}\"}}");
            let _ = s.write_all(format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{}", body.len(), body).as_bytes());
            let _ = s.flush();
        }
    });

    let spec = hermetic::Spec::new(&data_dir())
        .low_confidence_upgrade()
        .sidecar_at(&format!("http://127.0.0.1:{}", port));
    spec.apply();

    let want = format!("http://127.0.0.1:{}", port);
    let pinned_engine = translator_engine::engine::sidecar::SidecarTranslator::madlad();
    let resolved = pinned_engine.endpoint();
    assert_eq!(
        resolved, want,
        "the suite's own loopback pin did not win: the engine resolves to {resolved:?}, \
         not {want}"
    );

    let resp = pipeline::translate_full(TranslateRequest {
        engine_id: String::new(),
        input: "the contracted delivery date slipped".into(),
        source_lang: "en".into(),
        target_lang: "zh".into(),
        mode: TranslationMode::Full,
        privacy: true,
        domain: String::new(),
        use_tm: false,
    })
    .expect("the loopback sidecar answers");

    let dialed_body = rx.recv_timeout(std::time::Duration::from_secs(5))
        .expect("privacy mode must still use a sidecar that is on this machine");
    assert!(
        dialed_body.contains("contracted delivery date"),
        "the request should have carried the source text to the loopback service, got: {dialed_body}"
    );
    assert_eq!(resp.engine, "madlad", "the full-mode local slot must still serve");
    assert!(
        !resp.message.as_deref().unwrap_or("").contains("clamped"),
        "a loopback engine must not be clamped, message was {:?}",
        resp.message
    );
}
