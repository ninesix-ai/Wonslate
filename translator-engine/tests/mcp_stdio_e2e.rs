// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Integration tests: drive the MCP stdio server as a real subprocess.
//!
//! The unit tests in `src/bin/wonslate-mcp.rs` cover message handling in-process;
//! these cover what only a real process can prove: newline framing on the wire,
//! the no-response rule for notifications, recovery after a malformed line, and
//! the tool layer running on top of a freshly initialized engine stack
//! (config + TM store + distill thread), exactly as an MCP client would see it.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

struct Server {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Server {
    /// Spawn one fresh server with its own data dir, the way an MCP client
    /// launches it: bare binary, configuration only through the environment.
    fn spawn(tag: &str) -> Self {
        // Fresh data dir per test: the server initializes the real config/TM
        // stack, and a shared directory would make tests order-dependent
        // through the translation memory.
        let dir = std::env::temp_dir().join(format!("wonslate-mcp-e2e-{}-{}", std::process::id(), tag));
        std::fs::create_dir_all(dir.join("data")).unwrap();
        let _ = std::fs::remove_file(dir.join("data/translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("data/glossary.json"));

        let mut child = Command::new(env!("CARGO_BIN_EXE_wonslate-mcp"))
            // Pin every routing outcome the same way tests/pipeline_e2e.rs does:
            // dead sidecar/AI endpoints so a developer running Ollama or a
            // sidecar locally cannot flip assertions, and the low_confidence
            // upgrade policy so full mode cannot wander onto the network.
            .env("WONSLATE_DATA_DIR", &dir)
            .env("WONSLATE_FULL_UPGRADE_POLICY", "low_confidence")
            .env("LT_OLLAMA_URL", "http://127.0.0.1:1")
            .env("LT_ARGOS_URL", "http://127.0.0.1:1")
            .env("LT_MADLAD_URL", "http://127.0.0.1:1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn wonslate-mcp");
        let stdin = child.stdin.take().expect("child stdin");
        let stdout = BufReader::new(child.stdout.take().expect("child stdout"));
        Server { child, stdin, stdout }
    }

    fn send(&mut self, msg: &Value) {
        writeln!(self.stdin, "{}", msg).expect("write request");
        self.stdin.flush().expect("flush request");
    }

    fn send_raw(&mut self, line: &str) {
        writeln!(self.stdin, "{}", line).expect("write raw line");
        self.stdin.flush().expect("flush raw line");
    }

    /// Block for exactly one response line. Deterministic because the server
    /// answers every request with one line and notifications never answer.
    fn read(&mut self) -> Value {
        let mut line = String::new();
        self.stdout.read_line(&mut line).expect("read response line");
        serde_json::from_str(line.trim()).expect("response is one JSON line")
    }

    fn rpc(&mut self, msg: Value) -> Value {
        self.send(&msg);
        self.read()
    }

    /// initialize + notifications/initialized, the handshake every MCP client
    /// performs before anything else.
    fn handshake(&mut self) -> Value {
        let resp = self.rpc(json!({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "wonslate-e2e", "version": "0"}
            }
        }));
        self.send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
        resp
    }

    /// Call a tool and return (raw result object, parsed text payload).
    fn tool(&mut self, name: &str, arguments: Value) -> (Value, Value) {
        let resp = self.rpc(json!({
            "jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}
        }));
        let result = resp.get("result").expect("tools/call returns a result").clone();
        let text = result
            .pointer("/content/0/text")
            .expect("content[0].text")
            .as_str()
            .expect("text content")
            .to_string();
        (result, serde_json::from_str(&text).unwrap_or(Value::Null))
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

// ─────────────────────────────────────────────────────────────────

#[test]
fn initialize_negotiates_and_notifications_stay_silent() {
    let mut s = Server::spawn("handshake");
    let resp = s.handshake();
    assert_eq!(resp["jsonrpc"], "2.0");
    assert_eq!(resp["id"], 1);
    assert_eq!(resp["result"]["protocolVersion"], "2024-11-05");
    assert_eq!(resp["result"]["serverInfo"]["name"], "wonslate");
    assert!(resp["result"]["capabilities"]["tools"].is_object());

    // A version we do not know gets the oldest one we speak: every client ever
    // shipped understands it, and the tools behave identically under all three.
    let resp = s.rpc(json!({
        "jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"}
    }));
    assert_eq!(resp["result"]["protocolVersion"], "2024-11-05");

    // Notifications never produce a line: the very next line after two bogus
    // notifications must be the answer to the ping that follows them.
    s.send(&json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
    s.send(&json!({"jsonrpc": "2.0", "method": "no/such/notification"}));
    s.send(&json!({"jsonrpc": "2.0", "id": 3, "method": "ping"}));
    let resp = s.read();
    assert_eq!(resp["id"], 3);
    assert_eq!(resp["result"], json!({}));
}

#[test]
fn tools_list_advertises_the_toolset() {
    let mut s = Server::spawn("tools-list");
    s.handshake();
    let resp = s.rpc(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}));
    let tools = resp["result"]["tools"].as_array().expect("tools array");
    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
    for expected in ["translate", "list_engines", "tm_lookup", "glossary_list", "health"] {
        assert!(names.contains(&expected), "missing tool: {}", expected);
    }
    for t in tools {
        assert!(t["inputSchema"].is_object(), "tool {} lacks a schema", t["name"]);
    }
}

#[test]
fn translate_tool_produces_output_offline() {
    let mut s = Server::spawn("translate");
    s.handshake();
    // Realtime mode with all sidecar endpoints pinned to a dead port: the
    // router must fall back to the built-in demo dictionary and still answer.
    let (result, payload) = s.tool("translate", json!({
        "input": "hello world", "source_lang": "en", "target_lang": "zh", "mode": "realtime"
    }));
    assert_eq!(result["isError"], false, "translate result: {}", result);
    assert_eq!(payload["ok"], true, "translate payload: {}", payload);
    let output = payload["output"].as_str().expect("output is a string");
    assert!(!output.trim().is_empty(), "output must be non-empty");
}

#[test]
fn translate_tool_reports_bad_arguments_as_iserror() {
    let mut s = Server::spawn("translate-bad");
    s.handshake();
    let (result, payload) = s.tool("translate", json!({"input": "   "}));
    assert_eq!(result["isError"], true);
    assert_eq!(payload["ok"], false);
}

#[test]
fn tm_lookup_and_health_round_trip() {
    let mut s = Server::spawn("tm-health");
    s.handshake();
    let (_, payload) = s.tool("tm_lookup", json!({
        "text": "never-translated-string", "source_lang": "en", "target_lang": "zh"
    }));
    assert_eq!(payload["hit"], false, "no entry exists yet: {}", payload);

    let (_, payload) = s.tool("health", json!({}));
    assert_eq!(payload["status"], "ok");
    assert!(payload["version"].is_string());
    assert!(payload["engines"].is_array());
}

#[test]
fn unknown_tool_is_invalid_params() {
    let mut s = Server::spawn("unknown-tool");
    s.handshake();
    let resp = s.rpc(json!({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "teleport", "arguments": {}}
    }));
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn unknown_method_is_method_not_found() {
    let mut s = Server::spawn("unknown-method");
    s.handshake();
    let resp = s.rpc(json!({"jsonrpc": "2.0", "id": 6, "method": "resources/list"}));
    assert_eq!(resp["error"]["code"], -32601);
}

#[test]
fn malformed_line_gets_parse_error_and_server_recovers() {
    let mut s = Server::spawn("malformed");
    s.handshake();
    s.send_raw("this is not json {");
    let resp = s.read();
    assert_eq!(resp["error"]["code"], -32700);
    assert!(resp["id"].is_null());
    // The stream must survive the garbage: the next request still gets answered.
    let resp = s.rpc(json!({"jsonrpc": "2.0", "id": 7, "method": "ping"}));
    assert_eq!(resp["result"], json!({}));
}

#[test]
fn batch_requests_are_rejected_cleanly() {
    let mut s = Server::spawn("batch");
    s.handshake();
    s.send_raw(
        r#"[{"jsonrpc":"2.0","id":8,"method":"ping"},{"jsonrpc":"2.0","id":9,"method":"ping"}]"#,
    );
    let resp = s.read();
    assert_eq!(resp["error"]["code"], -32600);
}
