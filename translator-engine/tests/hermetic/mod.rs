// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

#![allow(dead_code)]

//! One hermetic environment for every Rust integration suite.
//!
//! Why this file exists. A suite that wants a dead AI endpoint pins
//! `WONSLATE_OLLAMA_URL` and moves on -- but resolution is `LT_*` first, then the brand
//! spelling, then the bare name, so a developer who exports `LT_OLLAMA_URL` to point the
//! engine at their own model server (the obvious thing to do) silently outranks the pin.
//! The suite then drives the machine instead of its own fixture and fails with an
//! assertion that looks like a product regression. That was defect D36, found in
//! `ai_upgrade_e2e`; the same shape was then measured in `tm_quality_floor_e2e`, where
//! the suite's own premise ("an upgrade engine is reachable") was overridden the same
//! way: ambient shell, 5 passed in 1.35 s; with `LT_OLLAMA_URL` exported to a dead port,
//! 1 failed in 0.21 s. The timing gap is the point -- two different code paths ran, so
//! the coverage number moves with the host too.
//!
//! A developer machine is the worst place to discover this: on the box where argos and
//! madlad sidecars and Ollama are all running, every unpinned network slot resolves to a
//! real service that CI does not have.
//!
//! Two rules, both enforced here rather than repeated per file:
//!
//! 1. Pin **every spelling** of every knob the engine reads, and *clear* the knobs the
//!    suite does not name, so neither an ambient variable nor a saved settings file can
//!    decide a result.
//! 2. **Assert the resolved value**, not the intended one. `verify()` reads the winning
//!    spelling through the product's own `first_env`, checks what `config::get()`
//!    actually resolved to, confirms the routes and settings files fall inside the
//!    suite's own temp directory, and probes whether the AI slot really is (or is not)
//!    reachable. A pin that lost turns into a named precondition failure instead of a
//!    confusing red assertion several frames away.
//!
//! Call shape: build the `Spec`, `apply()` it, then `config::load()` and `tm::init()`,
//! then `verify()`. Order matters because the config singleton keeps the first value it
//! is given.
//!
//! Pinning a slot dead does not abandon the code behind it: the paths that need a live
//! service are covered where an instance can be handed an explicit address -- the mock
//! servers in `engine::sidecar` and `engine::http`, and the stub `ai_upgrade_e2e` owns.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use translator_engine::config;

/// An address nothing listens on: a refused connection, not a hang.
pub const DEAD: &str = "http://127.0.0.1:1";

/// Model name pinned for every suite. Nothing in these tests reads a real model, and a
/// host `LT_OLLAMA_MODEL` would otherwise decide what a prompt claims to ask for.
const TEST_MODEL: &str = "hermetic-test-model";

/// Every environment knob the engine reads, with its spellings in the product's own
/// precedence order (the lists mirror `config::build_config`, `config::data_dir_for`,
/// `config::routes_file`, `config::settings_file` and `engine::sidecar::from_env`).
/// A knob listed here but left unset is *cleared*, which is what keeps a developer's
/// shell out of the result.
const KNOBS: &[(&str, &[&str])] = &[
    ("DATA_DIR", &["LT_DATA_DIR", "WONSLATE_DATA_DIR", "DATA_DIR"]),
    (
        "OLLAMA_URL",
        &["LT_OLLAMA_URL", "WONSLATE_OLLAMA_URL", "OLLAMA_URL"],
    ),
    (
        "OLLAMA_MODEL",
        &["LT_OLLAMA_MODEL", "WONSLATE_OLLAMA_MODEL", "OLLAMA_MODEL"],
    ),
    (
        "OLLAMA_TIMEOUT_MS",
        &[
            "LT_OLLAMA_TIMEOUT_MS",
            "WONSLATE_OLLAMA_TIMEOUT_MS",
            "OLLAMA_TIMEOUT_MS",
        ],
    ),
    (
        "SIDECAR_TIMEOUT_MS",
        &["LT_SIDECAR_TIMEOUT_MS", "WONSLATE_SIDECAR_TIMEOUT_MS"],
    ),
    ("ARGOS_URL", &["LT_ARGOS_URL", "WONSLATE_ARGOS_URL"]),
    ("MADLAD_URL", &["LT_MADLAD_URL", "WONSLATE_MADLAD_URL"]),
    (
        "QUALITY_PREFERENCE",
        &["LT_QUALITY_PREFERENCE", "WONSLATE_QUALITY_PREFERENCE"],
    ),
    (
        "FULL_UPGRADE_POLICY",
        &["LT_FULL_UPGRADE_POLICY", "WONSLATE_FULL_UPGRADE_POLICY"],
    ),
    (
        "FULL_TM_QUALITY_FLOOR",
        &["LT_FULL_TM_QUALITY_FLOOR", "WONSLATE_FULL_TM_QUALITY_FLOOR"],
    ),
    (
        "FULL_UPGRADE_THRESHOLD",
        &["LT_FULL_UPGRADE_THRESHOLD", "WONSLATE_FULL_UPGRADE_THRESHOLD"],
    ),
    (
        "DISTILL_MIN_CONF",
        &["LT_DISTILL_MIN_CONF", "WONSLATE_DISTILL_MIN_CONF"],
    ),
    ("ROUTES_FILE", &["LT_ROUTES_FILE", "WONSLATE_ROUTES_FILE"]),
    (
        "SETTINGS_FILE",
        &["LT_SETTINGS_FILE", "WONSLATE_SETTINGS_FILE"],
    ),
];

/// The environment one test binary wants. Start from `Spec::new`, which is "nothing is
/// reachable and every tuning knob is the shipped default", then name the exceptions.
pub struct Spec {
    dir: PathBuf,
    pinned: HashMap<&'static str, String>,
    /// Whether the AI slot must answer a connection probe. True only for a suite that
    /// started its own stub; a stub that stopped answering is a premise failure, and
    /// `verify()` says so before an unrelated assertion does.
    ai_reachable: bool,
}

impl Spec {
    /// Dead sidecars, dead AI, own temp data directory, tuning knobs cleared.
    ///
    /// The routes and settings files are not pinned by name: with `DATA_DIR` inside the
    /// suite's own directory they resolve there, and `verify()` checks that they do. A
    /// developer who saved a routing table from the UI therefore cannot retune a test.
    pub fn new(dir: &Path) -> Self {
        let mut spec = Self {
            dir: dir.to_path_buf(),
            pinned: HashMap::new(),
            ai_reachable: false,
        };
        spec.pinned.insert("DATA_DIR", dir.display().to_string());
        spec.pinned.insert("OLLAMA_URL", DEAD.to_string());
        spec.pinned.insert("OLLAMA_MODEL", TEST_MODEL.to_string());
        // Fail fast rather than wait out the shipped 120 s, if a slot ever is reached.
        spec.pinned.insert("OLLAMA_TIMEOUT_MS", "500".to_string());
        spec.pinned.insert("SIDECAR_TIMEOUT_MS", "500".to_string());
        spec.pinned.insert("ARGOS_URL", DEAD.to_string());
        spec.pinned.insert("MADLAD_URL", DEAD.to_string());
        spec
    }

    /// Point the AI slot at a stub this binary owns, and require that it answers.
    pub fn ai_at(mut self, url: &str) -> Self {
        self.pinned.insert("OLLAMA_URL", url.to_string());
        self.ai_reachable = true;
        self
    }

    /// Point both sidecar slots at a stub this binary owns.
    ///
    /// Both, not just the one the routing currently selects: a suite that says "the local
    /// engine answered" has to mean it whatever the router decides, and leaving the other
    /// slot at the shipped default would silently hand the developer's own argos or madlad
    /// service the request (that is how a coverage number came to describe a machine).
    /// Assertions still name which engine served, so this cannot hide a routing change.
    pub fn sidecar_at(mut self, url: &str) -> Self {
        self.pinned.insert("ARGOS_URL", url.to_string());
        self.pinned.insert("MADLAD_URL", url.to_string());
        self
    }

    /// The preference the settings page would write; it maps onto the full-mode policy
    /// and TM quality floor, which is why it belongs in the pinned set.
    pub fn quality_first(mut self) -> Self {
        self.pinned
            .insert("QUALITY_PREFERENCE", "quality_first".to_string());
        self
    }

    /// Opt out of D14's shipped "always ask the AI" for the full mode.
    pub fn low_confidence_upgrade(mut self) -> Self {
        self.pinned
            .insert("FULL_UPGRADE_POLICY", "low_confidence".to_string());
        self
    }

    /// How long a call to the AI slot may take. The shipped value is 120 s, which turns a
    /// fixture that stopped answering into a stalled suite.
    pub fn ai_timeout_ms(mut self, ms: &str) -> Self {
        self.pinned
            .insert("OLLAMA_TIMEOUT_MS", ms.to_string());
        self
    }

    /// State the floor a suite is studying, instead of inheriting a host override.
    pub fn full_tm_floor(mut self, value: &str) -> Self {
        self.pinned
            .insert("FULL_TM_QUALITY_FLOOR", value.to_string());
        self
    }

    /// Write every spelling of what is pinned, remove every spelling of what is not.
    pub fn apply(&self) {
        for (knob, spellings) in KNOBS {
            match self.pinned.get(*knob) {
                Some(value) => {
                    for name in *spellings {
                        std::env::set_var(name, value);
                    }
                }
                None => {
                    for name in *spellings {
                        std::env::remove_var(name);
                    }
                }
            }
        }
    }

    /// The same pins for a child process, which otherwise inherits this one's host.
    ///
    /// The MCP suite drives a real binary, so "the machine cannot decide the result" only
    /// holds if the child gets the same environment: `Command::env` beats what is
    /// inherited, and `env_remove` deletes a name the parent happens to export.
    pub fn apply_to(&self, cmd: &mut std::process::Command) {
        for (knob, spellings) in KNOBS {
            match self.pinned.get(*knob) {
                Some(value) => {
                    for name in *spellings {
                        cmd.env(name, value);
                    }
                }
                None => {
                    for name in *spellings {
                        cmd.env_remove(name);
                    }
                }
            }
        }
    }

    /// The value this suite asked for a knob, for its own assertions to quote.
    pub fn value(&self, knob: &str) -> String {
        self.pinned
            .get(knob)
            .unwrap_or_else(|| panic!("{} is not pinned by this Spec", knob))
            .clone()
    }

    /// Prove the pin took, through the product's own resolution.
    ///
    /// Runs after `config::load()`: reading `config::get()` first would freeze the
    /// singleton and let a stale value pass.
    pub fn verify(&self) {
        let lookup = |k: &str| std::env::var(k).ok();

        for (knob, spellings) in KNOBS {
            let want = self.pinned.get(*knob);
            let got = config::first_env(&lookup, spellings);
            match want {
                Some(value) => assert_eq!(
                    Some(value.clone()),
                    got,
                    "D36: the winning spelling of {} is not what this suite pinned -- \
                     an ambient variable outranks the test, so it is describing this \
                     machine and not the product",
                    knob
                ),
                None => assert!(
                    got.is_none(),
                    "D36: {} is set in the environment ({:?}); this suite expects the \
                     shipped default, so the knob is cleared and the failure names it",
                    knob,
                    got
                ),
            }
        }

        let cfg = config::get();
        assert_eq!(
            cfg.ollama_url,
            self.value("OLLAMA_URL"),
            "the AI endpoint the pipeline will dial is not the one this suite pinned"
        );
        assert_eq!(
            cfg.ollama_model,
            self.value("OLLAMA_MODEL"),
            "a host model name changed what the prompt asks for"
        );
        assert_eq!(
            config::data_dir(),
            self.dir,
            "the data directory is not this suite's own, so a saved routes file or the \
             user's real store decides the result"
        );
        for (label, path) in [
            ("routes", config::routes_file(&lookup)),
            ("settings", config::settings_file(&lookup)),
        ] {
            assert!(
                path.starts_with(&self.dir),
                "the {} file resolves to {:?}, outside this suite's temp directory -- \
                 a file the developer wrote would silently retune the routing",
                label,
                path
            );
        }
        assert_eq!(
            translator_engine::engine::is_reachable("ollama"),
            self.ai_reachable,
            "the AI slot's reachability is not what this suite assumes ({:?}); its \
             premises are stated in the file header, so a mismatch means the fixture \
             is not answering",
            self.ai_reachable
        );
    }
}
