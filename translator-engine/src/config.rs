// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Configuration management (embedded defaults + environment overrides,
//! zero external dependencies).

use std::path::{Path, PathBuf};
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
    /// Score an extracted term pair needs to be persisted as a glossary term (N-08).
    /// Consumed by the distill worker; the extractor stamps the real per-candidate score.
    pub distill_min_conf: f32,
    pub ollama_url: String,
    pub ollama_model: String,
    pub ollama_timeout_ms: u64,
    /// Routing table (N-11). Was hardcoded in router.rs; now data so a deployment can
    /// retune it without a rebuild (file + env), which is what the planned routes.yaml
    /// described. Privacy is deliberately NOT part of it: that clamp is an invariant.
    pub routing: Routing,
}

/// When the full (high-quality) mode is allowed to hand work to the AI engine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpgradePolicy {
    /// Upgrade whenever an AI engine is configured and reachable, whatever the local
    /// result scored. This is what the mode name promises: "full" means quality, and the
    /// speed-conscious user picks realtime instead. Translation-memory hits still
    /// short-circuit before this decision, so repeated content stays free.
    Always,
    /// Upgrade only when the local result scored below the rule's threshold. This is
    /// the local-first behaviour: the AI engine is a fallback for weak results.
    LowConfidence,
}

impl UpgradePolicy {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Always => "always",
            Self::LowConfidence => "low_confidence",
        }
    }

    fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_lowercase().as_str() {
            "always" => Some(Self::Always),
            "low_confidence" | "low-confidence" | "lowconfidence" => Some(Self::LowConfidence),
            _ => None,
        }
    }
}

/// One row of the routing table: what to try locally, what to upgrade to, and when.
#[derive(Debug, Clone, PartialEq)]
pub struct RouteRule {
    pub local_engine: String,
    pub upgrade_engine: String,
    pub upgrade_threshold: f32,
    pub upgrade_policy: UpgradePolicy,
    /// Minimum quality a translation-memory entry needs to be *served* under this rule.
    ///
    /// Distinct from `tm_min_quality`, which decides what may be *stored*: this decides
    /// what may be reused. With `0.0` any cached result is served, so the quality a
    /// request sees depends on whether the AI engine happened to be available the first
    /// time that sentence was translated (a locally-cached entry scores 0.88, an
    /// AI-written one 0.95). Raising it to the AI quality makes the mode's quality
    /// promise hold on cache hits too, at the cost of re-translating locally-cached
    /// content once. See docs/17 D15.
    pub tm_quality_floor: f32,
}

/// The user-facing quality/cost preference, which maps onto the two knobs above.
///
/// One semantic choice instead of two thresholds: a settings UI should ask "quality or
/// cost" and let the engine translate that into a policy and a floor.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QualityPreference {
    /// Ask the AI whenever it is reachable and refuse to serve locally-cached results.
    QualityFirst,
    /// Prefer the local result and serve whatever is cached. Cheapest, fastest.
    CostFirst,
}

impl QualityPreference {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::QualityFirst => "quality_first",
            Self::CostFirst => "cost_first",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_lowercase().as_str() {
            "quality_first" | "quality-first" | "quality" => Some(Self::QualityFirst),
            "cost_first" | "cost-first" | "cost" => Some(Self::CostFirst),
            _ => None,
        }
    }

    /// Apply the preference to the full-mode rule (the mode that has a quality promise
    /// to keep; realtime is defined by its latency budget, not by quality).
    pub fn apply(self, full: &mut RouteRule) {
        match self {
            Self::QualityFirst => {
                full.upgrade_policy = UpgradePolicy::Always;
                full.tm_quality_floor = crate::confidence::AI_UPGRADE_CONFIDENCE;
            }
            Self::CostFirst => {
                full.upgrade_policy = UpgradePolicy::LowConfidence;
                full.tm_quality_floor = 0.0;
            }
        }
    }
}

/// Full routing table: the language set plus one rule per mode.
#[derive(Debug, Clone, PartialEq)]
pub struct Routing {
    pub common_pairs: Vec<String>,
    pub realtime: RouteRule,
    pub full: RouteRule,
    pub rare_pair: RouteRule,
}

impl Default for Routing {
    fn default() -> Self {
        Self {
            // The 11 high-resource languages from the router's original hardcoded list.
            common_pairs: ["zh", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar"]
                .iter().map(|s| s.to_string()).collect(),
            realtime: RouteRule {
                local_engine: "argos".into(),
                upgrade_engine: String::new(),   // realtime has a latency budget: never upgrade
                upgrade_threshold: 0.0,
                upgrade_policy: UpgradePolicy::LowConfidence,
                tm_quality_floor: 0.0,
            },
            full: RouteRule {
                // MADLAD owns the full-mode local slot: one checkpoint covers
                // every common pair (argos only has en<->zh packages), so the
                // eleven-language picker works end to end offline, and the
                // quality floor under it is higher than word-level demo when
                // the ollama upgrade is unreachable. Realtime keeps argos.
                local_engine: "madlad".into(),
                upgrade_engine: "ollama".into(),
                upgrade_threshold: 0.85,
                // D14: the mode is called high-quality; honouring that is the default, and the
                // local-first behaviour is opt-in via the config.
                upgrade_policy: UpgradePolicy::Always,
                // D15: the floor ships at 0.0, which preserves the behaviour that existed
                // before the floor was configurable. Raising it to AI quality is the
                // "quality first" preference; changing a shipped default silently would
                // raise AI cost for everyone already using this mode, so it stays opt-in
                // until that is an explicit decision.
                tm_quality_floor: 0.0,
            },
            rare_pair: RouteRule {
                local_engine: "madlad".into(),
                upgrade_engine: "ollama".into(),
                upgrade_threshold: 0.80,
                upgrade_policy: UpgradePolicy::LowConfidence,
                tm_quality_floor: 0.0,
            },
        }
    }
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
            // N-08: the extractor now stamps a real per-candidate score, so the bar can
            // sit below 1.0 and still discriminate. 0.80 admits a pair whose aligned
            // span length is within one word of the ratio estimate and whose edges are
            // free of function words; the old 0.90 was a constant stamp that admitted
            // every candidate including misaligned ones.
            distill_min_conf: 0.80,
            ollama_url: "http://127.0.0.1:11434".into(),
            ollama_model: "qwen3:8b".into(),
            ollama_timeout_ms: 120_000,
            routing: Routing::default(),
        }
    }
}

// ---- Global singleton --------------------------------------------------------

static CONFIG: OnceLock<Config> = OnceLock::new();

/// Install the process-global config (the first call wins).
///
/// Precedence: built-in defaults, then the routing file (`<data_dir>/config/routes.json`,
/// override with `LT_ROUTES_FILE` / `WONSLATE_ROUTES_FILE`), then `LT_*` /
/// `WONSLATE_*` environment overrides, then `tt_init`'s `config_json`.
/// Only the routing table is configurable this way: it is the part deployments
/// actually retune, and it needs no extra dependency since the file is JSON
/// (the planned routes.yaml is withdrawn - see docs/07 §7.2).
pub fn load() {
    load_with(None);
}

/// Keys a caller may set through `tt_init`'s JSON. Anything else is reported back
/// rather than dropped in silence: an embedding app that passes a key this layer does
/// not understand has to be told, or it will believe a setting took effect.
const OVERRIDABLE_KEYS: &[&str] = &["common_pairs", "realtime", "full", "rare_pair"];

/// Install the global config, with the FFI caller's JSON as the highest precedence.
///
/// Precedence: defaults < routes file < environment < `tt_init` JSON. The explicit
/// argument from the embedding application beats ambient configuration, which is the
/// usual convention and the only ordering where `tt_init` means anything.
///
/// Returns the keys that were ignored, for the caller's ack.
pub fn load_with(override_json: Option<&str>) -> Vec<String> {
    let lookup = |k: &str| std::env::var(k).ok();
    let routes = read_routes_file(&lookup);
    let settings = read_json_file(&settings_file(&lookup), "settings");
    let mut cfg = build_config(&lookup, routes.as_ref(), settings.as_ref());

    let mut ignored = Vec::new();
    if let Some(text) = override_json {
        let text = text.trim();
        if !text.is_empty() && text != "{}" {
            match serde_json::from_str::<serde_json::Value>(text) {
                Ok(v) => {
                    if let Some(obj) = v.as_object() {
                        ignored = obj
                            .keys()
                            .filter(|k| !OVERRIDABLE_KEYS.contains(&k.as_str()))
                            .cloned()
                            .collect();
                        cfg.routing = routing_from_json(&v, cfg.routing);
                        if !ignored.is_empty() {
                            lt_warn!("[config] tt_init config_json keys ignored: {:?}", ignored);
                        }
                    } else {
                        ignored.push("<not an object>".to_string());
                    }
                }
                Err(e) => {
                    lt_warn!("[config] tt_init config_json is not valid JSON ({}), ignored", e);
                    ignored.push("<invalid json>".to_string());
                }
            }
        }
    }

    let _ = CONFIG.set(cfg);
    lt_info!("[config] loaded");
    ignored
}

/// Environment override lookup with the project's prefix chain:
/// legacy `LT_*` wins (backward compatibility), then the brand spelling
/// `WONSLATE_*`, then the bare legacy name (e.g. `OLLAMA_URL`) for
/// out-of-the-box third-party compatibility.
pub fn first_env(lookup: &dyn Fn(&str) -> Option<String>, candidates: &[&str]) -> Option<String> {
    candidates.iter().find_map(|k| lookup(k))
}

/// Path of the routing table. `<data_dir>/config/` is already created by `ensure_dirs`.
pub fn routes_file(lookup: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(v) = first_env(lookup, &["LT_ROUTES_FILE", "WONSLATE_ROUTES_FILE"]) {
        return PathBuf::from(v.trim());
    }
    data_dir_from(lookup).join("config").join("routes.json")
}

/// Pure: overlay a parsed routes file onto a base routing table.
///
/// Partial by design - one field can be retuned without restating the table. Invalid
/// values are ignored rather than applied, because a typo that silently rerouted
/// everything would be worse than a typo that does nothing: an unknown engine id would
/// otherwise make `get_engine` fall back to the demo dictionary, and an out-of-range
/// threshold would turn the gate into "always" or "never" without saying so.
pub fn routing_from_json(v: &serde_json::Value, base: Routing) -> Routing {
    let mut routing = base;

    if let Some(list) = v.get("common_pairs").and_then(|x| x.as_array()) {
        let langs: Vec<String> = list
            .iter()
            .filter_map(|x| x.as_str())
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty())
            .collect();
        if !langs.is_empty() {
            routing.common_pairs = langs;
        }
    }

    routing.realtime = rule_from_json(v.get("realtime"), routing.realtime, true);
    routing.full = rule_from_json(v.get("full"), routing.full, false);
    routing.rare_pair = rule_from_json(v.get("rare_pair"), routing.rare_pair, false);
    routing
}

/// One rule's overlay. `no_upgrade` marks a rule whose upgrade engine is intentionally
/// absent (realtime), so an empty string there is a value, not a mistake.
fn rule_from_json(v: Option<&serde_json::Value>, base: RouteRule, no_upgrade: bool) -> RouteRule {
    let mut rule = base;
    let Some(v) = v else { return rule };

    if let Some(id) = v.get("local_engine").and_then(|x| x.as_str()) {
        if crate::engine::is_available(id) {
            rule.local_engine = id.to_string();
        }
    }
    if let Some(id) = v.get("upgrade_engine").and_then(|x| x.as_str()) {
        let id = id.trim();
        if id.is_empty() {
            if no_upgrade {
                rule.upgrade_engine = String::new();
            }
        } else if crate::engine::is_available(id) {
            rule.upgrade_engine = id.to_string();
        }
    }
    if let Some(n) = v.get("upgrade_threshold").and_then(|x| x.as_f64()) {
        let n = n as f32;
        if (0.0..=1.0).contains(&n) {
            rule.upgrade_threshold = n;
        }
    }
    if let Some(p) = v.get("upgrade_policy").and_then(|x| x.as_str()) {
        if let Some(policy) = UpgradePolicy::parse(p) {
            rule.upgrade_policy = policy;
        }
    }
    if let Some(n) = v.get("tm_quality_floor").and_then(|x| x.as_f64()) {
        let n = n as f32;
        if (0.0..=1.0).contains(&n) {
            rule.tm_quality_floor = n;
        }
    }
    rule
}

/// Pure: environment overlay for the routing table (highest precedence).
///
/// The preference is applied before the individual knobs so that a deployment pinning
/// `LT_FULL_UPGRADE_POLICY` still gets exactly what it asked for, whatever preference
/// a user saved in the settings UI. This is the whole reason the environment sits at
/// the top of the chain: support and reproduction need to override what someone clicked.
fn routing_from_env(lookup: &dyn Fn(&str) -> Option<String>, base: Routing) -> Routing {
    let mut routing = base;
    if let Some(pref) = first_env(lookup, &["LT_QUALITY_PREFERENCE", "WONSLATE_QUALITY_PREFERENCE"])
        .and_then(|v| QualityPreference::parse(&v))
    {
        pref.apply(&mut routing.full);
    }
    if let Some(p) = first_env(lookup, &["LT_FULL_UPGRADE_POLICY", "WONSLATE_FULL_UPGRADE_POLICY"])
        .and_then(|v| UpgradePolicy::parse(&v))
    {
        routing.full.upgrade_policy = p;
    }
    if let Some(n) = first_env(lookup, &["LT_FULL_TM_QUALITY_FLOOR", "WONSLATE_FULL_TM_QUALITY_FLOOR"])
        .and_then(|v| v.trim().parse::<f32>().ok())
    {
        if (0.0..=1.0).contains(&n) {
            routing.full.tm_quality_floor = n;
        }
    }
    if let Some(n) = first_env(lookup, &["LT_FULL_UPGRADE_THRESHOLD", "WONSLATE_FULL_UPGRADE_THRESHOLD"])
        .and_then(|v| v.trim().parse::<f32>().ok())
    {
        if (0.0..=1.0).contains(&n) {
            routing.full.upgrade_threshold = n;
        }
    }
    routing
}

/// Path of the user settings file written by the app's settings UI.
///
/// Separate from `routes.json` on purpose: that one is the deployment's default and may
/// be shipped read-only or managed, while this one belongs to the person using the app.
/// Keeping them apart is what lets a user change a preference without shadowing (or
/// having to understand) the deployment's routing table.
pub fn settings_file(lookup: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(v) = first_env(lookup, &["LT_SETTINGS_FILE", "WONSLATE_SETTINGS_FILE"]) {
        return PathBuf::from(v.trim());
    }
    data_dir_from(lookup).join("config").join("settings.json")
}

/// Pure: overlay the user settings file onto a base routing table.
///
/// Accepts the friendly `quality_preference` and, as an escape hatch, the same raw
/// routing keys `routes.json` takes. Raw keys are applied last so an explicit knob beats
/// the preference derived from it - if both are written, the specific one is what the
/// author meant.
pub fn routing_from_settings(v: &serde_json::Value, base: Routing) -> (Routing, Option<QualityPreference>) {
    let preference = v
        .get("quality_preference")
        .and_then(|x| x.as_str())
        .and_then(QualityPreference::parse);

    let mut routing = base;
    if let Some(pref) = preference {
        pref.apply(&mut routing.full);
    }
    routing = routing_from_json(v, routing);
    (routing, preference)
}

/// Pure composition of the whole precedence chain:
/// defaults -> routes file -> user settings -> environment.
///
/// The parsed files are parameters so this stays testable without touching the disk.
/// `tt_init`'s JSON is layered on top of the result by [`load_with`], and outranks
/// everything.
pub fn build_config(
    lookup: &dyn Fn(&str) -> Option<String>,
    routes: Option<&serde_json::Value>,
    settings: Option<&serde_json::Value>,
) -> Config {
    let mut cfg = flat_env_overrides(lookup);
    if let Some(v) = routes {
        cfg.routing = routing_from_json(v, cfg.routing);
    }
    if let Some(v) = settings {
        cfg.routing = routing_from_settings(v, cfg.routing).0;
    }
    cfg.routing = routing_from_env(lookup, cfg.routing);
    cfg
}

fn read_routes_file(lookup: &dyn Fn(&str) -> Option<String>) -> Option<serde_json::Value> {
    read_json_file(&routes_file(lookup), "routes")
}

/// Read and parse a config file. Absent is normal; unreadable or malformed is reported
/// and skipped, so a bad edit cannot stop the engine from starting.
fn read_json_file(path: &Path, label: &str) -> Option<serde_json::Value> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return None,
        Err(e) => {
            lt_warn!("[config] {} file {} unreadable ({}), using defaults", label, path.display(), e);
            return None;
        }
    };
    match serde_json::from_str(&text) {
        Ok(v) => {
            lt_info!("[config] {} loaded from {}", label, path.display());
            Some(v)
        }
        Err(e) => {
            lt_warn!("[config] {} file {} is not valid JSON ({}), using defaults", label, path.display(), e);
            None
        }
    }
}

/// A snapshot of the routing that is actually in effect, for the settings UI.
///
/// The UI must be able to show the effective value *and* where it came from: with four
/// layers above the defaults, "I set it in the settings page but nothing changed" is the
/// most likely support question, and the answer is usually an environment variable.
pub fn effective_routing() -> serde_json::Value {
    let cfg = get();
    let lookup = |k: &str| std::env::var(k).ok();
    let env_pinned: Vec<&str> = [
        "LT_QUALITY_PREFERENCE", "WONSLATE_QUALITY_PREFERENCE",
        "LT_FULL_UPGRADE_POLICY", "WONSLATE_FULL_UPGRADE_POLICY",
        "LT_FULL_TM_QUALITY_FLOOR", "WONSLATE_FULL_TM_QUALITY_FLOOR",
        "LT_FULL_UPGRADE_THRESHOLD", "WONSLATE_FULL_UPGRADE_THRESHOLD",
        "LT_ROUTES_FILE", "WONSLATE_ROUTES_FILE",
        "LT_SETTINGS_FILE", "WONSLATE_SETTINGS_FILE",
    ]
    .iter()
    .filter(|k| lookup(k).is_some())
    .copied()
    .collect();

    serde_json::json!({
        "routing": {
            "common_pairs": cfg.routing.common_pairs,
            "realtime": rule_json(&cfg.routing.realtime, false),
            "full": rule_json(&cfg.routing.full, true),
            "rare_pair": rule_json(&cfg.routing.rare_pair, false),
        },
        "tm_quality_floor_note": "serving floor: a TM entry below it is re-translated instead of reused",
        "env_pinned": env_pinned,
        "routes_file": routes_file(&lookup).to_string_lossy(),
        "settings_file": settings_file(&lookup).to_string_lossy(),
    })
}

fn rule_json(rule: &RouteRule, with_floor: bool) -> serde_json::Value {
    let mut v = serde_json::json!({
        "local_engine": rule.local_engine,
        "upgrade_engine": rule.upgrade_engine,
        "upgrade_threshold": rule.upgrade_threshold,
        "upgrade_policy": rule.upgrade_policy.as_str(),
    });
    if with_floor {
        v["tm_quality_floor"] = serde_json::json!(rule.tm_quality_floor);
    }
    v
}

/// Pure, testable: overlay environment overrides (resolved via `lookup`) on top of the
/// default config. Prefix chain per key: `LT_*` > `WONSLATE_*` > bare legacy name.
///
/// Takes no routes file on purpose: the file is the one I/O in the chain, and keeping
/// it out of here is what lets the precedence tests run without a filesystem. Production
/// always composes with the file, so this is the env-only variant the tests exercise.
#[cfg(test)]
fn from_env_with(lookup: &dyn Fn(&str) -> Option<String>) -> Config {
    build_config(lookup, None, None)
}

/// The flat (non-routing) environment overrides.
fn flat_env_overrides(lookup: &dyn Fn(&str) -> Option<String>) -> Config {
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
    // N-08: the glossary store bar is a quality knob, so it has to be tunable without a
    // rebuild. Values outside 0.0~1.0 are ignored rather than clamped, so a typo cannot
    // silently turn the filter into "store everything".
    if let Some(v) = first_env(lookup, &["LT_DISTILL_MIN_CONF", "WONSLATE_DISTILL_MIN_CONF"])
        .and_then(|v| v.trim().parse::<f32>().ok())
    {
        if (0.0..=1.0).contains(&v) {
            cfg.distill_min_conf = v;
        }
    }
    cfg
}

/// The config in force, loading it on first access if nobody called `load()` yet.
///
/// Auto-loading rather than falling back to `Config::default()` matters: the defaults
/// ignore both the routes file and the settings file, so a caller that happened to read
/// config a moment before `tt_init` would pin the whole process to built-in values and
/// the user's saved preference would look like it did nothing.
pub fn get() -> &'static Config {
    CONFIG.get_or_init(|| {
        let lookup = |k: &str| std::env::var(k).ok();
        let routes = read_routes_file(&lookup);
        let settings = read_json_file(&settings_file(&lookup), "settings");
        build_config(&lookup, routes.as_ref(), settings.as_ref())
    })
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
        assert!((c.distill_min_conf - 0.80).abs() < 1e-6);
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
    fn distill_min_conf_parsed_from_env() {
        let cfg = from_env_with(&only(&[("LT_DISTILL_MIN_CONF", "0.65")]));
        assert!((cfg.distill_min_conf - 0.65).abs() < 1e-6);
    }

    #[test]
    fn wonslate_prefixed_distill_min_conf_is_honored() {
        let cfg = from_env_with(&only(&[("WONSLATE_DISTILL_MIN_CONF", "0.9")]));
        assert!((cfg.distill_min_conf - 0.9).abs() < 1e-6);
    }

    #[test]
    fn out_of_range_distill_min_conf_keeps_default() {
        // A typo must not disable the filter: 12 and -1 both keep the default bar.
        for raw in ["12", "-1", "not-a-number", ""] {
            let cfg = from_env_with(&only(&[("LT_DISTILL_MIN_CONF", raw)]));
            assert!(
                (cfg.distill_min_conf - Config::default().distill_min_conf).abs() < 1e-6,
                "{} must not change the default bar",
                raw
            );
        }
    }

    // ---- N-11: configurable routing, and the precedence chain ----------------

    #[test]
    fn default_routing_matches_the_shipped_expectations() {
        let r = Routing::default();
        assert_eq!(r.full.local_engine, "madlad",
            "full mode covers every common pair via the MADLAD checkpoint");
        assert_eq!(r.full.upgrade_engine, "ollama");
        assert_eq!(r.full.upgrade_policy, UpgradePolicy::Always, "D14: 精译 asks the AI engine");
        assert_eq!(r.realtime.upgrade_engine, "", "realtime has a latency budget");
        assert_eq!(r.realtime.local_engine, "argos", "realtime keeps the fast en<->zh slot");
        assert_eq!(r.rare_pair.local_engine, "madlad");
        assert_eq!(r.common_pairs.len(), 11);
    }

    #[test]
    fn routes_file_overlays_one_field_and_keeps_the_rest() {
        let v = serde_json::json!({ "full": { "upgrade_policy": "low_confidence" } });

        let r = routing_from_json(&v, Routing::default());

        assert_eq!(r.full.upgrade_policy, UpgradePolicy::LowConfidence);
        assert_eq!(r.full.local_engine, "madlad", "untouched fields keep their default");
        assert_eq!(r.rare_pair.upgrade_threshold, 0.80);
    }

    #[test]
    fn env_beats_the_routes_file() {
        let v = serde_json::json!({ "full": { "upgrade_policy": "low_confidence" } });
        let cfg = build_config(&only(&[("WONSLATE_FULL_UPGRADE_POLICY", "always")]), Some(&v), None);

        assert_eq!(cfg.routing.full.upgrade_policy, UpgradePolicy::Always);
    }

    #[test]
    fn invalid_engine_ids_are_ignored_not_applied() {
        // A typo must not silently reroute to the demo dictionary.
        let v = serde_json::json!({
            "full": { "local_engine": "gpt-9", "upgrade_engine": "gpt-9" }
        });

        let r = routing_from_json(&v, Routing::default());

        assert_eq!(r.full.local_engine, "madlad");
        assert_eq!(r.full.upgrade_engine, "ollama");
    }

    #[test]
    fn out_of_range_threshold_and_unknown_policy_are_ignored() {
        let v = serde_json::json!({
            "full": { "upgrade_threshold": 12.0, "upgrade_policy": "sometimes" }
        });

        let r = routing_from_json(&v, Routing::default());

        assert_eq!(r.full.upgrade_threshold, 0.85);
        assert_eq!(r.full.upgrade_policy, UpgradePolicy::Always);
    }

    #[test]
    fn realtime_upgrade_engine_can_be_cleared_explicitly() {
        let v = serde_json::json!({ "realtime": { "upgrade_engine": "" } });
        let r = routing_from_json(&v, Routing::default());
        assert_eq!(r.realtime.upgrade_engine, "", "empty is a value for a no-upgrade rule");
    }

    #[test]
    fn empty_common_pairs_does_not_wipe_the_language_set() {
        let v = serde_json::json!({ "common_pairs": [] });
        let r = routing_from_json(&v, Routing::default());
        assert_eq!(r.common_pairs.len(), 11, "an empty list would make every pair 'rare'");
    }

    #[test]
    fn full_upgrade_policy_env_parses_both_spellings() {
        for raw in ["always", "ALWAYS", " low_confidence ", "low-confidence"] {
            let cfg = from_env_with(&only(&[("LT_FULL_UPGRADE_POLICY", raw)]));
            let expected = if raw.trim().to_lowercase().starts_with("always") {
                UpgradePolicy::Always
            } else {
                UpgradePolicy::LowConfidence
            };
            assert_eq!(cfg.routing.full.upgrade_policy, expected, "raw={raw}");
        }
    }

    #[test]
    fn routes_file_path_follows_the_data_dir_rule() {
        let p = routes_file(&only(&[("LT_DATA_DIR", "/tmp/wonslate-routes")]));
        assert_eq!(p, PathBuf::from("/tmp/wonslate-routes/config/routes.json"));

        let explicit = routes_file(&only(&[("WONSLATE_ROUTES_FILE", " D:/cfg/my-routes.json ")]));
        assert_eq!(explicit, PathBuf::from("D:/cfg/my-routes.json"), "trimmed");
    }

    // ---- D15: the user-facing quality/cost preference ------------------------

    #[test]
    fn quality_first_asks_the_ai_and_refuses_local_quality_cache() {
        let mut rule = Routing::default().full;
        QualityPreference::QualityFirst.apply(&mut rule);

        assert_eq!(rule.upgrade_policy, UpgradePolicy::Always);
        assert_eq!(
            rule.tm_quality_floor,
            crate::confidence::AI_UPGRADE_CONFIDENCE,
            "the floor must be exactly what an AI-written entry scores, or quality_first \
             would still serve locally-written entries"
        );
    }

    #[test]
    fn cost_first_prefers_local_and_serves_any_cache() {
        let mut rule = Routing::default().full;
        QualityPreference::CostFirst.apply(&mut rule);

        assert_eq!(rule.upgrade_policy, UpgradePolicy::LowConfidence);
        assert_eq!(rule.tm_quality_floor, 0.0);
    }

    #[test]
    fn shipped_defaults_serve_any_cached_entry() {
        // Confirmed as the D15 factory default (2026-09-30). Both halves matter:
        // `always` keeps D14's promise that the high-quality mode really runs the AI, and a
        // 0.0 floor keeps cached content free. Raising the floor would silently raise AI
        // cost for everyone already using this mode - their existing 0.88 / 0.9 entries
        // would be refused and re-translated - so it stays an explicit opt-in
        // (`quality_first`), not a new default.
        //
        // This state is deliberately neither of the two preferences the settings page
        // offers; it exists only as "no settings file", and the page says so rather than
        // pretending a choice was made.
        let full = Routing::default().full;
        assert_eq!(full.upgrade_policy, UpgradePolicy::Always);
        assert_eq!(full.tm_quality_floor, 0.0);
    }

    #[test]
    fn settings_file_carries_the_preference() {
        let v = serde_json::json!({ "quality_preference": "quality_first" });
        let (routing, pref) = routing_from_settings(&v, Routing::default());

        assert_eq!(pref, Some(QualityPreference::QualityFirst));
        assert_eq!(routing.full.upgrade_policy, UpgradePolicy::Always);
        assert_eq!(routing.full.tm_quality_floor, crate::confidence::AI_UPGRADE_CONFIDENCE);
    }

    #[test]
    fn an_explicit_knob_in_settings_beats_the_preference() {
        // Escape hatch: someone who wants "always upgrade but still serve the local
        // cache" writes both, and the specific knob is what they meant.
        let v = serde_json::json!({
            "quality_preference": "quality_first",
            "full": { "tm_quality_floor": 0.0 }
        });
        let (routing, _) = routing_from_settings(&v, Routing::default());

        assert_eq!(routing.full.upgrade_policy, UpgradePolicy::Always);
        assert_eq!(routing.full.tm_quality_floor, 0.0);
    }

    #[test]
    fn settings_outrank_the_deployment_routes_file() {
        let routes = serde_json::json!({ "full": { "upgrade_policy": "low_confidence" } });
        let settings = serde_json::json!({ "quality_preference": "quality_first" });

        let cfg = build_config(&only(&[]), Some(&routes), Some(&settings));

        assert_eq!(cfg.routing.full.upgrade_policy, UpgradePolicy::Always);
    }

    #[test]
    fn environment_outranks_the_user_settings_file() {
        let settings = serde_json::json!({ "quality_preference": "quality_first" });

        let cfg = build_config(
            &only(&[("WONSLATE_QUALITY_PREFERENCE", "cost_first")]),
            None,
            Some(&settings),
        );

        assert_eq!(cfg.routing.full.upgrade_policy, UpgradePolicy::LowConfidence);
        assert_eq!(cfg.routing.full.tm_quality_floor, 0.0);
    }

    #[test]
    fn an_env_pinned_knob_survives_an_env_preference() {
        // Support needs to be able to pin one knob while a preference is otherwise in
        // force, so the individual overrides are applied after the preference.
        let cfg = from_env_with(&only(&[
            ("LT_QUALITY_PREFERENCE", "quality_first"),
            ("LT_FULL_TM_QUALITY_FLOOR", "0.0"),
        ]));

        assert_eq!(cfg.routing.full.upgrade_policy, UpgradePolicy::Always);
        assert_eq!(cfg.routing.full.tm_quality_floor, 0.0);
    }

    #[test]
    fn unknown_preference_is_ignored() {
        let v = serde_json::json!({ "quality_preference": "cheapest" });
        let (routing, pref) = routing_from_settings(&v, Routing::default());

        assert_eq!(pref, None);
        assert_eq!(routing, Routing::default(), "nothing changed");
    }

    #[test]
    fn settings_file_path_follows_the_data_dir_rule() {
        let p = settings_file(&only(&[("LT_DATA_DIR", "/tmp/wonslate-cfg")]));
        assert_eq!(p, PathBuf::from("/tmp/wonslate-cfg/config/settings.json"));

        let explicit = settings_file(&only(&[("LT_SETTINGS_FILE", "/tmp/custom.json")]));
        assert_eq!(explicit, PathBuf::from("/tmp/custom.json"));
    }

    #[test]
    fn out_of_range_quality_floor_is_ignored() {
        let v = serde_json::json!({ "full": { "tm_quality_floor": 7.0 } });
        let routing = routing_from_json(&v, Routing::default());
        assert_eq!(routing.full.tm_quality_floor, 0.0);
    }

    #[test]
    fn effective_routing_reports_the_effective_values() {
        let snapshot = effective_routing();

        assert_eq!(snapshot["routing"]["full"]["upgrade_policy"], "always");
        assert_eq!(snapshot["routing"]["full"]["tm_quality_floor"], 0.0);
        assert!(snapshot["settings_file"].as_str().unwrap().ends_with("settings.json"));
        assert!(snapshot["env_pinned"].is_array());
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
