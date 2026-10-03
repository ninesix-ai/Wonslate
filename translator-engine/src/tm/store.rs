// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! File-backed TM persistence (JSON files; no C deps, cross-platform, no serde derive)

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock, RwLock};
use serde_json::{json, Value};
use crate::error::EngineError;
use crate::types::{GlossaryEntry, TmEntry};

/// Dirty threshold: flush to disk automatically after N accumulated writes
const FLUSH_THRESHOLD: usize = 100;

// ---- Internal records (hand-written JSON conversion, no derive)--------------------------

#[derive(Debug, Clone)]
struct TmRecord {
    source_hash: String,
    entry: TmEntry,
    flagged: bool,
}

impl TmRecord {
    fn to_value(&self) -> Value {
        let mut v = self.entry.to_json();
        v["source_hash"] = json!(self.source_hash);
        v["flagged"]     = json!(self.flagged);
        v
    }
    fn from_value(v: &Value) -> Self {
        Self {
            source_hash: v.get("source_hash").and_then(|x| x.as_str()).unwrap_or("").to_string(),
            entry:   TmEntry::from_json(v),
            flagged: v.get("flagged").and_then(|x| x.as_bool()).unwrap_or(false),
        }
    }
}

#[derive(Clone)]
struct GlossaryRecord {
    entry: GlossaryEntry,
    user_locked: bool,
}

impl GlossaryRecord {
    fn to_value(&self) -> Value {
        let mut v = self.entry.to_json();
        v["user_locked"] = json!(self.user_locked);
        v
    }
    fn from_value(v: &Value) -> Self {
        Self {
            entry: GlossaryEntry::from_json(v),
            user_locked: v.get("user_locked").and_then(|x| x.as_bool()).unwrap_or(false),
        }
    }
}

// ---- TmStore (core struct)------------------------------------------------

pub struct TmStore {
    tm_path: PathBuf,
    gl_path: PathBuf,
    tm_index: RwLock<HashMap<String, TmRecord>>,
    gl_index: RwLock<HashMap<String, GlossaryRecord>>,   // key: gl_key(term, slang, tlang)
    tm_dirty: Mutex<usize>,
    gl_dirty: Mutex<usize>,
}

static TM_STORE: OnceLock<TmStore> = OnceLock::new();

impl TmStore {
    pub fn open(tm_path: &Path, gl_path: &Path) -> Result<(), EngineError> {
        std::fs::create_dir_all(tm_path.parent().unwrap_or(Path::new("."))).ok();

        let tm_records: Vec<TmRecord> = read_records(tm_path)?;
        let mut tm_index: HashMap<String, TmRecord> = HashMap::new();
        for rec in tm_records {
            tm_index.insert(rec.source_hash.clone(), rec);
        }

        let gl_records: Vec<GlossaryRecord> = read_records(gl_path)?;
        let mut gl_index: HashMap<String, GlossaryRecord> = HashMap::new();
        for rec in gl_records {
            let key = gl_key(&rec.entry.source_term, &rec.entry.source_lang, &rec.entry.target_lang);
            gl_index.insert(key, rec);
        }

        let _ = TM_STORE.set(Self {
            tm_path: tm_path.to_path_buf(),
            gl_path: gl_path.to_path_buf(),
            tm_index: RwLock::new(tm_index),
            gl_index: RwLock::new(gl_index),
            tm_dirty: Mutex::new(0),
            gl_dirty: Mutex::new(0),
        });
        Ok(())
    }

    pub fn instance() -> Option<&'static TmStore> { TM_STORE.get() }

    // ---- TM operations --------------------------------------------------------

    pub fn tm_get(&self, hash: &str) -> Result<Option<TmEntry>, EngineError> {
        let idx = self.tm_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        Ok(idx.get(hash).filter(|r| !r.flagged).map(|r| r.entry.clone()))
    }

    pub fn tm_upsert(&self, entry: &TmEntry) -> Result<(), EngineError> {
        let hash = crate::tm::compute_hash(
            &entry.source_text, &entry.source_lang, &entry.target_lang,
        );
        {
            let mut idx = self.tm_index.write()
                .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
            match idx.get_mut(&hash) {
                Some(existing) if !existing.flagged => {
                    existing.entry.hit_count += 1;
                    if entry.quality > existing.entry.quality {
                        existing.entry = entry.clone();
                    }
                }
                _ => {
                    idx.insert(hash.clone(), TmRecord {
                        source_hash: hash.clone(),
                        entry: entry.clone(),
                        flagged: false,
                    });
                }
            }
        }
        self.bump_tm_dirty()?;
        if let Some(c) = crate::tm::cache::TM_CACHE.get() {
            c.put(hash, entry.clone());
        }
        Ok(())
    }

    pub fn tm_increment_hit(&self, hash: &str) -> Result<(), EngineError> {
        let found = {
            let mut idx = self.tm_index.write()
                .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
            match idx.get_mut(hash) {
                Some(rec) => { rec.entry.hit_count += 1; true }
                None => false,
            }
        };
        if !found {
            return Ok(());
        }
        // Refresh the LRU cache view so hit_count read via lookup only ever grows
        if let Some(c) = crate::tm::cache::TM_CACHE.get() {
            c.bump_hit_count(hash);
        }
        // Hit counts are TM data: mark dirty so they reach disk on the normal flush
        // path instead of being lost when the process dies before the next upsert.
        self.bump_tm_dirty()
    }

    pub fn tm_flag_bad(&self, hash: &str) -> Result<(), EngineError> {
        {
            let mut idx = self.tm_index.write()
                .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
            if let Some(rec) = idx.get_mut(hash) { rec.flagged = true; }
        }
        if let Some(c) = crate::tm::cache::TM_CACHE.get() { c.invalidate(hash); }
        self.bump_tm_dirty()
    }

    pub fn tm_top_by_hit(&self, top_n: usize) -> Result<Vec<TmEntry>, EngineError> {
        let idx = self.tm_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        let mut v: Vec<&TmRecord> = idx.values().filter(|r| !r.flagged).collect();
        v.sort_by_key(|r| std::cmp::Reverse(r.entry.hit_count));
        Ok(v.into_iter().take(top_n).map(|r| r.entry.clone()).collect())
    }

    pub fn tm_fuzzy_search(
        &self, keyword: &str, source_lang: &str, target_lang: &str, limit: usize,
    ) -> Result<Vec<TmEntry>, EngineError> {
        let idx = self.tm_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        let kw_lower = keyword.to_lowercase();
        let mut results: Vec<&TmRecord> = idx.values()
            .filter(|r| !r.flagged
                && r.entry.source_lang == source_lang
                && r.entry.target_lang == target_lang
                && r.entry.source_text.to_lowercase().contains(&kw_lower))
            .collect();
        results.sort_by(|a, b| b.entry.quality.partial_cmp(&a.entry.quality)
            .unwrap_or(std::cmp::Ordering::Equal));
        Ok(results.into_iter().take(limit).map(|r| r.entry.clone()).collect())
    }

    /// List the active (not flagged) entries of one language pair, most-hit first.
    /// Powers the UI TM manager; flagged entries stay hidden so a "marked bad"
    /// pair cannot silently reappear in the list the user just corrected.
    pub fn tm_list(
        &self, source_lang: &str, target_lang: &str, limit: usize,
    ) -> Result<Vec<TmEntry>, EngineError> {
        let idx = self.tm_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        let mut v: Vec<&TmRecord> = idx.values()
            .filter(|r| !r.flagged
                && r.entry.source_lang == source_lang
                && r.entry.target_lang == target_lang)
            .collect();
        v.sort_by_key(|r| std::cmp::Reverse(r.entry.hit_count));
        Ok(v.into_iter().take(limit).map(|r| r.entry.clone()).collect())
    }

    pub fn tm_total(&self) -> Result<u64, EngineError> {
        let idx = self.tm_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        Ok(idx.values().filter(|r| !r.flagged).count() as u64)
    }

    // ---- Glossary operations ----------------------------------------------------

    pub fn glossary_list(
        &self, source_lang: &str, target_lang: &str, limit: usize,
    ) -> Result<Vec<GlossaryEntry>, EngineError> {
        let idx = self.gl_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        let mut v: Vec<&GlossaryRecord> = idx.values()
            .filter(|r| r.entry.source_lang == source_lang
                      && r.entry.target_lang == target_lang)
            .collect();
        v.sort_by(|a, b| b.entry.confidence.partial_cmp(&a.entry.confidence)
            .unwrap_or(std::cmp::Ordering::Equal));
        Ok(v.into_iter().take(limit).map(|r| r.entry.clone()).collect())
    }

    pub fn glossary_upsert(&self, entry: &GlossaryEntry) -> Result<(), EngineError> {
        let key = gl_key(&entry.source_term, &entry.source_lang, &entry.target_lang);
        {
            let mut idx = self.gl_index.write()
                .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
            match idx.get_mut(&key) {
                Some(existing) if !existing.user_locked => {
                    if entry.confidence > existing.entry.confidence {
                        existing.entry.target_term = entry.target_term.clone();
                        existing.entry.confidence = entry.confidence;
                    }
                    existing.entry.frequency += 1;
                }
                None => {
                    idx.insert(key, GlossaryRecord { entry: entry.clone(), user_locked: false });
                }
                _ => {}   // user_locked=true: distillation must not overwrite
            }
        }
        self.bump_gl_dirty()
    }

    pub fn glossary_delete(
        &self, source_term: &str, source_lang: &str, target_lang: &str,
    ) -> Result<(), EngineError> {
        let key = gl_key(source_term, source_lang, target_lang);
        {
            let mut idx = self.gl_index.write()
                .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
            idx.remove(&key);
        }
        self.bump_gl_dirty()
    }

    // ---- Dirty flush ------------------------------------------------

    fn bump_tm_dirty(&self) -> Result<(), EngineError> {
        let mut n = self.tm_dirty.lock()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        *n += 1;
        if *n >= FLUSH_THRESHOLD {
            *n = 0;
            drop(n);
            return self.flush_tm();
        }
        Ok(())
    }

    fn bump_gl_dirty(&self) -> Result<(), EngineError> {
        let mut n = self.gl_dirty.lock()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        *n += 1;
        if *n >= FLUSH_THRESHOLD {
            *n = 0;
            drop(n);
            return self.flush_glossary();
        }
        Ok(())
    }

    pub fn flush_tm(&self) -> Result<(), EngineError> {
        let idx = self.tm_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        let records: Vec<Value> = idx.values().map(|r| r.to_value()).collect();
        write_records(&self.tm_path, &records)
    }

    pub fn flush_glossary(&self) -> Result<(), EngineError> {
        let idx = self.gl_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        let records: Vec<Value> = idx.values().map(|r| r.to_value()).collect();
        write_records(&self.gl_path, &records)
    }

    pub fn flush_all(&self) -> Result<(), EngineError> {
        self.flush_tm()?;
        self.flush_glossary()
    }
}

// ---- File-IO helpers (serde_json::Value by hand, no derive)--------

fn gl_key(term: &str, slang: &str, tlang: &str) -> String {
    format!("{}|{}|{}", term.to_lowercase(), slang, tlang)
}

/// Read the record array from the JSON file; a missing or empty file yields an empty array
fn read_records<T: FromValue>(path: &Path) -> Result<Vec<T>, EngineError> {
    if !path.exists() { return Ok(vec![]); }
    let raw = std::fs::read_to_string(path)?;
    if raw.trim().is_empty() { return Ok(vec![]); }
    let v: Value = serde_json::from_str(&raw)
        .map_err(|e| EngineError::TmError(format!("JSON parse error in {:?}: {}", path, e)))?;
    let arr = v.get("records").and_then(|x| x.as_array()).cloned().unwrap_or_default();
    Ok(arr.iter().map(T::from_value).collect())
}

trait FromValue: Sized { fn from_value(v: &Value) -> Self; }
impl FromValue for TmRecord       { fn from_value(v: &Value) -> Self { TmRecord::from_value(v) } }
impl FromValue for GlossaryRecord { fn from_value(v: &Value) -> Self { GlossaryRecord::from_value(v) } }

/// Atomic write: write .tmp then rename, so a mid-write crash cannot corrupt the original
fn write_records(path: &Path, records: &[Value]) -> Result<(), EngineError> {
    let tmp = path.with_extension("tmp");
    let body = json!({ "version": 1, "records": records });
    let s = serde_json::to_string(&body)?;
    std::fs::write(&tmp, s)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

// ---- Unit tests ----------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gl_key_lowercases_term_only() {
        assert_eq!(gl_key("Hello", "zh", "en"), gl_key("hello", "zh", "en"));
        assert_eq!(gl_key("HELLO", "zh", "en"), gl_key("hello", "zh", "en"));
        assert_ne!(gl_key("x", "zh", "en"), gl_key("x", "en", "zh"));
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("lt_test_{}_{}", tag, std::process::id()));
        std::fs::create_dir_all(&p).ok();
        p
    }

    #[test]
    fn read_records_when_file_missing_returns_empty() {
        let p = temp_dir("missing").join("nope.json");
        let _ = std::fs::remove_file(&p);
        let v: Vec<TmRecord> = read_records(&p).unwrap();
        assert!(v.is_empty());
    }

    #[test]
    fn read_records_when_file_empty_returns_empty() {
        let dir = temp_dir("empty");
        let p = dir.join("empty.json");
        std::fs::write(&p, b"").unwrap();
        let v: Vec<TmRecord> = read_records(&p).unwrap();
        assert!(v.is_empty());
    }

    #[test]
    fn read_records_rejects_garbage() {
        let dir = temp_dir("garbage");
        let p = dir.join("bad.json");
        std::fs::write(&p, b"this is not json").unwrap();
        let r: Result<Vec<TmRecord>, EngineError> = read_records(&p);
        assert!(r.is_err());
        assert!(r.unwrap_err().to_string().contains("JSON parse error"));
    }

    #[test]
    fn write_then_read_roundtrip() {
        let dir = temp_dir("roundtrip");
        let p = dir.join("tm.json");
        let records = vec![TmRecord {
            source_hash: "abc123".into(),
            entry: TmEntry {
                source_text: "深度学习".into(), source_lang: "zh".into(),
                target_text: "deep learning".into(), target_lang: "en".into(),
                engine: "ollama".into(), quality: 0.95, hit_count: 3,
                domain: "tech".into(),
            },
            flagged: false,
        }];
        let vals: Vec<Value> = records.iter().map(|r| r.to_value()).collect();
        write_records(&p, &vals).unwrap();
        let back: Vec<TmRecord> = read_records(&p).unwrap();
        assert_eq!(back.len(), 1);
        assert_eq!(back[0].entry.source_text, "深度学习");
        assert_eq!(back[0].entry.hit_count, 3);
        assert!(!back[0].flagged);
    }

    #[test]
    fn write_records_is_atomic() {
        let dir = temp_dir("atomic");
        let p = dir.join("out.json");
        let _ = std::fs::remove_file(&p);
        let _ = std::fs::remove_file(p.with_extension("tmp"));
        write_records(&p, &[]).unwrap();
        assert!(p.exists());
        assert!(!p.with_extension("tmp").exists(),
            ".tmp 应被 rename 移走");
    }
}
