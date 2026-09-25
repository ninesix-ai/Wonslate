// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 精译档真实引擎：通过本机 Ollama 服务调用 Qwen 大模型翻译。
//!
//! 接入方式：
//!   - 服务地址：http://127.0.0.1:11434（本机 Ollama，OCI 容器 / 本地部署均可）
//!   - 兼容端点：/v1/chat/completions（OpenAI 兼容）
//!   - 模型：qwen3:8b（建议；可通过 OLLAMA_MODEL 环境变量覆盖）
//!   - 关闭思考：reasoning_effort="none"（/v1 兼容层正确映射 Think=false）+
//!               保留 think=false 以兼容 Ollama 原生语义
//!   - 超时：OLLAMA_TIMEOUT_MS 可调（默认 120000ms），失败回退且不 panic
//!
//! 该引擎不引入任何外部分词/模型文件，纯 HTTP 调本机 Ollama，天然离线；翻译为
//! 阻塞式（同步），由调用侧线程承担。返回 None 表示服务不可用/超时/解析失败，
//! 由上层路由层负责兜底。

use super::{http, Translator};
use crate::glossary::GlossaryContext;
use std::time::Duration;

/// 默认 Ollama 服务基址。
pub const DEFAULT_OLLAMA_URL: &str = "http://127.0.0.1:11434";
/// OpenAI 兼容的对话补全端点。
const CHAT_COMPLETIONS_PATH: &str = "/v1/chat/completions";
/// 默认模型名（可用环境变量 OLLAMA_MODEL 覆盖）。
const DEFAULT_MODEL: &str = "qwen3:8b";
/// 默认超时（毫秒）。
const DEFAULT_TIMEOUT_MS: u64 = 120_000;

/// 语言对 -> 指令后缀（用于构造翻译提示词）。
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
    /// 从环境变量读取可选配置，其余使用默认值。
    pub fn new() -> Self {
        let url = std::env::var("OLLAMA_URL").unwrap_or_else(|_| DEFAULT_OLLAMA_URL.to_string());
        let model =
            std::env::var("OLLAMA_MODEL").unwrap_or_else(|_| DEFAULT_MODEL.to_string());
        let timeout_ms = std::env::var("OLLAMA_TIMEOUT_MS")
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        Self {
            url,
            model,
            timeout: Duration::from_millis(timeout_ms),
        }
    }

    /// 触发一次对话补全，返回大模型原始 content 文本；失败返回 None。
    fn chat(&self, system: &str, user: &str) -> Option<String> {
        let endpoint = format!("{}{}", self.url.trim_end_matches('/'), CHAT_COMPLETIONS_PATH);

        let payload = serde_json::json!({
            "model": self.model,
            "messages": [
                { "role": "system", "content": system },
                { "role": "user", "content": user },
            ],
            "stream": false,
            // /v1 兼容层：OpenAI 语义下 "none" 显式关闭思考
            "reasoning_effort": "none",
            // 兼容旧版 / 原生语义（/v1 下会被忽略，无害）
            "think": false,
            "temperature": 0.3,
        });

        // 复用共享的纯 std HTTP 客户端（见 engine/http.rs）
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

    /// 解析/精简模型输出：去除思考块、代码围栏与多余说明，保证只有译文本身。
    fn clean_output(&self, raw: &str) -> String {
        let mut out = raw.trim().to_string();
        // 剥掉可能的思考内容块（形如  ... ，双标签/单标签均处理）
        if let Some(start) = out.find("<thinking>") {
            if let Some(end) = out.find("</thinking>") {
                let size = end + "</thinking>".len();
                if end > start {
                    out = format!("{}{}", &out[..start], &out[size.min(out.len())..]);
                }
            }
        }
        // 去掉可能包裹的代码围栏
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

    /// 构建含术语注入的 system prompt（层级 2：few-shot 示例）
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
