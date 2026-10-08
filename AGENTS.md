# AGENTS.md — how to work in this repository

This file is for AI coding agents and for contributors who are about to edit files.
It carries only what is written down nowhere else: the entry points that actually
work, the traps that have already cost a session, and where project truth is kept.

- Product overview, prerequisites, demo inputs: `README.md`
- Conventions, invariants, pull requests: `CONTRIBUTING.md`

## 1. What this repo is

Wonslate（万邦译）is a local, offline, multi-engine translation engine. The call chain
is WPF UI (C#) → C ABI FFI → Rust `translator_engine` cdylib → Python CT2 sidecar
(Argos / MADLAD), with a local Ollama engine behind the quality tier. Four languages
live in one build: Rust, C#, Python, XAML.

Requirements, task status and the commercial plan are **not in this repository**. They
live in a private umbrella repo that pins this one as a git submodule. Nothing below
quotes their numbers, because those numbers move.

## 2. Work here — only these entry points

Everything runs from the repo root through `script/build.py`. `build.bat` and
`build.sh` are thin per-OS forwarders that pass every argument through.

| Intent | Command |
|---|---|
| Build only | `python script/build.py` |
| Build + every test layer | `python script/build.py --test` |
| Build + Rust and .NET tests | `python script/build.py --unit` |
| Build + the Python suites | `python script/build.py --ffi` |
| Build and start the GUI | `python script/build.py --run` |
| XAML static lint + window-appears smoke | `verify.bat` / `verify.sh` |
| Self-sign output so local smokes repeat | `sign.bat` |
| Measure what a long batch actually costs (D24/D30 probes; `--help` for the four modes) | `python script/diag_sidecar_batch.py <mode>` |

## 3. Traps that have already cost someone a session

Every one of these was hit in practice. The `D` numbers point into the ledgers.

1. **Never verify with a bare `dotnet test`.** `DomainGlossaryTests` loads
   `Wonslate.UI/translator_engine.dll`, a file `script/build.py` copies there during
   its build step. In a fresh checkout or a new worktree it is absent and three cases
   fail for reasons unrelated to your change. Use `build.py --unit`, which builds the
   native library first. The authoritative entry point is the only one whose verdict
   can be trusted.
2. **The Python suites are enumerated by name, in two places** — `run_ffi_smoke()` in
   `script/build.py` and the `python-ffi` job in `.github/workflows/ci.yml`. This is
   not `unittest discover`: a new `tests/test_*.py` that is not registered in **both**
   silently never runs, locally and in CI.
3. **A test may not depend on the developer's machine.** Two defects were fixed on
   exactly this: fixtures that read host state (D12, now sealed), and a repo-root
   assertion that matched the *checkout directory name*, so every worktree and every
   renamed checkout went red on it (D19). Assert positions relative to the test file,
   and resolve both sides of the comparison. Watch the environment chain in particular:
   resolution is `LT_*` > `WONSLATE_*` > bare name, so pinning only `WONSLATE_OLLAMA_URL`
   still loses to a host that exports `LT_OLLAMA_URL` -- the suite then drives the
   developer's model server instead of its own stub, which is how D36 broke the AI
   upgrade cases on a clean checkout. Pin every spelling, then assert the resolved
   config really points where the test thinks it does; that guard turns a confusing red
   suite into a named precondition failure.
4. **Glossary seed packs bypass the Rust source-language guard.** Import arrives
   through `tt_glossary_import_pack` and never passes `distill/term_extractor.rs`, so
   a mis-labelled row can still enter the store and the extractor's check will not
   stop it. The gate that does catch it is `tests/test_glossary_hygiene.py` (D20).
   Keep it registered.
5. **A SKIP is not a pass.** When Windows application control blocks the freshly built
   native library, `script/build.py` reports the suites that cross the FFI boundary as
   SKIP instead of FAIL, because the block is an environment condition and not a
   product defect. Say which suites skipped. Never call that run green.
6. **`pipeline::translate_full` has several exits, and each must state its own facts.**
   The domain note and the fallback note describe *the engine that answered*, so an exit
   that re-uses a note computed for another engine is lying: that was D33, present on two
   of the four returns, surviving the S11 wiring and every Rust test until the AI success
   path got its first end-to-end case. If you add an exit, take its notes from the engine
   named in that response's `engine` field, and add a case that reads them back.
7. **When the store keeps two copies of one row, one decision must feed both.** `tm_upsert`
   decides whether a better translation replaces the stored one, and used to hand the
   *incoming* row to the LRU cache regardless of that decision -- a worse candidate for a known
   text therefore left the file and the running session disagreeing, with a restart quietly
   changing the answer back and nothing reported (defect D37, found by chasing a coverage gap
   rather than by a failing test). Any write path must publish what actually settled.
8. **Refuse before writing, not halfway through.** `glossary_import_pack` validated entries
   inside the loop that imported them, so an invalid entry aborted the run *after* its
   predecessors had been upserted: the caller saw `ok:false` and the store kept part of the
   pack (defect D38). A validator that can reject the whole document must stage first.
9. **A new FFI export must be called from `tests/test_phase1_ffi.py`.** That suite records
   which exports it really reached and compares the set against `lib.rs`; a difference fails
   the run. Coverage cannot answer this question -- llvm-cov instruments the binary
   `cargo test` builds, while `lib.rs` is exercised by Python and .NET loading the compiled
   cdylib, so the file reads 0% no matter how much of it is covered. Declaring an export in
   `Engine.__init__` does not count as reaching it; only calling it does.
10. **A restart test needs its own binary.** `TmStore::open` fills a `OnceLock`, so a process
   loads a store exactly once, and every other suite deletes the two JSON files before
   `init`. That is why `tests/tm_reload.rs` exists. Anything asserting what survives a reboot
   belongs in a target of its own; added to an initialised suite it would be testing the
   in-memory index instead, and it would pass.

## 4. Conventions and invariants

**This file is not the source of truth for either.** `CONTRIBUTING.md` is: comment
language, Conventional Commits, line endings, UTF-8 marshalling across the FFI
boundary, the `LT_*` > `WONSLATE_*` > bare environment prefix chain, and the two
invariants that must not be broken — the privacy fail-closed clamp in `pipeline.rs`
and the Apache-2.0 / MIT licensing gates. If a convention needs changing, change it
there, not here.

## 5. Layout map

| Path | Owns | Read first, because |
|---|---|---|
| `translator-engine/src/lib.rs` | the C ABI export surface and the panic guard | `docs/sdk.md` — the exported set is a frozen contract |
| `translator-engine/src/pipeline.rs` | the five-layer pipeline, privacy clamp, honesty notes | `CONTRIBUTING.md` §Invariants |
| `translator-engine/src/router.rs` | rule-based engine selection and the upgrade path | `pipeline.rs` — the clamp runs *after* routing |
| `translator-engine/src/engine/` | `Translator` trait, registry, demo/http/ollama/sidecar adapters | `engine/mod.rs` — an unregistered engine is unreachable |
| `translator-engine/src/tm/` | translation memory: persistence, LRU, flag-bad | `docs/terminology.md` |
| `translator-engine/src/distill/` | term extraction and its scoring thresholds | §3.4 and `tests/test_glossary_hygiene.py` |
| `translator-engine/src/config.rs` | four-level config precedence (also `lang.rs`, `glossary.rs`, `confidence.rs`) | `docs/sdk.md` — the config contract |
| `translator-engine/src/license_gate.rs` | `REGISTERED_DEPS`, the allow-list for non-crate deps | `CONTRIBUTING.md` §Invariants |
| `translator-engine/src/bin/wonslate-mcp.rs` | the MCP stdio server over the same core | `docs/mcp.md` |
| `Wonslate.UI/` | `Interop/` mirrors `lib.rs`; `Sidecar/` owns the process and tells liveness from readiness; `ViewModels/` is the app logic; `Audio/` is the VAD/ASR/TTS chain | change both sides of the FFI together; `docs/user-guide.md` for voice |
| `sidecar/ct2_sidecar.py` | the CT2 Argos / MADLAD HTTP service | `docs/sdk.md` §sidecar; the CT2 backends do not consume `glossary` |
| `script/` + `tests/` | the authoritative build/test entry, the benches, and the gate suites | §2, §3.2 and `.github/workflows/ci.yml` |

## 6. Verify before you claim done

1. `python script/build.py --test` from the repo root, and quote the last line you got.
2. Anything touching a quality claim: re-run the named bench and point at the evidence
   file (`docs/translation-benchmark.md`, `docs/flores-benchmark.md`).
3. Anything adding a suite, a gate or an engine: update both enumerations (§3.2), and
   `docs/sdk.md` if the export surface moved.
4. Do not write project status numbers into this repo's docs. They belong to the
   private ledgers, and here they would only go stale.

## 7. Where truth lives, and what is open right now

Status truth is `docs/requirements/00-需求总纲.md` and `docs/tasks/00-任务总纲.md` in
the private umbrella repo. The open items worth knowing before picking a task — the
identifiers are stable anchors, the detail lives in the ledgers:

- `D30` — half closed. `/health` now reports `requests_served`, a bounded latency window
  and `rss_bytes`, so degradation is observable; what is still open is that a request the
  client abandoned keeps decoding server-side, and there is no batch limit or reload.
- `D34` — closed 2026-10-08. A pack row that omitted `confidence` entered at the reader's
  0.90 default rather than the intended 1.0, so a seed pack could not displace the distilled
  rows it was written to override while still reporting `imported: N`. The stamp is now
  presence-based. Rows already in a user's store keep their old value until the pack is
  re-imported; the AV comparison was re-run after that re-import and moved only inside the
  repeat-to-repeat spread, so the published conclusion is unchanged.
- `D35` — open. `user_locked` is honoured by the store and restored faithfully on load, but no
  UI control or FFI field can set it. The documented "a locked term is not overwritten by
  distillation" therefore holds only for a `glossary.json` hand-edited while the engine is
  stopped. The mechanism exists; the entry point does not.
- `REQ-B2` — undecided scope: does the no-silent-degradation invariant cover the
  sidecar's public HTTP channel, or only the Rust pipeline?
- `S12` — closed as **a small but statistically supported gain**, once D32 was fixed and both arms were
  rerun under input-aware injection. The earlier "no attributable gain" reading was an artifact of
  injecting a pack prefix rather than the terms each sentence uses: a measurement hole can erase a
  real effect as easily as a scoring bug can invent one. Disclose it as terminology consistency plus
  a modest quality edge, never as "much better translation" — the polysemant subset came out
  slightly negative. T2 (more pairs) and T4 (larger corpus) remain.
- `N-10` — microphone capture is missing; the voice chain runs on files only.
- `F4` — cross-platform has not started; the client is Windows-only today.
- `REQ-F5` — MCP covers the stdio server; SDK, Resources and authentication are open.
