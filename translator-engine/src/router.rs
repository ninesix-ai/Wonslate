// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Core routing decision (five layers; moved down from Router.cs into the cross-platform Rust core)

use crate::config::{self, RouteRule, Routing, UpgradePolicy};
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
    /// Confidence threshold for upgrade under [`UpgradePolicy::LowConfidence`]
    pub confidence_threshold: f32,
    /// When the upgrade may happen at all (N-11 / D14).
    pub upgrade_policy: UpgradePolicy,
    /// Minimum quality a cached result needs to be served under this rule (D15).
    pub tm_quality_floor: f32,
}

/// Resolve a routing plan for one translation request, using the configured table.
pub fn resolve(req: &TranslateRequest) -> RoutePlan {
    resolve_with(req, &config::get().routing)
}

/// Resolve against an explicit table. Pure, so the configured behaviour can be tested
/// without installing a process-global config.
pub fn resolve_with(req: &TranslateRequest, routing: &Routing) -> RoutePlan {
    // L0: privacy layer -- force local engines, never upgrade. Deliberately NOT
    // configurable: a config file that could turn this off would make the privacy
    // guarantee a setting instead of an invariant.
    if req.privacy {
        return RoutePlan {
            local_engine_id: routing.full.local_engine.clone(),
            use_glossary: true,
            upgrade_engine_id: None,
            confidence_threshold: 0.0,
            upgrade_policy: UpgradePolicy::LowConfidence,
            tm_quality_floor: routing.full.tm_quality_floor,
        };
    }

    // L1: rare-language fallback
    if !is_common_pair(&req.source_lang, &req.target_lang, routing) {
        let rule = &routing.rare_pair;
        return RoutePlan {
            local_engine_id: rule.local_engine.clone(),
            use_glossary: false,
            upgrade_engine_id: upgrade_of(rule),
            confidence_threshold: rule.upgrade_threshold,
            upgrade_policy: rule.upgrade_policy,
            tm_quality_floor: rule.tm_quality_floor,
        };
    }

    // L2/L3: pick by mode
    let rule = match req.mode {
        // Realtime: local engine, no upgrade (latency budget)
        TranslationMode::Realtime => &routing.realtime,
        // Full: local first, then the rule's upgrade policy decides
        TranslationMode::Full => &routing.full,
    };
    RoutePlan {
        local_engine_id: rule.local_engine.clone(),
        use_glossary: true,
        upgrade_engine_id: upgrade_of(rule),
        confidence_threshold: rule.upgrade_threshold,
        upgrade_policy: rule.upgrade_policy,
        tm_quality_floor: rule.tm_quality_floor,
    }
}

/// An empty upgrade engine means "never upgrade" for this rule.
fn upgrade_of(rule: &RouteRule) -> Option<String> {
    if rule.upgrade_engine.is_empty() {
        None
    } else {
        Some(rule.upgrade_engine.clone())
    }
}

/// Common language-pair check (high-resource languages, from the routing table).
fn is_common_pair(src: &str, tgt: &str, routing: &Routing) -> bool {
    routing.common_pairs.iter().any(|l| l == src) && routing.common_pairs.iter().any(|l| l == tgt)
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

    // ---- N-11: the table is data, and the mode name means something -----------

    fn table() -> Routing {
        Routing::default()
    }

    #[test]
    fn defaults_match_the_shipped_routes() {
        let full = resolve_with(&make_req(false, TranslationMode::Full), &table());
        // Full mode goes to madlad (one checkpoint, every common pair); argos
        // only carries en<->zh packages and stays on the realtime slot.
        assert_eq!(full.local_engine_id, "madlad");
        assert_eq!(full.upgrade_engine_id.as_deref(), Some("ollama"));
        assert_eq!(full.confidence_threshold, 0.85);

        let rare = {
            let mut r = make_req(false, TranslationMode::Full);
            r.source_lang = "sw".into();
            resolve_with(&r, &table())
        };
        assert_eq!(rare.local_engine_id, "madlad");
        assert_eq!(rare.confidence_threshold, 0.80);
    }

    #[test]
    fn full_mode_defaults_to_honouring_its_name() {
        // D14: full mode must actually ask the AI engine, not silently behave like realtime.
        let plan = resolve_with(&make_req(false, TranslationMode::Full), &table());
        assert_eq!(plan.upgrade_policy, UpgradePolicy::Always);
    }

    #[test]
    fn a_configured_table_changes_the_route() {
        let mut routing = table();
        routing.full.upgrade_policy = UpgradePolicy::LowConfidence;
        routing.full.upgrade_threshold = 0.5;
        routing.full.upgrade_engine = "argos".into();

        let plan = resolve_with(&make_req(false, TranslationMode::Full), &routing);

        assert_eq!(plan.upgrade_policy, UpgradePolicy::LowConfidence);
        assert_eq!(plan.confidence_threshold, 0.5);
        assert_eq!(plan.upgrade_engine_id.as_deref(), Some("argos"));
    }

    #[test]
    fn an_empty_upgrade_engine_means_never_upgrade() {
        let mut routing = table();
        routing.full.upgrade_engine = String::new();

        let plan = resolve_with(&make_req(false, TranslationMode::Full), &routing);

        assert!(plan.upgrade_engine_id.is_none());
    }

    #[test]
    fn configured_language_set_drives_the_rare_pair_branch() {
        let mut routing = table();
        routing.common_pairs = vec!["en".into(), "sw".into()];

        let mut req = make_req(false, TranslationMode::Full);
        req.target_lang = "zh".into();   // zh is no longer common in this table
        let plan = resolve_with(&req, &routing);

        assert_eq!(plan.local_engine_id, "madlad", "zh left the common set, so the pair is rare");
    }

    #[test]
    fn privacy_stays_an_invariant_whatever_the_table_says() {
        // A table that tries to route privacy traffic to the cloud must not win: the
        // privacy clamp is not a setting.
        let mut routing = table();
        routing.full.local_engine = "demo".into();
        routing.full.upgrade_engine = "ollama".into();
        routing.full.upgrade_policy = UpgradePolicy::Always;

        let plan = resolve_with(&make_req(true, TranslationMode::Full), &routing);

        assert!(plan.upgrade_engine_id.is_none(), "privacy must never upgrade");
        assert!(crate::engine::is_local_engine(&plan.local_engine_id));
    }
}
