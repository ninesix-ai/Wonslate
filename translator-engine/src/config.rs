// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 配置管理（内嵌默认值 + 环境变量覆盖，零外部依赖）

use std::path::PathBuf;
use std::sync::OnceLock;

// ── 配置结构体 ──────────────────────────────────────────────────────

#[derive(Debug, Clone)]
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
    let mut cfg = Config::default();
    // 环境变量覆盖（与旧 ollama.rs 保持兼容，兼容 LT_ 和旧 OLLAMA_ 两种前缀）
    cfg.ollama_url = env_or("LT_OLLAMA_URL",
                     &env_or("OLLAMA_URL", &cfg.ollama_url));
    cfg.ollama_model = env_or("LT_OLLAMA_MODEL",
                       &env_or("OLLAMA_MODEL", &cfg.ollama_model));
    if let Ok(v) = std::env::var("LT_OLLAMA_TIMEOUT_MS")
        .or_else(|_| std::env::var("OLLAMA_TIMEOUT_MS")) {
        if let Ok(n) = v.trim().parse::<u64>() { cfg.ollama_timeout_ms = n; }
    }
    let _ = CONFIG.set(cfg);
    lt_info!("[config] loaded");
}

pub fn get() -> &'static Config {
    CONFIG.get_or_init(Config::default)
}

fn env_or(key: &str, def: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| def.to_string())
}

// ── DATA_DIR 跨平台解析（std，零依赖）─────────────────────────────

pub fn data_dir() -> PathBuf {
    if let Ok(v) = std::env::var("LT_DATA_DIR") {
        return PathBuf::from(v);
    }
    #[cfg(target_os = "windows")]
    {
        std::env::var("LOCALAPPDATA")
            .map(|p| PathBuf::from(p).join("Wonslate"))
            .unwrap_or_else(|_| PathBuf::from("./lt-data"))
    }
    #[cfg(target_os = "linux")]
    {
        std::env::var("XDG_DATA_HOME")
            .or_else(|_| std::env::var("HOME").map(|h| format!("{}/.local/share", h)))
            .map(|p| PathBuf::from(p).join("wonslate"))
            .unwrap_or_else(|_| PathBuf::from("./lt-data"))
    }
    #[cfg(target_os = "macos")]
    {
        std::env::var("HOME")
            .map(|h| PathBuf::from(h)
                .join("Library/Application Support/Wonslate"))
            .unwrap_or_else(|_| PathBuf::from("./lt-data"))
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
