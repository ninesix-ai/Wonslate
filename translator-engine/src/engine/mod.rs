// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Engine registration and the unified Translator interface.
//!
//! Every engine implements the same trait; callers hot-swap by need.

pub mod demo;
pub mod ollama;
pub mod http;
pub mod sidecar;

use crate::glossary::GlossaryContext;

/// Unified translation-engine interface (v2.0: adds translate_with_context)
pub trait Translator: Send + Sync {
    fn name(&self) -> &'static str;

    /// Basic translation (no glossary context).
    /// None means this engine cannot handle the input; the router falls back.
    fn translate(&self, text: &str, source: &str, target: &str) -> Option<String>;

    /// Translation with glossary context (advanced engines like ollama.rs override this).
    /// The default degrades to the no-glossary call for backward compatibility.
    fn translate_with_context(
        &self,
        text: &str,
        source: &str,
        target: &str,
        _glossary: &GlossaryContext,
    ) -> Option<String> {
        self.translate(text, source, target)
    }

    /// Whether this engine can act on glossary term context at all.
    ///
    /// Default is `false`, which matches `translate_with_context`'s default body -
    /// it drops the context and calls the plain translate. Engines that weave the
    /// terms into a prompt override this to `true`. Engines that cannot honour them
    /// must not, because claiming `true` lets a request carrying a domain look
    /// scoped when nothing applied it (same spirit as REQ-B2).
    fn accepts_term_context(&self) -> bool {
        false
    }

    /// Whether this engine looks reachable right now, cheaply.
    ///
    /// The default is `true`: engines that fail fast need no probe, and answering
    /// `true` keeps the pre-probe behaviour. Engines that dial a service somebody has
    /// to be running (ollama) override it, because attempting them when they are down
    /// burns the whole request timeout for nothing.
    fn is_reachable(&self) -> bool {
        true
    }

    /// The address this engine would dial, when it dials one at all.
    ///
    /// `None` for engines that run in-process. Exposed because the privacy message has
    /// to name what was refused: an operator who set `WONSLATE_MADLAD_URL` and watched
    /// the engine get clamped needs to see the address that caused it, not guess.
    fn endpoint(&self) -> Option<&str> {
        None
    }

    /// Whether every address this engine can dial stays on this machine.
    ///
    /// Default `true` -- an engine that makes no network calls cannot leak. A networked
    /// engine overrides it, and answers from the address it *resolved*, never from a
    /// shipped constant, because the resolved address is the one the text travels to.
    fn reaches_only_loopback(&self) -> bool {
        true
    }
}

/// Is `url`'s host this machine's loopback?
///
/// Deliberately narrow, and it fails closed. Accepted: an IPv4 in 127.0.0.0/8 written
/// as four decimal octets, the IPv6 `::1`, and the name `localhost`. Rejected:
/// everything else, including shapes that a given stack might happen to route locally
/// (`127.1`, `0.0.0.0`, an IPv4-mapped `::ffff:127.0.0.1`, a missing or unparseable
/// scheme). The reason for the narrowness is the asymmetry of the mistake: a false
/// `true` here ships source text off the device while a false `false` only costs a
/// fallback that the response says out loud.
///
/// Written by hand rather than pulled from a URL crate on purpose -- every new
/// dependency has to be registered in `license_gate.rs` `REGISTERED_DEPS`, and this
/// needs eight lines of parsing.
pub fn url_host_is_loopback(url: &str) -> bool {
    let Some(rest) = url.split_once("://").map(|(_, tail)| tail) else {
        return false;
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    // A userinfo section may contain '@' and even ':'; the host is after the last one.
    let hostport = authority.rsplit_once('@').map(|(_, h)| h).unwrap_or(authority);

    if let Some(bracketed) = hostport.strip_prefix('[') {
        return bracketed.split(']').next().unwrap_or("") == "::1";
    }
    // A bare ':' in an unbracketed authority that is not a hostname:port pair is a
    // malformed IPv6 literal -- take the part before the last colon and let the checks
    // below refuse anything that is not one of the three accepted shapes.
    let host = hostport.rsplit_once(':').map(|(h, _)| h).unwrap_or(hostport);
    let host = host.trim_end_matches('.').to_ascii_lowercase();

    if host == "localhost" {
        return true;
    }
    let mut octets = host.split('.');
    let (Some(first), true) = (octets.next(), host.starts_with("127.")) else {
        return false;
    };
    first == "127"
        && octets.count() == 3
        && host
            .split('.')
            .all(|o| !o.is_empty() && o.len() <= 3 && o.parse::<u8>().is_ok())
}

/// Whether an engine may run while `privacy=true` is in force.
///
/// Two independent axes, and both have to hold. The id whitelist (`is_local_engine`) is
/// necessary but not sufficient: `argos` and `madlad` are known-local *by id* while
/// their address comes from `ARGOS_URL` / `MADLAD_URL` through the same env prefix
/// chain that excluded `ollama` for exactly that reason (defect D68). So a request is
/// privacy-safe only if the id is whitelisted *and* the engine that id resolves to
/// dials nothing outside this machine.
pub fn is_privacy_safe(id: &str) -> bool {
    let engine = get_engine(id);
    is_local_engine(id) && engine.reaches_only_loopback()
}

/// Reachability of an engine id, without holding an instance.
pub fn is_reachable(id: &str) -> bool {
    get_engine(id).is_reachable()
}

/// Whether an engine id can act on term context; unknown ids fail closed.
pub fn accepts_term_context(id: &str) -> bool {
    get_engine(id).accepts_term_context()
}

/// Get an engine instance by id; unknown ids fall back to demo, never a null pointer.
pub fn get_engine(id: &str) -> Box<dyn Translator> {
    match id {
        "demo" => Box::new(demo::DemoTranslator::default()),
        "ollama" | "qwen" | "ollama-qwen" => Box::new(ollama::OllamaTranslator::new()),
        "argos" => Box::new(sidecar::SidecarTranslator::argos()),
        "madlad" => Box::new(sidecar::SidecarTranslator::madlad()),
        _ => Box::new(demo::DemoTranslator::default()),
    }
}

/// Whether the engine id is genuinely registered. Stays aligned with get_engine's real
/// branches (no unknown-id fallback) so callers can expose why a fallback happened.
pub fn is_available(id: &str) -> bool {
    matches!(id, "demo" | "ollama" | "qwen" | "ollama-qwen" | "argos" | "madlad")
}

/// Whether an engine id is guaranteed to run fully on this machine.
///
/// Privacy-mode invariant basis: fail-closed -- only ids we know to be local
/// (built-in dictionary, or a 127.0.0.1 sidecar slot) count as local. Any
/// other id, including LLM-backed engines whose URL is env-overridable
/// (ollama/qwen) and anything unregistered, is treated as non-local.
pub fn is_local_engine(id: &str) -> bool {
    matches!(id, "demo" | "argos" | "madlad")
}

/// Catalog of supported engines (for UI display and routing config).
pub fn available_engines() -> Vec<(&'static str, &'static str)> {
    vec![
        ("demo",   "演示引擎（内置词表，兜底用）"),
        ("ollama", "AI 精译引擎（本机 Ollama + Qwen3，质量最高）"),
        ("argos",  "实时档引擎（本机 Argos/CTranslate2 sidecar）"),
        ("madlad", "冷门语言兜底（本机 MADLAD-400 sidecar，Apache-2.0）"),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_engine_falls_back_to_demo() {
        let e = get_engine("no-such-engine");
        assert_eq!(e.name(), "demo");
    }

    #[test]
    fn ollama_registered() {
        let e = get_engine("ollama");
        assert_eq!(e.name(), "ollama-qwen");
    }

    #[test]
    fn argos_and_madlad_registered() {
        // Phase 2 sidecar engines are registered (no more silent demo fallback)
        assert_eq!(get_engine("argos").name(), "argos");
        assert_eq!(get_engine("madlad").name(), "madlad");
    }

    #[test]
    fn available_has_demo_and_ollama() {
        let ids: Vec<_> = available_engines().iter().map(|(id, _)| *id).collect();
        assert!(ids.contains(&"demo"));
        assert!(ids.contains(&"ollama"));
        assert!(ids.contains(&"argos"));
        assert!(ids.contains(&"madlad"));
    }

    #[test]
    fn is_available_true_for_registered() {
        assert!(is_available("demo"));
        assert!(is_available("ollama"));
        assert!(is_available("qwen"));
        assert!(is_available("ollama-qwen"));
        // Phase 2: argos/madlad are genuinely available now
        assert!(is_available("argos"));
        assert!(is_available("madlad"));
    }

    #[test]
    fn is_available_false_for_unregistered() {
        // Still-unregistered ids must return false (so callers can expose the fallback)
        assert!(!is_available("bert"));
        assert!(!is_available("no-such-engine"));
    }

    #[test]
    fn local_engines_are_privacy_safe() {
        // Privacy-mode invariant basis, id axis only: an engine that never leaves the
        // machine by construction. This is necessary but not sufficient -- `argos` and
        // `madlad` pass here while their address comes from the environment, which is the
        // whole of defect D68. The address axis is `only_the_loopback_shapes_count_as_on_
        // device` below, and the two together are covered end to end by
        // `tests/privacy_loopback_e2e.rs`.
        assert!(is_local_engine("demo"));
        assert!(is_local_engine("argos"));
        assert!(is_local_engine("madlad"));
    }

    #[test]
    fn ollama_is_not_a_local_engine_for_privacy_purposes() {
        // Ollama reaches an LLM over HTTP and its URL is env-overridable,
        // so privacy mode must treat it as non-local.
        assert!(!is_local_engine("ollama"));
        assert!(!is_local_engine("qwen"));
        assert!(!is_local_engine("ollama-qwen"));
    }

    #[test]
    fn unknown_engines_are_not_local() {
        // Fail-closed: an id we do not know must default to "not local".
        assert!(!is_local_engine("no-such-engine"));
    }

    #[test]
    fn only_the_loopback_shapes_count_as_on_device() {
        // Table-driven, because the cases that matter are the shapes some stack might
        // happen to route locally and that must still be refused: a false `true` here
        // ships the user's text to another host.
        let accepted = [
            "http://127.0.0.1:11435",
            "http://127.5.5.5:11436/translate",
            "http://localhost:11436",
            "http://LOCALHOST:11436",
            "http://[::1]:11435",
            "http://user:pass@127.0.0.1:11435",   // userinfo must not hide the host
        ];
        let refused = [
            "http://192.168.2.121:50621",          // the address D68 was measured on
            "http://10.0.0.5:11435",
            "https://translate.example.com",
            "http://127.0.0.1.evil.com:11435",     // looks like 127/8, is a hostname
            "http://127.1:11435",                  // shorthand, not four octets
            "http://0.0.0.0:11435",                // reaches every interface
            "http://[::ffff:127.0.0.1]:11435",     // IPv4-mapped, not the loopback literal
            "127.0.0.1:11435",                     // no scheme: nothing to prove
            "",
            "http://",
            "http://:11435",
        ];
        for url in accepted {
            assert!(url_host_is_loopback(url), "loopback shape refused: {url}");
        }
        for url in refused {
            assert!(!url_host_is_loopback(url), "off-device shape accepted: {url}");
        }
    }

    #[test]
    fn the_address_verdict_survives_being_held_as_a_trait_object() {
        // The pipeline holds Box<dyn Translator>, and SidecarTranslator also has an
        // inherent endpoint(); an inherent method shadowing the trait one would leave the
        // clamp reading a different answer than this test asserts.
        use std::time::Duration;
        let local = sidecar::SidecarTranslator::with_url(
            "madlad", "http://127.0.0.1:11436".into(), Duration::from_millis(200));
        let remote = sidecar::SidecarTranslator::with_url(
            "madlad", "http://192.168.2.121:11436".into(), Duration::from_millis(200));
        assert!(local.reaches_only_loopback());
        assert!(!remote.reaches_only_loopback());

        let boxed: Box<dyn Translator> = Box::new(remote);
        assert!(!boxed.reaches_only_loopback(), "dyn dispatch lost the address verdict");
        assert_eq!(boxed.endpoint(), Some("http://192.168.2.121:11436"));
    }

    #[test]
    fn privacy_safety_needs_both_axes_and_never_asks_the_host() {
        // Asserted only where the answer cannot depend on the machine: demo reads no
        // environment, and the id axis settles ollama and unknown ids whatever the
        // environment says. argos/madlad deliberately NOT asserted here -- their verdict
        // depends on ARGOS_URL/MADLAD_URL, so testing them at this level would let the
        // developer's shell decide the outcome (the exact shape of D36/D41). That pair is
        // covered in tests/privacy_loopback_e2e.rs, on a pinned environment.
        assert!(is_privacy_safe("demo"));
        assert!(!is_privacy_safe("ollama"));
        assert!(!is_privacy_safe("qwen"));
        assert!(!is_privacy_safe("no-such-engine"));
    }

    #[test]
    fn term_context_capability_matches_the_backends() {
        // ollama composes the terms into the prompt; the dictionary engine and
        // the CT2 / MADLAD sidecars cannot act on them (the sidecar backends say so
        // in their own docstrings), so claiming `true` for those would let a scoped
        // request look honoured when nothing applied it.
        assert!(get_engine("ollama").accepts_term_context());
        assert!(!get_engine("demo").accepts_term_context());
        assert!(!get_engine("argos").accepts_term_context());
        assert!(!get_engine("madlad").accepts_term_context());
        // Fail-closed on ids we do not know, same as is_local_engine.
        assert!(!accepts_term_context("no-such-engine"));
    }
}
