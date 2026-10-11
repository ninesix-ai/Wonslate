// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Sidecar engines (argos / madlad) -- thin local HTTP services
//! (CTranslate2 / Argos) called over 127.0.0.1.
//!
//! Design: reuse the pure-std HTTP client in engine/http.rs. The Rust side adds
//! zero new dependencies and no build scripts, so Smart App Control is not
//! triggered; an absent service / timeout / parse failure always returns None
//! and the router falls back to demo (and pipeline exposes the fallback via
//! is_available / the actual engine name -- never a silent misreport).
//!
//! One non-200 is not a fact about the request but about this instant: `429 busy`, which the
//! service only sends when started with `--decode-slots` and only sends after stating that
//! nothing was started for the caller. That one is waited out inside a capped budget (see
//! `request`); every other non-200 still means "no answer", unchanged.

use super::{http, Translator};
use crate::glossary::GlossaryContext;
use std::time::{Duration, Instant};

/// Default service URL for the realtime slot (Argos); override via
/// LT_ARGOS_URL (legacy) or WONSLATE_ARGOS_URL (brand spelling).
const DEFAULT_ARGOS_URL: &str = "http://127.0.0.1:11435";
/// Default service URL for the rare-language slot (MADLAD); same prefix chain.
const DEFAULT_MADLAD_URL: &str = "http://127.0.0.1:11436";
/// Default timeout in milliseconds; the sidecar runs locally, shorter than ollama.
///
/// This is a *per-attempt* socket read timeout, and it is the only thing that bounds a call the
/// service has put in its queue. A `--queue-wait-ms` hold is not a `429`: the handler writes
/// nothing while it waits, so the `busy_retry` budget below never engages and cannot rescue the
/// caller either. Therefore, when this client talks to a service started with a queue, the
/// inequality that keeps the translation is `timeout >= queue_wait + the caller's own decode`;
/// if it does not hold, this call times out and the pipeline falls back to demo while the service
/// goes on decoding for a socket that has already closed (the abandoned-work shape of external
/// item 004, now countable via `/health`'s `decodes_queued`). But this is only the RUST-side
/// segment: a desktop `/translate` also has an OUTER .NET UI logical timeout
/// (`MainViewModel.TranslateTimeout`) that wraps this whole FFI call and fires BEFORE the 30 s
/// here. It shipped at 20 s with a retry that re-issued a full decode while the abandoned one
/// still ran (the D30 item-2 overlap); the owner raised the default to 45 s -- above this client's
/// own worst case of `busy_retry + timeout` -- made it env-overridable via
/// `*_UI_TRANSLATE_TIMEOUT_MS`, and turned the auto-retry default off. So covering W on the Rust
/// side is necessary but not sufficient on its own; the desktop's outer layer has to cover it too
/// (it can now, and its default already exceeds this Rust segment's worst case). A multi-line
/// paste can still be slow because the sidecar decodes lines sequentially on one slot. See
/// `docs/hardware-requirements.md` for the four-layer chain. Proven both ways by the two
/// `a_server_hold_*_the_timeout_*` cases, each killed by breaking the timeout in the opposite
/// direction.
const DEFAULT_TIMEOUT_MS: u64 = 30_000;
/// How long a caller may wait behind a `429 busy` refusal before it falls back, in
/// milliseconds.
///
/// Not invented, and not a duration either: `Retry-After` is the ceiling of the service's own
/// measured p50 (`docs/evidence/gate-decode-slots-madlad-calls.json`: 5-6 s on the AV corpus,
/// where every group that refused finished with `unserved: 0`, i.e. one retry was enough), so
/// twelve seconds buys **two whole steps** at that p50 -- a caller refused twice still gets its
/// answer. If the step grows to 7 s it buys one, which is the difference between covering a
/// decode of ~6 s and covering one of ~14 s; see the grid in `docs/evidence/
/// busy-budget-sweep-madlad.json`.
///
/// It costs nothing when it is not needed: a `429` can only come from a service started with
/// `--decode-slots`, which is off by default, so every existing deployment never waits.
///
/// Where it stops being enough, measured: `Retry-After` is the ceiling of the service's own
/// p50, so the budget is spent in *whole steps of that number* and never in part. One run's
/// grid is in `docs/evidence/busy-budget-sweep-madlad.json` (reproduce with
/// `python tests/test_sidecar_busy_e2e.py --sweep`): with the step at 7 s, 8000 and 12000 ms
/// each afforded one step and recovered nothing above ~6.2 s of occupancy, 20000 ms afforded
/// two and recovered up to ~9.0 s, and 30000 ms reached ~26.7 s. Three consequences worth knowing
/// before changing the number: a non-zero budget *below one step* waits zero seconds (it is
/// equivalent to `0`, not to "a little patience" -- measured at 4000 ms); the step describes the
/// decodes that have **finished**, not the one you are blocked behind (behind a 25.18 s decode the
/// service promised 2 s, because the window then held a 1.84 s sample while p99 was 25.18 s), so a
/// budget sized by the step under-buys any mixed workload; and page-scale work is cheaper on
/// `--queue-wait-ms`, which is woken by the release instead of re-asking -- the caller in
/// `docs/evidence/queue-wait-madlad.json` (`python tests/test_sidecar_busy_e2e.py --queue`) came
/// back 0.06 s *under* the sum of the two decodes it waited through, i.e. it paid no rounding at
/// all. A caller who does run out gets the demo answer plus
/// `"engine 'madlad' produced no result, fell back to local demo"`, never a silent wrong answer.
const DEFAULT_BUSY_RETRY_MS: u64 = 12_000;

/// The prefix chain each slot reads, in precedence order. Named once and used by the
/// constructors themselves, so a test cannot pass on a list the product does not read.
/// `pub` for the same reason: `tests/env_pin_coverage.rs` asserts what the hermetic table
/// pins against what these engines actually resolve, and a copied list would re-create D41.
pub const ARGOS_CANDIDATES: &[&str] = &["LT_ARGOS_URL", "WONSLATE_ARGOS_URL"];
pub const MADLAD_CANDIDATES: &[&str] = &["LT_MADLAD_URL", "WONSLATE_MADLAD_URL"];
/// The busy-wait budget's own keys, same legacy-first chain as every other knob.
pub const RETRY_CANDIDATES: &[&str] = &["LT_SIDECAR_RETRY_MS", "WONSLATE_SIDECAR_RETRY_MS"];
/// Per-request timeout keys, named for the same reason as the URL chains -- the inline literal
/// this replaces was read by the constructor and by nothing else, so a suite could not prove
/// it had pinned the value the engine would use.
pub const TIMEOUT_CANDIDATES: &[&str] = &["LT_SIDECAR_TIMEOUT_MS", "WONSLATE_SIDECAR_TIMEOUT_MS"];
/// Translation endpoint path.
const TRANSLATE_PATH: &str = "/translate";

/// A translation engine backed by a local HTTP sidecar (argos / madlad share it).
pub struct SidecarTranslator {
    id: &'static str,
    url: String,
    timeout: Duration,
    /// Ceiling on waiting behind a busy refusal. Bounds the sleeping only: a request that
    /// finally gets a slot still gets the full per-request `timeout`, so the worst case is
    /// `busy_retry + timeout` rather than `timeout`.
    busy_retry: Duration,
}

impl SidecarTranslator {
    /// Explicit constructor (used for registration and for tests that inject a
    /// custom url/timeout).
    pub fn with_url(id: &'static str, url: String, timeout: Duration) -> Self {
        Self::with_retry(id, url, timeout, Duration::from_millis(DEFAULT_BUSY_RETRY_MS))
    }

    /// The same with an explicit busy-wait budget, so a case can pin the waiting behaviour
    /// without paying the shipped default.
    pub fn with_retry(id: &'static str, url: String, timeout: Duration, busy_retry: Duration) -> Self {
        Self { id, url, timeout, busy_retry }
    }

    /// What this engine resolved the busy-wait budget to. The fields are private and a suite
    /// that re-derived them from the environment would be asserting its own copy instead of
    /// the engine's resolution (D41's shape again), so the read is exposed instead.
    pub fn busy_retry(&self) -> Duration {
        self.busy_retry
    }

    /// The address this engine will dial, for suites that must prove their own pin won.
    pub fn endpoint(&self) -> &str {
        &self.url
    }

    fn from_env(id: &'static str, url_candidates: &[&str], default_url: &str) -> Self {
        Self::from_lookup(&|k| std::env::var(k).ok(), id, url_candidates, default_url)
    }

    /// The same resolution against an arbitrary environment, so both sides of every key
    /// (unset, so the shipped address applies; set, so the legacy prefix wins) execute in
    /// every test run instead of only on a matching machine (defect D41, same shape as
    /// `ollama::from_lookup`).
    fn from_lookup(
        lookup: &dyn Fn(&str) -> Option<String>,
        id: &'static str,
        url_candidates: &[&str],
        default_url: &str,
    ) -> Self {
        let url = crate::config::first_env(lookup, url_candidates)
            .unwrap_or_else(|| default_url.to_string());
        let timeout_ms = crate::config::first_env(lookup, TIMEOUT_CANDIDATES)
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        let busy_retry_ms = crate::config::first_env(lookup, RETRY_CANDIDATES)
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_BUSY_RETRY_MS);
        Self {
            id,
            url,
            timeout: Duration::from_millis(timeout_ms),
            busy_retry: Duration::from_millis(busy_retry_ms),
        }
    }

    /// Realtime-slot engine (Argos).
    pub fn argos() -> Self {
        Self::from_env("argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL)
    }

    /// Rare-language fallback engine (MADLAD-400).
    pub fn madlad() -> Self {
        Self::from_env("madlad", MADLAD_CANDIDATES, DEFAULT_MADLAD_URL)
    }

    /// POST to the sidecar's /translate and return the translation.
    ///
    /// Two kinds of "no answer" arrive here and treating them alike is what loses
    /// translations. A `429 busy` states that every decode slot is busy with *other* text and
    /// nothing was started for this request, so the wait is cheap and buys back an answer;
    /// everything else -- absent service, timeout, 422, a body without `text` -- is a fact a
    /// second dial cannot change, so it returns None and the router falls back (exposed via
    /// `is_available` and the engine name, never a silent misreport).
    ///
    /// Waiting is capped by `busy_retry`, and a wait only starts when the *whole* promised
    /// delay still fits inside that cap: sleeping part of a promised 60 s ends in a refusal
    /// anyway, having spent the caller's patience on the way. Worst case wall time is
    /// therefore `busy_retry + timeout`, because a request that finally gets a slot still gets
    /// its full per-request timeout.
    fn request(&self, text: &str, source: &str, target: &str, glossary: &GlossaryContext) -> Option<String> {
        let endpoint = format!("{}{}", self.url.trim_end_matches('/'), TRANSLATE_PATH);
        let mut payload = serde_json::json!({
            "text": text, "source": source, "target": target,
        });
        if !glossary.is_empty() {
            // Pass the whole context through. The budget was applied upstream by
            // `build_context`; a second hard-coded 20 here meant a larger
            // `glossary_max_terms` never reached the service (leftover of D32). These
            // backends ignore the array today, so the fix is about not lying on the wire.
            let terms: Vec<_> = glossary.entries.iter().map(|e| {
                serde_json::json!({ "src": e.source_term, "tgt": e.target_term })
            }).collect();
            payload["glossary"] = serde_json::Value::Array(terms);
        }
        // The payload is built once and resent unchanged. Re-building it per attempt would
        // let a retry answer a different request than the one that was refused, which is a
        // wrong result rather than a slow one.
        let deadline = Instant::now() + self.busy_retry;
        loop {
            let reply = http::http_post_reply(&endpoint, &payload, self.timeout)?;
            if reply.status == 200 {
                let root: serde_json::Value = serde_json::from_str(&reply.body).ok()?;
                let out = root.get("text").and_then(|v| v.as_str())
                    .map(|s| s.trim().to_string())?;
                return if out.is_empty() { None } else { Some(out) };
            }
            if !is_busy_refusal(&reply) {
                return None;
            }
            let wait = busy_wait(&reply)?;
            if Instant::now() + wait > deadline {
                return None;
            }
            std::thread::sleep(wait);
        }
    }
}

impl Translator for SidecarTranslator {
    fn name(&self) -> &'static str {
        self.id
    }

    fn translate(&self, text: &str, source: &str, target: &str) -> Option<String> {
        let text = text.trim();
        if text.is_empty() {
            return None;
        }
        self.request(text, source, target, &GlossaryContext::empty())
    }

    fn translate_with_context(
        &self,
        text: &str,
        source: &str,
        target: &str,
        glossary: &GlossaryContext,
    ) -> Option<String> {
        let text = text.trim();
        if text.is_empty() {
            return None;
        }
        self.request(text, source, target, glossary)
    }

    // Deliberately NOT overriding accepts_term_context: the terms travel in the
    // request payload, but the CT2 and MADLAD backends read them and do nothing -
    // their own docstrings state "Glossary injection is not supported by these
    // checkpoints". Reporting `true` here would describe the wire format rather than
    // the behaviour, and would let a scoped request look applied when it was ignored.
    // The trait default (false) is the honest answer.
}

/// A refusal from the busy family: the status **and** the body's own `"error":"busy"`.
///
/// Both halves are required. A bare 429 is also what a rate limit over completed work looks
/// like, and nothing in that answer promises that trying again is free. The body is where the
/// service states that no decode was started (`sidecar/ct2_sidecar.py`'s `_send_busy`), and
/// that statement is the entire justification for spending wall time instead of falling back.
fn is_busy_refusal(reply: &http::HttpReply) -> bool {
    if reply.status != 429 {
        return false;
    }
    match serde_json::from_str::<serde_json::Value>(&reply.body) {
        Ok(root) => root.get("error").and_then(|v| v.as_str()) == Some("busy"),
        Err(_) => false,
    }
}

/// The wait the service promised, or None when it promised nothing honest to wait on.
///
/// Absent, unparsable and zero all mean the same thing: waiting here would be a guess. This
/// repo's rule for a number it cannot back is to leave it out rather than invent one -- the
/// same reason `/health` answers `requests_until_exit: null` when the process is unbounded. A
/// guessed interval has a second cost: a caller that polls a busy engine once a second *is*
/// the concurrency the gate exists to keep off it.
fn busy_wait(reply: &http::HttpReply) -> Option<Duration> {
    let seconds: u64 = reply.header("Retry-After")?.trim().parse().ok()?;
    if seconds == 0 {
        return None;
    }
    Some(Duration::from_secs(seconds))
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::Translator;
    use std::io::{Read, Write};
    use std::net::{Shutdown, TcpListener, TcpStream};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    /// Start a one-shot local mock HTTP server and return its base url
    /// (e.g. http://127.0.0.1:PORT). The background thread accepts one
    /// connection, drains the request, writes a fixed JSON body, then closes.
    fn spawn_mock_once(response_json: &'static str) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = vec![0u8; 4096];
                let _ = stream.read(&mut buf); // drain the request (headers + body)
                let body = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                    response_json.len(),
                    response_json
                );
                let _ = stream.write_all(body.as_bytes());
                let _ = stream.shutdown(Shutdown::Both);
            }
        });
        format!("http://127.0.0.1:{}", port)
    }

    fn free_port_url() -> String {
        // Bind then drop immediately to get a port with almost certainly no
        // listener, so connecting gets refused.
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);
        format!("http://127.0.0.1:{}", port)
    }

    fn e(url: String) -> SidecarTranslator {
        SidecarTranslator::with_url("argos", url, Duration::from_millis(1500))
    }

    /// A fixed environment, so both sides of every key are exercised in every run rather
    /// than only on a machine whose environment happens to match them (defect D41).
    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let owned: Vec<(String, String)> = pairs.iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k: &str| owned.iter().find(|(name, _)| name == k).map(|(_, v)| v.clone())
    }

    #[test]
    fn an_empty_environment_gets_the_documented_sidecar_addresses() {
        let argos = SidecarTranslator::from_lookup(
            &env(&[]), "argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL);
        let madlad = SidecarTranslator::from_lookup(
            &env(&[]), "madlad", MADLAD_CANDIDATES, DEFAULT_MADLAD_URL);

        assert_eq!(argos.url, DEFAULT_ARGOS_URL);
        assert_eq!(madlad.url, DEFAULT_MADLAD_URL);
        assert_ne!(argos.url, madlad.url, "the two slots must not share a port");
        assert_eq!(argos.timeout, Duration::from_millis(DEFAULT_TIMEOUT_MS));
    }

    #[test]
    fn the_legacy_prefix_wins_and_an_unparsable_timeout_is_ignored() {
        let both = SidecarTranslator::from_lookup(
            &env(&[
                ("LT_ARGOS_URL", "http://lt:1"),
                ("WONSLATE_ARGOS_URL", "http://brand:1"),
                ("LT_SIDECAR_TIMEOUT_MS", "not-a-number"),
            ]),
            "argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL);
        assert_eq!(both.url, "http://lt:1",
            "the legacy prefix must keep winning, or a deployment's existing config breaks");
        assert_eq!(both.timeout, Duration::from_millis(DEFAULT_TIMEOUT_MS),
            "a typo in the timeout must not hand the sidecar a zero budget");

        let brand_only = SidecarTranslator::from_lookup(
            &env(&[("WONSLATE_ARGOS_URL", "http://brand:1")]),
            "argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL);
        assert_eq!(brand_only.url, "http://brand:1");

        let padded = SidecarTranslator::from_lookup(
            &env(&[("WONSLATE_SIDECAR_TIMEOUT_MS", "  700  ")]),
            "madlad", MADLAD_CANDIDATES, DEFAULT_MADLAD_URL);
        assert_eq!(padded.timeout, Duration::from_millis(700), "padding is tolerated");
    }

    #[test]
    fn name_is_the_registered_id() {
        let a = SidecarTranslator::with_url("argos", "http://127.0.0.1:9".into(), Duration::from_millis(200));
        let m = SidecarTranslator::with_url("madlad", "http://127.0.0.1:9".into(), Duration::from_millis(200));
        assert_eq!(a.name(), "argos");
        assert_eq!(m.name(), "madlad");
    }

    #[test]
    fn empty_input_returns_none_without_calling_service() {
        let t = e("http://127.0.0.1:9".into());
        assert_eq!(t.translate("   ", "en", "zh"), None);
    }

    #[test]
    fn returns_translated_text_on_valid_response() {
        let url = spawn_mock_once("{\"text\":\"你好世界\"}");
        let t = e(url);
        assert_eq!(t.translate("hello world", "en", "zh").as_deref(), Some("你好世界"));
    }

    #[test]
    fn returns_none_when_service_absent() {
        let t = e(free_port_url());
        assert_eq!(t.translate("hello", "en", "zh"), None, "连接被拒应返回 None 交路由兜底");
    }

    #[test]
    fn malformed_response_yields_none() {
        let url = spawn_mock_once("{\"unexpected\":\"field\"}");
        let t = e(url);
        assert_eq!(t.translate("hello", "en", "zh"), None, "无 text 字段应视为失败");
    }

    /// Read one request off a connection and hand back its body, so a case can assert on
    /// what actually went over the wire. Reading to the declared length is not optional: a
    /// request can arrive in several segments, and answering the first one would measure the
    /// mock instead of the client.
    fn read_body(stream: &mut TcpStream) -> String {
        let mut buf: Vec<u8> = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            match stream.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    buf.extend_from_slice(&chunk[..n]);
                    let text = String::from_utf8_lossy(&buf).to_string();
                    let Some(head) = text.find("\r\n\r\n") else { continue };
                    let want = text[..head].to_lowercase().lines()
                        .find_map(|l| l.strip_prefix("content-length:")
                            .and_then(|v| v.trim().parse::<usize>().ok()))
                        .unwrap_or(0);
                    if text.len() - head - 4 >= want { return text[head + 4..].to_string() }
                }
            }
        }
        String::new()
    }

    /// One-shot listener that hands the request body back to the test, so a case can
    /// assert on what actually went over the wire; the mock above only drains it.
    fn spawn_body_capture() -> (String, std::sync::mpsc::Receiver<String>) {
        let (tx, rx) = std::sync::mpsc::channel();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let body = read_body(&mut stream);
                let _ = tx.send(body);
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{\"text\":\"done\"}");
                let _ = stream.flush();
            }
        });
        (format!("http://127.0.0.1:{}", port), rx)
    }

    /// The sidecar leg had the same hard ceiling as the ollama one: `request` re-cut the
    /// context with `top_terms(20)`, so a configured budget above 20 never reached the
    /// service. Asserted on the wire rather than on a helper, because the truncation lived
    /// in the serialisation step itself (defect D32, leftover of its first two fixes).
    #[test]
    fn translate_request_carries_every_term_in_the_context() {
        use crate::types::GlossaryEntry;
        let (url, rx) = spawn_body_capture();
        let t = e(url);
        let ctx = GlossaryContext {
            entries: (0..25).map(|i| GlossaryEntry {
                source_term: format!("yuan文{}", i), source_lang: "zh".into(),
                target_term: format!("pair{}", i), target_lang: "en".into(),
                confidence: 0.9, frequency: 1, domain: "av".into(), source: "seed:av".into(),
            }).collect(),
        };
        assert_eq!(t.request("yuan文0", "zh", "en", &ctx).as_deref(), Some("done"),
            "the mock must answer, or the body assertion below proves nothing");
        let body = rx.recv_timeout(Duration::from_secs(5))
            .expect("the server must read a request body");
        let json: serde_json::Value = serde_json::from_str(&body)
            .unwrap_or_else(|e| panic!("body must be JSON, got {:?}: {}", body, e));
        let terms = json["glossary"].as_array()
            .unwrap_or_else(|| panic!("no glossary array in {:?}", body));
        assert_eq!(25, terms.len(),
            "only {} of the 25 terms reached the sidecar: the engine re-truncates the \
             context it was handed, so a budget above that hard limit does nothing",
            terms.len());
        assert_eq!(Some("pair24"), terms[24]["tgt"].as_str(),
            "the rows that survive must be the ones the context held");
    }

    // ---- a busy refusal is waited out, not lost ---------------------------------------

    /// A refusal in the shape `ct2_sidecar._send_busy` actually sends. The status alone is
    /// not what justifies waiting: the body is where the service states that **nothing was
    /// started**, which is the only reason a retry cannot cost a duplicated answer.
    const BUSY: &str = concat!(
        "{\"error\":\"busy\",\"decode_slots\":1,\"decodes_in_flight\":1,\"waited_ms\":0.0,",
        "\"note\":\"every decode slot is working on other text; nothing was started\"}");

    /// One scripted answer: status, JSON body, optional `Retry-After`.
    type Answer = (u16, &'static str, Option<&'static str>);

    struct MockSidecar {
        url: String,
        hits: Arc<AtomicUsize>,
        bodies: Arc<Mutex<Vec<String>>>,
    }

    impl MockSidecar {
        fn calls(&self) -> usize { self.hits.load(Ordering::SeqCst) }
        fn bodies(&self) -> Vec<String> { self.bodies.lock().unwrap().clone() }
    }

    fn reason(code: u16) -> &'static str {
        match code {
            200 => "OK",
            400 => "Bad Request",
            422 => "Unprocessable Entity",
            429 => "Too Many Requests",
            503 => "Service Unavailable",
            _ => "Server Error",
        }
    }

    /// A sidecar that answers every connection from a script and then **repeats its last
    /// entry forever**. The repetition is the point: a case about giving up needs a service
    /// that never runs out of refusals, so the only thing that can end the loop is the
    /// client's own budget.
    fn spawn_scripted(answers: Vec<Answer>) -> MockSidecar {
        assert!(!answers.is_empty(), "a mock with no script answers nothing, which tests nothing");
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let hits = Arc::new(AtomicUsize::new(0));
        let bodies = Arc::new(Mutex::new(Vec::new()));
        let (h, b) = (hits.clone(), bodies.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { break };
                let n = h.fetch_add(1, Ordering::SeqCst);
                let request = read_body(&mut stream);
                b.lock().unwrap().push(request);
                let (code, payload, retry_after) = &answers[n % answers.len()];
                let mut reply = format!(
                    "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\n", code, reason(*code));
                if let Some(seconds) = retry_after {
                    reply.push_str(&format!("Retry-After: {}\r\n", seconds));
                }
                reply.push_str(&format!("Content-Length: {}\r\n\r\n{}", payload.len(), payload));
                let _ = stream.write_all(reply.as_bytes());
                let _ = stream.flush();
                // Close it explicitly: `Connection: close` is what the client sends, and the
                // reader only gets an end-of-file once the socket is really gone.
                let _ = stream.shutdown(Shutdown::Both);
            }
        });
        MockSidecar { url: format!("http://127.0.0.1:{}", port), hits, bodies }
    }

    fn e_retry(url: String, timeout_ms: u64, busy_retry_ms: u64) -> SidecarTranslator {
        SidecarTranslator::with_retry("argos", url, Duration::from_millis(timeout_ms),
                                      Duration::from_millis(busy_retry_ms))
    }

    /// A service that accepts the connection, reads the request, **holds it silent**, and only
    /// then answers -- the wire shape of a caller the service has put in its queue.
    ///
    /// This is what `--queue-wait-ms` does to `/translate`: the request is neither served nor
    /// refused while it waits, the handler simply has written nothing yet. `spawn_scripted`
    /// cannot express that, because it replies the instant it has read the body -- so a question
    /// about the client's *read* timeout versus a server-side hold needs its own mock. Reading
    /// the request first (not blocking on EOF) is what lets the hold be measured honestly:
    /// `read_body` returns at the declared Content-Length, then the sleep starts.
    fn spawn_slow_reply(hold: Duration, code: u16, payload: &'static str) -> MockSidecar {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let hits = Arc::new(AtomicUsize::new(0));
        let bodies = Arc::new(Mutex::new(Vec::new()));
        let (h, b) = (hits.clone(), bodies.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { break };
                h.fetch_add(1, Ordering::SeqCst);
                let request = read_body(&mut stream);
                b.lock().unwrap().push(request);
                // Hold first, answer second: no byte goes over the wire for `hold`, exactly as a
                // queued request sees nothing until its slot frees.
                std::thread::sleep(hold);
                let mut reply = format!(
                    "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\n", code, reason(code));
                reply.push_str(&format!("Content-Length: {}\r\n\r\n{}", payload.len(), payload));
                let _ = stream.write_all(reply.as_bytes());
                let _ = stream.flush();
                let _ = stream.shutdown(Shutdown::Both);
            }
        });
        MockSidecar { url: format!("http://127.0.0.1:{}", port), hits, bodies }
    }

    #[test]
    fn a_server_hold_longer_than_the_timeout_costs_the_translation_without_a_retry() {
        // Desktop timeout-chain audit, stated as a wire behaviour. A service started with
        // `--queue-wait-ms` holds a queued `/translate` on its semaphore and writes nothing until
        // the slot frees -- it is NOT a `429`, so the busy-retry budget never engages and cannot
        // rescue this caller. The only thing standing between a queued caller and a lost answer
        // is the per-attempt read timeout: when the hold outruns it, `tcp_roundtrip` errors, the
        // request returns None, and the pipeline falls back to demo. Lost, but never a *silent*
        // wrong answer -- and exactly one dial, because re-asking would only queue a second
        // caller behind the same slot the first just abandoned.
        //
        // Hold 900 ms, timeout 200 ms: the read fires long before the server would answer, and
        // the 700 ms margin means a loaded CI runner cannot flip this by writing late -- the
        // server writes no bytes at all until 900 ms.
        let m = spawn_slow_reply(Duration::from_millis(900), 200, "{\"text\":\"迟到\"}");
        let t = e_retry(m.url.clone(), 200, 12_000);
        let started = Instant::now();
        assert_eq!(None, t.translate("hello", "en", "zh"),
            "a caller whose read timeout is shorter than the server's hold must give up rather \
             than block forever; this is the desktop's LT_SIDECAR_TIMEOUT_MS vs --queue-wait-ms \
             coupling, and the fallback must be None (-> demo), not the late text");
        assert_eq!(1, m.calls(),
            "the hold never returned a 429, so the retry budget must not fire -- a second dial \
             would only re-queue behind the slot the first caller walked away from");
        assert!(started.elapsed() < Duration::from_millis(900),
            "gave up at the timeout (200 ms), not after the server's full hold ({:?}) -- if it \
             waited for the answer the per-attempt timeout is not being honoured", started.elapsed());
    }

    #[test]
    fn a_server_hold_shorter_than_the_timeout_is_waited_out_and_answers() {
        // The safe side of the same combination, and the reason the rule is an inequality rather
        // than "keep timeouts small": when the per-attempt timeout covers the hold (queue wait +
        // this request's own decode), the queued answer arrives on the first connection and no
        // server work is wasted. `busy_retry` is still not involved -- the caller simply waits on
        // the socket the service is already holding for it.
        //
        // Hold 300 ms, timeout 3000 ms: a 10x margin so this cannot flake on a loaded runner.
        let m = spawn_slow_reply(Duration::from_millis(300), 200, "{\"text\":\"轮到你了\"}");
        let t = e_retry(m.url.clone(), 3000, 12_000);
        assert_eq!(t.translate("hello", "en", "zh").as_deref(), Some("轮到你了"),
            "a timeout sized to cover the hold recovers the queued translation");
        assert_eq!(1, m.calls(), "one connection, waited out; nothing to retry");
    }

    #[test]
    fn a_busy_refusal_is_waited_out_and_still_answers() {
        let m = spawn_scripted(vec![(429, BUSY, Some("1")), (200, "{\"text\":\"你好世界\"}", None)]);
        let t = e_retry(m.url.clone(), 1500, 3000);
        let started = Instant::now();
        assert_eq!(t.translate("hello world", "en", "zh").as_deref(), Some("你好世界"),
            "a refusal that promises no work was started must be waited out, not reported as \
             a lost translation");
        assert_eq!(2, m.calls(), "one refusal and one retry, no more");
        assert!(started.elapsed() >= Duration::from_millis(900),
            "the wait came out of Retry-After -- retrying immediately would hammer the very \
             service the gate exists to protect (elapsed {:?})", started.elapsed());
    }

    #[test]
    fn a_busy_refusal_that_promises_no_wait_is_not_retried() {
        // Absent, unparsable or zero means the service gave us no honest number to sleep on.
        // Inventing one would be the same mistake as a `requests_until_exit` countdown that
        // no budget backs: this repo answers "unknown" instead of guessing.
        for (label, promised) in [("absent", None), ("zero", Some("0")), ("not a number", Some("soon"))] {
            let m = spawn_scripted(vec![(429, BUSY, promised)]);
            let t = e_retry(m.url.clone(), 1500, 3000);
            assert_eq!(None, t.translate("hello", "en", "zh"), "{} Retry-After", label);
            assert_eq!(1, m.calls(),
                "with {} there is nothing to wait on, so a second dial would be a guess", label);
        }
    }

    #[test]
    fn a_429_outside_the_busy_family_is_not_retried() {
        // The busy contract is the one that says "nothing was started". Another 429 -- a rate
        // limit over work already done, say -- does not, so waiting on it is a different bet
        // and needs its own evidence.
        let m = spawn_scripted(vec![(429, "{\"error\":\"rate_limited\",\"window_s\":3600}", Some("1"))]);
        let t = e_retry(m.url.clone(), 1500, 3000);
        assert_eq!(None, t.translate("hello", "en", "zh"));
        assert_eq!(1, m.calls(),
            "only a refusal that states no work was started is cheap enough to retry");
    }

    #[test]
    fn a_busy_body_under_any_other_status_is_not_a_refusal() {
        // The status is the half that says which family an answer belongs to. Without it, a
        // 200 whose body happens to name "busy" (so: no `text`, therefore no answer) would put
        // this caller to sleep and have it re-dial a service that already answered, and a 503
        // carrying the same words would be waited on instead of falling back while a model is
        // still loading.
        for code in [200u16, 404, 503] {
            let m = spawn_scripted(vec![(code, BUSY, Some("1"))]);
            let t = e_retry(m.url.clone(), 1500, 3000);
            assert_eq!(None, t.translate("hello", "en", "zh"), "status {}", code);
            assert_eq!(1, m.calls(),
                "status {} is not a 429, so nothing in it promises a retry is free", code);
        }
    }

    #[test]
    fn an_answer_about_the_request_is_never_retried() {
        // Each of these is a fact about the request, not about this instant. Retrying a bad
        // target code three times does not make the code supported; it only multiplies the
        // latency in front of the fallback note (D22's rule, applied to the retry path).
        for code in [400u16, 422, 500, 503] {
            let m = spawn_scripted(vec![(code, "{\"error\":\"nope\"}", Some("1"))]);
            let t = e_retry(m.url.clone(), 1500, 3000);
            assert_eq!(None, t.translate("hello", "en", "zh"), "status {}", code);
            assert_eq!(1, m.calls(),
                "status {} is permanent; retrying it only delays the honest fallback", code);
        }
    }

    #[test]
    fn a_wait_the_budget_cannot_cover_is_not_started() {
        // Sleeping 5 of a promised 60 seconds ends in a refusal anyway, and has spent the
        // caller's patience on the way there. Either pay it whole or fall back now.
        let m = spawn_scripted(vec![(429, BUSY, Some("60")), (200, "{\"text\":\"late\"}", None)]);
        let t = e_retry(m.url.clone(), 1500, 1500);
        let started = Instant::now();
        assert_eq!(None, t.translate("hello", "en", "zh"));
        assert_eq!(1, m.calls(), "the whole promised wait does not fit, so it must not be paid");
        assert!(started.elapsed() < Duration::from_secs(5),
            "the budget has to bound the wall clock, not just the number of attempts ({:?})",
            started.elapsed());
    }

    #[test]
    fn a_service_that_keeps_refusing_is_given_up_on_inside_the_budget() {
        let m = spawn_scripted(vec![(429, BUSY, Some("1"))]);
        let t = e_retry(m.url.clone(), 1500, 3000);
        let started = Instant::now();
        assert_eq!(None, t.translate("hello", "en", "zh"),
            "a service that stays busy is still a refusal, just a waited-out one");
        let calls = m.calls();
        assert!((2..=4).contains(&calls),
            "the budget must be spent on real waits ({} attempts): one means it never \
             retried, more than four means the budget did not bound it", calls);
        assert!(started.elapsed() < Duration::from_secs(6),
            "wall time is bounded by the budget plus one request, not by how long the service \
             stays busy ({:?})", started.elapsed());
    }

    #[test]
    fn an_answer_on_the_first_try_costs_no_waiting() {
        // The gate is off by default, so this is the path every deployment actually takes:
        // adding the retry machinery must not cost a patient caller a sleep or a second
        // connection when nothing refused.
        let m = spawn_scripted(vec![(200, "{\"text\":\"你好\"}", None)]);
        let t = e_retry(m.url.clone(), 1500, 12000);
        let started = Instant::now();
        assert_eq!(t.translate("hello", "en", "zh").as_deref(), Some("你好"));
        assert_eq!(1, m.calls());
        assert!(started.elapsed() < Duration::from_millis(800),
            "a plain success must not pay for the retry machinery ({:?})", started.elapsed());
    }

    #[test]
    fn a_socket_that_hangs_up_is_not_redialed() {
        // A transport failure is not a refusal: the service never said anything, so there is
        // no Retry-After to honour and no promise that a retry is cheap. Without this rule a
        // sidecar that is simply absent would be dialled once per second for the whole budget.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let hits = Arc::new(AtomicUsize::new(0));
        let h = hits.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { break };
                h.fetch_add(1, Ordering::SeqCst);
                drop(stream); // accept, then hang up without answering
            }
        });
        let t = e_retry(format!("http://127.0.0.1:{}", port), 1500, 3000);
        assert_eq!(None, t.translate("hello", "en", "zh"));
        assert_eq!(1, hits.load(Ordering::SeqCst),
            "an unanswered connection gets one dial, not a retry loop");
    }

    #[test]
    fn a_retried_request_sends_the_same_payload_as_the_refused_one() {
        use crate::types::GlossaryEntry;
        let m = spawn_scripted(vec![(429, BUSY, Some("1")), (200, "{\"text\":\"done\"}", None)]);
        let t = e_retry(m.url.clone(), 1500, 3000);
        let ctx = GlossaryContext {
            entries: (0..3).map(|i| GlossaryEntry {
                source_term: format!("yuan文{}", i), source_lang: "zh".into(),
                target_term: format!("pair{}", i), target_lang: "en".into(),
                confidence: 0.9, frequency: 1, domain: "av".into(), source: "seed:av".into(),
            }).collect(),
        };
        assert_eq!(t.request("yuan文0", "zh", "en", &ctx).as_deref(), Some("done"));
        let bodies = m.bodies();
        assert_eq!(2, bodies.len(), "the refusal and the retry, both captured");
        assert_eq!(bodies[0], bodies[1],
            "a retry that rebuilt the request could send different terms than the ones the \
             caller was refused on: a wrong answer dressed up as a slow one");
    }

    #[test]
    fn the_busy_wait_budget_is_a_configured_knob() {
        let unset = SidecarTranslator::from_lookup(
            &env(&[]), "argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL);
        assert_eq!(unset.busy_retry, Duration::from_millis(DEFAULT_BUSY_RETRY_MS),
            "the shipped budget must be the documented one -- and it is only ever paid on a \
             service started with --decode-slots, which is off by default");

        let both = SidecarTranslator::from_lookup(
            &env(&[("LT_SIDECAR_RETRY_MS", "750"), ("WONSLATE_SIDECAR_RETRY_MS", "900")]),
            "argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL);
        assert_eq!(both.busy_retry, Duration::from_millis(750),
            "the legacy prefix must keep winning, or a deployment's existing config breaks");

        let brand = SidecarTranslator::from_lookup(
            &env(&[("WONSLATE_SIDECAR_RETRY_MS", "900")]),
            "madlad", MADLAD_CANDIDATES, DEFAULT_MADLAD_URL);
        assert_eq!(brand.busy_retry, Duration::from_millis(900));

        let typo = SidecarTranslator::from_lookup(
            &env(&[("LT_SIDECAR_RETRY_MS", "not-a-number")]),
            "argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL);
        assert_eq!(typo.busy_retry, Duration::from_millis(DEFAULT_BUSY_RETRY_MS),
            "a typo must not hand the desktop client a zero -- or a guessed -- wait");

        let padded = SidecarTranslator::from_lookup(
            &env(&[("WONSLATE_SIDECAR_RETRY_MS", "  400  ")]),
            "madlad", MADLAD_CANDIDATES, DEFAULT_MADLAD_URL);
        assert_eq!(padded.busy_retry, Duration::from_millis(400), "padding is tolerated");
    }
}
