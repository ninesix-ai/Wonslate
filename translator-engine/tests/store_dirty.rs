// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Two store contracts that need a process to themselves.
//!
//! Both are about counters, and counters are the reason this is a separate binary:
//! `TmStore` keeps one dirty-write counter and one index per process, so a case that
//! asserts "N writes reached the disk" or "this row left the total" cannot run beside
//! other writers -- under `cargo test` the cases in one binary share threads, and any of
//! them could move the number being asserted. Isolated here, each case can state an exact
//! value instead of a range.
//!
//! What is protected: `tm_total` is what `/health` reports and what the TM panel shows,
//! so a flagged row leaking into it would count a translation the user already rejected as
//! an active memory. And the dirty counter is the only thing that gets a session's writes
//! onto disk without an explicit shutdown -- if it stops firing, a crash loses every row
//! since the last flush and nothing reports it.
//!
//! Run: `cargo test --test store_dirty` (Release profile is the project default).

use std::sync::{Mutex, OnceLock};
use translator_engine::{config, tm, types::*};

mod hermetic;

/// Serialize the cases: they share one counter and one index, so the second writer
/// would move the number the first is asserting. A lock keeps each measurement alone
/// without dictating an execution order.
static MEASURE: Mutex<()> = Mutex::new(());

fn store_dir() -> &'static std::path::PathBuf {
    static DIR: OnceLock<std::path::PathBuf> = OnceLock::new();
    DIR.get_or_init(|| {
        let mut dir = std::env::temp_dir();
        dir.push(format!("wonslate-store-dirty-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("create temp dir");
        let _ = std::fs::remove_file(dir.join("translator_tm.json"));
        let _ = std::fs::remove_file(dir.join("glossary.json"));
        // Both counters under test here are process-global, and both are durability
        // promises: the dirty threshold is what gets a session's writes onto disk without
        // a clean shutdown. Nothing about that may depend on the machine, so the whole
        // environment is pinned and then asserted (`tests/hermetic/mod.rs`, defect D36).
        let spec = hermetic::Spec::new(&dir);
        spec.apply();
        config::load();
        tm::init(&dir, 500, 10).expect("TM init failed");
        spec.verify();
        dir
    })
}

fn row(text: &str, quality: f32) -> TmEntry {
    TmEntry {
        source_text: text.into(), source_lang: "zhd".into(),
        target_text: format!("target of {}", text), target_lang: "end".into(),
        engine: "demo".into(), quality, hit_count: 1, domain: String::new(),
    }
}

/// A row marked bad is hidden from every read path; `tm_total` is the one that reports
/// the size of the memory to the user, so it has to agree with what they can see.
#[test]
fn a_flagged_row_leaves_the_total_that_health_reports() {
    let _guard = MEASURE.lock().unwrap();
    let _dir = store_dir();

    let before = tm::total_entries().unwrap();
    tm::put(&row("counted then rejected", 0.9)).unwrap();
    assert_eq!(before + 1, tm::total_entries().unwrap(),
        "a fresh row is an active row, so it must be counted");

    tm::flag_bad("counted then rejected", "zhd", "end").unwrap();
    assert_eq!(before, tm::total_entries().unwrap(),
        "and marking it bad must take it back out -- the count is what /health and the \
         TM panel show, so a flagged row left in it reports a memory the user rejected");
    assert!(tm::lookup("counted then rejected", "zhd", "end").unwrap().is_none(),
        "and it must stay out of the read path too");
}

/// `FLUSH_THRESHOLD` is the durability promise for a session that never shuts down
/// cleanly: after enough accumulated writes the store writes itself, without waiting for
/// `tt_shutdown`. Nothing executed that branch, so an off-by-one or a lost reset would
/// have been found first by a crash.
///
/// What is asserted is that the counter fired at all, not the exact row count it fired
/// on: the counter is process-global and may carry writes from the case above, so the
/// trigger lands somewhere within these hundred. That is the contract worth pinning -- a
/// prefix on disk is fine, an absent file is the data loss.
#[test]
fn a_hundred_writes_reach_the_disk_without_anyone_asking() {
    use std::time::{SystemTime, UNIX_EPOCH};
    let _guard = MEASURE.lock().unwrap();
    let dir = store_dir();

    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
    let path = dir.join("translator_tm.json");
    let _ = std::fs::remove_file(&path);

    for i in 0..100 {
        tm::put(&row(&format!("bulk write {} {}", i, stamp), 0.8)).unwrap();
    }

    let raw = std::fs::read_to_string(&path)
        .expect("100 writes must have flushed the store on their own (FLUSH_THRESHOLD)");
    let v: serde_json::Value = serde_json::from_str(&raw).expect("flushed file must be JSON");
    let records = v.get("records").and_then(|x| x.as_array())
        .unwrap_or_else(|| panic!("no records array in {}", &raw[..raw.len().min(200)]));
    assert!(!records.is_empty(), "an auto-flush of an empty index proves nothing");
    let mine = records.iter()
        .filter(|r| r.get("source_text").and_then(|x| x.as_str())
            .unwrap_or("").starts_with("bulk write "))
        .count();
    assert!(mine > 0,
        "the flushed file must carry rows from this run, got {} records none of them ours",
        records.len());
}
