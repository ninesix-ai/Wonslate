// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 内存 Cache（HashMap + 简单 FIFO 淘汰，零外部依赖）

use std::collections::{HashMap, VecDeque};
use std::sync::{Mutex, OnceLock};
use crate::types::TmEntry;

pub struct TmCache {
    inner: Mutex<CacheInner>,
}

struct CacheInner {
    map: HashMap<String, TmEntry>,
    order: VecDeque<String>,   // FIFO 淘汰顺序
    capacity: usize,
}

impl TmCache {
    pub fn new(capacity: usize) -> Self {
        Self {
            inner: Mutex::new(CacheInner {
                map: HashMap::with_capacity(capacity),
                order: VecDeque::with_capacity(capacity),
                capacity,
            }),
        }
    }

    pub fn get(&self, hash: &str) -> Option<TmEntry> {
        self.inner.lock().ok()?.map.get(hash).cloned()
    }

    pub fn put(&self, hash: String, entry: TmEntry) {
        if let Ok(mut c) = self.inner.lock() {
            // 已存在：先移除旧位置再插入新位置（模拟 LRU touch）
            if c.map.remove(&hash).is_some() {
                let pos = c.order.iter().position(|h| h == &hash);
                if let Some(p) = pos { c.order.remove(p); }
            }
            // 容量满：FIFO 淘汰最老条目
            while c.map.len() >= c.capacity {
                if let Some(old) = c.order.pop_front() {
                    c.map.remove(&old);
                } else { break; }
            }
            c.order.push_back(hash.clone());
            c.map.insert(hash, entry);
        }
    }

    pub fn invalidate(&self, hash: &str) {
        if let Ok(mut c) = self.inner.lock() {
            c.map.remove(hash);
            if let Some(pos) = c.order.iter().position(|h| h == hash) {
                c.order.remove(pos);
            }
        }
    }

    /// 与 store 同步命中计数（保持 cache 视图 hit_count 单调递增）
    pub fn bump_hit_count(&self, hash: &str) {
        if let Ok(mut c) = self.inner.lock() {
            if let Some(e) = c.map.get_mut(hash) {
                e.hit_count += 1;
            }
        }
    }

    pub fn len(&self) -> usize {
        self.inner.lock().map(|c| c.map.len()).unwrap_or(0)
    }

    pub fn warmup(&self, entries: Vec<TmEntry>) {
        let n = entries.len();
        for entry in entries {
            let hash = crate::tm::compute_hash(
                &entry.source_text, &entry.source_lang, &entry.target_lang,
            );
            self.put(hash, entry);
        }
        eprintln!("[tm.cache] warmed up {} entries", n);
    }
}

pub static TM_CACHE: OnceLock<TmCache> = OnceLock::new();

pub fn init(capacity: usize) {
    let _ = TM_CACHE.set(TmCache::new(capacity));
}

// ── 单元测试（用局部实例，绕开全局 OnceLock）──────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::TmEntry;

    fn e(text: &str, quality: f32) -> TmEntry {
        TmEntry {
            source_text: text.into(), source_lang: "zh".into(),
            target_text: format!("en_{}", text), target_lang: "en".into(),
            engine: "test".into(), quality, hit_count: 1, domain: String::new(),
        }
    }

    #[test]
    fn put_then_get_returns_entry() {
        let c = TmCache::new(10);
        c.put("h1".into(), e("hello", 0.9));
        let got = c.get("h1").expect("should hit");
        assert_eq!(got.source_text, "hello");
    }

    #[test]
    fn get_missing_returns_none() {
        let c = TmCache::new(10);
        assert!(c.get("nope").is_none());
    }

    #[test]
    fn invalidate_removes_entry() {
        let c = TmCache::new(10);
        c.put("h1".into(), e("x", 0.9));
        c.invalidate("h1");
        assert!(c.get("h1").is_none());
    }

    #[test]
    fn capacity_eviction_uses_fifo_order() {
        // 容量 3，插入 4 条，最早一条应被淘汰
        let c = TmCache::new(3);
        c.put("a".into(), e("A", 0.9));
        c.put("b".into(), e("B", 0.9));
        c.put("c".into(), e("C", 0.9));
        c.put("d".into(), e("D", 0.9));
        assert!(c.get("a").is_none(), "oldest should be evicted");
        assert!(c.get("b").is_some());
        assert!(c.get("c").is_some());
        assert!(c.get("d").is_some());
    }

    #[test]
    fn re_put_moves_to_end_and_updates_value() {
        let c = TmCache::new(3);
        c.put("a".into(), e("old", 0.5));
        c.put("b".into(), e("B", 0.9));
        c.put("c".into(), e("C", 0.9));
        // 更新 a 的内容，并把它移到队尾
        c.put("a".into(), e("new", 0.95));
        // 再加一个触发淘汰：这时 b（最老未被 touch）应被踢
        c.put("d".into(), e("D", 0.9));
        assert!(c.get("b").is_none(), "b was oldest untouched after a-refresh");
        let a = c.get("a").expect("a refreshed, should survive");
        assert_eq!(a.source_text, "new");
    }

    #[test]
    fn len_tracks_current_size() {
        let c = TmCache::new(10);
        assert_eq!(c.len(), 0);
        c.put("h".into(), e("x", 0.9));
        assert_eq!(c.len(), 1);
        c.invalidate("h");
        assert_eq!(c.len(), 0);
    }

    #[test]
    fn zero_capacity_clamped_to_one() {
        // 防御性：容量 0 应被 clamp 到 1，不 panic
        let c = TmCache::new(0);
        c.put("k".into(), e("v", 0.9));
        assert!(c.get("k").is_some());
    }
}
