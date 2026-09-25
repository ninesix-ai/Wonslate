// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 统一错误类型（所有模块共用）

use std::fmt;

#[derive(Debug)]
pub enum EngineError {
    InvalidInput(String),
    UnknownEngine(String),
    EngineFailed { engine: String, reason: String },
    NoResult,
    TmError(String),
    ConfigError(String),
    Panic,
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidInput(msg) => write!(f, "INVALID_INPUT: {}", msg),
            Self::UnknownEngine(id) => write!(f, "UNKNOWN_ENGINE: {}", id),
            Self::EngineFailed { engine, reason } => {
                write!(f, "ENGINE_FAILED[{}]: {}", engine, reason)
            }
            Self::NoResult => write!(f, "NO_RESULT"),
            Self::TmError(msg) => write!(f, "TM_ERROR: {}", msg),
            Self::ConfigError(msg) => write!(f, "CONFIG_ERROR: {}", msg),
            Self::Panic => write!(f, "PANIC: engine panicked"),
        }
    }
}

impl std::error::Error for EngineError {}

impl EngineError {
    /// 转换为 JSON（供 FFI 返回错误时使用）
    pub fn to_json(&self) -> serde_json::Value {
        let code = match self {
            Self::InvalidInput(_) => "INVALID_INPUT",
            Self::UnknownEngine(_) => "UNKNOWN_ENGINE",
            Self::EngineFailed { .. } => "ENGINE_FAILED",
            Self::NoResult => "NO_RESULT",
            Self::TmError(_) => "TM_ERROR",
            Self::ConfigError(_) => "CONFIG_ERROR",
            Self::Panic => "PANIC",
        };
        serde_json::json!({
            "ok": false,
            "error": code,
            "message": self.to_string(),
        })
    }
}

impl From<std::io::Error> for EngineError {
    fn from(e: std::io::Error) -> Self {
        Self::TmError(e.to_string())
    }
}

impl From<serde_json::Error> for EngineError {
    fn from(e: serde_json::Error) -> Self {
        Self::TmError(e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_maps_each_variant_to_prefixed_code() {
        assert!(EngineError::InvalidInput("x".into()).to_string().starts_with("INVALID_INPUT"));
        assert!(EngineError::UnknownEngine("bad".into()).to_string().starts_with("UNKNOWN_ENGINE"));
        assert!(EngineError::EngineFailed{engine:"o".into(),reason:"r".into()}
            .to_string().starts_with("ENGINE_FAILED"));
        assert_eq!(EngineError::NoResult.to_string(), "NO_RESULT");
        assert!(EngineError::TmError("db".into()).to_string().starts_with("TM_ERROR"));
        assert!(EngineError::ConfigError("yaml".into()).to_string().starts_with("CONFIG_ERROR"));
        assert!(EngineError::Panic.to_string().starts_with("PANIC"));
    }

    #[test]
    fn to_json_has_ok_false_and_matching_code() {
        let v = EngineError::InvalidInput("bad utf".into()).to_json();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"], "INVALID_INPUT");
        assert!(v["message"].as_str().unwrap().contains("bad utf"));

        let v = EngineError::EngineFailed{engine:"ollama".into(), reason:"timeout".into()}.to_json();
        assert_eq!(v["error"], "ENGINE_FAILED");
        assert!(v["message"].as_str().unwrap().contains("ollama"));
        assert!(v["message"].as_str().unwrap().contains("timeout"));
    }

    #[test]
    fn io_error_converts_to_tm_error() {
        let e: EngineError = std::io::Error::other("boom").into();
        matches!(e, EngineError::TmError(_));
        assert!(e.to_string().contains("boom"));
    }

    #[test]
    fn serde_json_error_converts_to_tm_error() {
        let bad = serde_json::from_str::<serde_json::Value>("not json");
        let e: EngineError = bad.unwrap_err().into();
        assert!(e.to_string().starts_with("TM_ERROR"));
    }
}
