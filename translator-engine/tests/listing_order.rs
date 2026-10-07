// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! D32: a `take(n)` behind a tie is a coin flip, not a choice.
//!
//! Four listings in the store sort by one numeric key and then truncate. On real
//! data that key is almost always tied -- the shipped AV pack carries 266 zh->en
//! rows of which 264 share confidence 0.90 -- and `sort_by` leaves tied elements
//! in whatever order the preceding `HashMap` iteration produced. A fresh HashMap
//! gets a fresh random seed, so *every call* reshuffles the survivors: measured on
//! the released binary, five consecutive `tt_glossary_list_with_domain(zh,en,av,20)`
//! calls inside one process returned twenty-term windows overlapping by only 3-5
//! terms. That makes the terms reaching an ollama prompt unreproducible, and it is
//! why the S12 scoped-vs-unscoped delta could not be attributed.
//!
//! These tests pin two things: the property that matters (same store, same query,
//! same answer, call after call) and the documented tie-break rule the fix chose
//! (source term / source text ascending), so `docs/sdk.md` may promise an order.
//!
//! Run: `cargo test --test listing_order` (Release profile is the project default).

use std::sync::OnceLock;
use translator_engine::{config, tm, types::*};

/// Isolated temp dir, same shape as `domain_routing.rs::ensure_init`. The tests
/// below use their own language pairs so nothing from other suites can join a
/// window and make a truncation assertion depend on test execution order.
fn ensure_init() {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("wonslate-listing-order-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));
        std::env::set_var("LT_ARGOS_URL", "http://127.0.0.1:1");
        std::env::set_var("LT_MADLAD_URL", "http://127.0.0.1:1");
        config::load();
        tm::init(&dir, 100, 10).expect("TM init failed");
    });
}

const CALLS: usize = 12;

fn gl(term: &str, trans: &str, confidence: f32) -> GlossaryEntry {
    GlossaryEntry {
        source_term: term.into(),
        source_lang: "xzh".into(),
        target_term: trans.into(),
        target_lang: "xen".into(),
        confidence,
        frequency: 1,
        domain: "tie".into(),
        source: "manual".into(),
    }
}

fn tm_entry(text: &str, quality: f32, hit_count: u32) -> TmEntry {
    TmEntry {
        source_text: text.into(),
        source_lang: "yzh".into(),
        target_text: format!("of {}", text),
        target_lang: "yen".into(),
        engine: "demo".into(),
        quality,
        hit_count,
        domain: String::new(),
    }
}

/// Load four rows that differ only in source term, all at one confidence.
fn seed_glossary() {
    ensure_init();
    for (term, trans) in [("丙", "C"), ("甲", "A"), ("丁", "D"), ("乙", "B")] {
        tm::glossary_upsert(&gl(term, trans, 0.9)).unwrap();
    }
}

/// ① Tied confidence must not decide which rows the listing returns. The store
/// breaks the tie by source term, so the window is a choice, not a draw.
#[test]
fn glossary_confidence_ties_repeat_call_after_call() {
    seed_glossary();
    // The four fixture terms are CJK code points, and Rust compares Strings by
    // UTF-8 bytes, which for these is code-point order: U+4E01 < U+4E19 < U+4E59
    // < U+7532. Listing them ascending is therefore the reverse of the order the
    // upsert loop inserts them in, so a HashMap-dependent order cannot satisfy it.
    let expected = vec!["丁".to_string(), "丙".to_string(), "乙".to_string(), "甲".to_string()];
    for call in 1..=CALLS {
        let got: Vec<String> = tm::glossary_list("xzh", "xen", "tie", 10).unwrap()
            .into_iter().map(|e| e.source_term).collect();
        assert_eq!(expected, got,
            "call {} of {} returned {:?} for four tied rows -- the tie is still being \
             broken by HashMap iteration (D32)", call, CALLS, got);
    }
}

/// ② The truncation is where D32 did its damage: the pipeline asks for 20 and the
/// pack has 266, so which 20 survive was the question the coin flip answered.
/// Narrow the window and the same instability must show up as a membership flip.
#[test]
fn glossary_truncated_window_holds_the_same_terms_every_call() {
    seed_glossary();
    let expected: Vec<String> = tm::glossary_list("xzh", "xen", "tie", 2).unwrap()
        .into_iter().map(|e| e.source_term).collect();
    assert_eq!(2, expected.len(), "the window must actually truncate, or this proves nothing");
    for call in 2..=CALLS {
        let got: Vec<String> = tm::glossary_list("xzh", "xen", "tie", 2).unwrap()
            .into_iter().map(|e| e.source_term).collect();
        assert_eq!(expected, got,
            "call {} handed back {:?} where call 1 handed {:?} -- the injected term \
             set is not reproducible (D32)", call, got, expected);
    }
}

/// ③ The few-shot leg (pipeline.rs `similar_entries` -> `with_few_shot`) has the
/// identical shape: quality descending, then take(5). It is switched off in the
/// S12 harness (`use_tm: false`) but it does run for ordinary desktop requests,
/// so a fix limited to the glossary would leave real user output unreproducible.
#[test]
fn tm_fuzzy_quality_ties_are_ordered_deterministically() {
    ensure_init();
    for text in ["tie-probe 丙", "tie-probe 甲", "tie-probe 丁", "tie-probe 乙"] {
        tm::put(&tm_entry(text, 0.95, 1)).unwrap();
    }
    let listed = || -> Vec<String> {
        tm::similar_entries("tie-probe", "yzh", "yen", 10).unwrap()
            .into_iter().map(|e| e.source_text)
            .filter(|t| t.starts_with("tie-probe")).collect()
    };
    // Tied quality, so the tie-break owns the whole ordering: ascending source text,
    // which is code-point order for the same four CJK characters as above.
    let expected = vec!["tie-probe 丁".to_string(), "tie-probe 丙".to_string(),
                        "tie-probe 乙".to_string(), "tie-probe 甲".to_string()];
    for call in 1..=CALLS {
        let got = listed();
        assert_eq!(expected, got,
            "few-shot call {} of {}: got {:?} -- tied quality is still being ordered by \
             HashMap iteration (D32), and these rows go straight into the prompt",
            call, CALLS, got);
    }
}

/// ④ The UI manager lists by hit count. Nothing correctness-critical hangs on it,
/// but a list that reorders itself between refreshes is its own defect report, and
/// the same tie rule should cover all four listings rather than two.
#[test]
fn tm_list_hit_count_ties_are_ordered_deterministically() {
    ensure_init();
    for text in ["list-probe 乙", "list-probe 丁", "list-probe 甲", "list-probe 丙"] {
        tm::put(&tm_entry(text, 0.9, 7)).unwrap();
    }
    let listed = || -> Vec<String> {
        tm::list("yzh", "yen", 100).unwrap()
            .into_iter().map(|e| e.source_text)
            .filter(|t| t.starts_with("list-probe")).collect()
    };
    let expected = vec!["list-probe 丁".to_string(), "list-probe 丙".to_string(),
                        "list-probe 乙".to_string(), "list-probe 甲".to_string()];
    for call in 1..=CALLS {
        let got = listed();
        assert_eq!(expected, got,
            "the UI listing reordered itself on call {}: got {:?} -- tied hit counts \
             must not be decided by iteration order (D32)", call, got);
    }
}
