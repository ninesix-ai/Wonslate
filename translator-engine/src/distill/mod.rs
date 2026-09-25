// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 蒸馏管道（异步后台线程，AI 精译完成后自动提取术语对）

pub mod term_extractor;

use std::sync::{mpsc, OnceLock};
use std::thread;
use crate::types::DistillTask;

static SENDER: OnceLock<mpsc::Sender<DistillTask>> = OnceLock::new();

/// 初始化蒸馏后台线程（在 tt_init 中调用一次）
pub fn init() {
    let (tx, rx) = mpsc::channel::<DistillTask>();
    let _ = SENDER.set(tx);
    thread::spawn(move || {
        for task in rx {
            let pairs = term_extractor::extract_term_pairs(
                &task.source_text,
                &task.source_lang,
                &task.target_text,
                &task.target_lang,
            );
            let mut stored = 0usize;
            for pair in pairs {
                if pair.confidence >= 0.90 {
                    if crate::glossary::upsert(&pair).is_ok() {
                        stored += 1;
                    }
                }
            }
            if stored > 0 {
                lt_debug!("[distill] stored {} new terms", stored);
            }
        }
    });
    lt_info!("[distill] background thread started");
}

/// 提交蒸馏任务（非阻塞，无界 channel 上 send 实际不阻塞）
pub fn submit(task: DistillTask) {
    if let Some(tx) = SENDER.get() {
        let _ = tx.send(task);
    }
}
