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

use super::{http, Translator};
use crate::glossary::GlossaryContext;
use std::time::Duration;

/// Default service URL for the realtime slot (Argos); override via
/// LT_ARGOS_URL (legacy) or WONSLATE_ARGOS_URL (brand spelling).
const DEFAULT_ARGOS_URL: &str = "http://127.0.0.1:11435";
/// Default service URL for the rare-language slot (MADLAD); same prefix chain.
const DEFAULT_MADLAD_URL: &str = "http://127.0.0.1:11436";
/// Default timeout in milliseconds; the sidecar runs locally, shorter than ollama.
const DEFAULT_TIMEOUT_MS: u64 = 30_000;

/// The prefix chain each slot reads, in precedence order. Named once and used by the
/// constructors themselves, so a test cannot pass on a list the product does not read.
const ARGOS_CANDIDATES: &[&str] = &["LT_ARGOS_URL", "WONSLATE_ARGOS_URL"];
const MADLAD_CANDIDATES: &[&str] = &["LT_MADLAD_URL", "WONSLATE_MADLAD_URL"];
/// Translation endpoint path.
const TRANSLATE_PATH: &str = "/translate";

/// A translation engine backed by a local HTTP sidecar (argos / madlad share it).
pub struct SidecarTranslator {
    id: &'static str,
    url: String,
    timeout: Duration,
}

impl SidecarTranslator {
    /// Explicit constructor (used for registration and for tests that inject a
    /// custom url/timeout).
    pub fn with_url(id: &'static str, url: String, timeout: Duration) -> Self {
        Self { id, url, timeout }
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
        let timeout_ms = crate::config::first_env(
            lookup, &["LT_SIDECAR_TIMEOUT_MS", "WONSLATE_SIDECAR_TIMEOUT_MS"])
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        Self { id, url, timeout: Duration::from_millis(timeout_ms) }
    }

    /// Realtime-slot engine (Argos).
    pub fn argos() -> Self {
        Self::from_env("argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL)
    }

    /// Rare-language fallback engine (MADLAD-400).
    pub fn madlad() -> Self {
        Self::from_env("madlad", MADLAD_CANDIDATES, DEFAULT_MADLAD_URL)
    }

    /// POST to the sidecar's /translate and return the translation; any failure
    /// (absent / non-200 / missing "text") yields None.
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
        let root = http::http_post_json(&endpoint, &payload, self.timeout)?;
        let out = root.get("text").and_then(|v| v.as_str()).map(|s| s.trim().to_string())?;
        if out.is_empty() { None } else { Some(out) }
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


#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::Translator;
    use std::io::{Read, Write};
    use std::net::{Shutdown, TcpListener};
    use std::time::Duration;

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

    /// One-shot listener that hands the request body back to the test, so a case can
    /// assert on what actually went over the wire; the mock above only drains it.
    fn spawn_body_capture() -> (String, std::sync::mpsc::Receiver<String>) {
        let (tx, rx) = std::sync::mpsc::channel();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf: Vec<u8> = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    match stream.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            buf.extend_from_slice(&chunk[..n]);
                            let text = String::from_utf8_lossy(&buf).to_string();
                            let head = match text.find("\r\n\r\n") {
                                Some(h) => h,
                                None => continue,
                            };
                            let want = text[..head].to_lowercase().lines()
                                .find_map(|l| l.strip_prefix("content-length:")
                                    .and_then(|v| v.trim().parse::<usize>().ok()))
                                .unwrap_or(0);
                            if text.len() - head - 4 >= want { break }
                        }
                    }
                }
                let body = String::from_utf8_lossy(&buf)
                    .split_once("\r\n\r\n")
                    .map(|(_, b)| b.to_string())
                    .unwrap_or_default();
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
}
