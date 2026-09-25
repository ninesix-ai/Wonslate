// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

// ── 轻量日志宏（零外部依赖，stderr 输出）────────────────────────

#[macro_use]
mod logging {
    /// info 级：stderr 输出
    #[macro_export]
    macro_rules! lt_info  { ($($a:tt)*) => { eprintln!("[INFO ] {}", format!($($a)*)) } }
    /// warn 级
    #[macro_export]
    macro_rules! lt_warn  { ($($a:tt)*) => { eprintln!("[WARN ] {}", format!($($a)*)) } }
    /// error 级
    #[macro_export]
    macro_rules! lt_error { ($($a:tt)*) => { eprintln!("[ERROR] {}", format!($($a)*)) } }
    /// debug 级（release 模式静默，零开销）
    #[macro_export]
    macro_rules! lt_debug { ($($a:tt)*) => { } }
}

// 内部模块声明（pub 供 rlib 消费者：集成测试 / 未来 CLI / REST API 使用；
// 不影响 cdylib 动态导出符号——只由 #[no_mangle] extern "C" 决定）
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

// ── 内部工具函数 ──────────────────────────────────────────────────

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

/// 从 FFI 返回的错误响应字符串（统一格式）
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

// ── 稳定接口（P0 首版定型，签名永不改变）─────────────────────────

/// 返回引擎版本号。
#[no_mangle]
pub extern "C" fn tt_version() -> *mut c_char {
    to_c_string(VERSION)
}

/// 基础翻译入口（向后兼容，不含 TM/蒸馏）。
/// 新代码推荐用 tt_translate_full。
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

/// 释放由本 DLL 分配的字符串内存。
#[no_mangle]
pub extern "C" fn tt_free_string(ptr: *mut c_char) {
    if ptr.is_null() { return; }
    unsafe { drop(CString::from_raw(ptr)); }
}

// ── v2.0 新接口 ────────────────────────────────────────────────────

/// 初始化引擎（应用启动时调一次，打开 TM DB、加载配置、启动蒸馏线程）。
/// config_json 传 "{}" 即使用默认配置。
#[no_mangle]
pub extern "C" fn tt_init(config_json: *const c_char) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| {
        // 读取可选的自定义 config_json（MVP 先忽略，直接用文件/内嵌默认值）
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

/// 关闭引擎（退出应用时调用，刷新所有待写入数据）。
#[no_mangle]
pub extern "C" fn tt_shutdown() {
    if let Some(store) = tm::TmStore::instance() {
        store.flush_all().ok();
    }
}

/// 全功能翻译（含 TM + 路由 + 蒸馏，新代码推荐用此接口）。
///
/// request_json 格式见 TranslateRequest，所有字段均有默认值：
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

/// 查询 TM（不触发翻译，只检查是否有历史记录）。
/// 命中返回 TmEntry JSON；未命中返回 "null"。
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

/// 将一条翻译对写入 TM（用户确认或手动导入时使用）。
/// entry_json 格式：{"source_text":"...","source_lang":"zh","target_text":"...","target_lang":"en","engine":"manual","quality":1.0}
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

/// 列出可用引擎（返回 JSON 数组）。
#[no_mangle]
pub extern "C" fn tt_engines() -> *mut c_char {
    let list: Vec<_> = engine::available_engines()
        .iter()
        .map(|(id, desc)| serde_json::json!({"id": id, "name": desc}))
        .collect();
    to_c_string(&serde_json::to_string(&list).unwrap_or_else(|_| "[]".into()))
}

/// 健康检查（返回版本、TM 条目数等）。
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

/// 获取术语表（JSON 数组）。
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
