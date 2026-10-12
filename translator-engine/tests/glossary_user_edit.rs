// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! D49 and D35: what a user edit does to a glossary row, and what its caller is told.
//!
//! Measured on the released binary (head 2d74760), through the FFI the desktop app uses:
//! saving a term writes it at confidence 1.0, and correcting it later -- same key, same
//! confidence -- left the stored row on the first value, incremented only its frequency,
//! and answered `{"ok":true}`. The UI then prints "saved". Root cause: `tt_glossary_upsert`
//! shared the machine path's rule ("replace only if strictly higher"), while
//! `docs/sdk.md` promised "confidence is the caller's" and `lib.rs` promised the row is
//! marked manual. Both promises were false on an update, and no test covered the equal
//! case. A seed-pack row at 1.0 has the same problem for any embedder that edits one.
//!
//! The rule these tests pin is the one the product owner chose:
//!   * a user edit always lands, whatever the stored confidence;
//!   * a user edit is the lock's entry point (D35) -- it can set, keep or clear the flag;
//!   * a lock stops *machine* writes (distillation, seed packs), never the user;
//!   * a write that did not happen must not be counted as one (`written` beside
//!     `imported`), and a delete that removed nothing must say so (`removed`).
//!
//! Run: `cargo test --test glossary_user_edit` (Release profile is the project default).

use std::sync::OnceLock;
use translator_engine::tm::{self, GlWrite, GlossaryRecord};
use translator_engine::types::GlossaryEntry;
use translator_engine::{config, tm::GlImportReport};

mod hermetic;

/// Own temp dir and own language pair (`uzh`/`uen`), so no row from another suite can
/// join a listing window and make an assertion depend on execution order. Every engine
/// slot is unreachable and every knob is at the shipped default (`tests/hermetic/mod.rs`,
/// defect D36) -- none of these cases should be decided by this machine.
fn ensure_init() {
    static INIT: OnceLock<()> = OnceLock::new();
    INIT.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("wonslate-glossary-user-edit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).ok();
        std::fs::remove_file(dir.join("translator_tm.json")).ok();
        std::fs::remove_file(dir.join("glossary.json")).ok();
        let spec = hermetic::Spec::new(&dir);
        spec.apply();
        config::load();
        tm::init(&dir, 100, 10).expect("TM init failed");
        spec.verify();
    });
}

fn row(term: &str, confidence: f32, domain: &str, source: &str) -> GlossaryEntry {
    GlossaryEntry {
        source_term: term.into(),
        source_lang: "uzh".into(),
        target_term: "placeholder".into(),
        target_lang: "uen".into(),
        confidence,
        frequency: 1,
        domain: domain.into(),
        source: source.into(),
    }
}

fn with_target(mut e: GlossaryEntry, target: &str) -> GlossaryEntry {
    e.target_term = target.into();
    e
}

/// The single row behind one term, read from the listing that carries the lock.
fn record(term: &str, domain: &str) -> GlossaryRecord {
    tm::glossary_records("uzh", "uen", domain, 200).unwrap()
        .into_iter()
        .find(|r| r.entry().source_term == term)
        .unwrap_or_else(|| panic!("term {} is not in the {} listing at all", term, domain))
}

fn pack(domain: &str, entries: &[(&str, &str, Option<f32>)]) -> String {
    let body: Vec<String> = entries.iter().map(|(t, v, conf)| {
        match conf {
            Some(c) => format!("{{\"source_term\":\"{}\",\"target_term\":\"{}\",\"confidence\":{}}}", t, v, c),
            None => format!("{{\"source_term\":\"{}\",\"target_term\":\"{}\"}}", t, v),
        }
    }).collect();
    format!("{{\"domain\":\"{}\",\"version\":\"probe\",\"source_lang\":\"uzh\",\"target_lang\":\"uen\",\"entries\":[{}]}}",
            domain, body.join(","))
}

fn import(pack_json: &str) -> GlImportReport {
    tm::glossary_import_pack(pack_json).expect("a well-formed pack must import")
}

// ---- a user edit lands ---------------------------------------------------------------

/// D49 core: the second edit of a term is the case that used to matter most -- a user
/// fixing a typo they had just saved. Equal confidence must not mean "nothing to do".
#[test]
fn a_second_user_edit_of_the_same_term_replaces_the_row() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("durterm", 1.0, "eq", "distill"), "first value")).unwrap();

    tm::glossary_upsert_manual(&with_target(row("durterm", 1.0, "eq", ""), "second value"), None)
        .expect("a user edit must be accepted");

    assert_eq!("second value", record("durterm", "eq").entry().target_term,
        "the store kept the first value while the caller was told the edit was fine (D49)");
}

/// The user's intent outranks a number: a stored row at 1.0 must not survive an edit
/// that happens to carry a lower confidence.
#[test]
fn a_user_edit_below_the_stored_confidence_still_wins() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("lowconf", 1.0, "eq", "seed:eq"), "machine value")).unwrap();

    tm::glossary_upsert_manual(&with_target(row("lowconf", 0.4, "eq", ""), "user value"), None)
        .expect("a user edit must be accepted");

    let got = record("lowconf", "eq");
    assert_eq!("user value", got.entry().target_term, "the edit must land whatever the stored confidence");
    assert!(got.entry().confidence < 0.5,
        "and the confidence is the user's too, not lifted to what the row happened to hold: {}",
        got.entry().confidence);
}

/// `docs/sdk.md` says a user edit is marked `manual`. On the update path it used to stay
/// `seed:av` / `distill`, so a reviewer could not tell a curated row from a machine one.
#[test]
fn a_user_edit_marks_the_row_as_manual() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("prov", 0.9, "eq", "distill"), "machine value")).unwrap();

    tm::glossary_upsert_manual(&with_target(row("prov", 0.9, "eq", "distill"), "user value"), None)
        .expect("a user edit must be accepted");

    assert_eq!("manual", record("prov", "eq").entry().source,
        "provenance must say a person wrote this, whatever the caller put in the payload");
}

// ---- the lock, and who it stops ------------------------------------------------------

/// D35: the flag existed in the store and on disk, and nothing could set it.
#[test]
fn a_user_edit_can_lock_the_row_and_a_machine_write_then_stops_moving_it() {
    ensure_init();
    tm::glossary_upsert_manual(&with_target(row("lockme", 0.5, "lock", ""), "locked value"), Some(true))
        .expect("a user edit must be accepted");
    assert!(record("lockme", "lock").user_locked(), "the edit was asked to lock the row");

    let outcome = tm::glossary_upsert(
        &with_target(row("lockme", 1.0, "lock", "distill"), "machine push")).unwrap();

    assert_eq!(GlWrite::Locked, outcome, "a locked row must report the refusal, not silence");
    assert_eq!("locked value", record("lockme", "lock").entry().target_term,
        "distillation must not overwrite a locked term, whatever its confidence");
}

/// The lock protects against machines, not against the person who set it -- otherwise
/// "edit means lock" would reintroduce exactly the dead end D49 is about.
#[test]
fn a_user_edit_of_a_locked_row_still_lands_and_keeps_the_lock() {
    ensure_init();
    tm::glossary_upsert_manual(&with_target(row("selflock", 0.9, "lock", ""), "first value"), Some(true))
        .expect("a user edit must be accepted");

    tm::glossary_upsert_manual(&with_target(row("selflock", 0.9, "lock", ""), "corrected value"), None)
        .expect("a locked row must stay editable by the user");

    let got = record("selflock", "lock");
    assert_eq!("corrected value", got.entry().target_term, "the user must be able to fix their own lock");
    assert!(got.user_locked(), "and correcting a row must not quietly unlock it");
}

/// No opinion about the lock means leave it as the row has it, so an embedder that knows
/// nothing about this field cannot start locking or unlocking rows by accident.
#[test]
fn a_user_edit_without_a_lock_opinion_keeps_the_lock_it_found() {
    ensure_init();
    tm::glossary_upsert_manual(&with_target(row("opin", 0.9, "lock", ""), "first value"), Some(true)).unwrap();
    tm::glossary_upsert_manual(&with_target(row("opin", 0.9, "lock", ""), "second value"), None).unwrap();
    assert!(record("opin", "lock").user_locked(), "absent opinion must preserve a set lock");

    tm::glossary_upsert_manual(&with_target(row("opin", 0.9, "lock", ""), "third value"), Some(false)).unwrap();
    assert!(!record("opin", "lock").user_locked(), "an explicit false must clear the lock");
}

// ---- a write that did not happen must not be counted as one --------------------------

/// D49's accounting half: a pack that displaced a locked row still reported every entry
/// of itself as imported.
#[test]
fn a_pack_row_refused_by_a_lock_is_counted_as_locked_not_written() {
    ensure_init();
    tm::glossary_upsert_manual(&with_target(row("pklock", 0.9, "pack", ""), "locked value"), Some(true))
        .expect("a user edit must be accepted");

    let report = import(&pack("pack", &[("pklock", "pack value", Some(1.0)), ("pknew", "fresh", None)]));

    assert_eq!(2, report.rows, "the pack carried two entries");
    assert_eq!(1, report.written, "only the row nobody had locked was written");
    assert_eq!(1, report.skipped_locked, "the locked row must be named as skipped by a lock");
    assert_eq!("locked value", record("pklock", "pack").entry().target_term);
    assert_eq!("fresh", record("pknew", "pack").entry().target_term);
}

/// A declared low confidence is the pack author's decision and must not be lifted (the
/// D34 rule), nor counted as a write.
#[test]
fn a_pack_row_not_better_than_the_stored_one_is_counted_as_not_higher() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("pkheld", 0.9, "declared", "distill"), "stored value")).unwrap();

    let report = import(&pack("declared", &[("pkheld", "pack value", Some(0.4))]));

    assert_eq!(1, report.rows);
    assert_eq!(0, report.written, "nothing was written, so nothing may be counted");
    assert_eq!(1, report.skipped_not_higher, "and the reason is the confidence rule, not a lock");
    assert_eq!(0, report.skipped_locked);
    let got = record("pkheld", "declared");
    assert_eq!("stored value", got.entry().target_term, "a declared low confidence must lose");
    assert!(got.entry().confidence > 0.89, "and must not be lifted: {}", got.entry().confidence);
}

// ---- a delete that removed nothing is not a success ----------------------------------

/// Measured shape: deleting a domain-scoped row through the keyless delete removed
/// nothing and still answered ok, so the UI reported "deleted" for a row still on screen.
#[test]
fn deleting_a_domain_row_reports_that_nothing_was_removed() {
    ensure_init();
    import(&pack("del", &[("delterm", "pack value", None)]));
    assert_eq!("pack value", record("delterm", "del").entry().target_term, "the row exists");

    let removed = tm::glossary_delete("delterm", "uzh", "uen").unwrap();

    assert!(!removed, "the delete key carries no domain, so this row cannot be reached by it");
    assert_eq!("pack value", record("delterm", "del").entry().target_term, "and the row is still there");
}

/// The same call on a generic row must report the removal it actually performed.
#[test]
fn deleting_a_generic_row_reports_the_removal() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("genrow", 1.0, "", "manual"), "generic value")).unwrap();

    assert!(tm::glossary_delete("genrow", "uzh", "uen").unwrap(), "a generic row is reachable");
    assert!(tm::glossary_records("uzh", "uen", "", 200).unwrap().iter()
            .all(|r| r.entry().source_term != "genrow"), "and is gone after it");
    assert!(!tm::glossary_delete("genrow", "uzh", "uen").unwrap(), "deleting twice removes nothing");
}

/// D50, pinned as the status quo rather than endorsed: when a term exists both as a generic
/// row and inside a domain, the keyless delete reaches the *generic* one. A UI that offers a
/// domain filter can therefore be handed a selection whose delete would remove a different
/// row -- which is why `MainViewModel::GlossaryDeleteGuard` refuses domain rows until the
/// signature gains a domain segment (a contract change, not this round's call).
#[test]
fn a_keyless_delete_reaches_the_generic_twin_and_leaves_the_domain_row() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("twin", 1.0, "", "distill"), "generic twin")).unwrap();
    import(&pack("twin", &[("twin", "domain twin", None)]));

    assert!(tm::glossary_delete("twin", "uzh", "uen").unwrap(), "the generic row was removed");

    let generic: Vec<String> = tm::glossary_records("uzh", "uen", "", 200).unwrap()
        .iter().filter(|r| r.entry().source_term == "twin")
        .map(|r| r.entry().target_term.clone()).collect();
    assert!(generic.is_empty(), "the generic twin must be gone, got {:?}", generic);
    let scoped: Vec<String> = tm::glossary_records("uzh", "uen", "twin", 200).unwrap()
        .iter().filter(|r| r.entry().source_term == "twin")
        .map(|r| r.entry().target_term.clone()).collect();
    assert_eq!(vec!["domain twin".to_string()], scoped,
        "and the row the user actually selected is the one still there (D50)");
}

/// D50's fix, half one: the domain is part of the primary key, so addressing it must remove
/// the row the caller named and leave the generic twin of the same term alone.
#[test]
fn deleting_with_a_domain_reaches_that_row_and_not_the_generic_twin() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("aimme", 1.0, "", "distill"), "generic twin")).unwrap();
    import(&pack("aim", &[("aimme", "domain row", None)]));

    let removed = tm::glossary_delete_with_domain("aimme", "uzh", "uen", "aim").unwrap();

    assert!(removed, "the domain-scoped row must be reachable by its own key");
    // A scoped read returns generic UNION domain rows, so "the term is still listed" cannot
    // be the test -- the generic twin is supposed to be there. Discriminate on the row's own
    // domain segment instead, which is the thing the delete key names.
    let left: Vec<(String, String)> = tm::glossary_records("uzh", "uen", "aim", 200).unwrap()
        .iter().filter(|r| r.entry().source_term == "aimme")
        .map(|r| (r.entry().target_term.clone(), r.entry().domain.clone())).collect();
    assert!(!left.iter().any(|(_, d)| d == "aim"),
        "the `aim` row must be gone, got {:?}", left);
    assert_eq!(vec![("generic twin".to_string(), String::new())], left,
        "and the generic twin the caller did not name must be the only survivor");
}

/// D50's fix, half two: a domain that holds no such row removes nothing and says so -- the
/// same "do not report a write that did not happen" rule the machine path reports through.
#[test]
fn deleting_with_an_unmatched_domain_reports_no_removal() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("lonely", 1.0, "", "distill"), "generic only")).unwrap();

    assert!(!tm::glossary_delete_with_domain("lonely", "uzh", "uen", "nosuch").unwrap(),
        "there is no `nosuch` row, so nothing may be reported as removed");
    assert!(tm::glossary_records("uzh", "uen", "", 200).unwrap().iter()
            .any(|r| r.entry().source_term == "lonely"),
        "and the generic row must survive an attempt aimed at another domain");
}

/// Guard: the empty-domain form must keep addressing the generic row, so the new export is
/// a superset rather than a change of meaning for existing callers.
#[test]
fn an_empty_domain_is_the_generic_form() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("eqform", 1.0, "", "distill"), "generic value")).unwrap();

    assert!(tm::glossary_delete_with_domain("eqform", "uzh", "uen", "").unwrap(),
        "domain=\"\" must reach the generic row exactly like the three-argument export");
    assert!(!tm::glossary_delete("eqform", "uzh", "uen").unwrap(), "and it is gone for both");
}

// ---- the machine rule stays the machine rule -----------------------------------------

/// Guard against over-correction: none of the above may relax the rule that keeps a
/// distilled row from being shuffled by every equal-confidence candidate.
#[test]
fn a_machine_write_still_needs_a_strictly_higher_confidence() {
    ensure_init();
    tm::glossary_upsert(&with_target(row("macheld", 0.7, "eq", "distill"), "stored value")).unwrap();

    let outcome = tm::glossary_upsert(&with_target(row("macheld", 0.7, "eq", "distill"), "equal value")).unwrap();

    assert_eq!(GlWrite::NotHigher, outcome);
    assert_eq!("stored value", record("macheld", "eq").entry().target_term,
        "an equal-confidence machine row must not displace what is stored (D34 rule intact)");
}
