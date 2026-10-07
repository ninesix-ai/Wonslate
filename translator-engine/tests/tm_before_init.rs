// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! What every TM and glossary entry point must do before `tt_init` has run.
//!
//! Separate binary on purpose: the store and the cache are process-global singletons, so
//! one initialised test anywhere would make these calls succeed and this file would prove
//! nothing. Kept apart, it exercises the only state in which the "not initialized" branch
//! is reachable at all.
//!
//! That branch is not decoration. The facade sits under the whole FFI surface, so a
//! caller that reaches it early -- a UI wiring a TM panel before engine startup, an
//! embedding bug, a future entry point that forgets the init order -- is answered here.
//! The contract that matters is that it is answered the way every other engine error is:
//! an explicit `TM_ERROR` the boundary can serialise. A panic would cross the FFI and
//! abort the host process; an empty `Ok` would look like "no memory entries" and be
//! indistinguishable from a genuinely empty store, which is the silent-degradation shape
//! REQ-B2 forbids.

use translator_engine::error::EngineError;
use translator_engine::{tm, types::{GlossaryEntry, TmEntry}};

fn tm_entry() -> TmEntry {
    TmEntry {
        source_text: "pre-init probe".into(),
        source_lang: "en".into(),
        target_text: "预留".into(),
        target_lang: "zh".into(),
        engine: "demo".into(),
        quality: 0.9,
        hit_count: 1,
        domain: String::new(),
    }
}

fn glossary_entry() -> GlossaryEntry {
    GlossaryEntry {
        source_term: "pre-init 术语".into(),
        source_lang: "zh".into(),
        target_term: "pre-init term".into(),
        target_lang: "en".into(),
        confidence: 0.9,
        frequency: 1,
        domain: String::new(),
        source: "manual".into(),
    }
}

/// Unwrap the failure and check it is the one the caller can act on. `what` names the
/// entry point so a regression says which facade started answering differently.
fn assert_refused<T: std::fmt::Debug>(what: &str, outcome: Result<T, EngineError>) {
    match outcome {
        Err(e) => {
            let text = e.to_string();
            assert!(
                text.contains("not initialized"),
                "{what}: refused, but with an unhelpful message: {text}"
            );
        }
        Ok(value) => panic!(
            "{what}: an uninitialized store answered Ok({value:?}). A caller cannot tell \
             that apart from an empty memory, which is exactly the silence REQ-B2 forbids",
        ),
    }
}

#[test]
fn tm_reads_and_writes_refuse_before_init() {
    assert_refused("tm::lookup", tm::lookup("pre-init probe", "en", "zh"));
    assert_refused(
        "tm::lookup_with_domain",
        tm::lookup_with_domain("pre-init probe", "en", "zh", "av"),
    );
    assert_refused("tm::put", tm::put(&tm_entry()));
    assert_refused("tm::increment_hit_count", tm::increment_hit_count(&tm_entry()));
    assert_refused("tm::flag_bad", tm::flag_bad("pre-init probe", "en", "zh"));
    assert_refused(
        "tm::similar_entries",
        tm::similar_entries("pre-init", "en", "zh", 5),
    );
    assert_refused("tm::list", tm::list("en", "zh", 10));
    assert_refused("tm::total_entries", tm::total_entries());
}

#[test]
fn glossary_reads_and_writes_refuse_before_init() {
    assert_refused(
        "tm::glossary_list",
        tm::glossary_list("zh", "en", "av", 20),
    );
    assert_refused(
        "tm::glossary_upsert",
        tm::glossary_upsert(&glossary_entry()),
    );
    assert_refused(
        "tm::glossary_delete",
        tm::glossary_delete("pre-init 术语", "zh", "en"),
    );
    // A malformed pack and an uninitialized store are different failures; whichever
    // comes first has to be the honest one, so this pins the init check on a body the
    // importer would otherwise accept.
    assert_refused(
        "tm::glossary_import_pack",
        tm::glossary_import_pack("{\"domain\":\"av\",\"entries\":[]}"),
    );
}

/// The boundary contract: the same failure, seen the way a non-Rust caller sees it.
///
/// `.NET` and Python read the `error` field out of this shape, so "TM_ERROR" is as much a
/// promise as the message text. A panic or a rewritten code would surface in the client as
/// an unclassifiable failure.
#[test]
fn the_refusal_surfaces_as_the_error_code_the_ffi_promises() {
    let err = tm::total_entries().expect_err("an uninitialized store must not answer");
    let json = err.to_json();
    assert_eq!(json["error"], "TM_ERROR", "the code the client switches on");
    assert!(
        json["message"]
            .as_str()
            .unwrap_or_default()
            .contains("not initialized"),
        "the message has to say what was missing, got: {json}"
    );
}
