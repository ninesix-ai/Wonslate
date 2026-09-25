// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 纯 std HTTP/1.1 客户端（仅 http 明文，不引 TLS、不引外部 crate）。
//!
//! 从 `ollama.rs` 抽取而来，供 ollama / argos / madlad 等"调本机服务"的引擎共享，
//! 避免手抄三份。约束：Rust 侧零外部依赖，且这些函数不产生 build-script/proc-macro，
//! 不触发 Smart App Control。所有失败路径返回 None，由上层路由兜底，绝不 panic。

use std::io::{Read, Write};
use std::time::Duration;

/// 从 URL 解析 host:port 与 path。
pub fn parse_url(url: &str) -> Option<(String, u16, String)> {
    let rest = url.strip_prefix("http://")?;
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let (host, port) = match authority.split_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().ok()?),
        None => (authority.to_string(), 80),
    };
    Some((host, port, path.to_string()))
}

fn host_of(url: &str) -> Option<String> {
    parse_url(url).map(|(h, _, _)| h)
}

/// 组装 HTTP/1.1 POST 请求字节（无 body 时转为 GET）。
pub fn build_http_post(url: &str, body: &[u8]) -> Option<Vec<u8>> {
    let (_, _, path) = parse_url(url)?;
    let mut req = String::new();
    if body.is_empty() {
        req.push_str(&format!("GET {} HTTP/1.1\r\n", path));
    } else {
        req.push_str(&format!("POST {} HTTP/1.1\r\n", path));
    }
    req.push_str(&format!("Host: {}\r\n", host_of(url)?));
    req.push_str("Connection: close\r\n");
    req.push_str("Content-Type: application/json\r\n");
    if !body.is_empty() {
        req.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    req.push_str("Accept: */*\r\n\r\n");
    let mut bytes = req.into_bytes();
    bytes.extend_from_slice(body);
    Some(bytes)
}

/// 建立 TCP 连接并发送请求、读完整响应。
pub fn tcp_roundtrip(url: &str, request: &[u8], timeout: Duration) -> Option<Vec<u8>> {
    let (host, port, _) = parse_url(url)?;
    let addr = format!("{}:{}", host, port);
    let mut stream = std::net::TcpStream::connect_timeout(&addr.parse().ok()?, timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    stream.write_all(request).ok()?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).ok()?;
    Some(buf)
}

/// 从原始 HTTP 响应中剥离状态行/头部，仅返回 body 文本。
pub fn response_body_from_http(response: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(response).ok()?;
    if !text.starts_with("HTTP/1.1 200") && !text.starts_with("HTTP/1.0 200") {
        return None;
    }
    let idx = text.find("\r\n\r\n")?;
    Some(text[idx + 4..].to_string())
}

/// 便捷封装：向 url POST 一个 JSON，返回解析后的响应 JSON。任一步失败 → None。
pub fn http_post_json(
    url: &str,
    payload: &serde_json::Value,
    timeout: Duration,
) -> Option<serde_json::Value> {
    let body = serde_json::to_vec(payload).ok()?;
    let request = build_http_post(url, &body)?;
    let response = tcp_roundtrip(url, &request, timeout)?;
    let json_body = response_body_from_http(&response)?;
    serde_json::from_str(&json_body).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_url_works() {
        let (h, p, path) = parse_url("http://127.0.0.1:11434/v1/chat/completions").unwrap();
        assert_eq!(h, "127.0.0.1");
        assert_eq!(p, 11434);
        assert_eq!(path, "/v1/chat/completions");
    }

    #[test]
    fn build_http_post_has_length() {
        let req = build_http_post("http://127.0.0.1:11434/v1/chat/completions", b"{}").unwrap();
        let s = String::from_utf8(req).unwrap();
        assert!(s.contains("POST /v1/chat/completions"));
        assert!(s.contains("Content-Length: 2"));
    }

    #[test]
    fn response_body_rejects_non_200() {
        let resp = b"HTTP/1.1 500 Internal Server Error\r\n\r\nboom";
        assert!(response_body_from_http(resp).is_none());
    }

    #[test]
    fn response_body_extracts_body_on_200() {
        let resp = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true}";
        assert_eq!(response_body_from_http(resp).as_deref(), Some("{\"ok\":true}"));
    }
}
