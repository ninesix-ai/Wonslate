// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 路由决策核心（五层，从 Router.cs 下沉至 Rust 跨平台共享）

use crate::types::{TranslateRequest, TranslationMode};

/// 路由决策结果
#[derive(Debug, Clone)]
pub struct RoutePlan {
    /// L2 本地引擎 id
    pub local_engine_id: String,
    /// 是否注入 glossary
    pub use_glossary: bool,
    /// L3 升级 AI 引擎 id（None = 不升级）
    pub upgrade_engine_id: Option<String>,
    /// 升级置信度阈值（本地结果 < 此值时升级）
    pub confidence_threshold: f32,
}

/// 对一次翻译请求做路由决策
pub fn resolve(req: &TranslateRequest) -> RoutePlan {
    // L0: 隐私层 — 强制本地，永不升级
    if req.privacy {
        return RoutePlan {
            local_engine_id: "argos".into(),
            use_glossary: true,
            upgrade_engine_id: None,
            confidence_threshold: 0.0,
        };
    }

    // L1: 冷门语言降级
    if !is_common_pair(&req.source_lang, &req.target_lang) {
        return RoutePlan {
            local_engine_id: "madlad".into(),
            use_glossary: false,
            upgrade_engine_id: Some("ollama".into()),
            confidence_threshold: 0.80,
        };
    }

    // L2/L3: 按模式选择
    match req.mode {
        // 实时档：本地引擎，不升级（保证延迟）
        TranslationMode::Realtime => RoutePlan {
            local_engine_id: "argos".into(),
            use_glossary: true,
            upgrade_engine_id: None,
            confidence_threshold: 0.0,
        },
        // 精译档：本地先行，低置信度时升级 AI
        TranslationMode::Full => RoutePlan {
            local_engine_id: "argos".into(),
            use_glossary: true,
            upgrade_engine_id: Some("ollama".into()),
            confidence_threshold: 0.85,
        },
    }
}

/// 主流语言对判断（11 种高资源语言）
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
        req.source_lang = "sw".into(); // 斯瓦希里语，冷门
        let plan = resolve(&req);
        assert_eq!(plan.local_engine_id, "madlad"); // Phase 2：冷门兜底走 MADLAD
    }
}
