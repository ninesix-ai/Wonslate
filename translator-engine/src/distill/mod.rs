// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 蒸馏管道（异步后台线程，AI 精译完成后自动提取术语对）

pub mod term_extractor;

use std::sync::{mpsc, OnceLock};
use std::thread;
use crate::types::{DistillTask, GlossaryEntry};

static SENDER: OnceLock<mpsc::Sender<DistillTask>> = OnceLock::new();

/// 初始化蒸馏后台线程（在 tt_init 中调用一次）
pub fn init() {
    let (tx, rx) = mpsc::channel::<DistillTask>();
    let _ = SENDER.set(tx);
    thread::spawn(move || {
        for task in rx {
            let stored = process_task(&task, &|p| crate::glossary::upsert(p));
            if stored > 0 {
                lt_debug!("[distill] stored {} new terms", stored);
            }
        }
    });
    lt_info!("[distill] background thread started");
}

/// Minimum confidence at which an extracted candidate term is trusted enough to persist.
/// Kept in sync with the value stamped by `term_extractor::extract_term_pairs`.
const STORE_CONFIDENCE: f32 = 0.90;

/// Pure, thread/global-free core of the distill worker: filter candidate pairs by
/// `confidence >= STORE_CONFIDENCE` and persist survivors via `upsert`. Returns the
/// number of entries actually stored (upserts that returned Err are not counted).
fn process_pairs<I>(
    pairs: I,
    upsert: &dyn Fn(&GlossaryEntry) -> Result<(), crate::error::EngineError>,
) -> usize
where
    I: Iterator<Item = GlossaryEntry>,
{
    let mut stored = 0usize;
    for pair in pairs {
        if pair.confidence >= STORE_CONFIDENCE && upsert(&pair).is_ok() {
            stored += 1;
        }
    }
    stored
}

/// Process a single `DistillTask`: extract candidate pairs via the term extractor and
/// hand them to `process_pairs`. Kept thread/global-free so it can be unit-tested with
/// an injected `upsert`; `init()` wires this to `crate::glossary::upsert`.
fn process_task(
    task: &DistillTask,
    upsert: &dyn Fn(&GlossaryEntry) -> Result<(), crate::error::EngineError>,
) -> usize {
    let pairs = term_extractor::extract_term_pairs(
        &task.source_text,
        &task.source_lang,
        &task.target_text,
        &task.target_lang,
    );
    process_pairs(pairs.into_iter(), upsert)
}

/// 提交蒸馏任务（非阻塞，无界 channel 上 send 实际不阻塞）
pub fn submit(task: DistillTask) {
    if let Some(tx) = SENDER.get() {
        let _ = tx.send(task);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::EngineError;
    use std::cell::RefCell;

    fn entry(target: &str, conf: f32) -> GlossaryEntry {
        GlossaryEntry {
            source_term: "src".into(),
            source_lang: "zh".into(),
            target_term: target.into(),
            target_lang: "en".into(),
            confidence: conf,
            frequency: 1,
            domain: String::new(),
        }
    }

    #[test]
    fn below_threshold_is_never_upserted() {
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_pairs(
            vec![entry("ml", 0.50)].into_iter(),
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
            vec![entry("ai", STORE_CONFIDENCE)].into_iter(),
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
        let n = process_pairs(items.into_iter(), &|p| {
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
    fn process_task_uses_real_extractor_for_supported_pair() {
        let task = DistillTask {
            source_text: "深度学习很好".into(),
            source_lang: "zh".into(),
            target_text: "deep learning is great".into(),
            target_lang: "en".into(),
            domain: String::new(),
        };
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_task(&task, &|p| {
            seen.borrow_mut().push(p.target_term.clone());
            Ok(())
        });
        let expected = term_extractor::extract_term_pairs(
            &task.source_text,
            &task.source_lang,
            &task.target_text,
            &task.target_lang,
        )
        .len();
        assert_eq!(n, expected);
        assert!(n > 0, "supported pair should extract at least one term");
    }

    #[test]
    fn process_task_unsupported_pair_is_noop() {
        let task = DistillTask {
            source_text: "bonjour".into(),
            source_lang: "fr".into(),
            target_text: "hello".into(),
            target_lang: "en".into(),
            domain: String::new(),
        };
        let seen: RefCell<Vec<String>> = RefCell::new(vec![]);
        let n = process_task(&task, &|p| {
            seen.borrow_mut().push(p.target_term.clone());
            Ok(())
        });
        assert_eq!(n, 0);
        assert!(seen.borrow().is_empty());
    }
}
