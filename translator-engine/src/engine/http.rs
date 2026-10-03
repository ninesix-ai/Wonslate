// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Pure-std HTTP/1.1 client (plain http only: no TLS stack, no external crates).
//!
//! Extracted from `ollama.rs` and shared by every engine that dials a local service
//! (ollama / argos / madlad), so it is not hand-copied three times. Constraint: zero external crates and no build-script/proc-macro, so
//! Smart App Control is not triggered. Every failure path returns None and the router falls back; it never panics.

use std::io::{Read, Write};
use std::time::Duration;

/// Parse host:port and path out of a URL.
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

/// Can a TCP connection be established at all? A connect-only liveness probe.
///
/// Used to decide whether an AI upgrade is worth attempting: a full request against a
/// port nobody listens on costs the whole configured timeout (measured 1036 ms with a
/// 1 s budget, and the default budget is 120 s), and the caller would learn nothing
/// except that the service is down. Keep `timeout` short for the same reason.
pub fn probe(url: &str, timeout: Duration) -> bool {
    let Some((host, port, _)) = parse_url(url) else {
        return false;
    };
    match format!("{}:{}", host, port).parse() {
        Ok(addr) => std::net::TcpStream::connect_timeout(&addr, timeout).is_ok(),
        Err(_) => false,
    }
}

/// Assemble HTTP/1.1 request bytes (POST when a body is present, GET otherwise).
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

/// Open the TCP connection, send the request, read the full response.
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

/// Strip the status line and headers from a raw HTTP response, returning only the body text.
pub fn response_body_from_http(response: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(response).ok()?;
    if !text.starts_with("HTTP/1.1 200") && !text.starts_with("HTTP/1.0 200") {
        return None;
    }
    let idx = text.find("\r\n\r\n")?;
    Some(text[idx + 4..].to_string())
}

/// Convenience wrapper: POST a JSON to url and return the parsed response JSON. Any step failing returns None.
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
    fn probe_reports_a_listening_socket_and_a_closed_one() {
        // The gate that stops an upgrade attempt against a service nobody runs. A real
        // listener makes the positive case deterministic instead of depending on
        // whatever happens to be listening on the machine.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let url = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());

        assert!(probe(&url, Duration::from_millis(500)), "a listening socket is reachable");

        drop(listener);
        assert!(!probe(&url, Duration::from_millis(200)), "a closed port is not");
    }

    #[test]
    fn probe_is_false_for_unparsable_or_non_http_urls() {
        assert!(!probe("not-a-url", Duration::from_millis(50)));
        assert!(!probe("https://127.0.0.1:443", Duration::from_millis(50)));
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
