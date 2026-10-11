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
/// 200 only: anything else is `None`, which is the right answer for a caller that can use an
/// answer and nothing else. A caller that has to act on *why* it got no answer needs
/// [`parse_http_reply`] instead, because this function cannot tell a refusal from a crash.
pub fn response_body_from_http(response: &[u8]) -> Option<String> {
    let reply = parse_http_reply(response)?;
    if reply.status != 200 {
        return None;
    }
    Some(reply.body)
}

/// One complete answer from a service, before anyone judges whether it succeeded.
///
/// `response_body_from_http` throws the status away and that is fine for ollama, which has no
/// use for a refusal. It is not fine for the sidecar, which now has to tell "every decode slot
/// is busy, come back in five seconds -- nothing was started for you" apart from "this target
/// code is permanently unsupported": waiting on the first is cheap and recovers a translation,
/// retrying the second only multiplies the latency in front of an honest fallback (defect D30
/// item 2, external item 004). One header parser serves both views.
#[derive(Debug)]
pub struct HttpReply {
    pub status: u16,
    pub body: String,
    headers: Vec<(String, String)>,
}

impl HttpReply {
    /// A header value, matched case-insensitively because HTTP field names are.
    pub fn header(&self, name: &str) -> Option<&str> {
        let wanted = name.to_ascii_lowercase();
        self.headers.iter()
            .find(|(key, _)| key.to_ascii_lowercase() == wanted)
            .map(|(_, value)| value.as_str())
    }
}

/// Split raw response bytes into status, headers and body.
///
/// Only a response with a parsable status line and a header terminator is an answer at all. A
/// truncated read or non-UTF-8 bytes give `None`: that is a transport failure, not something
/// the service said, and collapsing the two is how a half-read connection would end up
/// looking like a status that was never sent.
pub fn parse_http_reply(response: &[u8]) -> Option<HttpReply> {
    let text = std::str::from_utf8(response).ok()?;
    let head_end = text.find("\r\n\r\n")?;
    let mut lines = text[..head_end].split("\r\n");
    let status = lines.next()?.split_whitespace().nth(1)?.parse::<u16>().ok()?;
    let headers = lines.filter_map(|line| {
        let (name, value) = line.split_once(':')?;
        Some((name.trim().to_string(), value.trim().to_string()))
    }).collect();
    Some(HttpReply { status, body: text[head_end + 4..].to_string(), headers })
}

/// POST a JSON and keep whatever the service answered, status included. Transport failures
/// are still `None`.
pub fn http_post_reply(
    url: &str,
    payload: &serde_json::Value,
    timeout: Duration,
) -> Option<HttpReply> {
    let body = serde_json::to_vec(payload).ok()?;
    let request = build_http_post(url, &body)?;
    let response = tcp_roundtrip(url, &request, timeout)?;
    parse_http_reply(&response)
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

    #[test]
    fn a_reply_keeps_the_status_and_reads_headers_case_insensitively() {
        let resp = b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\n\
                    retry-after: 5\r\n\r\n{\"error\":\"busy\"}";
        let reply = parse_http_reply(resp).expect("a status line and a body are an answer");
        assert_eq!(429, reply.status);
        assert_eq!(Some("5"), reply.header("Retry-After"),
            "field names are case-insensitive on the wire; matching one spelling exactly \
             would silently drop the promise the retry depends on");
        assert_eq!("{\"error\":\"busy\"}", reply.body);
    }

    #[test]
    fn a_reply_expresses_an_error_that_the_200_only_view_cannot() {
        // The reason `HttpReply` exists rather than another bool: a 503 is a fact a caller may
        // act on, and `None` cannot hold it.
        let resp = b"HTTP/1.1 503 Service Unavailable\r\n\r\n{\"error\":\"backend_unavailable\"}";
        assert!(response_body_from_http(resp).is_none(),
            "the 200-only view must stay 200-only, or ollama starts reading error bodies as answers");
        assert_eq!(503, parse_http_reply(resp).expect("a refusal is still an answer").status);
    }

    #[test]
    fn bytes_that_are_not_an_answer_stay_a_transport_failure() {
        // A truncated read, a status line without a code and non-UTF-8 noise are all `None`.
        // Treating any of them as a refusal is what would let a half-open connection be
        // retried for the whole budget.
        assert!(parse_http_reply(b"HTTP/1.1 200 OK").is_none(), "no header terminator");
        assert!(parse_http_reply(b"garbage\r\n\r\n").is_none(), "no status code");
        assert!(parse_http_reply(&[0xff, 0xfe, 0x00]).is_none(), "not UTF-8");
    }
}
