// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! The real full-quality engine: translates via a Qwen model served by local Ollama.
//!
//! Integration:
//!   -   - endpoint: http://127.0.0.1:11434 (local Ollama; OCI container or native install both work)
//!   -   - API: /v1/chat/completions (OpenAI-compatible)
//!   -   - model: qwen3:8b (suggested; env-overridable via the LT_ > WONSLATE_ > bare prefix chain)
//!   -   - thinking off: reasoning_effort="none" (the /v1 layer maps this to Think=false) plus
//!                   think=false kept for native Ollama semantics
//!   -   - timeout: *_TIMEOUT_MS (default 120000ms); failures degrade without panicking
//!
//! This engine ships no tokenizer or model files: plain HTTP to local Ollama keeps it
//! offline by nature. Translation is synchronous; the caller's thread bears the wait.
//! None means unavailable / timeout / parse failure; the router falls back.

use super::{http, Translator};
use crate::glossary::GlossaryContext;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

/// How long a reachability answer is trusted. Long enough that a 10k-request run does
/// not open 10k probe connections, short enough that starting Ollama is noticed
/// promptly without restarting the process.
const PROBE_TTL: Duration = Duration::from_secs(5);
/// Connect budget for the probe. Deliberately short: this call sits in front of every
/// request that might escalate, so a black-holed host must not reintroduce the stall
/// the probe exists to prevent.
const PROBE_TIMEOUT: Duration = Duration::from_millis(200);

/// (url, reachable, when) for the last probe. Reachability is a property of the machine,
/// not of one request, so caching it process-wide is the point.
static PROBE_CACHE: OnceLock<Mutex<Option<(String, bool, Instant)>>> = OnceLock::new();

/// Default Ollama base URL.
pub const DEFAULT_OLLAMA_URL: &str = "http://127.0.0.1:11434";
/// OpenAI-compatible chat-completions path.
const CHAT_COMPLETIONS_PATH: &str = "/v1/chat/completions";
/// Default model name (env-overridable through the OLLAMA_MODEL prefix chain).
const DEFAULT_MODEL: &str = "qwen3:8b";
/// Default timeout in milliseconds.
const DEFAULT_TIMEOUT_MS: u64 = 120_000;

/// Language pair -> instruction suffix (used to build the translation prompt).
fn language_prompt(source: &str, target: &str) -> String {
    match (source.to_lowercase().as_str(), target.to_lowercase().as_str()) {
        ("zh", "en") => "Chinese to English".to_string(),
        ("en", "zh") => "English to Chinese".to_string(),
        _ => format!("{} to {}", source, target),
    }
}

pub struct OllamaTranslator {
    url: String,
    model: String,
    timeout: Duration,
}

impl Default for OllamaTranslator {
    fn default() -> Self {
        Self::new()
    }
}

impl OllamaTranslator {
    /// Build from environment overrides, falling through to defaults.
    /// Prefix chain per key (see config::first_env): legacy `LT_*` wins for
    /// backward compatibility, then `WONSLATE_*`, then the bare name.
    pub fn new() -> Self {
        let lookup = |k: &str| std::env::var(k).ok();
        let url = crate::config::first_env(&lookup, &["LT_OLLAMA_URL", "WONSLATE_OLLAMA_URL", "OLLAMA_URL"])
            .unwrap_or_else(|| DEFAULT_OLLAMA_URL.to_string());
        let model = crate::config::first_env(&lookup, &["LT_OLLAMA_MODEL", "WONSLATE_OLLAMA_MODEL", "OLLAMA_MODEL"])
            .unwrap_or_else(|| DEFAULT_MODEL.to_string());
        let timeout_ms = crate::config::first_env(&lookup, &["LT_OLLAMA_TIMEOUT_MS", "WONSLATE_OLLAMA_TIMEOUT_MS", "OLLAMA_TIMEOUT_MS"])
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        Self {
            url,
            model,
            timeout: Duration::from_millis(timeout_ms),
        }
    }

    /// Issue one chat completion and return the raw content text; None on failure.
    fn chat(&self, system: &str, user: &str) -> Option<String> {
        let endpoint = format!("{}{}", self.url.trim_end_matches('/'), CHAT_COMPLETIONS_PATH);

        let payload = serde_json::json!({
            "model": self.model,
            "messages": [
                { "role": "system", "content": system },
                { "role": "user", "content": user },
            ],
            "stream": false,
            // /v1 layer: under OpenAI semantics "none" explicitly disables thinking
            "reasoning_effort": "none",
            // native-semantics compat (ignored under /v1; harmless)
            "think": false,
            "temperature": 0.3,
        });

        // Reuse the shared pure-std HTTP client (see engine/http.rs)
        let root = http::http_post_json(&endpoint, &payload, self.timeout)?;

        // choices[0].message.content
        let content = root
            .pointer("/choices/0/message/content")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        if content.is_empty() {
            return None;
        }
        Some(content)
    }

    /// Clean the model output: strip thinking blocks, code fences and stray commentary so only the translation remains.
    fn clean_output(&self, raw: &str) -> String {
        let mut out = raw.trim().to_string();
        // Remove any thinking block (both single- and double-tag forms)
        if let Some(start) = out.find("<thinking>") {
            if let Some(end) = out.find("</thinking>") {
                let size = end + "</thinking>".len();
                if end > start {
                    out = format!("{}{}", &out[..start], &out[size.min(out.len())..]);
                }
            }
        }
        // Strip wrapping code fences
        if out.trim_start().starts_with("```") {
            let no_fence = out.trim_start().trim_start_matches('`').trim_start();
            if let Some(idx) = no_fence.find("```") {
                out = no_fence[..idx].to_string();
            } else {
                out = no_fence.to_string();
            }
        }
        out.trim().to_string()
    }
}

impl Translator for OllamaTranslator {
    fn name(&self) -> &'static str {
        "ollama-qwen"
    }

    fn translate(&self, text: &str, source: &str, target: &str) -> Option<String> {
        self.do_translate(text, source, target, &GlossaryContext::empty())
    }

    fn translate_with_context(
        &self,
        text: &str,
        source: &str,
        target: &str,
        glossary: &GlossaryContext,
    ) -> Option<String> {
        self.do_translate(text, source, target, glossary)
    }

    fn is_reachable(&self) -> bool {
        self.probe_reachable()
    }

    /// The terms are composed into the system prompt (build_system_prompt), so a
    /// requested domain genuinely shapes the output here. This is the one engine
    /// tier that can carry S11's scoped glossary.
    fn accepts_term_context(&self) -> bool {
        true
    }
}

impl OllamaTranslator {
    fn do_translate(
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
        let pair = language_prompt(source, target);
        let system = self.build_system_prompt(&pair, glossary);
        let user = text.to_string();
        let raw = self.chat(&system, &user)?;
        let cleaned = self.clean_output(&raw);
        if cleaned.is_empty() {
            return None;
        }
        Some(cleaned)
    }

    /// Cached TCP liveness probe against the configured base URL.
    ///
    /// A poisoned lock is treated as "no cache" rather than a failure: the probe is
    /// advisory, and answering `false` because a lock broke would silently disable
    /// escalation.
    fn probe_reachable(&self) -> bool {
        let cache = PROBE_CACHE.get_or_init(|| Mutex::new(None));
        if let Ok(guard) = cache.lock() {
            if let Some((url, ok, at)) = guard.as_ref() {
                if *url == self.url && at.elapsed() < PROBE_TTL {
                    return *ok;
                }
            }
        }

        let ok = http::probe(&self.url, PROBE_TIMEOUT);
        if let Ok(mut guard) = cache.lock() {
            *guard = Some((self.url.clone(), ok, Instant::now()));
        }
        ok
    }

    /// Build the system prompt with term injection (level 2: few-shot examples)
    fn build_system_prompt(&self, pair: &str, glossary: &GlossaryContext) -> String {
        let mut prompt = format!(
            "You are a professional offline translation engine. Translate the user's text \
             from {}. Output ONLY the translated text with no explanations, no quotes, \
             no annotations.",
            pair
        );
        if !glossary.is_empty() {
            prompt.push_str("\n\nUse these consistent term translations:");
            for e in glossary.top_terms(20) {
                prompt.push_str(&format!("\n  {} → {}", e.source_term, e.target_term));
            }
            prompt.push_str("\n\nMaintain terminology consistency with the above.");
        }
        prompt
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn language_prompt_map() {
        assert_eq!(language_prompt("zh", "en"), "Chinese to English");
        assert_eq!(language_prompt("en", "zh"), "English to Chinese");
    }

    #[test]
    fn clean_output_strips_fence() {
        let e = OllamaTranslator::default();
        assert_eq!(e.clean_output("```\nhello\n```"), "hello");
    }
}
