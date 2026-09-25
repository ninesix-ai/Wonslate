// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 配置管理（内嵌默认值 + 环境变量覆盖，零外部依赖）

use std::path::PathBuf;
use std::sync::OnceLock;

// ── 配置结构体 ──────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub tm_enabled: bool,
    pub tm_cache_size: usize,
    pub tm_warmup_n: usize,
    pub tm_min_quality: f32,
    pub glossary_enabled: bool,
    pub glossary_max_terms: usize,
    pub distill_enabled: bool,
    pub distill_min_conf: f32,
    pub ollama_url: String,
    pub ollama_model: String,
    pub ollama_timeout_ms: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            tm_enabled: true,
            tm_cache_size: 1000,
            tm_warmup_n: 500,
            tm_min_quality: 0.70,
            glossary_enabled: true,
            glossary_max_terms: 20,
            distill_enabled: true,
            distill_min_conf: 0.90,
            ollama_url: "http://127.0.0.1:11434".into(),
            ollama_model: "qwen3:8b".into(),
            ollama_timeout_ms: 120_000,
        }
    }
}

// ── 全局单例 ────────────────────────────────────────────────────────

static CONFIG: OnceLock<Config> = OnceLock::new();

pub fn load(_config_path: &std::path::Path) {
    // Build the effective config from defaults + environment overrides, then install
    // it as the process-global singleton (the first call wins). The path argument is
    // reserved for a future file-based config and is intentionally not read yet.
    let cfg = from_env_with(&|k| std::env::var(k).ok());
    let _ = CONFIG.set(cfg);
    lt_info!("[config] loaded");
}

/// Pure, testable: overlay environment overrides (resolved via `lookup`) on top of the
/// default config. Kept compatible with the legacy ollama.rs: both `LT_`-prefixed and
/// bare `OLLAMA_` variables are honored, and `LT_` takes precedence.
fn from_env_with(lookup: &dyn Fn(&str) -> Option<String>) -> Config {
    let mut cfg = Config::default();
    cfg.ollama_url = lookup("LT_OLLAMA_URL")
        .or_else(|| lookup("OLLAMA_URL"))
        .unwrap_or_else(|| cfg.ollama_url.clone());
    cfg.ollama_model = lookup("LT_OLLAMA_MODEL")
        .or_else(|| lookup("OLLAMA_MODEL"))
        .unwrap_or_else(|| cfg.ollama_model.clone());
    if let Some(n) = lookup("LT_OLLAMA_TIMEOUT_MS")
        .or_else(|| lookup("OLLAMA_TIMEOUT_MS"))
        .and_then(|v| v.trim().parse::<u64>().ok())
    {
        cfg.ollama_timeout_ms = n;
    }
    cfg
}

pub fn get() -> &'static Config {
    CONFIG.get_or_init(Config::default)
}

// ── DATA_DIR 跨平台解析（std，零依赖）─────────────────────────────

pub fn data_dir() -> PathBuf {
    data_dir_from(&|k| std::env::var(k).ok())
}

/// Pure, testable: resolve the data directory. `LT_DATA_DIR` always wins; otherwise a
/// per-OS user data location is used, falling back to `./lt-data` when none is set.
fn data_dir_from(lookup: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(v) = lookup("LT_DATA_DIR") {
        return PathBuf::from(v);
    }
    #[cfg(target_os = "windows")]
    {
        lookup("LOCALAPPDATA")
            .map(|p| PathBuf::from(p).join("Wonslate"))
            .unwrap_or_else(|| PathBuf::from("./lt-data"))
    }
    #[cfg(target_os = "linux")]
    {
        lookup("XDG_DATA_HOME")
            .or_else(|| lookup("HOME").map(|h| format!("{h}/.local/share")))
            .map(|p| PathBuf::from(p).join("wonslate"))
            .unwrap_or_else(|| PathBuf::from("./lt-data"))
    }
    #[cfg(target_os = "macos")]
    {
        lookup("HOME")
            .map(|h| PathBuf::from(h)
                .join("Library/Application Support/Wonslate"))
            .unwrap_or_else(|| PathBuf::from("./lt-data"))
    }
    #[cfg(not(any(target_os = "windows", target_os = "linux", target_os = "macos")))]
    { PathBuf::from("./lt-data") }
}

pub fn routes_yaml_path() -> PathBuf {
    data_dir().join("config").join("routes.yaml")
}

pub fn ensure_dirs() -> Result<(), crate::error::EngineError> {
    let d = data_dir();
    std::fs::create_dir_all(d.join("data")).ok();
    std::fs::create_dir_all(d.join("config")).ok();
    std::fs::create_dir_all(d.join("models")).ok();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Fake env: a closure that answers lookups from a fixed key/value list.
    fn empty() -> impl Fn(&str) -> Option<String> {
        |_| None
    }
    fn only<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k| pairs.iter().find(|(key, _)| *key == k).map(|(_, v)| v.to_string())
    }

    #[test]
    fn defaults_match_documented_values() {
        let c = Config::default();
        assert!(c.tm_enabled);
        assert_eq!(c.tm_cache_size, 1000);
        assert_eq!(c.tm_warmup_n, 500);
        assert!((c.tm_min_quality - 0.70).abs() < 1e-6);
        assert!(c.glossary_enabled);
        assert_eq!(c.glossary_max_terms, 20);
        assert!(c.distill_enabled);
        assert!((c.distill_min_conf - 0.90).abs() < 1e-6);
        assert_eq!(c.ollama_url, "http://127.0.0.1:11434");
        assert_eq!(c.ollama_model, "qwen3:8b");
        assert_eq!(c.ollama_timeout_ms, 120_000);
    }

    #[test]
    fn no_env_yields_defaults() {
        assert_eq!(from_env_with(&empty()), Config::default());
    }

    #[test]
    fn lt_prefixed_ollama_overrides_win() {
        let cfg = from_env_with(&only(&[
            ("LT_OLLAMA_URL", "http://lt:11434"),
            ("OLLAMA_URL", "http://bare:11434"),
            ("LT_OLLAMA_MODEL", "lt-model:1"),
            ("OLLAMA_MODEL", "bare-model:1"),
        ]));
        assert_eq!(cfg.ollama_url, "http://lt:11434");
        assert_eq!(cfg.ollama_model, "lt-model:1");
    }

    #[test]
    fn bare_ollama_used_when_no_lt_prefix() {
        let cfg = from_env_with(&only(&[("OLLAMA_URL", "http://bare:11434")]));
        assert_eq!(cfg.ollama_url, "http://bare:11434");
    }

    #[test]
    fn timeout_parsed_from_env() {
        let cfg = from_env_with(&only(&[("LT_OLLAMA_TIMEOUT_MS", "5000")]));
        assert_eq!(cfg.ollama_timeout_ms, 5000);
    }

    #[test]
    fn invalid_timeout_keeps_default() {
        let cfg = from_env_with(&only(&[("OLLAMA_TIMEOUT_MS", "not-a-number")]));
        assert_eq!(cfg.ollama_timeout_ms, Config::default().ollama_timeout_ms);
    }

    #[test]
    fn timeout_whitespace_is_trimmed() {
        let cfg = from_env_with(&only(&[("LT_OLLAMA_TIMEOUT_MS", "  8000  ")]));
        assert_eq!(cfg.ollama_timeout_ms, 8000);
    }

    #[test]
    fn data_dir_env_var_wins() {
        let p = data_dir_from(&only(&[("LT_DATA_DIR", "/tmp/wonslate-data")]));
        assert_eq!(p, PathBuf::from("/tmp/wonslate-data"));
    }

    #[test]
    fn data_dir_falls_back_when_no_env() {
        // No env keys set -> OS-independent fallback.
        assert_eq!(data_dir_from(&empty()), PathBuf::from("./lt-data"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_data_dir_uses_localappdata() {
        let p = data_dir_from(&only(&[("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")]));
        assert_eq!(p, PathBuf::from("C:\\Users\\x\\AppData\\Local").join("Wonslate"));
    }
}
