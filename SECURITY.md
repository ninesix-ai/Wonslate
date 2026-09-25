# Security Policy

Thank you for helping keep Wonslate and its users safe. Wonslate is a **privacy-first, offline** translation tool — we take security reports seriously and will work with you to investigate and fix issues.

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Use one of the private channels below:

- Email: `ninesix-ai@users.noreply.github.com` (include as much detail as you can share privately)
- GitHub **Private vulnerability reporting**: on this repository, choose *Report a vulnerability* under the **Security** tab. This opens a private, tracked advisory between you and the maintainers.

A good report includes:

- A description of the issue and its potential impact
- Step-by-step instructions to reproduce it (a minimal PoC is ideal)
- The affected version(s) and platform (see [README](README.md))
- Any relevant logs, patches, or links

## What we consider in scope

- The Rust translation engine core and its **C ABI / FFI boundary** with the .NET client
- The local HTTP **sidecar** engines (argos / madlad) and any localhost-only network surface
- Translation memory (TM) storage, config handling, and the **license gate**
- Privilege, path handling, and any behavior that could violate the **"data never leaves the device"** privacy invariant

## Out of scope

- Vulnerabilities in upstream dependencies or models that are already addressed by their own maintainers (we still welcome a heads-up so we can bump versions)
- Issues in builds you compile yourself with modified source
- Social engineering, physical access, or attacks requiring you to already control the user's machine

## Response process

1. We acknowledge valid reports within **2 business days**.
2. We confirm the impact, identify affected versions, and prepare a fix.
3. We coordinate disclosure with you and aim to ship a patched release before going public. We ask for a **90-day** private window, and will shorten it when you need.
4. Once fixed, we may publish a GitHub **security advisory** and credit you (unless you prefer to stay anonymous).

## Notes

- There is currently **no paid bug-bounty program**; reports are acknowledged in release notes / advisories.
- Wonslate is early-stage (**v0.0.1**); the attack surface may still change. Thank you for your patience as we harden it.
