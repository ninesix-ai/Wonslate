// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 建表 DDL（幂等，可重复执行）

pub const DDL: &str = r#"
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS translation_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_hash TEXT NOT NULL,
    source_text TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_text TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    engine      TEXT NOT NULL,
    quality     REAL DEFAULT 0.0,
    hit_count   INTEGER DEFAULT 1,
    domain      TEXT DEFAULT '',
    flagged     INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(source_hash, source_lang, target_lang)
);

CREATE INDEX IF NOT EXISTS idx_tm_hash     ON translation_memory(source_hash);
CREATE INDEX IF NOT EXISTS idx_tm_langpair ON translation_memory(source_lang, target_lang);
CREATE INDEX IF NOT EXISTS idx_tm_quality  ON translation_memory(quality DESC);
CREATE INDEX IF NOT EXISTS idx_tm_hit      ON translation_memory(hit_count DESC);

CREATE TABLE IF NOT EXISTS glossary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_term TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_term TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    alt_term    TEXT DEFAULT '',
    confidence  REAL DEFAULT 0.90,
    frequency   INTEGER DEFAULT 1,
    domain      TEXT DEFAULT '',
    user_locked INTEGER DEFAULT 0,
    UNIQUE(source_term, source_lang, target_lang)
);
CREATE INDEX IF NOT EXISTS idx_glossary_langpair ON glossary(source_lang, target_lang);
CREATE INDEX IF NOT EXISTS idx_glossary_freq     ON glossary(frequency DESC);

INSERT OR IGNORE INTO schema_version(version) VALUES (1);
"#;
