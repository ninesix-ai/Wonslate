// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! License compliance gate (zero external dependencies).
//!
//! The component/model license inventory is compiled in as data and checked
//! against a blacklist/whitelist, so every dependency that ends up in a
//! closed-source commercial artifact must carry a permissive, commercially
//! usable license (Apache/MIT/BSD/ISC/CC0...).
//!
//! Design notes: the crate side of this project is nearly dependency-free
//! (just the serde_json tree). The real license red lines live in the
//! model / third-party-component layer (NLLB / SeamlessM4T = CC-BY-NC,
//! LibreTranslate = AGPL), which cargo-deny cannot see -- hence this module
//! registers them explicitly and gates them.
//!
//! Single source of truth: this list covers Rust crates, NuGet packages,
//! Python libraries and model weights. tests/test_component_licenses.py parses
//! it to enforce that every PackageReference shipped in the .csproj files is
//! registered here (fail-closed) and carries a provenance note.

/// Dependency kind (crate / model weights / third-party component).
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    Crate,
    Model,
    Component,
}

/// One registered dependency and its license identifier.
#[derive(Debug, Clone, Copy)]
pub struct Dep {
    pub name: &'static str,
    pub license: &'static str,
    pub kind: Kind,
}

/// Permissive, closed-source-commercial-friendly whitelist
/// (exact match; unknown licenses are denied by default).
///
/// This list is the single source of truth for allowed licenses: `deny.toml`
/// mirrors it for the crate layer, and tests/test_component_licenses.py fails
/// when the two lists diverge.
pub const ALLOWED_LICENSES: &[&str] = &[
    "Apache-2.0",
    "MIT",
    "ISC",
    "BSD-3-Clause",
    "CC0-1.0",
    // Unicode data-file/software license. Required in practice: unicode-ident
    // (pulled by proc-macro2 / syn -> serde_derive) declares
    // "(MIT OR Apache-2.0) AND Unicode-DFS-2016". Permissive, commercial OK.
    "Unicode-DFS-2016",
];

/// Known-dangerous blacklist (for self-check and warnings only; the verdict
/// is always driven by the whitelist above).
pub const DENIED_LICENSES: &[&str] = &[
    "CC-BY-NC-4.0",  // NLLB-200 / SeamlessM4T / Aya: non-commercial only
    "AGPL-3.0",      // LibreTranslate server: network copyleft
    "GPL-3.0",       // strong copyleft
];

/// The component/model/crate inventory actually used by this artifact.
/// Each entry carries a provenance note (official upstream source), enforced
/// by tests/test_component_licenses.py -- an entry without one fails the gate.
pub const REGISTERED_DEPS: &[Dep] = &[
    // crates.io registry: https://crates.io/crates/serde_json
    Dep { name: "serde_json",                    license: "MIT",        kind: Kind::Crate },
    // official k2-fsa NuGet package (sherpa-onnx upstream: https://github.com/k2-fsa/sherpa-onnx)
    Dep { name: "org.k2fsa.sherpa.onnx",         license: "Apache-2.0", kind: Kind::Component },
    // official k2-fsa runtime binding, same upstream/repository
    Dep { name: "org.k2fsa.sherpa.onnx.runtime.win-x64", license: "Apache-2.0", kind: Kind::Component },
    // PyPI official project (upstream: https://github.com/OpenNmt/CTranslate2)
    Dep { name: "ctranslate2",                   license: "MIT",        kind: Kind::Component },
    // PyPI official project (upstream: https://github.com/google/sentencepiece)
    Dep { name: "sentencepiece",                 license: "Apache-2.0", kind: Kind::Component },
    // PyPI official project (upstream: https://github.com/mjpost/sacrebleu).
    // Reached by the benchmark scoring path (script/bench_translation.py::score),
    // not by the shipped product runtime; registered because it is a real dep.
    Dep { name: "sacrebleu",                     license: "Apache-2.0", kind: Kind::Component },
    // PyPI official project (upstream: https://github.com/Unbabel/COMET).
    // Imported only by script/score_comet.py, the offline COMET scorer; it never
    // enters the product runtime, which stays free of any Python ML stack.
    Dep { name: "unbabel-comet",                 license: "Apache-2.0", kind: Kind::Component },
    // PyPI official project (upstream: https://github.com/pytorch/pytorch), the
    // tensor runtime COMET loads for scoring; same offline-only boundary as above.
    Dep { name: "torch",                         license: "BSD-3-Clause", kind: Kind::Component },
    // PyPI official project (upstream: https://github.com/huggingface/huggingface_hub).
    // Registered license is Apache-2.0 per that repository: the gate reads no
    // installed metadata, whose free-form License field here is a bare "Apache".
    Dep { name: "huggingface_hub",               license: "Apache-2.0", kind: Kind::Component },
    // Google official release on Hugging Face (facebook/madlad400)
    Dep { name: "madlad-400",                    license: "Apache-2.0", kind: Kind::Model },
    // Argos translate official model repository (MIT-licensed package exports)
    Dep { name: "argos-ct2",                     license: "MIT",        kind: Kind::Model },
    // FunAudioLLM official release (https://github.com/FunAudioLLM/SenseVoice)
    Dep { name: "sensevoice",                    license: "MIT",        kind: Kind::Model },
    // Kokoro official release (https://github.com/hexgrad/kokoro)
    Dep { name: "kokoro",                        license: "Apache-2.0", kind: Kind::Model },
    // Alibaba Qwen official release (https://github.com/QwenLM)
    Dep { name: "qwen3",                         license: "Apache-2.0", kind: Kind::Model },
    // Ollama server, official MIT repository (https://github.com/ollama/ollama);
    // accessed only via its plain-HTTP local API, no third-party client shipped
    Dep { name: "ollama",                        license: "MIT",        kind: Kind::Component },
    // xunit on NuGet (upstream: https://github.com/xunit/xunit, LICENSE = MIT)
    Dep { name: "xunit",                         license: "MIT",        kind: Kind::Component },
    // official xunit runner, same upstream org (https://github.com/xunit/visualstudio.xunit, MIT)
    Dep { name: "xunit.runner.visualstudio",     license: "MIT",        kind: Kind::Component },
    // official Microsoft test platform (upstream: https://github.com/microsoft/vstest)
    Dep { name: "Microsoft.NET.Test.Sdk",        license: "MIT",        kind: Kind::Component },
];

/// Whether a license is permissive and commercially usable
/// (exact whitelist match; unknown licenses are always denied).
pub fn is_allowed(license: &str) -> bool {
    ALLOWED_LICENSES.contains(&license)
}

/// Audit a dependency set, returning the names of all license violations
/// (empty = fully compliant).
pub fn audit(deps: &[Dep]) -> Vec<&'static str> {
    deps.iter()
        .filter(|d| !is_allowed(d.license))
        .map(|d| d.name)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn permissive_licenses_are_allowed() {
        assert!(is_allowed("Apache-2.0"));
        assert!(is_allowed("MIT"));
        assert!(is_allowed("ISC"));
        assert!(is_allowed("BSD-3-Clause"));
        assert!(is_allowed("CC0-1.0"));
        assert!(is_allowed("Unicode-DFS-2016"));
    }

    #[test]
    fn copyleft_and_noncommercial_licenses_are_denied() {
        assert!(!is_allowed("CC-BY-NC-4.0"));
        assert!(!is_allowed("AGPL-3.0"));
        assert!(!is_allowed("GPL-3.0"));
        // Unknown licenses are denied by default (fail-closed, never fail-open).
        assert!(!is_allowed("Weird-Unknown-License"));
    }

    #[test]
    fn allowed_and_denied_lists_are_disjoint() {
        for d in DENIED_LICENSES {
            assert!(!ALLOWED_LICENSES.contains(d),
                "license {} appears in both the allow and deny lists", d);
        }
    }

    #[test]
    fn registered_deps_are_all_compliant() {
        // The real inventory must be fully compliant or the gate is decoration.
        let violations = audit(&REGISTERED_DEPS);
        assert!(violations.is_empty(), "registered components/models have license violations: {:?}", violations);
    }

    #[test]
    fn audit_flags_a_poisoned_entry() {
        let poisoned = [
            Dep { name: "nllb-200", license: "CC-BY-NC-4.0", kind: Kind::Model },
            Dep { name: "libretranslate", license: "AGPL-3.0", kind: Kind::Component },
        ];
        let v = audit(&poisoned);
        assert!(v.contains(&"nllb-200"), "CC-BY-NC models must be blocked");
        assert!(v.contains(&"libretranslate"), "AGPL components must be blocked");
    }

    #[test]
    fn registered_deps_cover_key_models() {
        // Guard against the inventory being accidentally emptied: the core
        // commercially usable models and engines must stay registered.
        let names: Vec<_> = REGISTERED_DEPS.iter().map(|d| d.name).collect();
        for expected in ["qwen3", "sensevoice", "kokoro", "sherpa-onnx",
                         "org.k2fsa.sherpa.onnx", "ctranslate2", "sentencepiece"] {
            if expected == "sherpa-onnx" {
                // Historical name; the NuGet id is the registered one.
                assert!(names.contains(&"org.k2fsa.sherpa.onnx"),
                    "core component {} is not registered in the license inventory", expected);
                continue;
            }
            assert!(names.contains(&expected),
                "core component {} is not registered in the license inventory", expected);
        }
    }

    #[test]
    fn no_orphan_entries_in_the_inventory() {
        // Every registered component must actually ship or be documented as a
        // runtime dependency; naudio-style orphans were removed in the audit.
        let names: Vec<_> = REGISTERED_DEPS.iter().map(|d| d.name).collect();
        assert!(!names.contains(&"naudio"), "naudio is not referenced anywhere; must stay removed");
    }
}
