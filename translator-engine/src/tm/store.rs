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
            // S11: the on-disk key now includes the domain segment. Rows written
            // by a pre-S11 build carry an empty `domain` field, which the
            // GlossaryEntry default already yields on missing key -- so loading
            // old JSON is a no-op upgrade that lands every row in the generic
            // bucket. No destructive migration, no data rewrite on first load.
            let key = gl_key(
                &rec.entry.source_term, &rec.entry.source_lang,
                &rec.entry.target_lang, &rec.entry.domain);
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
        // D37: the cache has to be handed the row the store *kept*, not the one the
        // caller passed in. It used to receive the incoming row unconditionally, so a
        // worse candidate for a known text left the two disagreeing -- the file kept the
        // better translation, this session answered with the worse one, and restarting
        // the process changed the answer back with nothing reported anywhere. Returning
        // `settled` makes the quality rule the single source of both copies.
        let settled = {
            let mut idx = self.tm_index.write()
                .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
            match idx.get_mut(&hash) {
                Some(existing) if !existing.flagged => {
                    existing.entry.hit_count += 1;
                    if entry.quality > existing.entry.quality {
                        existing.entry = entry.clone();
                    }
                    existing.entry.clone()
                }
                _ => {
                    idx.insert(hash.clone(), TmRecord {
                        source_hash: hash.clone(),
                        entry: entry.clone(),
                        flagged: false,
                    });
                    entry.clone()
                }
            }
        };
        self.bump_tm_dirty()?;
        if let Some(c) = crate::tm::cache::TM_CACHE.get() {
            c.put(hash, settled);
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
        // D32: a tie must not be settled by HashMap iteration, which restarts from a
        // fresh random seed on every call. Hit counts repeat by the thousand on real
        // memory, so without this the warmup set is drawn rather than chosen. The
        // source text is unique per language pair but not across pairs, hence the hash.
        v.sort_by(|a, b| b.entry.hit_count.cmp(&a.entry.hit_count)
            .then_with(|| a.entry.source_text.cmp(&b.entry.source_text))
            .then_with(|| a.source_hash.cmp(&b.source_hash)));
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
            .unwrap_or(std::cmp::Ordering::Equal)
            // D32: distilled rows pile up on a handful of quality scores, so these
            // few-shot rows -- which go straight into the prompt -- were being picked
            // by iteration order. Distinct source texts are unique within a pair.
            .then_with(|| a.entry.source_text.cmp(&b.entry.source_text)));
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
        // D32: same tie rule as `tm_top_by_hit` -- the UI manager's list must not
        // reorder itself between refreshes while the user is working through it.
        v.sort_by(|a, b| b.entry.hit_count.cmp(&a.entry.hit_count)
            .then_with(|| a.entry.source_text.cmp(&b.entry.source_text))
            .then_with(|| a.source_hash.cmp(&b.source_hash)));
        Ok(v.into_iter().take(limit).map(|r| r.entry.clone()).collect())
    }

    pub fn tm_total(&self) -> Result<u64, EngineError> {
        let idx = self.tm_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        Ok(idx.values().filter(|r| !r.flagged).count() as u64)
    }

    // ---- Glossary operations ----------------------------------------------------

    pub fn glossary_list(
        &self, source_lang: &str, target_lang: &str, domain: &str, limit: usize,
    ) -> Result<Vec<GlossaryEntry>, EngineError> {
        // S11: "generic ∪ specific" per D-S11.2. When `domain` is empty only
        // generic rows survive. When `domain` is non-empty, generic rows are
        // still visible (they never contradict a specific request) and specific
        // rows from other domains are filtered out. Per source_term we keep the
        // more specific row.
        //
        // The confidence comparison below is the tie-break for two candidates of
        // equal specificity -- which the primary key makes impossible today: a
        // generic row is keyed by `term|src|tgt|""` and a `domain=X` row by
        // `term|src|tgt|X`, so two rows with the same lowercased term and the same
        // specificity are the same index entry. It is deliberately kept: if the key
        // ever gains a segment that lets such a pair exist, the winner should be the
        // better-attested term rather than HashMap order (the D32 lesson). Coverage
        // tools report this line as unreachable; that is the key rule talking, not a
        // missing test.
        let idx = self.gl_index.read()
            .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
        let mut chosen: HashMap<String, &GlossaryRecord> = HashMap::new();
        for r in idx.values() {
            if r.entry.source_lang != source_lang || r.entry.target_lang != target_lang {
                continue;
            }
            let is_generic = r.entry.domain.is_empty();
            let matches_specific = !domain.is_empty() && r.entry.domain == domain;
            if !(is_generic || matches_specific) { continue; }
            let key = r.entry.source_term.to_lowercase();
            let take = match chosen.get(&key) {
                None => true,
                Some(prev) => {
                    let prev_specific = !prev.entry.domain.is_empty();
                    if matches_specific != prev_specific {
                        matches_specific
                    } else {
                        r.entry.confidence > prev.entry.confidence
                    }
                }
            };
            if take { chosen.insert(key, r); }
        }
        // D32: `chosen` is keyed by the lowercased source term, so carrying the key
        // through the sort gives the tie a total order that does not depend on HashMap
        // iteration. This is the one that mattered most: the pipeline asks for 20 rows
        // and the shipped AV pack holds 264 of its 266 at one confidence, so which 20
        // reached the prompt used to be redrawn on every single call.
        let mut v: Vec<(String, &GlossaryRecord)> = chosen.into_iter().collect();
        v.sort_by(|a, b| b.1.entry.confidence.partial_cmp(&a.1.entry.confidence)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0)));
        Ok(v.into_iter().take(limit).map(|(_, r)| r.entry.clone()).collect())
    }

    pub fn glossary_upsert(&self, entry: &GlossaryEntry) -> Result<(), EngineError> {
        let key = gl_key(&entry.source_term, &entry.source_lang, &entry.target_lang, &entry.domain);
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
        // S11: the UI delete removes the *generic* row only. Removing a specific
        // domain row goes through a follow-up task (S11 successor) once the UI
        // exposes a domain filter per row; widening this signature now would
        // ripple through the FFI for no user-visible gain.
        let key = gl_key(source_term, source_lang, target_lang, "");
        {
            let mut idx = self.gl_index.write()
                .map_err(|e| EngineError::TmError(format!("lock: {}", e)))?;
            idx.remove(&key);
        }
        self.bump_gl_dirty()
    }

    // ---- Glossary import (S11) ---------------------------------------------------

    /// Import one seed pack. The pack-level `domain` is stamped onto every entry
    /// so a caller cannot smuggle a mixed-domain file through a single endpoint.
    /// Entries keep `source="seed:<domain>"` so a user can distinguish a shipped
    /// pack row from a hand-added one in the UI.
    ///
    /// A pack also declares `source_lang` and `target_lang` at its root; per-
    /// entry values still win if set, so a pack can carry a mostly-uniform pair
    /// while overriding the odd row. This is the format `docs/glossary-packs/*.json`
    /// uses -- the language pair is a property of the pack, not of each term.
    pub fn glossary_import_pack(&self, pack_json: &str) -> Result<u32, EngineError> {
        let v: Value = serde_json::from_str(pack_json)
            .map_err(|e| EngineError::InvalidInput(format!("pack json: {}", e)))?;
        let domain = v.get("domain").and_then(|x| x.as_str()).unwrap_or("");
        if domain.is_empty() {
            return Err(EngineError::InvalidInput(
                "pack.domain must be non-empty (an untagged import would pollute the generic table)".into()));
        }
        let pack_src = v.get("source_lang").and_then(|x| x.as_str()).unwrap_or("");
        let pack_tgt = v.get("target_lang").and_then(|x| x.as_str()).unwrap_or("");
        if pack_src.is_empty() || pack_tgt.is_empty() {
            return Err(EngineError::InvalidInput(
                "pack.source_lang and pack.target_lang are required (a seed pack is scoped to one language pair)".into()));
        }
        let entries = v.get("entries").and_then(|x| x.as_array())
            .ok_or_else(|| EngineError::InvalidInput("pack.entries must be an array".into()))?;
        // D38: stage the whole pack, then write. The loop used to validate and import in
        // one pass, so the entry that failed the check aborted after its predecessors had
        // already been upserted -- the caller got `ok:false` while the store kept part of
        // the file, which is the half-applied state the validation exists to prevent.
        let mut staged: Vec<GlossaryEntry> = Vec::with_capacity(entries.len());
        for raw in entries {
            let mut entry = GlossaryEntry::from_json(raw);
            entry.domain = domain.to_string();
            if entry.source_lang.is_empty() { entry.source_lang = pack_src.to_string(); }
            if entry.target_lang.is_empty() { entry.target_lang = pack_tgt.to_string(); }
            if entry.source_term.is_empty() || entry.target_term.is_empty() {
                return Err(EngineError::InvalidInput(
                    "every pack entry must carry source_term and target_term".into()));
            }
            entry.source = format!("seed:{}", domain);
            // D34: stamp on key *presence*, never on a sentinel value. This line used to
            // read `if entry.confidence <= 0.0`, but `GlossaryEntry::from_json` had
            // already filled an absent key with 0.9, so it could not fire and every
            // shipped pack -- all twelve of them omit `confidence` on every row -- entered
            // the store at 0.90. Import replaces a row only on a strictly higher
            // confidence, so a seed pack displaced nothing while still answering
            // `imported: N`. A declared value is left alone: it is the pack author's call.
            if raw.get("confidence").is_none() { entry.confidence = 1.0; }
            staged.push(entry);
        }
        let mut imported = 0u32;
        for entry in &staged {
            self.glossary_upsert(entry)?;
            imported += 1;
        }
        Ok(imported)
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

fn gl_key(term: &str, slang: &str, tlang: &str, domain: &str) -> String {
    // S11: the domain is the fourth segment of the primary key, so the same
    // source term can coexist as a generic row and as a per-domain override.
    // Empty domain is the "generic" bucket; readers treat it as always-visible.
    format!("{}|{}|{}|{}", term.to_lowercase(), slang, tlang, domain)
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

/// Atomic write: write a staging file then rename, so a mid-write crash cannot corrupt
/// the original, and a reader only ever sees a complete document.
///
/// The staging name is unique per call (defect D39). It used to be `path` with its
/// extension swapped for `.tmp` -- one shared name per file -- while the store legitimately
/// flushes from several threads: a request thread through the dirty counter or
/// `tt_shutdown`, and the distillation worker through every term it upserts. Two writers
/// then renamed each other's staging file: the loser reported "The system cannot find the
/// file specified (os error 2)" for a save that really happened upstream, and a rename
/// that won while the other writer was still filling the staging file moved a half-written
/// document into place, destroying the very thing this dance is for. The pid and a counter
/// keep the name unique across processes too, because the desktop app and an MCP server
/// can hold the same store files open at once.
static WRITE_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

fn write_records(path: &Path, records: &[Value]) -> Result<(), EngineError> {
    let seq = WRITE_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let stem = path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "store".to_string());
    let tmp = path.with_file_name(format!("{}.{}.{}.tmp", stem, std::process::id(), seq));
    let body = json!({ "version": 1, "records": records });
    let s = serde_json::to_string(&body)?;
    std::fs::write(&tmp, s)?;
    let renamed = std::fs::rename(&tmp, path);
    if renamed.is_err() {
        // A failed rename must not strand a staging file in the data directory: the
        // next reader of that directory would meet a file nothing owns.
        let _ = std::fs::remove_file(&tmp);
    }
    renamed?;
    Ok(())
}

// ---- Unit tests ----------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gl_key_lowercases_term_only() {
        assert_eq!(gl_key("Hello", "zh", "en", ""), gl_key("hello", "zh", "en", ""));
        assert_eq!(gl_key("HELLO", "zh", "en", ""), gl_key("hello", "zh", "en", ""));
        assert_ne!(gl_key("x", "zh", "en", ""), gl_key("x", "en", "zh", ""));
        // S11: the domain segment distinguishes generic and specific rows for the
        // same term, so `gl_key("x","zh","en","")` and `gl_key("x","zh","en","av")`
        // must not collide.
        assert_ne!(gl_key("x", "zh", "en", ""), gl_key("x", "zh", "en", "av"));
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

    /// The store flushes from more than one thread: a request thread reaches
    /// `write_records` through the dirty counter (`FLUSH_THRESHOLD` writes) or through
    /// `tt_shutdown`, and the distillation worker reaches it through every term it
    /// upserts. Those two can land on the same file at the same moment.
    ///
    /// With one shared temp name per file that is not a benign race: the writer whose
    /// rename loses the race gets "The system cannot find the file specified (os error
    /// 2)", so a save that the user was told succeeded reports an error at random -- and
    /// a rename that wins while the other writer is still filling the file moves a
    /// half-written document into place, which is the exact corruption the write-then-
    /// rename dance exists to prevent. This is how defect D39 surfaced: twice under
    /// `cargo llvm-cov` in `tests/tm_reload.rs`, where two cases call `flush_all()` on
    /// parallel threads, and never under a plain `cargo test` because nothing slowed the
    /// window down. Reproduced here instead of relying on that timing.
    #[test]
    fn concurrent_writers_of_one_file_never_lose_a_save_or_a_torn_one() {
        use std::thread;

        let dir = temp_dir("concurrent-writers");
        let p = dir.join("store.json");
        let rounds = 6;
        let writers = 8;

        for round in 0..rounds {
            let rows: Vec<Value> = (0..writers)
                .map(|i| json!({ "source_hash": format!("r{}w{}", round, i) }))
                .collect();
            let mut handles = Vec::new();
            for _ in 0..writers {
                let path = p.clone();
                let rows = rows.clone();
                handles.push(thread::spawn(move || write_records(&path, &rows)));
            }
            for handle in handles {
                let result = handle.join().expect("a writer thread panicked");
                result.unwrap_or_else(|e| {
                    panic!("round {}: a concurrent save must not fail: {}", round, e)
                });
            }

            // Whatever interleaved, the file on disk is a document and not a fragment:
            // a reader must always parse it, and it must carry a whole records array.
            let raw = std::fs::read_to_string(&p)
                .unwrap_or_else(|e| panic!("round {}: the store file is unreadable: {}", round, e));
            let parsed: Value = serde_json::from_str(&raw)
                .unwrap_or_else(|e| panic!("round {}: torn write reached the store: {} -- {} bytes",
                                          round, e, raw.len()));
            assert!(parsed["records"].is_array(), "round {}: no records array on disk",
                    round);

            let leftovers: Vec<String> = std::fs::read_dir(&dir)
                .unwrap()
                .filter_map(|e| e.ok())
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .filter(|name| name.ends_with(".tmp"))
                .collect();
            assert!(leftovers.is_empty(),
                "round {}: temp files were left behind: {:?}", round, leftovers);
        }
    }
}
