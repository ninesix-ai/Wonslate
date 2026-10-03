// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! Distillation pipeline (async background thread; extracts term pairs after each AI translation)

pub mod term_extractor;

use std::sync::{mpsc, OnceLock};
use std::thread;
use crate::config;
use crate::types::{DistillTask, GlossaryEntry};

static SENDER: OnceLock<mpsc::Sender<DistillTask>> = OnceLock::new();

/// Initialize the distillation background thread (called once from tt_init)
pub fn init() {
    let (tx, rx) = mpsc::channel::<DistillTask>();
    let _ = SENDER.set(tx);
    thread::spawn(move || {
        for task in rx {
            // Read the bar per task so a config change does not need a restart of the worker.
            let min_conf = config::get().distill_min_conf;
            let stored = process_task(&task, min_conf, &|p| crate::glossary::upsert(p));
            if stored > 0 {
                lt_debug!("[distill] stored {} new terms", stored);
            }
        }
    });
    lt_info!("[distill] background thread started");
}

/// Pure, thread/global-free core of the distill worker: persist candidate pairs scoring
/// `>= min_conf` via `upsert`. Returns the number of entries actually stored (upserts
/// that returned Err are not counted).
///
/// `min_conf` comes from `config.distill_min_conf`. Until N-08 this was a hardcoded 0.90
/// that also happened to be the constant the extractor stamped on every candidate, so
/// the filter admitted everything, including misaligned pairs.
fn process_pairs<I>(
    pairs: I,
    min_conf: f32,
    upsert: &dyn Fn(&GlossaryEntry) -> Result<(), crate::error::EngineError>,
) -> usize
where
    I: Iterator<Item = GlossaryEntry>,
{
    let mut stored = 0usize;
    let mut held_back = 0usize;
    for pair in pairs {
        if pair.confidence >= min_conf {
            if upsert(&pair).is_ok() {
                stored += 1;
            }
        } else {
            // Not persisted anywhere: the only other channel a term could reach is the
            // AI prompt, and `build_system_prompt` phrases every entry as a term the
            // model must follow. A wrong pair there is worse than a missing one.
            held_back += 1;
        }
    }
    if held_back > 0 {
        lt_info!("[distill] {} candidate(s) below the {:.2} bar were not persisted", held_back, min_conf);
    }
    stored
}

/// Process a single `DistillTask`: extract candidate pairs via the term extractor and
/// hand them to `process_pairs`. Kept thread/global-free so it can be unit-tested with
/// an injected `upsert`; `init()` wires this to `crate::glossary::upsert`.
fn process_task(
    task: &DistillTask,
    min_conf: f32,
    upsert: &dyn Fn(&GlossaryEntry) -> Result<(), crate::error::EngineError>,
) -> usize {
    let pairs = term_extractor::extract_term_pairs(
        &task.source_text,
        &task.source_lang,
        &task.target_text,
        &task.target_lang,
    );
    process_pairs(pairs.into_iter(), min_conf, upsert)
}

/// Submit a distillation task (non-blocking; send never blocks on the unbounded channel)
pub fn submit(task: DistillTask) {
    if let Some(tx) = SENDER.get() {
        let _ = tx.send(task);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::error::EngineError;
    use std::cell::RefCell;

    /// The shipped default bar (the worker reads the same value from the global config).
    fn default_bar() -> f32 {
        Config::default().distill_min_conf
    }

    fn entry(target: &str, conf: f32) -> GlossaryEntry {
        GlossaryEntry {
            source_term: "src".into(),
            source_lang: "zh".into(),
            target_term: target.into(),
            target_lang: "en".into(),
            confidence: conf,
            frequency: 1,
            domain: String::new(),
            source: "distill".into(),
        }
    }

    fn task(src: &str, slang: &str, tgt: &str, tlang: &str) -> DistillTask {
        DistillTask {
            source_text: src.into(),
            source_lang: slang.into(),
            target_text: tgt.into(),
            target_lang: tlang.into(),
            domain: String::new(),
        }
    }

    #[test]
    fn below_threshold_is_never_upserted() {
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_pairs(
            vec![entry("ml", 0.50)].into_iter(),
            default_bar(),
            &|p| {
                seen.borrow_mut().push(p.target_term.clone());
                Ok(())
            },
        );
        assert_eq!(n, 0);
        assert!(seen.borrow().is_empty(), "upsert must not run below threshold");
    }

    #[test]
    fn at_threshold_is_upserted_inclusively() {
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_pairs(
            vec![entry("ai", default_bar())].into_iter(),
            default_bar(),
            &|p| {
                seen.borrow_mut().push(p.target_term.clone());
                Ok(())
            },
        );
        assert_eq!(n, 1);
        assert_eq!(&*seen.borrow(), &vec!["ai".to_string()]);
    }

    #[test]
    fn upsert_error_is_not_counted() {
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_pairs(
            vec![entry("nope", 0.95)].into_iter(),
            default_bar(),
            &|p| {
                seen.borrow_mut().push(p.target_term.clone());
                Err(EngineError::EngineFailed {
                    engine: "glossary".into(),
                    reason: "injected".into(),
                })
            },
        );
        assert_eq!(n, 0);
        assert_eq!(&*seen.borrow(), &vec!["nope".to_string()]);
    }

    #[test]
    fn mixed_batch_counts_only_ok_and_permissive() {
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let items = vec![
            entry("b", 0.50),  // below threshold -> skipped, upsert never called
            entry("o1", 0.90), // counted
            entry("o2", 0.95), // counted
            entry("e", 0.99),  // upsert returns Err -> not counted
        ];
        let n = process_pairs(items.into_iter(), default_bar(), &|p| {
            let t = p.target_term.clone();
            seen.borrow_mut().push(t.clone());
            if t == "e" {
                Err(EngineError::EngineFailed { engine: "glossary".into(), reason: "x".into() })
            } else {
                Ok(())
            }
        });
        assert_eq!(n, 2);
        assert!(seen.borrow().iter().all(|t| t != "b"), "below-threshold must not reach upsert");
    }

    #[test]
    fn config_bar_is_the_default_the_extractor_scores_against() {
        // The bar and the extractor's scoring are separate concerns that must agree on
        // scale; a bar above 1.0 would silently stop storing anything at all.
        assert!((default_bar() - 0.80).abs() < 1e-6);
        assert!(default_bar() <= 1.0);
    }

    #[test]
    fn process_task_stores_a_well_aligned_pair() {
        let t = task("深度学习很好", "zh", "deep learning is great", "en");
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_task(&t, default_bar(), &|p| {
            seen.borrow_mut().push(format!("{}|{}", p.source_term, p.target_term));
            Ok(())
        });
        assert!(n > 0, "a supported, well-aligned pair should extract at least one term");
        assert!(
            seen.borrow().iter().any(|k| k == "深度|deep learning"),
            "the aligned pair should be stored, got {:?}",
            seen.borrow()
        );
    }

    #[test]
    fn process_task_rejects_a_misaligned_negative_sample() {
        // End-to-end guard for D2 (N-08): before the fix this sentence stored a fragment
        // aligned to the preposition phrase "on the", because every candidate was
        // stamped with the same 0.90 and the bar could not reject anything.
        let t = task("在桌子上", "zh", "on the table", "en");
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_task(&t, default_bar(), &|p| {
            seen.borrow_mut().push(format!("{}|{}", p.source_term, p.target_term));
            Ok(())
        });
        assert_eq!(n, 0, "nothing from this misaligned pair may be stored, got {:?}", seen.borrow());
        assert!(seen.borrow().is_empty(), "upsert must not even be reached");
    }

    #[test]
    fn process_task_unsupported_pair_is_noop() {
        let t = task("bonjour", "fr", "hello", "en");
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_task(&t, default_bar(), &|p| {
            seen.borrow_mut().push(p.target_term.clone());
            Ok(())
        });
        assert_eq!(n, 0);
        assert!(seen.borrow().is_empty());
    }

    #[test]
    fn a_stricter_bar_stores_fewer_pairs() {
        // Proves the bar actually governs the decision, which the old constant-stamp
        // arrangement could not demonstrate (every candidate sat exactly on the bar).
        let t = task("深度学习很好", "zh", "deep learning is great", "en");
        let stored_at = |min_conf: f32| -> usize {
            process_task(&t, min_conf, &|_| Ok(()))
        };

        let permissive = stored_at(0.0);
        let shipped = stored_at(default_bar());
        let strict = stored_at(0.95);

        assert!(permissive > shipped, "the shipped bar must reject some candidates");
        assert!(shipped >= strict, "a stricter bar cannot store more than a laxer one");
        assert!(permissive > 0, "the extractor must produce candidates for this sentence");
    }
}
