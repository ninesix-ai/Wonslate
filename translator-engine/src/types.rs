// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 跨模块共享数据结构（零外部依赖，手动 JSON 转换）

// ── 翻译模式 ────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub enum TranslationMode {
    Realtime,
    Full,
}

impl TranslationMode {
    pub fn from_str(s: &str) -> Self {
        if s.eq_ignore_ascii_case("realtime") { Self::Realtime } else { Self::Full }
    }
    pub fn as_str(&self) -> &'static str {
        match self { Self::Realtime => "realtime", Self::Full => "full" }
    }
}

// ── 翻译来源 ────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub enum TranslationSource {
    TmHit,
    Local,
    AiUpgraded,
    Fallback,
}

impl TranslationSource {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::TmHit      => "tm_hit",
            Self::Local      => "local",
            Self::AiUpgraded => "ai_upgraded",
            Self::Fallback   => "fallback",
        }
    }
}

// ── 翻译请求 ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct TranslateRequest {
    pub engine_id: String,
    pub input: String,
    pub source_lang: String,
    pub target_lang: String,
    pub mode: TranslationMode,
    pub privacy: bool,
    pub domain: String,
    pub use_tm: bool,
}

impl TranslateRequest {
    /// 从 JSON 字符串解析（用于 FFI 入口）
    pub fn from_json(json: &str) -> Result<Self, String> {
        let v: serde_json::Value = serde_json::from_str(json)
            .map_err(|e| format!("JSON parse error: {}", e))?;
        let get = |key: &str| -> String {
            v.get(key).and_then(|x| x.as_str()).unwrap_or("").to_string()
        };
        let get_bool = |key: &str, def: bool| -> bool {
            v.get(key).and_then(|x| x.as_bool()).unwrap_or(def)
        };
        Ok(Self {
            engine_id:   get("engine_id"),
            input:       get("input"),
            source_lang: { let s = get("source_lang"); if s.is_empty() { "auto".into() } else { s } },
            target_lang: get("target_lang"),
            mode:        TranslationMode::from_str(&get("mode")),
            privacy:     get_bool("privacy", false),
            domain:      get("domain"),
            use_tm:      get_bool("use_tm", true),
        })
    }
}

// ── 翻译响应 ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct TranslateResponse {
    pub ok: bool,
    pub engine: String,
    pub source_lang: String,
    pub target_lang: String,
    pub input: String,
    pub output: Option<String>,
    pub source: TranslationSource,
    pub latency_ms: u64,
    pub confidence: f32,
    pub error: Option<String>,
    pub message: Option<String>,
}

impl TranslateResponse {
    pub fn to_json(&self) -> String {
        let mut obj = serde_json::json!({
            "ok":           self.ok,
            "engine":       self.engine,
            "source_lang":  self.source_lang,
            "target_lang":  self.target_lang,
            "input":        self.input,
            "source":       self.source.as_str(),
            "latency_ms":   self.latency_ms,
            "confidence":   self.confidence,
        });
        if let Some(ref o) = self.output   { obj["output"]   = serde_json::json!(o); }
        if let Some(ref e) = self.error    { obj["error"]    = serde_json::json!(e); }
        if let Some(ref m) = self.message  { obj["message"]  = serde_json::json!(m); }
        obj.to_string()
    }
}

// ── TM 条目 ─────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct TmEntry {
    pub source_text: String,
    pub source_lang: String,
    pub target_text: String,
    pub target_lang: String,
    pub engine: String,
    pub quality: f32,
    pub hit_count: u32,
    pub domain: String,
}

impl TmEntry {
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "source_text": self.source_text,
            "source_lang": self.source_lang,
            "target_text": self.target_text,
            "target_lang": self.target_lang,
            "engine":      self.engine,
            "quality":     self.quality,
            "hit_count":   self.hit_count,
            "domain":      self.domain,
        })
    }

    pub fn from_json(v: &serde_json::Value) -> Self {
        let s = |key: &str| v.get(key).and_then(|x| x.as_str()).unwrap_or("").to_string();
        Self {
            source_text: s("source_text"),
            source_lang: s("source_lang"),
            target_text: s("target_text"),
            target_lang: s("target_lang"),
            engine:      s("engine"),
            quality:     v.get("quality").and_then(|x| x.as_f64()).unwrap_or(0.0) as f32,
            hit_count:   v.get("hit_count").and_then(|x| x.as_u64()).unwrap_or(1) as u32,
            domain:      s("domain"),
        }
    }
}

// ── 术语表条目 ──────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct GlossaryEntry {
    pub source_term: String,
    pub source_lang: String,
    pub target_term: String,
    pub target_lang: String,
    pub confidence: f32,
    pub frequency: u32,
    pub domain: String,
}

impl GlossaryEntry {
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "source_term": self.source_term,
            "source_lang": self.source_lang,
            "target_term": self.target_term,
            "target_lang": self.target_lang,
            "confidence":  self.confidence,
            "frequency":   self.frequency,
            "domain":      self.domain,
        })
    }

    pub fn from_json(v: &serde_json::Value) -> Self {
        let s = |key: &str| v.get(key).and_then(|x| x.as_str()).unwrap_or("").to_string();
        Self {
            source_term: s("source_term"),
            source_lang: s("source_lang"),
            target_term: s("target_term"),
            target_lang: s("target_lang"),
            confidence:  v.get("confidence").and_then(|x| x.as_f64()).unwrap_or(0.9) as f32,
            frequency:   v.get("frequency").and_then(|x| x.as_u64()).unwrap_or(1) as u32,
            domain:      s("domain"),
        }
    }
}

// ── 蒸馏任务 ────────────────────────────────────────────────────────

pub struct DistillTask {
    pub source_text: String,
    pub source_lang: String,
    pub target_text: String,
    pub target_lang: String,
    pub domain: String,
}

// ── 单元测试 ────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── TranslationMode ──────────────────────────────────────────

    #[test]
    fn mode_from_str_recognizes_realtime() {
        assert_eq!(TranslationMode::from_str("realtime"), TranslationMode::Realtime);
        assert_eq!(TranslationMode::from_str("REALTIME"), TranslationMode::Realtime);
        assert_eq!(TranslationMode::from_str("RealTime"), TranslationMode::Realtime);
    }

    #[test]
    fn mode_from_str_defaults_to_full() {
        assert_eq!(TranslationMode::from_str("full"),   TranslationMode::Full);
        assert_eq!(TranslationMode::from_str(""),       TranslationMode::Full);
        assert_eq!(TranslationMode::from_str("garbage"),TranslationMode::Full);
    }

    #[test]
    fn mode_as_str_round_trip() {
        assert_eq!(TranslationMode::Realtime.as_str(), "realtime");
        assert_eq!(TranslationMode::Full.as_str(),     "full");
    }

    // ── TranslateRequest.from_json ───────────────────────────────

    #[test]
    fn req_from_json_minimal() {
        // 最小请求：只给必填字段，其它走默认
        let r = TranslateRequest::from_json(
            r#"{"input":"你好","target_lang":"en"}"#
        ).unwrap();
        assert_eq!(r.input, "你好");
        assert_eq!(r.target_lang, "en");
        assert_eq!(r.source_lang, "auto");        // 默认
        assert_eq!(r.mode, TranslationMode::Full); // 默认
        assert!(!r.privacy);                        // 默认
        assert!(r.use_tm);                          // 默认 true
    }

    #[test]
    fn req_from_json_full() {
        let json = r#"{
            "engine_id": "ollama",
            "input": "hello",
            "source_lang": "en",
            "target_lang": "zh",
            "mode": "realtime",
            "privacy": true,
            "domain": "tech",
            "use_tm": false
        }"#;
        let r = TranslateRequest::from_json(json).unwrap();
        assert_eq!(r.engine_id, "ollama");
        assert_eq!(r.mode, TranslationMode::Realtime);
        assert!(r.privacy);
        assert_eq!(r.domain, "tech");
        assert!(!r.use_tm);
    }

    #[test]
    fn req_from_json_rejects_bad_json() {
        assert!(TranslateRequest::from_json("not json").is_err());
        assert!(TranslateRequest::from_json("").is_err());
        assert!(TranslateRequest::from_json("{").is_err());
    }

    #[test]
    fn req_from_json_empty_source_defaults_auto() {
        let r = TranslateRequest::from_json(
            r#"{"input":"x","source_lang":"","target_lang":"en"}"#
        ).unwrap();
        assert_eq!(r.source_lang, "auto");
    }

    // ── TranslateResponse.to_json ────────────────────────────────

    #[test]
    fn resp_to_json_contains_all_fields() {
        let resp = TranslateResponse {
            ok: true,
            engine: "ollama-qwen".into(),
            source_lang: "zh".into(),
            target_lang: "en".into(),
            input: "你好".into(),
            output: Some("Hello".into()),
            source: TranslationSource::TmHit,
            latency_ms: 42,
            confidence: 0.95,
            error: None,
            message: None,
        };
        let v: serde_json::Value = serde_json::from_str(&resp.to_json()).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["engine"], "ollama-qwen");
        assert_eq!(v["output"], "Hello");
        assert_eq!(v["source"], "tm_hit");
        assert_eq!(v["latency_ms"], 42);
        // None 字段应被跳过（不出现在 JSON 里）
        assert!(v.get("error").is_none());
        assert!(v.get("message").is_none());
    }

    #[test]
    fn resp_to_json_includes_error_when_present() {
        let resp = TranslateResponse {
            ok: false, engine: "unknown".into(),
            source_lang: "zh".into(), target_lang: "en".into(),
            input: "x".into(), output: None,
            source: TranslationSource::Fallback,
            latency_ms: 0, confidence: 0.0,
            error: Some("NO_RESULT".into()),
            message: Some("engine returned nothing".into()),
        };
        let v: serde_json::Value = serde_json::from_str(&resp.to_json()).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"], "NO_RESULT");
        assert_eq!(v["message"], "engine returned nothing");
    }

    // ── TmEntry JSON 往返 ────────────────────────────────────────

    #[test]
    fn tm_entry_json_roundtrip() {
        let original = TmEntry {
            source_text: "深度学习".into(),
            source_lang: "zh".into(),
            target_text: "deep learning".into(),
            target_lang: "en".into(),
            engine: "ollama-qwen".into(),
            quality: 0.95,
            hit_count: 7,
            domain: "tech".into(),
        };
        let v = original.to_json();
        let parsed = TmEntry::from_json(&v);
        assert_eq!(parsed.source_text, original.source_text);
        assert_eq!(parsed.target_text, original.target_text);
        assert_eq!(parsed.engine, original.engine);
        assert!((parsed.quality - 0.95).abs() < 1e-6);
        assert_eq!(parsed.hit_count, 7);
        assert_eq!(parsed.domain, "tech");
    }

    #[test]
    fn tm_entry_from_json_handles_missing_optional_fields() {
        let v = serde_json::json!({
            "source_text": "x", "source_lang": "zh",
            "target_text": "y", "target_lang": "en",
            "engine": "demo",
        });
        let e = TmEntry::from_json(&v);
        assert_eq!(e.quality, 0.0);
        assert_eq!(e.hit_count, 1);       // 默认 1
        assert_eq!(e.domain, "");
    }

    // ── GlossaryEntry JSON 往返 ──────────────────────────────────

    #[test]
    fn glossary_entry_json_roundtrip() {
        let original = GlossaryEntry {
            source_term: "神经网络".into(),
            source_lang: "zh".into(),
            target_term: "neural network".into(),
            target_lang: "en".into(),
            confidence: 0.92,
            frequency: 15,
            domain: String::new(),
        };
        let v = original.to_json();
        let parsed = GlossaryEntry::from_json(&v);
        assert_eq!(parsed.source_term, original.source_term);
        assert_eq!(parsed.target_term, original.target_term);
        assert!((parsed.confidence - 0.92).abs() < 1e-6);
        assert_eq!(parsed.frequency, 15);
    }

    // ── TranslationSource.as_str ─────────────────────────────────

    #[test]
    fn all_translation_sources_have_stable_strings() {
        // 这些字符串是跨语言契约（.NET / Python 依赖），改动会破坏兼容
        assert_eq!(TranslationSource::TmHit.as_str(),      "tm_hit");
        assert_eq!(TranslationSource::Local.as_str(),      "local");
        assert_eq!(TranslationSource::AiUpgraded.as_str(), "ai_upgraded");
        assert_eq!(TranslationSource::Fallback.as_str(),   "fallback");
    }
}
