# CI & local quality gates

Wonslate keeps itself healthy through two complementary layers.

## Server-side CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and pull
request:

- Rust engine build + tests (`cargo test --release`) on **three OSes**
  (`ubuntu-latest`, `windows-latest`, `macos-latest`); each job publishes its native
  library (`dll` / `so` / `dylib`) as a `translator-engine-<os>` build artifact
- Python FFI smoke (`tests/test_phase1_ffi.py` + `tests/test_sidecar_e2e.py`)
- .NET WPF build + xUnit (Windows runner)
- Two-layer license gate:
  - **crate layer** — `cargo-deny` checks `Cargo.lock` against the allow-list in
    [`translator-engine/deny.toml`](../translator-engine/deny.toml)
  - **model/component layer** — `license_gate` unit tests cover non-crate licenses
    that cargo-deny cannot see
- Rust coverage baseline (`cargo-llvm-cov`, non-blocking)

## Local pre-commit hook

[`ci/hooks/pre-commit`](hooks/pre-commit) runs the fastest high-signal checks before
each commit to catch obvious breakage early. Enable it once per clone:

```bash
python script/install_hooks.py
```

This copies `ci/hooks/*` into your active `.git/hooks/` directory (re-running picks up
updates). The hook only runs checks for staged files that touch the relevant subtree:

| Staged path | Check |
|---|---|
| `translator-engine/` | `cargo test --release --lib` |
| `translator-engine/` license files (`Cargo.lock`, `deny.toml`, `license_gate.rs`, `engine/`) | `license_gate` unit tests |
| `Wonslate.UI/`, `Wonslate.UI.Tests/` | `dotnet test` (Release) |
| `tests/`, `sidecar/` (`*.py`) | `python -m py_compile` |

Bypass in an emergency (do not make it a habit):

```bash
git commit --no-verify
```

For the full suite, use the one-click entrypoint:

```bash
python script/build.py --test
```

## Known gaps

A green pipeline is not "everything is tested". What these gates deliberately do **not**
cover, so the boundary is explicit:

| Gap | Why it is open | Current mitigation |
|---|---|---|
| GUI end-to-end (XAML bindings, real window) | No UI-driver stack chosen yet (WinAppDriver / Appium) | `MainViewModel` is covered headless by xUnit; a human smoke pass against the `demo` glossary table in the README exercises the real window |
| Real model inference (CT2 / Argos / MADLAD checkpoints) | Shipping and downloading model weights is out of scope for CI | the HTTP contract and the absence-fallback path are covered end-to-end against the mock backend (`tests/test_sidecar_e2e.py`) |
| Engine absence behaviour on the .NET side | Needs a live sidecar | Rust-side behaviour is asserted in `translator-engine/tests/pipeline_e2e.rs`; the .NET sidecar manager is tested with injected fake delegates (no real process) |
| Coverage on a developer machine | the local coverage toolchain is not available on every host | the `coverage` job produces the number on ubuntu and is intentionally non-blocking (baseline, not a gate) |
| Model / component licenses | `cargo-deny` only sees crates.io entries | the second layer, `license_gate` unit tests, enforces the model/component allow-list |
| macOS / Linux client UI | Only the WPF client exists today | the `rust` matrix builds and tests the engine on all three OSes and publishes each native library |
