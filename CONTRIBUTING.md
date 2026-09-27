# Contributing to Wonslate（万邦译）

Thanks for your interest in improving Wonslate! Wonslate is a **privacy-first, offline, commercially usable** multi-engine translation tool (.NET 10 WPF client + a Rust engine core over FFI).

## Reporting issues
Use the issue templates. Include OS, version, engine, and (for bugs) reproduction steps. Never paste real secrets or sensitive text you don't own.

## Building locally
```
# Windows
build.bat                 # forwards to script/build.py

# Linux / macOS
./build.sh

# options
build.py --test           # build + Rust / .NET / Python-FFI tests
build.py --run            # build, then launch the app (Windows)
```
Prereqs: Rust 1.98+, .NET SDK 10.0+. The native engine (`translator_engine.dll`/.so/.dylib) is built by `cargo build --release` and is **not committed** (see `.gitignore`).

## Conventions
- **Code comments in English.** CJK test *data* (e.g. `"你好"`) is fine; comment *prose* is not. Enforced by `python tests/test_source_lint.py` (pre-commit + CI).
- **Conventional Commits** for messages (`feat:` / `fix:` / `chore:` / `docs:` / `refactor:` …).
- **Line endings**: LF is stored in the repo (`.gitattributes`); `.bat`/`.cmd` stay CRLF. Editor defaults are in `.editorconfig`.
- **UTF-8 across the FFI boundary**: Rust returns UTF-8 `char*`; .NET must marshal with `LibraryImport`/`StringMarshalling.Utf8` and `PtrToStringUTF8`.
- **Rust core stays lean** (zero proc-macro, minimal deps); new engines must be registered in `engine/mod.rs`.
- **Environment variables** use the `WONSLATE_*` spelling. Legacy `LT_*` names keep working and win over `WONSLATE_*` (prefix chain `LT_*` > `WONSLATE_*` > bare, see `config.rs`); new docs/examples should use the brand spelling.

## Invariants to preserve
- **Privacy**: data must never leave the device; routing must not silently escalate to the cloud (see the confidence-gated pipeline). The guarantee is enforced twice: the router's privacy branch *and* a fail-closed clamp in `pipeline.rs` -- under `privacy=true` any engine id not known-local (including an explicit `engine_id` bypass) is clamped to the local `demo` engine and the clamp is surfaced in `message`. Add a cloud engine later? It must stay out of `engine::is_local_engine` and the clamp keeps you honest (regression test: `privacy_mode_clamps_non_local_explicit_engine_to_local`).
- **Licensing**: keep the Apache-2.0 / MIT stack; CC-BY-NC models are prototype-only, never for commercial use. Every non-crate dependency (NuGet package, Python library, model) must be registered in `license_gate.rs` `REGISTERED_DEPS` with a provenance note -- `python tests/test_component_licenses.py` fails closed otherwise.

## Local signing (development only)
`python script/misc/sign_wonslate.py` creates a self-signed code-signing certificate in the *current user's* store and trusts its public part in the user Trusted Publisher + Root stores; when run from an **elevated** shell it also imports it into the **machine-wide** stores, which widens trust for every user on that box. That trade-off is intentional for reproducible local smoke tests and the private key never leaves the certificate store. Never rely on this for distributed builds -- those require a real code-signing certificate (free options for open source exist, e.g. Sigstore).

## Pull requests
Fill in the PR template, ensure `build.py` compiles and relevant tests pass, and open a PR against `main`.

## License
By contributing, you agree that your contributions are licensed under the project's [Apache-2.0](LICENSE) license.
