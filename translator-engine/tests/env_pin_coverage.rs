// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Every environment variable the engine reads must be named by the hermetic table.
//!
//! Why this is a build gate rather than a habit. D36 was a suite that pinned one spelling of
//! one knob while the developer's shell exported another, so the suite drove the machine
//! instead of its fixture; D41 was production code reading the environment with no seam, so
//! which half of a prefix chain executed depended on the host. The answer was a table --
//! `tests/hermetic/mod.rs::KNOBS` -- that pins every spelling of every knob a suite names and
//! clears the rest.
//!
//! A table nobody checks is decoration. **D45** is the proof: a new knob shipped with
//! behaviour, tests, public docs and a decision record, and *nothing failed* when it was left
//! out of the table -- so an ambient `LT_SIDECAR_RETRY_MS` would have decided how long every
//! integration suite may wait, and the suites would still have gone green. Both tests below
//! fail on exactly that shape: the first reads the source and asks whether the table knows the
//! name, the second poisons the environment the way a developer's shell would and asks whether
//! the product still resolves to what the suite asked for.

mod hermetic;

use hermetic::KNOBS;
use std::collections::BTreeSet;
use std::path::Path;

/// Recollect every `LT_*` / `WONSLATE_*` literal in the crate's own sources.
///
/// Scans text rather than parsing Rust because the names we care about appear in string
/// literals *and* in the candidate lists built from them; a comment that mentions a knob is a
/// false positive we can afford, while a knob nobody thought to write down is not.
fn names_in_sources() -> BTreeSet<String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut found = BTreeSet::new();
    let mut queue = vec![root];
    while let Some(dir) = queue.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                queue.push(path);
                continue;
            }
            if path.extension().and_then(|e| e.to_str()) != Some("rs") {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&path) else { continue };
            for word in text.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_')) {
                // Keep real names only: prose about the chain itself (`LT_*`, `WONSLATE_*`)
                // tokenises to the bare prefix, and demanding a table entry for a wildcard
                // would teach the next reader to weaken this gate.
                let tail = word.strip_prefix("LT_")
                    .or_else(|| word.strip_prefix("WONSLATE_"));
                if tail.map_or(false, |t| t.starts_with(|c: char| c.is_ascii_uppercase())) {
                    found.insert(word.to_string());
                }
            }
        }
    }
    found
}

/// The spellings the table knows, flattened.
fn names_in_table() -> BTreeSet<&'static str> {
    KNOBS.iter().flat_map(|(_, spellings)| spellings.iter().copied()).collect()
}

#[test]
fn every_env_knob_the_engine_reads_is_pinned_by_the_hermetic_table() {
    let table = names_in_table();
    let unpin: Vec<String> = names_in_sources().into_iter()
        .filter(|name| !table.contains(name.as_str()))
        .collect();
    assert!(unpin.is_empty(),
        "D45: {} reads an environment name that tests/hermetic/mod.rs does not know: {:?}. \
         Add it with every spelling it accepts (legacy first) and state what the suites pin it \
         to -- otherwise an ambient export decides test behaviour on the developer's machine \
         while CI disagrees, and both are green",
        if unpin.len() == 1 { "one file" } else { "the crate" }, unpin);
}

#[test]
fn applying_a_spec_outranks_a_host_export_for_every_knob_it_names() {
    // The behavioral half of the same rule, because the table could list a name and still not
    // be applied to it. Poison the environment the way a developer's shell would, apply a
    // fresh Spec, and ask what the *product's own* resolution now says. Anything the suite
    // does not clear keeps the poisoned value -- that is D36 arriving again.
    let poisoned: &[(&str, &str)] = &[
        ("LT_SIDECAR_RETRY_MS", "999999"),
        ("WONSLATE_SIDECAR_RETRY_MS", "999999"),
        ("LT_SIDECAR_TIMEOUT_MS", "999999"),
        ("LT_ARGOS_URL", "http://the-developers-own-service:11435"),
    ];
    for (name, value) in poisoned {
        std::env::set_var(name, value);
    }

    let dir = std::env::temp_dir().join(format!("wonslate-env-pin-{}", std::process::id()));
    let spec = hermetic::Spec::new(&dir);
    spec.apply();

    let lookup = |k: &str| std::env::var(k).ok();
    let engine = translator_engine::engine::sidecar::SidecarTranslator::argos();
    assert_eq!(engine.busy_retry(), std::time::Duration::ZERO,
        "D45: a host LT_SIDECAR_RETRY_MS still decides how long the sidecar engine waits, so \
         integration suites are measuring this machine (the winning spelling resolved to {:?})",
        translator_engine::config::first_env(&lookup,
            translator_engine::engine::sidecar::RETRY_CANDIDATES));
    assert_ne!(engine.endpoint(), "http://the-developers-own-service:11435",
        "D36 again: the suite's own endpoint pin lost to an ambient LT_ARGOS_URL");

    for (name, _) in poisoned {
        std::env::remove_var(name);
    }
    let _ = std::fs::remove_dir_all(&dir);
}
