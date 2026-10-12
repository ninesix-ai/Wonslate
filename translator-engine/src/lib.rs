// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

// ---- Lightweight logging macros (zero deps, stderr output)------------------------

#[macro_use]
mod logging {
    /// info level: stderr output
    #[macro_export]
    macro_rules! lt_info  { ($($a:tt)*) => { eprintln!("[INFO ] {}", format!($($a)*)) } }
    /// warn level
    #[macro_export]
    macro_rules! lt_warn  { ($($a:tt)*) => { eprintln!("[WARN ] {}", format!($($a)*)) } }
    /// error level
    #[macro_export]
    macro_rules! lt_error { ($($a:tt)*) => { eprintln!("[ERROR] {}", format!($($a)*)) } }
    /// debug level (silent in release, zero cost)
    #[macro_export]
    macro_rules! lt_debug { ($($a:tt)*) => { } }
}

// Internal module declarations (pub for rlib consumers: integration tests, the future CLI / REST API;
// they do not affect cdylib exports -- those are decided only by #[no_mangle] extern "C")
pub mod engine;
pub mod error;
pub mod types;
pub mod config;
pub mod router;
pub mod confidence;
mod lang;
pub mod glossary;
pub mod tm;
pub mod distill;
pub mod pipeline;
pub mod license_gate;

use std::ffi::{CStr, CString};
use std::os::raw::c_char;
use std::panic::{catch_unwind, AssertUnwindSafe};

use error::EngineError;
use types::TranslateRequest;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const PANIC_JSON: &str = r#"{"ok":false,"engine":"unknown","error":"PANIC","message":"engine panicked","source":"fallback","latency_ms":0,"confidence":0.0}"#;
/// Ack shape for write-only endpoints (no payload beyond success).
const OK_JSON: &str = r#"{"ok":true}"#;

// ---- Internal helpers ----------------------------------------------------

fn to_c_string(s: &str) -> *mut c_char {
    match CString::new(s) {
        Ok(c) => c.into_raw(),
        Err(_) => std::ptr::null_mut(),
    }
}

unsafe fn read_cstr(ptr: *const c_char) -> Option<String> {
    if ptr.is_null() { return None; }
    CStr::from_ptr(ptr).to_str().ok().map(|s| s.to_string())
}

/// Error-response string returned across FFI (unified shape).
fn err_response(err: EngineError) -> String {
    serde_json::json!({
        "ok": false,
        "engine": "unknown",
        "source_lang": "", "target_lang": "",
        "input": "", "output": null,
        "source": "fallback",
        "latency_ms": 0, "confidence": 0.0,
        "error": err.to_string(),
        "message": err.to_string(),
    }).to_string()
}

// ---- Stable API (frozen at P0; signatures never change)--------------------------

/// Return the engine version.
#[no_mangle]
pub extern "C" fn tt_version() -> *mut c_char {
    to_c_string(VERSION)
}

/// Basic translation entry (backward compatible; no TM / distillation).
/// New code should prefer tt_translate_full.
#[no_mangle]
pub extern "C" fn tt_translate(
    engine_id: *const c_char,
    input: *const c_char,
    lang_pair: *const c_char,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let eid = unsafe { read_cstr(engine_id) }.unwrap_or_default();
        let inp = unsafe { read_cstr(input) }.unwrap_or_default();
        let lp  = unsafe { read_cstr(lang_pair) }.unwrap_or_default();
        match pipeline::translate_simple(&eid, &inp, &lp) {
            Ok(resp)  => resp.to_json(),
            Err(err)  => err_response(err),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}

/// Free a string allocated by this DLL.
#[no_mangle]
pub extern "C" fn tt_free_string(ptr: *mut c_char) {
    if ptr.is_null() { return; }
    unsafe { drop(CString::from_raw(ptr)); }
}

// ---- v2.0 API --------------------------------------------------------

/// Initialize the engine (call once at app start: open the TM store, load config, start the distill thread).
/// Pass "{}" as config_json to use default settings.
#[no_mangle]
pub extern "C" fn tt_init(config_json: *const c_char) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        // The caller's config_json is consumed, not decorative: routing overrides in it
        // outrank the file and the environment. Keys this layer does not understand come
        // back in the ack, because an embedding app that believes a setting took effect
        // when it did not is worse off than one that is told to drop it.
        let raw = unsafe { read_cstr(config_json) }.unwrap_or_else(|| "{}".into());

        config::ensure_dirs().ok();
        let ignored_keys = config::load_with(Some(&raw));

        let data_dir = config::data_dir().join("data");
        let (cache_sz, warmup_n) = (config::get().tm_cache_size, config::get().tm_warmup_n);
        tm::init(&data_dir, cache_sz, warmup_n)
            .map_err(|e| lt_error!("[tt_init] TM init failed: {}", e)).ok();

        distill::init();

        if ignored_keys.is_empty() {
            serde_json::json!({"ok": true, "version": VERSION}).to_string()
        } else {
            serde_json::json!({
                "ok": true,
                "version": VERSION,
                "ignored_config_keys": ignored_keys,
            }).to_string()
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}

/// The routing that is actually in effect, plus which files and environment variables
/// produced it. Read-only, no arguments; safe to call after `tt_init`.
///
/// The settings UI needs this to answer "why is it still doing X after I changed the
/// setting": with four layers involved, the honest answer is usually an environment
/// variable or a deployment file, and both are named here.
#[no_mangle]
pub extern "C" fn tt_config_json() -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| config::effective_routing().to_string()));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}

/// Shut the engine down (call at app exit; flush all pending writes).
#[no_mangle]
pub extern "C" fn tt_shutdown() {
    if let Some(store) = tm::TmStore::instance() {
        store.flush_all().ok();
    }
}

/// Full-featured translation (TM + routing + distillation; preferred entry for new code).
///
/// request_json follows TranslateRequest; every field has a default:
/// ```json
/// {
///   "input": "你好世界",
///   "source_lang": "zh",
///   "target_lang": "en",
///   "mode": "full",
///   "privacy": false,
///   "use_tm": true
/// }
/// ```
#[no_mangle]
pub extern "C" fn tt_translate_full(request_json: *const c_char) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let raw = unsafe { read_cstr(request_json) }
            .ok_or_else(|| EngineError::InvalidInput("null or non-UTF8".into()))?;
        let req = TranslateRequest::from_json(&raw)
            .map_err(EngineError::InvalidInput)?;
        match pipeline::translate_full(req) {
            Ok(resp) => Ok(resp.to_json()),
            Err(err) => Err(err),
        }
    }));
    let s = match result {
        Ok(Ok(s))   => s,
        Ok(Err(e))  => err_response(e),
        Err(_)      => PANIC_JSON.to_string(),
    };
    to_c_string(&s)
}

/// Look up the TM (never triggers translation; only checks history).
/// Returns TmEntry JSON on a hit; "null" on a miss.
#[no_mangle]
pub extern "C" fn tt_tm_lookup(
    text: *const c_char,
    source_lang: *const c_char,
    target_lang: *const c_char,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let t = unsafe { read_cstr(text) }.unwrap_or_default();
        let s = unsafe { read_cstr(source_lang) }.unwrap_or_default();
        let g = unsafe { read_cstr(target_lang) }.unwrap_or_default();
        match tm::lookup(&t, &s, &g) {
            Ok(Some(entry)) => entry.to_json().to_string(),
            Ok(None)        => "null".to_string(),
            Err(e)          => e.to_json().to_string(),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| "null".to_string()))
}

/// Write one translation pair into the TM (user-confirmed or manual import).
/// entry_json shape: {"source_text":"...","source_lang":"zh","target_text":"...","target_lang":"en","engine":"manual","quality":1.0}
#[no_mangle]
pub extern "C" fn tt_tm_put(entry_json: *const c_char) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let raw = unsafe { read_cstr(entry_json) }.unwrap_or_default();
        match serde_json::from_str::<serde_json::Value>(&raw) {
            Ok(v) => {
                let entry = types::TmEntry::from_json(&v);
                tm::put(&entry).map(|_| OK_JSON.to_string())
                    .unwrap_or_else(|e| e.to_json().to_string())
            }
            Err(e) => EngineError::InvalidInput(e.to_string()).to_json().to_string(),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}

/// Mark one TM entry as bad (soft delete): it stops being returned by lookups and
/// disappears from tt_tm_list, while the record stays on disk for later inspection.
/// Returns {"ok":true}; the write is a no-op when the entry is unknown.
#[no_mangle]
pub extern "C" fn tt_tm_flag_bad(
    text: *const c_char,
    source_lang: *const c_char,
    target_lang: *const c_char,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let t = unsafe { read_cstr(text) }.unwrap_or_default();
        let s = unsafe { read_cstr(source_lang) }.unwrap_or_default();
        let g = unsafe { read_cstr(target_lang) }.unwrap_or_default();
        match tm::flag_bad(&t, &s, &g) {
            Ok(())  => OK_JSON.to_string(),
            Err(e)  => e.to_json().to_string(),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}

/// List the active TM entries of one language pair (JSON array, most-hit first).
/// Backs the UI TM manager; `limit` is clamped to a sane maximum.
#[no_mangle]
pub extern "C" fn tt_tm_list(
    source_lang: *const c_char,
    target_lang: *const c_char,
    limit: u32,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let s = unsafe { read_cstr(source_lang) }.unwrap_or_default();
        let t = unsafe { read_cstr(target_lang) }.unwrap_or_default();
        let n = (limit as usize).clamp(1, 1000);
        match tm::list(&s, &t, n) {
            Ok(entries) => {
                let vals: Vec<serde_json::Value> = entries.iter().map(|e| e.to_json()).collect();
                serde_json::to_string(&vals).unwrap_or_else(|_| "[]".into())
            }
            Err(e) => e.to_json().to_string(),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| "[]".to_string()))
}

/// Insert or update one glossary term as a user edit.
///
/// entry_json shape: {"source_term":"...","source_lang":"zh","target_term":"...",
/// "target_lang":"en","confidence":1.0,"frequency":1,"domain":"","user_locked":true?}
///
/// Unlike a machine write (distillation, seed-pack import), which the store may keep,
/// a user edit always lands: what the person typed is what the glossary says, whatever
/// confidence the stored row carries (defect D49 -- this path used to obey the
/// strictly-higher rule, so a second edit of the same term silently changed nothing
/// while the ack answered `ok`). `user_locked` is optional: absent means "leave the
/// lock as the row has it", present means "set it to that" (D35: the flag existed in
/// the store and nothing could reach it). The ack echoes the resulting lock state, so a
/// UI can show the lock without guessing.
#[no_mangle]
pub extern "C" fn tt_glossary_upsert(entry_json: *const c_char) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let raw = unsafe { read_cstr(entry_json) }.unwrap_or_default();
        match serde_json::from_str::<serde_json::Value>(&raw) {
            Ok(v) => {
                let lock = v.get("user_locked").and_then(|x| x.as_bool());
                let entry = types::GlossaryEntry::from_json(&v);
                // Provenance is stamped by the store's manual door, not here: exactly one
                // place decides that a row reaching this endpoint was written by a person,
                // so a payload claiming `distill` cannot survive it.
                match tm::glossary_upsert_manual(&entry, lock) {
                    Ok(state) => serde_json::to_string(&serde_json::json!({
                        "ok": true, "user_locked": state.user_locked,
                    })).unwrap_or_else(|_| OK_JSON.to_string()),
                    Err(e) => e.to_json().to_string(),
                }
            }
            Err(e) => EngineError::InvalidInput(e.to_string()).to_json().to_string(),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}

/// Delete one glossary term by its (source_term, source_lang, target_lang) key.
///
/// That key addresses the **generic** row only. New callers should use
/// `tt_glossary_delete_with_domain`; this one is kept because the export surface is a frozen
/// contract ("add only, never change or remove within a major version") and widening its arity
/// in place would be silently wrong for embedders compiled against it -- measured: a call that
/// omits the new parameter still returns a plausible answer, it just aims at another row.
/// The ack says `removed: false` when no row matched, rather than a bare success.
#[no_mangle]
pub extern "C" fn tt_glossary_delete(
    source_term: *const c_char,
    source_lang: *const c_char,
    target_lang: *const c_char,
) -> *mut c_char {
    delete_impl(source_term, source_lang, target_lang, None)
}

/// D50: delete one glossary row by its full primary key, which includes the domain. The
/// listing has been domain-scoped since S11 (`tt_glossary_list_with_domain`), so a row that
/// came from a seed pack could be seen but not removed; this is the symmetric entry point.
/// An empty `domain` is exactly `tt_glossary_delete`.
#[no_mangle]
pub extern "C" fn tt_glossary_delete_with_domain(
    source_term: *const c_char,
    source_lang: *const c_char,
    target_lang: *const c_char,
    domain: *const c_char,
) -> *mut c_char {
    let d = unsafe { read_cstr(domain) }.unwrap_or_default();
    delete_impl(source_term, source_lang, target_lang, Some(d))
}

fn delete_impl(
    source_term: *const c_char, source_lang: *const c_char, target_lang: *const c_char,
    domain: Option<String>,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let term = unsafe { read_cstr(source_term) }.unwrap_or_default();
        let s = unsafe { read_cstr(source_lang) }.unwrap_or_default();
        let t = unsafe { read_cstr(target_lang) }.unwrap_or_default();
        let removed = match domain {
            None => tm::glossary_delete(&term, &s, &t),
            Some(d) => tm::glossary_delete_with_domain(&term, &s, &t, &d),
        };
        match removed {
            Ok(removed) => serde_json::to_string(&serde_json::json!({
                "ok": true, "removed": removed,
            })).unwrap_or_else(|_| OK_JSON.to_string()),
            Err(e)  => e.to_json().to_string(),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}

/// List available engines (JSON array).
#[no_mangle]
pub extern "C" fn tt_engines() -> *mut c_char {
    let list: Vec<_> = engine::available_engines()
        .iter()
        .map(|(id, desc)| serde_json::json!({"id": id, "name": desc}))
        .collect();
    to_c_string(&serde_json::to_string(&list).unwrap_or_else(|_| "[]".into()))
}

/// Health check (version, TM entry count, ...).
#[no_mangle]
pub extern "C" fn tt_health() -> *mut c_char {
    let tm_count = tm::total_entries().unwrap_or(0);
    let json = serde_json::json!({
        "status": "ok",
        "version": VERSION,
        "tm_entries": tm_count,
        "engines": engine::available_engines().iter().map(|(id,_)| *id).collect::<Vec<_>>(),
    });
    to_c_string(&json.to_string())
}

/// Fetch the glossary (JSON array).
#[no_mangle]
pub extern "C" fn tt_glossary_list(
    source_lang: *const c_char,
    target_lang: *const c_char,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let s = unsafe { read_cstr(source_lang) }.unwrap_or_default();
        let t = unsafe { read_cstr(target_lang) }.unwrap_or_default();
        // S11: an empty request domain returns only generic rows -- which is exactly
        // what every pre-S11 glossary contained, so this stays visually identical for
        // existing users. Callers that want a domain-scoped view go through
        // `tt_glossary_list_with_domain`.
        // Rows are serialized as full records, so `user_locked` travels with them: a UI
        // cannot show a lock it cannot read (D35).
        let rows = tm::glossary_records(&s, &t, "", 500).unwrap_or_default();
        let vals: Vec<serde_json::Value> = rows.iter().map(|r| r.to_value()).collect();
        serde_json::to_string(&vals).unwrap_or_else(|_| "[]".into())
    }));
    to_c_string(&result.unwrap_or_else(|_| "[]".to_string()))
}

/// S11: domain-scoped glossary fetch. `domain` empty == generic rows only;
/// `domain="av"` == generic ∪ av (specific rows win over same-source generic
/// ones). The store filter matches `tm::glossary_list`'s contract.
#[no_mangle]
pub extern "C" fn tt_glossary_list_with_domain(
    source_lang: *const c_char,
    target_lang: *const c_char,
    domain: *const c_char,
    limit: u32,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let s = unsafe { read_cstr(source_lang) }.unwrap_or_default();
        let t = unsafe { read_cstr(target_lang) }.unwrap_or_default();
        let d = unsafe { read_cstr(domain) }.unwrap_or_default();
        let cap = if limit == 0 { 500usize } else { limit as usize };
        let rows = tm::glossary_records(&s, &t, &d, cap).unwrap_or_default();
        let vals: Vec<serde_json::Value> = rows.iter().map(|r| r.to_value()).collect();
        serde_json::to_string(&vals).unwrap_or_else(|_| "[]".into())
    }));
    to_c_string(&result.unwrap_or_else(|_| "[]".to_string()))
}

/// S11: import one seed pack in a single call. `pack_json` shape:
/// `{"domain":"av","version":"...","entries":[{source_term,target_term,confidence?},...]}`.
/// A non-empty pack-level domain is mandatory; every entry inherits that domain
/// and the source tag `seed:<domain>`. Returns
/// `{"ok":true,"imported":N,"written":W,"skipped_locked":L,"skipped_not_higher":K}` or an
/// `{"ok":false,"error":"..."}` shape so a caller can distinguish "no data" from
/// "schema wrong". `imported` is the number of entries the pack carried -- the value it
/// has always had, so a released installer keeps reading it; the three counters beside it
/// say what the store did with them (D49: before, a pack that displaced nothing still
/// answered `imported: N`).
#[no_mangle]
pub extern "C" fn tt_glossary_import_pack(pack_json: *const c_char) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        let raw = unsafe { read_cstr(pack_json) }.unwrap_or_default();
        match tm::glossary_import_pack(&raw) {
            Ok(r) => serde_json::to_string(&serde_json::json!({
                "ok": true, "imported": r.rows, "written": r.written,
                "skipped_locked": r.skipped_locked, "skipped_not_higher": r.skipped_not_higher,
            })).unwrap_or_else(|_| "{\"ok\":true}".into()),
            Err(e) => e.to_json().to_string(),
        }
    }));
    to_c_string(&result.unwrap_or_else(|_| PANIC_JSON.to_string()))
}
