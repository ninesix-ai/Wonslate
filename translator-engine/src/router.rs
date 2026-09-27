// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Core routing decision (five layers; moved down from Router.cs into the cross-platform Rust core)

use crate::types::{TranslateRequest, TranslationMode};

/// Result of one routing decision
#[derive(Debug, Clone)]
pub struct RoutePlan {
    /// L2 local engine id
    pub local_engine_id: String,
    /// Whether to inject the glossary
    pub use_glossary: bool,
    /// L3 AI upgrade engine id (None = never upgrade)
    pub upgrade_engine_id: Option<String>,
    /// Confidence threshold for upgrade (local result below it triggers the upgrade)
    pub confidence_threshold: f32,
}

/// Resolve a routing plan for one translation request.
pub fn resolve(req: &TranslateRequest) -> RoutePlan {
    // L0: privacy layer -- force local engines, never upgrade
    if req.privacy {
        return RoutePlan {
            local_engine_id: "argos".into(),
            use_glossary: true,
            upgrade_engine_id: None,
            confidence_threshold: 0.0,
        };
    }

    // L1: rare-language fallback
    if !is_common_pair(&req.source_lang, &req.target_lang) {
        return RoutePlan {
            local_engine_id: "madlad".into(),
            use_glossary: false,
            upgrade_engine_id: Some("ollama".into()),
            confidence_threshold: 0.80,
        };
    }

    // L2/L3: pick by mode
    match req.mode {
        // Realtime: local engine, no upgrade (latency budget)
        TranslationMode::Realtime => RoutePlan {
            local_engine_id: "argos".into(),
            use_glossary: true,
            upgrade_engine_id: None,
            confidence_threshold: 0.0,
        },
        // Full: local first, upgrade to AI on low confidence
        TranslationMode::Full => RoutePlan {
            local_engine_id: "argos".into(),
            use_glossary: true,
            upgrade_engine_id: Some("ollama".into()),
            confidence_threshold: 0.85,
        },
    }
}

/// Common language-pair check (11 high-resource languages)
fn is_common_pair(src: &str, tgt: &str) -> bool {
    const LANGS: &[&str] = &[
        "zh", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar",
    ];
    LANGS.contains(&src) && LANGS.contains(&tgt)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::TranslationMode;

    fn make_req(privacy: bool, mode: TranslationMode) -> TranslateRequest {
        TranslateRequest {
            engine_id: String::new(),
            input: "hello".into(),
            source_lang: "en".into(),
            target_lang: "zh".into(),
            mode,
            privacy,
            domain: String::new(),
            use_tm: true,
        }
    }

    #[test]
    fn privacy_no_upgrade() {
        let plan = resolve(&make_req(true, TranslationMode::Full));
        assert!(plan.upgrade_engine_id.is_none());
    }

    #[test]
    fn realtime_no_upgrade() {
        let plan = resolve(&make_req(false, TranslationMode::Realtime));
        assert!(plan.upgrade_engine_id.is_none());
    }

    #[test]
    fn full_upgrades_to_ollama() {
        let plan = resolve(&make_req(false, TranslationMode::Full));
        assert_eq!(plan.upgrade_engine_id.as_deref(), Some("ollama"));
    }

    #[test]
    fn rare_lang_uses_fallback_engine() {
        let mut req = make_req(false, TranslationMode::Full);
        req.source_lang = "sw".into(); // Swahili: rare pair
        let plan = resolve(&req);
        assert_eq!(plan.local_engine_id, "madlad"); // Phase 2: rare-pair fallback goes to MADLAD
    }
}
