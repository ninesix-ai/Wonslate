// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 引擎注册与统一 Translator 接口。
//!
//! 所有引擎实现同一 trait，业务侧按需热插拔。

pub mod demo;
pub mod ollama;
pub mod http;
pub mod sidecar;

use crate::glossary::GlossaryContext;

/// 统一翻译引擎接口（v2.0：新增 translate_with_context）
pub trait Translator: Send + Sync {
    fn name(&self) -> &'static str;

    /// 基础翻译（无 glossary 上下文）。
    /// 返回 None 表示该引擎无法处理，由路由层兜底。
    fn translate(&self, text: &str, source: &str, target: &str) -> Option<String>;

    /// 带 glossary 上下文的翻译（ollama.rs 等高级引擎 override 此方法）。
    /// 默认退化到无 glossary 版本，保证向后兼容。
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

/// 按 id 获取引擎实例；未知 id 回落到 demo，保证永不空指针。
pub fn get_engine(id: &str) -> Box<dyn Translator> {
    match id {
        "demo" => Box::new(demo::DemoTranslator::default()),
        "ollama" | "qwen" | "ollama-qwen" => Box::new(ollama::OllamaTranslator::new()),
        "argos" => Box::new(sidecar::SidecarTranslator::argos()),
        "madlad" => Box::new(sidecar::SidecarTranslator::madlad()),
        _ => Box::new(demo::DemoTranslator::default()),
    }
}

/// 引擎是否【真正注册可用】。与 get_engine 的真实分支保持一致（不含未知回落），
/// 供上层判断路由声称的引擎是否会静默回落，从而显式暴露回落原因。
pub fn is_available(id: &str) -> bool {
    matches!(id, "demo" | "ollama" | "qwen" | "ollama-qwen" | "argos" | "madlad")
}

/// 支持的引擎清单（供 UI 展示和路由配置）。
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
        // Phase 2 sidecar 引擎已注册（不再静默回落 demo）
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
        // Phase 2：argos/madlad 现已真正可用
        assert!(is_available("argos"));
        assert!(is_available("madlad"));
    }

    #[test]
    fn is_available_false_for_unregistered() {
        // 仍未注册的 id 必须返回 false（供上层暴露回落）
        assert!(!is_available("bert"));
        assert!(!is_available("no-such-engine"));
    }
}
