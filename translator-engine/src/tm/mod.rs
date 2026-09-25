// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! TM 统一公开 API（所有模块通过此文件访问 TM 和 glossary）

pub mod store;
pub mod cache;

use crate::error::EngineError;
use crate::types::{GlossaryEntry, TmEntry};

pub use store::TmStore;

/// 初始化（应用启动时调一次，顺序：cache → store → warmup）
pub fn init(data_dir: &std::path::Path, cache_size: usize, warmup_n: usize) -> Result<(), EngineError> {
    cache::init(cache_size);
    let tm_path = data_dir.join("translator_tm.json");
    let gl_path = data_dir.join("glossary.json");
    store::TmStore::open(&tm_path, &gl_path)?;
    // 预热 LRU
    if let Some(store) = TmStore::instance() {
        if let Ok(top) = store.tm_top_by_hit(warmup_n) {
            if let Some(c) = cache::TM_CACHE.get() {
                c.warmup(top);
            }
        }
    }
    lt_info!("[tm] initialized, data_dir={}", data_dir.display());
    Ok(())
}
/// SHA256 哈希（trim + lowercase 规范化，使用 std DefaultHasher）
pub fn compute_hash(text: &str, source_lang: &str, target_lang: &str) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let normalized = text.trim().to_lowercase();
    let input = format!("{}|{}|{}", normalized, source_lang, target_lang);
    let mut h = DefaultHasher::new();
    input.hash(&mut h);
    format!("{:016x}", h.finish())
}

// ── TM 快捷函数（封装 TmStore，供 pipeline 调用）──────────────────

pub fn lookup(text: &str, source_lang: &str, target_lang: &str)
    -> Result<Option<TmEntry>, EngineError>
{
    let hash = compute_hash(text, source_lang, target_lang);
    // L1: 内存
    if let Some(entry) = cache::TM_CACHE.get().and_then(|c| c.get(&hash)) {
        return Ok(Some(entry));
    }
    // L2: SQLite
    let store = TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?;
    if let Some(entry) = store.tm_get(&hash)? {
        if let Some(c) = cache::TM_CACHE.get() {
            c.put(hash, entry.clone());
        }
        return Ok(Some(entry));
    }
    Ok(None)
}

pub fn put(entry: &TmEntry) -> Result<(), EngineError> {
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .tm_upsert(entry)
}

pub fn increment_hit_count(entry: &TmEntry) -> Result<(), EngineError> {
    let hash = compute_hash(&entry.source_text, &entry.source_lang, &entry.target_lang);
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .tm_increment_hit(&hash)
}

pub fn flag_bad(text: &str, source_lang: &str, target_lang: &str) -> Result<(), EngineError> {
    let hash = compute_hash(text, source_lang, target_lang);
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .tm_flag_bad(&hash)
}

/// 子串模糊匹配（取 top_n 条，供 few-shot 使用）
pub fn similar_entries(
    text: &str,
    source_lang: &str,
    target_lang: &str,
    top_n: usize,
) -> Result<Vec<TmEntry>, EngineError> {
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .tm_fuzzy_search(text, source_lang, target_lang, top_n)
}

pub fn total_entries() -> Result<u64, EngineError> {
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .tm_total()
}

// ── Glossary 快捷函数 ─────────────────────────────────────────────

pub fn glossary_list(
    source_lang: &str,
    target_lang: &str,
    limit: usize,
) -> Result<Vec<GlossaryEntry>, EngineError> {
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .glossary_list(source_lang, target_lang, limit)
}

pub fn glossary_upsert(entry: &GlossaryEntry) -> Result<(), EngineError> {
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .glossary_upsert(entry)
}

pub fn glossary_delete(
    source_term: &str,
    source_lang: &str,
    target_lang: &str,
) -> Result<(), EngineError> {
    TmStore::instance()
        .ok_or_else(|| EngineError::TmError("not initialized".into()))?
        .glossary_delete(source_term, source_lang, target_lang)
}
