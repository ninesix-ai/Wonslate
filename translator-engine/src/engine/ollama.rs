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
///
/// The two-letter code alone is ambiguous to an LLM: "it" reads as English for
/// "it" (the pronoun), so `en -> it` produced Romanian-looking text in the
/// FLORES run (COMET was never taken because chrF collapsed to 31.7). Every
/// code the picker can emit therefore maps to an explicit language name.
fn language_prompt(source: &str, target: &str) -> String {
    let name = |code: &str| -> Option<&'static str> {
        Some(match code.to_lowercase().as_str() {
            "zh" => "Chinese",
            "en" => "English",
            "ja" => "Japanese",
            "ko" => "Korean",
            "fr" => "French",
            "de" => "German",
            "es" => "Spanish",
            "ru" => "Russian",
            "pt" => "Portuguese",
            "it" => "Italian",
            "ar" => "Arabic",
            _ => return None,
        })
    };
    let from = name(source);
    let to = name(target);
    match (from, to) {
        (Some(f), Some(t)) => format!("{} to {}", f, t),
        // Unknown codes keep the old behaviour rather than inventing a language.
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
        Self::from_lookup(&|k| std::env::var(k).ok())
    }

    /// The same resolution against an arbitrary environment.
    ///
    /// Split out because the interesting part of a prefix chain is *both* sides of every
    /// key: "absent, so the shipped default applies" and "present, so it wins in this
    /// order". Read the process environment inline and each side only executes on a
    /// machine that happens to match -- which is why this file's region coverage used to
    /// move with the host (defect D41). A test can now drive both branches in every run.
    fn from_lookup(lookup: &dyn Fn(&str) -> Option<String>) -> Self {
        let url = crate::config::first_env(lookup, &["LT_OLLAMA_URL", "WONSLATE_OLLAMA_URL", "OLLAMA_URL"])
            .unwrap_or_else(|| DEFAULT_OLLAMA_URL.to_string());
        let model = crate::config::first_env(lookup, &["LT_OLLAMA_MODEL", "WONSLATE_OLLAMA_MODEL", "OLLAMA_MODEL"])
            .unwrap_or_else(|| DEFAULT_MODEL.to_string());
        let timeout_ms = crate::config::first_env(lookup, &["LT_OLLAMA_TIMEOUT_MS", "WONSLATE_OLLAMA_TIMEOUT_MS", "OLLAMA_TIMEOUT_MS"])
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
            // Every row the context carries, without a second ceiling here: the budget
            // is `config.glossary_max_terms`, already applied by `build_context` after it
            // filtered down to the terms this text actually uses. Cutting to a hard 20 on
            // top of that made any larger setting silently do nothing (leftover of D32).
            for e in &glossary.entries {
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

    /// A fixed environment, so the prefix chain can be asserted key by key without
    /// touching the process -- which is what let this file's coverage number depend on the
    /// machine before `from_lookup` existed (defect D41).
    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let owned: Vec<(String, String)> = pairs.iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k: &str| owned.iter().find(|(name, _)| name == k).map(|(_, v)| v.clone())
    }

    #[test]
    fn an_empty_environment_gets_the_shipped_endpoint_model_and_timeout() {
        let e = OllamaTranslator::from_lookup(&env(&[]));
        assert_eq!(e.url, DEFAULT_OLLAMA_URL);
        assert_eq!(e.model, DEFAULT_MODEL);
        assert_eq!(e.timeout, Duration::from_millis(DEFAULT_TIMEOUT_MS));
    }

    #[test]
    fn legacy_prefix_beats_brand_beats_bare_name() {
        let all = OllamaTranslator::from_lookup(&env(&[
            ("LT_OLLAMA_URL", "http://lt:1"),
            ("WONSLATE_OLLAMA_URL", "http://brand:1"),
            ("OLLAMA_URL", "http://bare:1"),
            ("LT_OLLAMA_MODEL", "lt-model"),
            ("WONSLATE_OLLAMA_MODEL", "brand-model"),
            ("OLLAMA_MODEL", "bare-model"),
        ]));
        assert_eq!(all.url, "http://lt:1",
            "`LT_*` keeps winning for backward compatibility, whatever else is exported");
        assert_eq!(all.model, "lt-model");

        let brand = OllamaTranslator::from_lookup(&env(&[
            ("WONSLATE_OLLAMA_URL", "http://brand:1"),
            ("OLLAMA_URL", "http://bare:1"),
        ]));
        assert_eq!(brand.url, "http://brand:1", "the brand spelling outranks the bare name");

        let bare = OllamaTranslator::from_lookup(&env(&[("OLLAMA_URL", "http://bare:1")]));
        assert_eq!(bare.url, "http://bare:1",
            "a third-party server exported as OLLAMA_URL is honoured without a rename");
    }

    #[test]
    fn a_timeout_that_is_not_a_number_keeps_the_default_instead_of_becoming_zero() {
        let junk = OllamaTranslator::from_lookup(&env(&[("LT_OLLAMA_TIMEOUT_MS", "later")]));
        assert_eq!(junk.timeout, Duration::from_millis(DEFAULT_TIMEOUT_MS),
            "a typo must not cut the request budget to nothing");

        let padded = OllamaTranslator::from_lookup(&env(&[("WONSLATE_OLLAMA_TIMEOUT_MS", "  4000  ")]));
        assert_eq!(padded.timeout, Duration::from_millis(4000),
            "padding is tolerated, since a value read out of a file often carries it");
    }

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

    /// The engine must not impose its own smaller ceiling on top of the context it was
    /// handed. `config.glossary_max_terms` is the budget, and `build_context` already
    /// cuts to it; a second, hard-coded 20 here made any setting above 20 silently do
    /// nothing -- the leftover after D32's first two fixes (same defect number).
    #[test]
    fn system_prompt_carries_every_term_it_was_given() {
        use crate::types::GlossaryEntry;
        let e = OllamaTranslator::default();
        let ctx = GlossaryContext {
            entries: (0..25).map(|i| GlossaryEntry {
                source_term: format!("shu语{}", i), source_lang: "zh".into(),
                target_term: format!("term{}", i), target_lang: "en".into(),
                confidence: 0.9, frequency: 1, domain: "av".into(), source: "seed:av".into(),
            }).collect(),
        };
        let prompt = e.build_system_prompt("Chinese to English", &ctx);
        assert_eq!(25, ctx.entries.len(), "the fixture must really exceed 20 rows");
        for i in 0..25 {
            assert!(prompt.contains(&format!("term{}", i)),
                "row {} never reached the prompt: the engine re-truncates the context, \
                 so raising glossary_max_terms above its hard-coded limit does nothing",
                i);
        }
    }
}
