// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! MCP (Model Context Protocol) stdio server: lets AI clients call the local
//! Wonslate engine for translation.
//!
//! Until now the engine's only stable surface was the C FFI (`tt_*` in lib.rs),
//! which only the WPF shell speaks. AI assistants (ZCode, Claude Desktop,
//! Cursor, ...) had no way in. This binary closes that gap: it speaks MCP over
//! stdio -- newline-delimited JSON-RPC 2.0 -- and links the engine's rlib
//! directly, so there is no FFI and no DLL loading involved.
//!
//! The wire format needs nothing beyond `serde_json`, which the crate already
//! carries, so the zero-external-crate constraint (Smart App Control, see
//! engine/http.rs) still holds.
//!
//! Wire rules (spec 2024-11-05 .. 2025-06-18, tools-only server):
//! - One JSON-RPC message per stdin line; each response is one stdout line.
//! - stdout carries protocol messages ONLY. Engine logging already goes to
//!   stderr (the macros in lib.rs), which is what keeps the stream clean.
//! - Notifications (no `id`) never get a response.
//! - Tool failures are results with `isError: true`; unknown tools and unknown
//!   methods are JSON-RPC errors (-32602 / -32601).
//!
//! Build: `cargo build --release` puts `wonslate-mcp.exe` next to the cdylib.
//! Register with an MCP client (generic stdio shape):
//!   { "command": "<path>/wonslate-mcp.exe" }
//! The server shares `<data_dir>` with the WPF app, so glossary terms and TM
//! entries curated in the UI immediately shape what an AI client gets back.

use std::io::{BufRead, Write};

use translator_engine::{config, distill, engine, pipeline, tm, types::TranslateRequest};

const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Protocol versions this server can answer with. A tools-only server behaves
/// identically under all three; when the client asks for something we do not
/// know we reply with the oldest, because every client ever shipped understands
/// it (the spec only requires answering with *a* version we support).
const SUPPORTED_VERSIONS: [&str; 3] = ["2024-11-05", "2025-03-26", "2025-06-18"];

// JSON-RPC 2.0 reserved error codes.
const PARSE_ERROR: i64 = -32700;
const INVALID_REQUEST: i64 = -32600;
const METHOD_NOT_FOUND: i64 = -32601;
const INVALID_PARAMS: i64 = -32602;

fn main() {
    init_engine();

    let stdin = std::io::stdin();
    let mut out = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break }; // stdin closed: client is shutting us down
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue; // tolerate blank lines; they are not messages
        }
        let Some(response) = handle_message(trimmed) else { continue };
        // A write failure means stdout went away (client died / pipe broken):
        // nothing sensible to send anymore, so stop and flush state on the way out.
        if writeln!(out, "{}", response).is_err() || out.flush().is_err() {
            break;
        }
    }
    shutdown();
}

/// Mirror of `tt_init` without the FFI layer: same config precedence, same TM
/// store, same distillation thread, so MCP callers get exactly the behavior the
/// desktop app gets.
fn init_engine() {
    config::ensure_dirs().ok();
    config::load_with(None);
    let data_dir = config::data_dir().join("data");
    tm::init(&data_dir, config::get().tm_cache_size, config::get().tm_warmup_n)
        .map_err(|e| eprintln!("[mcp] TM init failed: {}", e))
        .ok();
    distill::init();
}

fn shutdown() {
    if let Some(store) = tm::TmStore::instance() {
        store.flush_all().ok();
    }
}

// ---- Message dispatch -----------------------------------------------------

/// Handle one raw stdin line; `None` means "produce no output" (notifications,
/// blank input). Returns the response as a compact single-line JSON string.
fn handle_message(raw: &str) -> Option<String> {
    let v: serde_json::Value = match serde_json::from_str(raw) {
        Ok(v) => v,
        Err(_) => {
            return Some(rpc_error(serde_json::Value::Null, PARSE_ERROR, "parse error"));
        }
    };

    // Batching was removed from MCP in 2025-06-18 and no client we target sends
    // it; a single explicit rejection beats silently answering only one element.
    if v.is_array() {
        return Some(rpc_error(
            serde_json::Value::Null,
            INVALID_REQUEST,
            "batch requests are not supported",
        ));
    }

    let has_id = v.get("id").is_some();
    let id = v.get("id").cloned().unwrap_or(serde_json::Value::Null);
    let method = match v.get("method").and_then(|m| m.as_str()) {
        Some(m) => m,
        // An object with an id but no method is a broken request, not a
        // notification: it deserves an error, not silence.
        None => {
            return has_id.then(|| {
                rpc_error(id, INVALID_REQUEST, "missing method")
            });
        }
    };

    // Notifications never get a response, whatever they are.
    if method.starts_with("notifications/") {
        return None;
    }
    if !has_id {
        return None; // a notification wearing an unknown method name
    }

    let response = match method {
        "initialize" => rpc_result(
            id,
            serde_json::json!({
                "protocolVersion": negotiate_version(&v),
                "capabilities": {"tools": {"listChanged": false}},
                "serverInfo": {
                    "name": "wonslate",
                    "title": "Wonslate local translation",
                    "version": VERSION,
                },
                "instructions": "Local offline-first translation engine. Use `translate` for text \
                    translation; it shares the translation memory and glossary with the Wonslate \
                    desktop app. Without a configured backend (Ollama / Argos sidecar / MADLAD \
                    sidecar) the built-in demo dictionary answers with rough word-level quality.",
            }),
        ),
        "ping" => rpc_result(id, serde_json::json!({})),
        "tools/list" => rpc_result(
            id,
            serde_json::json!({ "tools": tool_definitions() }),
        ),
        "tools/call" => {
            let name = v.pointer("/params/name").and_then(|n| n.as_str()).unwrap_or("");
            match call_tool(name, v.pointer("/params/arguments")) {
                Ok(tool) => rpc_result(id, tool_result_json(tool)),
                Err(msg) => rpc_error(id, INVALID_PARAMS, &msg),
            }
        }
        _ => rpc_error(id, METHOD_NOT_FOUND, &format!("method not found: {}", method)),
    };
    Some(response)
}

/// Echo the requested version when we speak it; otherwise fall back to the
/// oldest supported one (see SUPPORTED_VERSIONS for why).
fn negotiate_version(req: &serde_json::Value) -> &'static str {
    let requested = req
        .pointer("/params/protocolVersion")
        .and_then(|x| x.as_str())
        .unwrap_or("");
    SUPPORTED_VERSIONS
        .iter()
        .find(|v| **v == requested)
        .copied()
        .unwrap_or(SUPPORTED_VERSIONS[0])
}

fn rpc_result(id: serde_json::Value, result: serde_json::Value) -> String {
    serde_json::json!({"jsonrpc": "2.0", "id": id, "result": result}).to_string()
}

fn rpc_error(id: serde_json::Value, code: i64, message: &str) -> String {
    serde_json::json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
        .to_string()
}

// ---- Tool layer -----------------------------------------------------------

/// Outcome of one tool execution: the text handed back to the model, plus the
/// spec's `isError` flag (a failed execution is still a successful RPC).
struct ToolResult {
    text: String,
    is_error: bool,
}

impl ToolResult {
    fn ok(v: serde_json::Value) -> Self {
        Self { text: v.to_string(), is_error: false }
    }
    fn err(msg: &str) -> Self {
        Self {
            text: serde_json::json!({"ok": false, "error": msg}).to_string(),
            is_error: true,
        }
    }
}

fn tool_result_json(tool: ToolResult) -> serde_json::Value {
    serde_json::json!({
        "content": [{"type": "text", "text": tool.text}],
        "isError": tool.is_error,
    })
}

fn call_tool(name: &str, arguments: Option<&serde_json::Value>) -> Result<ToolResult, String> {
    // An absent arguments object behaves like an empty one, so zero-argument
    // tools can be called with `"arguments"` omitted entirely.
    let empty = serde_json::json!({});
    let args = arguments.unwrap_or(&empty);
    match name {
        "translate" => Ok(tool_translate(args)),
        "list_engines" => Ok(tool_list_engines()),
        "tm_lookup" => Ok(tool_tm_lookup(args)),
        "glossary_list" => Ok(tool_glossary_list(args)),
        "health" => Ok(tool_health()),
        other => Err(format!("unknown tool: {}", other)),
    }
}

fn tool_translate(args: &serde_json::Value) -> ToolResult {
    let input = args.get("input").and_then(|x| x.as_str()).unwrap_or("");
    if input.trim().is_empty() {
        return ToolResult::err("input is required and must be non-empty");
    }
    if args.get("target_lang").and_then(|x| x.as_str()).unwrap_or("").is_empty() {
        return ToolResult::err("target_lang is required (e.g. \"zh\", \"en\")");
    }
    // The tool arguments already are the TranslateRequest JSON shape that the
    // FFI entry documents, so parse them with the same function instead of
    // growing a second parser that would drift from it.
    let req = match TranslateRequest::from_json(&args.to_string()) {
        Ok(r) => r,
        Err(e) => return ToolResult::err(&e),
    };
    match pipeline::translate_full(req) {
        Ok(resp) => ToolResult { text: resp.to_json(), is_error: !resp.ok },
        Err(e) => ToolResult::err(&e.to_string()),
    }
}

fn tool_list_engines() -> ToolResult {
    let list: Vec<serde_json::Value> = engine::available_engines()
        .iter()
        .map(|(id, desc)| serde_json::json!({"id": id, "description": desc}))
        .collect();
    ToolResult::ok(serde_json::Value::Array(list))
}

fn tool_tm_lookup(args: &serde_json::Value) -> ToolResult {
    let get = |k: &str| args.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
    let (text, src, tgt) = (get("text"), get("source_lang"), get("target_lang"));
    if text.is_empty() || src.is_empty() || tgt.is_empty() {
        return ToolResult::err("text, source_lang and target_lang are all required");
    }
    // Wrapped in an explicit hit/miss object: a bare `null` is easy for a model
    // to misread as a failed call rather than an empty result.
    match tm::lookup(&text, &src, &tgt) {
        Ok(Some(entry)) => ToolResult::ok(serde_json::json!({"hit": true, "entry": entry.to_json()})),
        Ok(None) => ToolResult::ok(serde_json::json!({"hit": false})),
        Err(e) => ToolResult::err(&e.to_string()),
    }
}

fn tool_glossary_list(args: &serde_json::Value) -> ToolResult {
    let get = |k: &str| args.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
    let (src, tgt) = (get("source_lang"), get("target_lang"));
    if src.is_empty() || tgt.is_empty() {
        return ToolResult::err("source_lang and target_lang are both required");
    }
    let entries = tm::glossary_list(&src, &tgt, 500).unwrap_or_default();
    let list: Vec<serde_json::Value> = entries.iter().map(|e| e.to_json()).collect();
    ToolResult::ok(serde_json::Value::Array(list))
}

fn tool_health() -> ToolResult {
    ToolResult::ok(serde_json::json!({
        "status": "ok",
        "version": VERSION,
        "tm_entries": tm::total_entries().unwrap_or(0),
        "engines": engine::available_engines().iter().map(|(id, _)| *id).collect::<Vec<_>>(),
    }))
}

/// Tool catalogue handed out by `tools/list`. Schemas stay explicit per tool:
/// the model reading them is the caller, and `input` vs `target_lang` is the
/// difference between a translation and an error message.
fn tool_definitions() -> Vec<serde_json::Value> {
    vec![
        serde_json::json!({
            "name": "translate",
            "description": "Translate text with the local Wonslate engine. Shares the \
                translation memory and glossary with the Wonslate desktop app. Quality \
                depends on configured backends: demo (built-in zh/en dictionary, always \
                available, rough), argos / madlad (local sidecars, real NMT), ollama \
                (local LLM, best quality). With no backend reachable the demo dictionary \
                answers, so the result always says which engine produced it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Text to translate"},
                    "target_lang": {"type": "string", "description": "Target language code, e.g. \"zh\" or \"en\""},
                    "source_lang": {"type": "string", "description": "Source language code; \"auto\" by default"},
                    "engine_id": {"type": "string", "description": "Optional engine override: demo | ollama | argos | madlad. Empty = automatic routing"},
                    "mode": {"type": "string", "description": "\"full\" (best quality, default) or \"realtime\" (fast)"},
                    "domain": {"type": "string", "description": "Optional domain hint, e.g. \"it\", \"legal\""},
                    "privacy": {"type": "boolean", "description": "true forbids any AI upgrade; local engines only (default false)"},
                    "use_tm": {"type": "boolean", "description": "Consult and update the translation memory (default true)"}
                },
                "required": ["input", "target_lang"]
            }
        }),
        serde_json::json!({
            "name": "list_engines",
            "description": "List the translation engines built into this Wonslate binary. \
                Presence in this list does not mean the backend is running; use translate \
                and read the `engine` field to see what actually answered.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": true}
        }),
        serde_json::json!({
            "name": "tm_lookup",
            "description": "Exact-match lookup in the persistent translation memory \
                (UI-curated and past-translation entries). Never translates anything; \
                returns {\"hit\": false} on a miss.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Source text to look up"},
                    "source_lang": {"type": "string"},
                    "target_lang": {"type": "string"}
                },
                "required": ["text", "source_lang", "target_lang"]
            },
            "annotations": {"readOnlyHint": true}
        }),
        serde_json::json!({
            "name": "glossary_list",
            "description": "List glossary terms for one language pair. These terms are \
                enforced during translation, so checking them first tells you which \
                renderings the engine will already prefer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_lang": {"type": "string"},
                    "target_lang": {"type": "string"}
                },
                "required": ["source_lang", "target_lang"]
            },
            "annotations": {"readOnlyHint": true}
        }),
        serde_json::json!({
            "name": "health",
            "description": "Server health: engine version, TM entry count, registered engines.",
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": true}
        }),
    ]
}

// ---- Unit tests (in-process; wire behavior is covered by mcp_stdio_e2e.rs) --

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initialize_echoes_a_supported_version() {
        let req = r#"{"jsonrpc":"2.0","id":1,"method":"initialize",
            "params":{"protocolVersion":"2025-06-18"}}"#;
        let resp: serde_json::Value =
            serde_json::from_str(&handle_message(req).unwrap()).unwrap();
        assert_eq!(resp["result"]["protocolVersion"], "2025-06-18");
        assert_eq!(resp["result"]["serverInfo"]["name"], "wonslate");
        assert!(resp["result"]["capabilities"]["tools"].is_object());
        assert_eq!(resp["id"], 1);
    }

    #[test]
    fn initialize_answers_an_unknown_version_with_the_oldest_supported() {
        let req = r#"{"jsonrpc":"2.0","id":2,"method":"initialize",
            "params":{"protocolVersion":"1999-01-01"}}"#;
        let resp: serde_json::Value =
            serde_json::from_str(&handle_message(req).unwrap()).unwrap();
        assert_eq!(resp["result"]["protocolVersion"], "2024-11-05");
    }

    #[test]
    fn notifications_produce_no_response() {
        assert!(handle_message(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#).is_none());
        assert!(handle_message(r#"{"jsonrpc":"2.0","method":"anything/else"}"#).is_none());
        assert!(handle_message(r#"{"jsonrpc":"2.0","method":"tools/list"}"#).is_none());
    }

    #[test]
    fn parse_error_answers_with_null_id() {
        let resp: serde_json::Value =
            serde_json::from_str(&handle_message("{not json").unwrap()).unwrap();
        assert_eq!(resp["error"]["code"], PARSE_ERROR);
        assert!(resp["id"].is_null());
    }

    #[test]
    fn batch_requests_are_rejected() {
        let resp: serde_json::Value = serde_json::from_str(
            &handle_message(r#"[{"jsonrpc":"2.0","id":1,"method":"ping"}]"#).unwrap(),
        )
        .unwrap();
        assert_eq!(resp["error"]["code"], INVALID_REQUEST);
    }

    #[test]
    fn unknown_method_is_method_not_found() {
        let resp: serde_json::Value = serde_json::from_str(
            &handle_message(r#"{"jsonrpc":"2.0","id":3,"method":"resources/list"}"#).unwrap(),
        )
        .unwrap();
        assert_eq!(resp["error"]["code"], METHOD_NOT_FOUND);
    }

    #[test]
    fn ping_and_tools_list_work_without_engine_init() {
        let resp: serde_json::Value =
            serde_json::from_str(&handle_message(r#"{"jsonrpc":"2.0","id":4,"method":"ping"}"#).unwrap())
                .unwrap();
        assert_eq!(resp["result"], serde_json::json!({}));

        let resp: serde_json::Value =
            serde_json::from_str(&handle_message(r#"{"jsonrpc":"2.0","id":5,"method":"tools/list"}"#).unwrap())
                .unwrap();
        let names: Vec<&str> = resp["result"]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|t| t["name"].as_str())
            .collect();
        assert_eq!(names, ["translate", "list_engines", "tm_lookup", "glossary_list", "health"]);
    }

    #[test]
    fn responses_are_single_line() {
        // The stdio transport frames messages with newlines; any embedded raw
        // newline in a response would desynchronize every MCP client.
        for raw in [
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}"#,
            r#"{"jsonrpc":"2.0","id":2,"method":"ping"}"#,
        ] {
            let resp = handle_message(raw).unwrap();
            assert!(!resp.contains('\n'), "response must be one line: {:?}", resp);
        }
    }
}
