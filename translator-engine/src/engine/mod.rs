// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Engine registration and the unified Translator interface.
//!
//! Every engine implements the same trait; callers hot-swap by need.

pub mod demo;
pub mod ollama;
pub mod http;
pub mod sidecar;

use crate::glossary::GlossaryContext;

/// Unified translation-engine interface (v2.0: adds translate_with_context)
pub trait Translator: Send + Sync {
    fn name(&self) -> &'static str;

    /// Basic translation (no glossary context).
    /// None means this engine cannot handle the input; the router falls back.
    fn translate(&self, text: &str, source: &str, target: &str) -> Option<String>;

    /// Translation with glossary context (advanced engines like ollama.rs override this).
    /// The default degrades to the no-glossary call for backward compatibility.
    fn translate_with_context(
        &self,
        text: &str,
        source: &str,
        target: &str,
        _glossary: &GlossaryContext,
    ) -> Option<String> {
        self.translate(text, source, target)
    }
}

/// Get an engine instance by id; unknown ids fall back to demo, never a null pointer.
pub fn get_engine(id: &str) -> Box<dyn Translator> {
    match id {
        "demo" => Box::new(demo::DemoTranslator::default()),
        "ollama" | "qwen" | "ollama-qwen" => Box::new(ollama::OllamaTranslator::new()),
        "argos" => Box::new(sidecar::SidecarTranslator::argos()),
        "madlad" => Box::new(sidecar::SidecarTranslator::madlad()),
        _ => Box::new(demo::DemoTranslator::default()),
    }
}

/// Whether the engine id is genuinely registered. Stays aligned with get_engine's real
/// branches (no unknown-id fallback) so callers can expose why a fallback happened.
pub fn is_available(id: &str) -> bool {
    matches!(id, "demo" | "ollama" | "qwen" | "ollama-qwen" | "argos" | "madlad")
}

/// Whether an engine id is guaranteed to run fully on this machine.
///
/// Privacy-mode invariant basis: fail-closed -- only ids we know to be local
/// (built-in dictionary, or a 127.0.0.1 sidecar slot) count as local. Any
/// other id, including LLM-backed engines whose URL is env-overridable
/// (ollama/qwen) and anything unregistered, is treated as non-local.
pub fn is_local_engine(id: &str) -> bool {
    matches!(id, "demo" | "argos" | "madlad")
}

/// Catalog of supported engines (for UI display and routing config).
pub fn available_engines() -> Vec<(&'static str, &'static str)> {
    vec![
        ("demo",   "演示引擎（内置词表，兜底用）"),
        ("ollama", "AI 精译引擎（本机 Ollama + Qwen3，质量最高）"),
        ("argos",  "实时档引擎（本机 Argos/CTranslate2 sidecar）"),
        ("madlad", "冷门语言兜底（本机 MADLAD-400 sidecar，Apache-2.0）"),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_engine_falls_back_to_demo() {
        let e = get_engine("no-such-engine");
        assert_eq!(e.name(), "demo");
    }

    #[test]
    fn ollama_registered() {
        let e = get_engine("ollama");
        assert_eq!(e.name(), "ollama-qwen");
    }

    #[test]
    fn argos_and_madlad_registered() {
        // Phase 2 sidecar engines are registered (no more silent demo fallback)
        assert_eq!(get_engine("argos").name(), "argos");
        assert_eq!(get_engine("madlad").name(), "madlad");
    }

    #[test]
    fn available_has_demo_and_ollama() {
        let ids: Vec<_> = available_engines().iter().map(|(id, _)| *id).collect();
        assert!(ids.contains(&"demo"));
        assert!(ids.contains(&"ollama"));
        assert!(ids.contains(&"argos"));
        assert!(ids.contains(&"madlad"));
    }

    #[test]
    fn is_available_true_for_registered() {
        assert!(is_available("demo"));
        assert!(is_available("ollama"));
        assert!(is_available("qwen"));
        assert!(is_available("ollama-qwen"));
        // Phase 2: argos/madlad are genuinely available now
        assert!(is_available("argos"));
        assert!(is_available("madlad"));
    }

    #[test]
    fn is_available_false_for_unregistered() {
        // Still-unregistered ids must return false (so callers can expose the fallback)
        assert!(!is_available("bert"));
        assert!(!is_available("no-such-engine"));
    }

    #[test]
    fn local_engines_are_privacy_safe() {
        // Privacy-mode invariant basis: engines that never leave the machine.
        assert!(is_local_engine("demo"));
        assert!(is_local_engine("argos"));
        assert!(is_local_engine("madlad"));
    }

    #[test]
    fn ollama_is_not_a_local_engine_for_privacy_purposes() {
        // Ollama reaches an LLM over HTTP and its URL is env-overridable,
        // so privacy mode must treat it as non-local.
        assert!(!is_local_engine("ollama"));
        assert!(!is_local_engine("qwen"));
        assert!(!is_local_engine("ollama-qwen"));
    }

    #[test]
    fn unknown_engines_are_not_local() {
        // Fail-closed: an id we do not know must default to "not local".
        assert!(!is_local_engine("no-such-engine"));
    }
}
