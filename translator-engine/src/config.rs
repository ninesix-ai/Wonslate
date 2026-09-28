// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Configuration management (embedded defaults + environment overrides,
//! zero external dependencies).

use std::path::PathBuf;
use std::sync::OnceLock;

// ---- Config struct ----------------------------------------------------------

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

// ---- Global singleton --------------------------------------------------------

static CONFIG: OnceLock<Config> = OnceLock::new();

/// Install the process-global config (the first call wins).
///
/// The effective config is the built-in defaults overlaid with `LT_*` /
/// `WONSLATE_*` environment overrides; there is no config file. Routing rules
/// live in code (`router.rs`), and `tt_init`'s `config_json` is likewise not
/// consumed yet. The previous signature took a path that was never read, which
/// advertised a file-based config that does not exist.
pub fn load() {
    let cfg = from_env_with(&|k| std::env::var(k).ok());
    let _ = CONFIG.set(cfg);
    lt_info!("[config] loaded");
}

/// Environment override lookup with the project's prefix chain:
/// legacy `LT_*` wins (backward compatibility), then the brand spelling
/// `WONSLATE_*`, then the bare legacy name (e.g. `OLLAMA_URL`) for
/// out-of-the-box third-party compatibility.
pub fn first_env(lookup: &dyn Fn(&str) -> Option<String>, candidates: &[&str]) -> Option<String> {
    candidates.iter().find_map(|k| lookup(k))
}

/// Pure, testable: overlay environment overrides (resolved via `lookup`) on top of the
/// default config. Prefix chain per key: `LT_*` > `WONSLATE_*` > bare legacy name.
fn from_env_with(lookup: &dyn Fn(&str) -> Option<String>) -> Config {
    let mut cfg = Config::default();
    cfg.ollama_url = first_env(lookup, &["LT_OLLAMA_URL", "WONSLATE_OLLAMA_URL", "OLLAMA_URL"])
        .unwrap_or_else(|| cfg.ollama_url.clone());
    cfg.ollama_model = first_env(lookup, &["LT_OLLAMA_MODEL", "WONSLATE_OLLAMA_MODEL", "OLLAMA_MODEL"])
        .unwrap_or_else(|| cfg.ollama_model.clone());
    if let Some(n) = first_env(lookup, &["LT_OLLAMA_TIMEOUT_MS", "WONSLATE_OLLAMA_TIMEOUT_MS", "OLLAMA_TIMEOUT_MS"])
        .and_then(|v| v.trim().parse::<u64>().ok())
    {
        cfg.ollama_timeout_ms = n;
    }
    cfg
}

pub fn get() -> &'static Config {
    CONFIG.get_or_init(Config::default)
}

// ---- DATA_DIR resolution across platforms (std only, zero deps) -------------

pub fn data_dir() -> PathBuf {
    data_dir_from(&|k| std::env::var(k).ok())
}

/// Pure, testable: resolve the data directory. `LT_DATA_DIR` (legacy) and
/// `WONSLATE_DATA_DIR` (brand) win, legacy first for backward compatibility;
/// otherwise a per-OS user data location is used, falling back to `./lt-data`
/// (kept as-is so existing local data directories continue to be found) when
/// none is set.
fn data_dir_from(lookup: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(v) = first_env(lookup, &["LT_DATA_DIR", "WONSLATE_DATA_DIR"]) {
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

    #[test]
    fn wonslate_prefixed_ollama_overrides_win_over_bare() {
        // Brand migration: WONSLATE_* is the documented spelling and beats the
        // legacy bare OLLAMA_* variables.
        let cfg = from_env_with(&only(&[
            ("WONSLATE_OLLAMA_URL", "http://wonslate:11434"),
            ("OLLAMA_URL", "http://bare:11434"),
            ("WONSLATE_OLLAMA_MODEL", "wonslate-model:1"),
            ("OLLAMA_MODEL", "bare-model:1"),
            ("WONSLATE_OLLAMA_TIMEOUT_MS", "7000"),
        ]));
        assert_eq!(cfg.ollama_url, "http://wonslate:11434");
        assert_eq!(cfg.ollama_model, "wonslate-model:1");
        assert_eq!(cfg.ollama_timeout_ms, 7000);
    }

    #[test]
    fn lt_prefixed_ollama_still_wins_for_backward_compat() {
        // Legacy LT_* spellings keep working during the transition and take
        // precedence over WONSLATE_* (documented compat rule).
        let cfg = from_env_with(&only(&[
            ("LT_OLLAMA_URL", "http://lt:11434"),
            ("WONSLATE_OLLAMA_URL", "http://wonslate:11434"),
        ]));
        assert_eq!(cfg.ollama_url, "http://lt:11434");
    }

    #[test]
    fn wonslate_data_dir_env_var_wins() {
        let p = data_dir_from(&only(&[("WONSLATE_DATA_DIR", "/tmp/wonslate-brand-data")]));
        assert_eq!(p, PathBuf::from("/tmp/wonslate-brand-data"));
    }

    #[test]
    fn lt_data_dir_wins_over_wonslate_for_compat() {
        let p = data_dir_from(&only(&[
            ("LT_DATA_DIR", "/tmp/legacy"),
            ("WONSLATE_DATA_DIR", "/tmp/new"),
        ]));
        assert_eq!(p, PathBuf::from("/tmp/legacy"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_data_dir_uses_localappdata() {
        let p = data_dir_from(&only(&[("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")]));
        assert_eq!(p, PathBuf::from("C:\\Users\\x\\AppData\\Local").join("Wonslate"));
    }
}
