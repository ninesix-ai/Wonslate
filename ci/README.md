# CI & local quality gates

Wonslate keeps itself healthy through two complementary layers.

## Server-side CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and pull
request, and can also be triggered by hand from the Actions tab (`workflow_dispatch`):

- Rust engine build + tests (`cargo test --release`) on **three OSes**
  (`ubuntu-latest`, `windows-latest`, `macos-latest`); each job publishes its native
  library (`dll` / `so` / `dylib`) as a `translator-engine-<os>` build artifact
- Python smoke: FFI contract (`tests/test_phase1_ffi.py`), sidecar end-to-end
  (`tests/test_sidecar_e2e.py`), sidecar unit tests (`tests/test_ct2_sidecar.py`), the
  XAML lint rules (`tests/test_xaml_lint.py`) and the hook guards
  (`tests/test_pre_commit_hook.py`)
- .NET WPF build + xUnit (Windows runner); `.trx` results are published as a check run
  with per-test annotations, so a failing case is readable in the PR itself. Pull
  requests from forks run with a read-only token and cannot create check runs, so there
  the same report falls back to the workflow job summary - it stays visible without ever
  turning an outside contributor's build red. `dotnet test` decides pass/fail either way.
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

Every check above is piped into `tail` to keep commit output short, so the hook needs
`set -o pipefail`: without it a pipeline reports `tail`'s status (always `0`) and no
test failure can ever refuse a commit. That was not theoretical - until 2026-09-27 the
hook printed the failing-test summary and let the commit through anyway.
`tests/test_pre_commit_hook.py` guards the flag, the paths and the LF endings so the
gate cannot rot back into a printout.

For the full suite, use the one-click entrypoint:

```bash
python script/build.py --test
```

## Local verification gate

`script/verify/build_check.py` (entrypoints `verify.bat` / `verify.sh`) covers two things
the automated suite cannot:

1. **XAML static lint** - two patterns that compile fine in BAML and only blow up when
   WPF loads the window: `DynamicResource` nested inside a `Binding` (R1), and
   `<Run Text="{Binding ...}">` without an explicit `Mode=OneWay` (R2 - `Run.Text`
   defaults to TwoWay and throws in `InitializeComponent` when the source is read-only).
2. **Launch smoke** - starts the built `Wonslate.exe`, waits for a *visible top-level
   window owned by that process*, requires it to stay alive, then kills the process tree.
   If an instance is already running the stage SKIPs: the app has no single-instance
   mutex, so a second launch would prove nothing.

Machine policy is never treated as a verdict: when code integrity refuses to start a
freshly built binary, the smoke stage reports `SKIP` (and prints the host's application
control state) instead of `FAIL`, so a locked-down developer machine neither blocks CI
semantics nor hides a real regression. This stage needs an interactive session and is
deliberately **not** part of the workflow.

`script/misc/sign_wonslate.py` (`sign.bat`, Windows) is the companion: it creates or
reuses a self-signed code-signing certificate, trusts it for the current user
(machine-wide when elevated), and signs `Wonslate.exe`, `Wonslate.dll` and
`translator_engine.dll` - our own artifacts only, never third-party native libraries.
Self-signed trust is host-local; distributed builds need a real certificate.

## Known gaps

A green pipeline is not "everything is tested". What these gates deliberately do **not**
cover, so the boundary is explicit:

| Gap | Why it is open | Current mitigation |
|---|---|---|
| GUI end-to-end (bindings resolve, clicks do the right thing) | No UI-driver stack chosen yet (WinAppDriver / Appium) | `MainViewModel` is covered headless by xUnit; `verify`/`build_check.py` proves a real window appears and survives (and its lint catches "builds but never shows a window"), but neither asserts on live control values |
| Real model inference (CT2 / Argos / MADLAD checkpoints) | Shipping and downloading model weights is out of scope for CI | the HTTP contract and the absence-fallback path are covered end-to-end against the mock backend (`tests/test_sidecar_e2e.py`) |
| Engine absence behaviour on the .NET side | Needs a live sidecar | Rust-side behaviour is asserted in `translator-engine/tests/pipeline_e2e.rs`; the .NET sidecar manager is tested with injected fake delegates (no real process) |
| Coverage on a developer machine | the local coverage toolchain is not available on every host | the `coverage` job produces the number on ubuntu and is intentionally non-blocking (baseline, not a gate) |
| Model / component licenses | `cargo-deny` only sees crates.io entries | the second layer, `license_gate` unit tests, enforces the model/component allow-list |
| macOS / Linux client UI | Only the WPF client exists today | the `rust` matrix builds and tests the engine on all three OSes and publishes each native library |
