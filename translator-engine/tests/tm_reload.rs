// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! What survives a restart.
//!
//! Why this is a separate binary: `TmStore::open` fills a `OnceLock`, so a process can
//! load a store exactly once. Every existing suite deletes the two JSON files before
//! calling `init` -- Python, .NET and Rust alike -- which means the code that turns
//! yesterday's writes into today's data had never been asserted in any language. It ran
//! on developer machines at app start with nobody checking it. That is the same shape as
//! D33: a branch nobody ever entered, surviving a complete wiring and 231 Rust tests.
//!
//! The fixtures below are written as files first and only then loaded, so the input to
//! every case is the on-disk format rather than an in-memory object. Two records per
//! store deliberately omit fields that newer builds add, because "old JSON loads as a
//! no-op upgrade" is a promise made in `store.rs` comments and in
//! `docs/glossary-packs/README.md`, and a promise about a data path is worth more than a
//! comment: a row the reader cannot reconstruct is a row the user loses without a warning.
//!
//! Run: `cargo test --test tm_reload` (Release profile is the project default).

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::OnceLock;
use serde_json::{json, Value};
use translator_engine::{config, tm, types::*};

/// One fixture TM row, written the way a previous process would have written it.
fn tm_record(text: &str, src: &str, tgt: &str, target: &str, quality: f32,
             hits: u32, domain: &str, flagged: bool) -> Value {
    json!({
        "source_hash": tm::compute_hash(text, src, tgt),
        "source_text": text, "source_lang": src,
        "target_text": target, "target_lang": tgt,
        "engine": "ollama", "quality": quality, "hit_count": hits,
        "domain": domain, "flagged": flagged,
    })
}

/// One fixture glossary row. `user_locked` is written only when given, so the callers
/// that pass `None` reproduce a record from before the field existed.
fn gl_record(term: &str, trans: &str, src: &str, tgt: &str, confidence: f32,
             domain: &str, source: &str, locked: Option<bool>) -> Value {
    let mut v = json!({
        "source_term": term, "source_lang": src,
        "target_term": trans, "target_lang": tgt,
        "confidence": confidence, "frequency": 4,
        "domain": domain, "source": source,
    });
    if let Some(flag) = locked {
        v["user_locked"] = json!(flag);
    }
    v
}

/// Pre-seed the store, then load it once. Returns the data directory so cases can read
/// the files back after a flush.
fn seeded_store() -> &'static PathBuf {
    static DIR: OnceLock<PathBuf> = OnceLock::new();
    DIR.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("wonslate-tm-reload-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("create fixture dir");
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));

        // No engine can be reached from here: the store is under test, not the routing.
        std::env::set_var("LT_ARGOS_URL", "http://127.0.0.1:1");
        std::env::set_var("LT_MADLAD_URL", "http://127.0.0.1:1");

        // One language pair per concern below, so a case cannot be decided by another
        // case's writes -- these tests run on parallel threads inside one store.
        let tm_rows = vec![
            // (a) the row a current build would have written
            tm_record("reload plain", "zha", "ena", "plain after reload", 0.81, 7, "", false),
            // (b) flagged by the user: must stay hidden and must not be dropped
            tm_record("reload flagged", "zha", "ena", "wrong once", 0.9, 12, "", true),
            // (c) written before the domain field existed -- no domain, no hit_count
            json!({
                "source_hash": tm::compute_hash("reload legacy", "zha", "ena"),
                "source_text": "reload legacy", "source_lang": "zha",
                "target_text": "legacy still readable", "target_lang": "ena",
                "engine": "demo", "quality": 0.5, "flagged": false,
            }),
            // (d..f) three rows tied on hit_count: which two the warmup keeps is D32's
            // remaining half for the listings that feed the cache and the UI manager.
            tm_record("tie gamma", "zhf", "enf", "gamma", 0.9, 5, "", false),
            tm_record("tie alpha", "zhf", "enf", "alpha", 0.9, 5, "", false),
            tm_record("tie beta", "zhf", "enf", "beta", 0.9, 5, "", false),
        ];
        write_fixture(&dir.join("translator_tm.json"), &tm_rows);

        let gl_rows = vec![
            // (a) a locked row, the shape `tt_glossary_upsert` cannot produce but the
            // reader must honour -- see case `locked_glossary_row_...`
            gl_record("lockme", "locked translation", "zhb", "enb", 0.5, "", "manual", Some(true)),
            // (b) no user_locked key: a record from before the field
            gl_record("openme", "old translation", "zhc", "enc", 0.5, "", "distill", None),
            // (c)+(d) the same term twice, generic and domain-scoped: the on-disk key
            // carries the domain segment, so both must survive loading
            gl_record("coexist", "generic reading", "zhd", "end", 0.8, "", "manual", None),
            gl_record("coexist", "av reading", "zhd", "end", 0.95, "av", "seed:av", None),
            // (e) provenance and confidence must come back intact (N-08)
            gl_record("provenance", "traced reading", "zhe", "ene", 0.93, "av", "seed:av", None),
            // (f)+(g) one more term with a generic and an av row, reserved for the
            // delete case so a destructive test cannot race a read-only one
            gl_record("deleteme", "generic gone", "zhk", "enk", 0.8, "", "manual", None),
            gl_record("deleteme", "av kept", "zhk", "enk", 0.95, "av", "seed:av", None),
        ];
        write_fixture(&dir.join("glossary.json"), &gl_rows);

        config::load();
        tm::init(&dir, 100, 2).expect("TM init failed");
        dir
    })
}

fn write_fixture(path: &std::path::Path, records: &[Value]) {
    let body = json!({ "version": 1, "records": records });
    std::fs::write(path, serde_json::to_string(&body).unwrap())
        .expect("write fixture");
}

/// Read back one flushed file the way a later process would.
fn read_fixture(path: &std::path::Path) -> Vec<Value> {
    let raw = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("{} unreadable: {e}", path.display()));
    let v: Value = serde_json::from_str(&raw).expect("flushed file is not JSON");
    v.get("records").and_then(|x| x.as_array())
        .unwrap_or_else(|| panic!("flushed file has no records array: {raw}"))
        .clone()
}

fn entry(term: &str, trans: &str, src: &str, tgt: &str, confidence: f32) -> GlossaryEntry {
    GlossaryEntry {
        source_term: term.into(), source_lang: src.into(),
        target_term: trans.into(), target_lang: tgt.into(),
        confidence, frequency: 1, domain: String::new(), source: "distill".into(),
    }
}

fn gloss(src: &str, tgt: &str, domain: &str) -> Vec<GlossaryEntry> {
    tm::glossary_list(src, tgt, domain, 50).unwrap()
}

// ---- the restart itself ---------------------------------------------------------------

/// A TM row is only reachable if the stored key equals the hash the reader computes from
/// the same text. Nothing pinned that coupling: change the normalization in
/// `compute_hash` and every memory row on every user's disk becomes an orphan -- the
/// store loads fine, `lookup` simply never hits, and no call returns an error.
#[test]
fn a_previous_process_row_is_found_by_hash() {
    let _dir = seeded_store();
    let hit = tm::lookup("reload plain", "zha", "ena").unwrap()
        .expect("a row written by an earlier process must be readable by this one");
    assert_eq!("plain after reload", hit.target_text, "output must survive");
    assert_eq!(7, hit.hit_count, "hit_count must survive, not reset to a default");
    assert!(
        (hit.quality - 0.81).abs() < 1e-6,
        "quality drives the confidence gate, got {}", hit.quality
    );
    assert_eq!("ollama", hit.engine, "engine provenance must survive");
}

/// A row the user marked bad must stay out of every read path *and* stay on disk. If a
/// flush dropped it, the next translation of that sentence would re-add the very output
/// the user rejected, with nothing left to explain why it was wrong before.
#[test]
fn a_flagged_row_stays_hidden_and_stays_written() {
    let dir = seeded_store();
    assert!(
        tm::lookup("reload flagged", "zha", "ena").unwrap().is_none(),
        "a flagged row must not answer a lookup"
    );
    let listed = tm::list("zha", "ena", 100).unwrap();
    assert!(
        listed.iter().all(|e| e.source_text != "reload flagged"),
        "a flagged row must not appear in the UI list, got {:?}",
        listed.iter().map(|e| &e.source_text).collect::<Vec<_>>()
    );

    tm::TmStore::instance().unwrap().flush_all().unwrap();
    let on_disk = read_fixture(&dir.join("translator_tm.json"));
    assert!(
        on_disk.iter().any(|r| {
            r.get("source_text").and_then(|x| x.as_str()) == Some("reload flagged")
                && r.get("flagged").and_then(|x| x.as_bool()) == Some(true)
        }),
        "the correction has to be saved, or it is lost at the next restart"
    );
}

/// `store.rs` claims old JSON loads as a no-op upgrade, and `docs/glossary-packs/README.md`
/// tells users their rows survive a version change. This is that claim, as a fact: a row
/// with no `domain` key reads back as generic and keeps its defaults.
#[test]
fn a_row_without_the_newer_fields_still_loads() {
    let _dir = seeded_store();
    let hit = tm::lookup("reload legacy", "zha", "ena").unwrap()
        .expect("a record predating the domain field must still load");
    assert!(hit.domain.is_empty(),
        "a missing domain must land in the generic bucket, got {:?}", hit.domain);
    assert_eq!("legacy still readable", hit.target_text);
    assert_eq!(
        1, hit.hit_count,
        "a missing hit_count must default to one use, not zero -- zero makes the row \
         sort below every real one in the warmup"
    );
}

// ---- glossary records: the lock, the overwrite, the domain key ------------------------

/// `user_locked=true` is the mechanism behind a documented promise: "distillation must
/// not overwrite a locked term". The branch that implements it (store.rs, the `_ => {}`
/// arm) had never been entered by any test, because no API sets the flag -- so the only
/// way a locked row can exist is one restored from disk, which is exactly what a user who
/// locked a term in a released build has.
#[test]
fn a_locked_row_survives_a_higher_confidence_upsert() {
    let _dir = seeded_store();
    let before = gloss("zhb", "enb", "")
        .into_iter().find(|e| e.source_term == "lockme")
        .expect("the locked row must load");
    assert_eq!("locked translation", before.target_term,
        "a locked row restored from disk must come back with its own term");

    tm::glossary_upsert(&entry("lockme", "machine suggestion", "zhb", "enb", 1.0)).unwrap();

    let after = gloss("zhb", "enb", "")
        .into_iter().find(|e| e.source_term == "lockme").unwrap();
    assert_eq!("locked translation", after.target_term,
        "a locked row must not be overwritten by distillation, whatever its confidence");
    assert!(
        (after.confidence - before.confidence).abs() < 1e-6,
        "confidence is part of what was locked: {} -> {}", before.confidence, after.confidence
    );
    assert_eq!(
        before.frequency, after.frequency,
        "even the frequency counter must stay put -- the locked arm is supposed to do \
         nothing at all, not nothing except bookkeeping"
    );
}

/// The other half of the same match: with no lock, a better term wins and the sighting is
/// counted. Without this, the previous case could be satisfied by an upsert that ignores
/// glossary writes entirely.
#[test]
fn an_unlocked_row_takes_the_better_term_and_counts_the_visit() {
    let _dir = seeded_store();
    let before = gloss("zhc", "enc", "")
        .into_iter().find(|e| e.source_term == "openme")
        .expect("the pre-lock-era row must load, unlocked");
    assert_eq!("old translation", before.target_term);

    tm::glossary_upsert(&entry("openme", "better translation", "zhc", "enc", 0.9)).unwrap();

    let after = gloss("zhc", "enc", "")
        .into_iter().find(|e| e.source_term == "openme").unwrap();
    assert_eq!("better translation", after.target_term, "a higher confidence must replace the term");
    assert!(after.confidence > 0.89, "and raise the stored confidence, got {}", after.confidence);
    assert_eq!(before.frequency + 1, after.frequency,
        "an upsert of an existing row is one more sighting");
}

/// Two rows, one term, different domains. The on-disk key is `term|src|tgt|domain`; load
/// them with the domain left out of the key and the second row silently replaces the
/// first at startup -- a seed pack would evaporate on the user's next launch while the
/// file on disk still holds it.
#[test]
fn generic_and_domain_rows_of_one_term_coexist_after_reload() {
    let _dir = seeded_store();
    let scoped = gloss("zhd", "end", "av");
    let generic = gloss("zhd", "end", "");
    let of = |rows: &[GlossaryEntry]| rows.iter().find(|e| e.source_term == "coexist")
        .map(|e| e.target_term.clone());
    assert_eq!(Some("av reading".to_string()), of(&scoped),
        "a scoped request must see the domain row, not the generic one it shares a term with");
    assert_eq!(Some("generic reading".to_string()), of(&generic),
        "the generic row must still be there on its own");
}

/// N-08's reason for existing: a machine-extracted term must be tellable apart from a
/// confirmed one. If the reader dropped `source`, or `confidence` drifted to the 0.9
/// fallback, the UI would show every imported row as unprovenanced guesswork.
#[test]
fn provenance_and_confidence_come_back_intact() {
    let _dir = seeded_store();
    let row = gloss("zhe", "ene", "av")
        .into_iter().find(|e| e.source_term == "provenance")
        .expect("a seed-pack row must reload with its domain");
    assert_eq!("seed:av", row.source, "provenance is the field N-08 added to keep");
    assert!(
        (row.confidence - 0.93).abs() < 1e-6,
        "confidence drives which row wins a tie; the reader defaulting it to 0.9 would \
         look plausible and change the injected set, got {}", row.confidence
    );
}

// ---- the two listings D32 did not reach ----------------------------------------------

/// D32 was closed for the term injection and the few-shot rows. Two listings kept drawing
/// their survivors from hash order because no test gave them a tie to break: `tm_top_by_hit`
/// decides which rows the LRU cache warms with at startup, and `tm_list` is the UI
/// manager's table. Same defect, same shape, different exit.
#[test]
fn tied_hit_counts_warm_the_same_rows_and_list_them_in_one_order() {
    let _dir = seeded_store();
    let store = tm::TmStore::instance().unwrap();
    // Ask for more than the tie holds so the whole tie group is visible: one row sits
    // above it with 7 hits, so a window of 6 ends on the last tied entry.
    for call in 1..=12 {
        let warmed: Vec<String> = store.tm_top_by_hit(6).unwrap()
            .into_iter().filter(|e| e.source_text.starts_with("tie "))
            .map(|e| e.source_text).collect();
        assert_eq!(
            vec!["tie alpha", "tie beta", "tie gamma"], warmed,
            "warmup call {} of {} handed the cache {:?} -- three rows tied on hit_count, \
             so this order is owned by the tie-break, not by hash iteration", call, 12, warmed
        );

        let shown: Vec<String> = tm::list("zhf", "enf", 100).unwrap()
            .into_iter().map(|e| e.source_text).collect();
        assert_eq!(
            vec!["tie alpha", "tie beta", "tie gamma"], shown,
            "the UI list must not reshuffle itself between refreshes (call {})", call
        );
    }
}

/// An unknown hash is a real caller's situation -- the row was flagged or deleted between
/// the lookup and the hit -- and it must be answered quietly instead of inventing a memory
/// row or failing the call.
#[test]
fn counting_a_hit_that_is_not_there_changes_nothing() {
    let _dir = seeded_store();
    let probe = TmEntry {
        source_text: "never stored".into(), source_lang: "zhg".into(),
        target_text: "absent".into(), target_lang: "eng".into(),
        engine: "demo".into(), quality: 0.5, hit_count: 1, domain: String::new(),
    };
    let before = tm::list("zhg", "eng", 100).unwrap().len();
    tm::increment_hit_count(&probe)
        .expect("a hit on a missing row is not an error");
    assert_eq!(before, tm::list("zhg", "eng", 100).unwrap().len(),
        "and it must not create a row");
}

// ---- writer/reader shape drift -------------------------------------------------------

/// The reader and the writer are hand-written JSON conversions with no derive, so the two
/// halves can disagree without any compiler help. A field `to_json` emits and `from_json`
/// ignores is data that dies at restart; a field the reader wants and the writer never
/// writes is a default the user never chose. Both are invisible until someone reboots.
///
/// Stated limit: this catches drift inside one process, by comparing what the writer emits
/// against the key set the loader is known to accept (the fixtures above are what the
/// loader accepted, by construction). A literal second process is not part of it.
#[test]
fn the_flushed_record_shape_is_exactly_what_the_loader_reads() {
    let dir = seeded_store();
    // Write something new first, so the flush has a reason to run and the shape check has
    // rows from both paths: one loaded, one created in this process.
    tm::put(&TmEntry {
        source_text: "shape probe".into(), source_lang: "zhh".into(),
        target_text: "shape".into(), target_lang: "enh".into(),
        engine: "demo".into(), quality: 0.7, hit_count: 1, domain: String::new(),
    }).unwrap();
    tm::glossary_upsert(&entry("shape term", "shape value", "zhi", "eni", 0.6)).unwrap();
    tm::TmStore::instance().unwrap().flush_all().unwrap();

    let tm_keys: BTreeSet<String> = read_fixture(&dir.join("translator_tm.json"))
        .iter().flat_map(|r| r.as_object().unwrap().keys().cloned()).collect();
    let gl_keys: BTreeSet<String> = read_fixture(&dir.join("glossary.json"))
        .iter().flat_map(|r| r.as_object().unwrap().keys().cloned()).collect();

    let want = |names: &[&str]| -> BTreeSet<String> {
        names.iter().map(|s| s.to_string()).collect()
    };
    assert_eq!(
        want(&["source_hash", "source_text", "source_lang", "target_text", "target_lang",
                "engine", "quality", "hit_count", "domain", "flagged"]),
        tm_keys,
        "TM record keys on disk must match what TmRecord::from_value reads"
    );
    assert_eq!(
        want(&["source_term", "source_lang", "target_term", "target_lang", "confidence",
                "frequency", "domain", "source", "user_locked"]),
        gl_keys,
        "glossary record keys on disk must match what GlossaryRecord::from_value reads"
    );
}

/// Deleting a term from the UI removes the generic row only (S11's deliberate scope
/// limit). That limit is a promise to a domain pack's users too: a plain delete must not
/// take the domain row with it. Neither half had a Rust test.
#[test]
fn deleting_the_generic_row_leaves_the_domain_row_alone() {
    let _dir = seeded_store();
    assert!(gloss("zhk", "enk", "").iter().any(|e| e.source_term == "deleteme"),
        "precondition: the generic row is loaded");

    tm::glossary_delete("deleteme", "zhk", "enk").unwrap();

    assert!(gloss("zhk", "enk", "").iter().all(|e| e.source_term != "deleteme"),
        "the generic row must be gone after a delete");
    assert!(gloss("zhk", "enk", "av").iter().any(|e| e.source_term == "deleteme"),
        "the av row must survive: the UI delete is documented as generic-only");
}
