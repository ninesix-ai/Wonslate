# CI & local quality gates

Wonslate keeps itself healthy through two complementary layers.

## Server-side CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and pull
request:

- Rust engine build + tests (`cargo test --release`)
- Python FFI smoke (`tests/test_phase1_ffi.py`)
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
| `LocalTranslator/`, `LocalTranslator.Tests/` | `dotnet test` (Release) |
| `tests/`, `sidecar/` (`*.py`) | `python -m py_compile` |

Bypass in an emergency (do not make it a habit):

```bash
git commit --no-verify
```

For the full suite, use the one-click entrypoint:

```bash
python script/build.py --test
```
