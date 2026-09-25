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
- **Code comments in English.**
- **Conventional Commits** for messages (`feat:` / `fix:` / `chore:` / `docs:` / `refactor:` …).
- **Line endings**: LF is stored in the repo (`.gitattributes`); `.bat`/`.cmd` stay CRLF. Editor defaults are in `.editorconfig`.
- **UTF-8 across the FFI boundary**: Rust returns UTF-8 `char*`; .NET must marshal with `LibraryImport`/`StringMarshalling.Utf8` and `PtrToStringUTF8`.
- **Rust core stays lean** (zero proc-macro, minimal deps); new engines must be registered in `engine/mod.rs`.

## Invariants to preserve
- **Privacy**: data must never leave the device; routing must not silently escalate to the cloud (see the confidence-gated pipeline).
- **Licensing**: keep the Apache-2.0 / MIT stack; CC-BY-NC models are prototype-only, never for commercial use.

## Pull requests
Fill in the PR template, ensure `build.py` compiles and relevant tests pass, and open a PR against `main`.

## License
By contributing, you agree that your contributions are licensed under the project's [Apache-2.0](LICENSE) license.
