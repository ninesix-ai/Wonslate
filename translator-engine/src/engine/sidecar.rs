// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Sidecar 引擎（argos / madlad）——通过本机 HTTP 调 CT2/Argos 薄服务。
//!
//! 设计：复用 engine/http.rs 的纯 std HTTP 客户端，
//! Rust 侧零新依赖、不触 SAC；服务缺席/超时/解析失败一律返回 None，由路由兜底 demo
//! （且 pipeline 会因 is_available/实际引擎名暴露回落，不谎报）。

use super::{http, Translator};
use crate::glossary::GlossaryContext;
use std::time::Duration;

/// 实时档（Argos）默认服务地址；可用 LT_ARGOS_URL 覆盖。
const DEFAULT_ARGOS_URL: &str = "http://127.0.0.1:11435";
/// 冷门兜底档（MADLAD）默认服务地址；可用 LT_MADLAD_URL 覆盖。
const DEFAULT_MADLAD_URL: &str = "http://127.0.0.1:11436";
/// 默认超时（毫秒）；sidecar 本机推理，较 ollama 短。
const DEFAULT_TIMEOUT_MS: u64 = 30_000;
/// 翻译端点。
const TRANSLATE_PATH: &str = "/translate";

/// 一个通过本机 HTTP sidecar 调用的翻译引擎（argos / madlad 共用实现）。
pub struct SidecarTranslator {
    id: &'static str,
    url: String,
    timeout: Duration,
}

impl SidecarTranslator {
    /// 显式构造（供注册与测试注入自定义 url/超时）。
    pub fn with_url(id: &'static str, url: String, timeout: Duration) -> Self {
        Self { id, url, timeout }
    }

    fn from_env(id: &'static str, env_key: &str, default_url: &str) -> Self {
        let url = std::env::var(env_key).unwrap_or_else(|_| default_url.to_string());
        let timeout_ms = std::env::var("LT_SIDECAR_TIMEOUT_MS")
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(DEFAULT_TIMEOUT_MS);
        Self { id, url, timeout: Duration::from_millis(timeout_ms) }
    }

    /// 实时档引擎（Argos）。
    pub fn argos() -> Self {
        Self::from_env("argos", "LT_ARGOS_URL", DEFAULT_ARGOS_URL)
    }

    /// 冷门兜底引擎（MADLAD-400）。
    pub fn madlad() -> Self {
        Self::from_env("madlad", "LT_MADLAD_URL", DEFAULT_MADLAD_URL)
    }

    /// 调 sidecar 的 /translate，返回译文；任何失败（缺席/超时/非200/无 text）→ None。
    fn request(&self, text: &str, source: &str, target: &str, glossary: &GlossaryContext) -> Option<String> {
        let endpoint = format!("{}{}", self.url.trim_end_matches('/'), TRANSLATE_PATH);
        let mut payload = serde_json::json!({
            "text": text, "source": source, "target": target,
        });
        if !glossary.is_empty() {
            let terms: Vec<_> = glossary.top_terms(20).iter().map(|e| {
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
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::Translator;
    use std::io::{Read, Write};
    use std::net::{Shutdown, TcpListener};
    use std::time::Duration;

    /// 起一个单次应答的本机 mock HTTP 服务，返回其 base url（如 http://127.0.0.1:PORT）。
    /// 后台线程 accept 一个连接、读完请求、写回固定 JSON body 后关闭。
    fn spawn_mock_once(response_json: &'static str) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = vec![0u8; 4096];
                let _ = stream.read(&mut buf); // 读掉请求（含 header+body）
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
        // 绑定再立即释放，得到一个大概率无人监听的端口 → 连接被拒
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);
        format!("http://127.0.0.1:{}", port)
    }

    fn e(url: String) -> SidecarTranslator {
        SidecarTranslator::with_url("argos", url, Duration::from_millis(1500))
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
}
