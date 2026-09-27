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
        // Read the optional custom config_json (ignored in the MVP; file/embedded defaults are used directly)
        let _raw = unsafe { read_cstr(config_json) }.unwrap_or_else(|| "{}".into());

        config::ensure_dirs().ok();
        let routes_path = config::routes_yaml_path();
        config::load(&routes_path);

        let data_dir = config::data_dir().join("data");
        let (cache_sz, warmup_n) = (config::get().tm_cache_size, config::get().tm_warmup_n);
        tm::init(&data_dir, cache_sz, warmup_n)
            .map_err(|e| lt_error!("[tt_init] TM init failed: {}", e)).ok();

        distill::init();

        serde_json::json!({"ok": true, "version": VERSION}).to_string()
    }));
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
                tm::put(&entry).map(|_| r#"{"ok":true}"#.to_string())
                    .unwrap_or_else(|e| e.to_json().to_string())
            }
            Err(e) => EngineError::InvalidInput(e.to_string()).to_json().to_string(),
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
        let entries = tm::glossary_list(&s, &t, 500).unwrap_or_default();
        let vals: Vec<serde_json::Value> = entries.iter().map(|e| e.to_json()).collect();
        serde_json::to_string(&vals).unwrap_or_else(|_| "[]".into())
    }));
    to_c_string(&result.unwrap_or_else(|_| "[]".to_string()))
}
