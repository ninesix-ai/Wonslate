# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Cross-language license gate for non-crate dependencies.

Why this exists: cargo-deny only sees the crates.io tree, and license_gate.rs
only audits what is explicitly registered. The real supply-chain surface for a
closed-source-commercial-friendly Apache-2.0 product also includes NuGet
packages, Python libraries and model weights. This lint closes that gap with
two fail-closed checks:

1. Every PackageReference name in every *.csproj must be registered in
   REGISTERED_DEPS inside translator-engine/src/license_gate.rs (or listed in
   TRANSITIVE_OK, which is empty on purpose), and its registered license must
   pass the same allow-list the Rust gate uses (parsed from ALLOWED_LICENSES).
2. REGISTERED_DEPS entries must carry a provenance note (a comment naming the
   official source on the line above), so no dependency can be whitelisted by
   name alone without saying where it comes from.
3. deny.toml's [licenses] allow list (the crate-layer gate run by cargo-deny in
   CI) must be identical to ALLOWED_LICENSES. The two layers previously drifted
   (deny.toml allowed Unicode-DFS-2016, the Rust list did not), and a whitelist
   that disagrees with the gate it mirrors is not a gate.
4. The PyPI layer, added 2026-10-06 as defect D28: every third-party module
   imported by the shipped scripts under script/, sidecar/ and tests/ must be
   registered in REGISTERED_DEPS, and so must every line of
   sidecar/requirements.txt. Until then this file read only .csproj, so
   CONTRIBUTING's promise that Python libraries fail closed was not true, and a
   benchmark dependency reached CI without any check touching it.
   The check deliberately never reads installed package metadata: a build agent
   may not have the package installed, and the free-form `License:` field is not
   machine-readable (huggingface_hub ships a bare "Apache"). What is audited is
   the registered name, license and provenance note, which is the same contract
   the NuGet layer uses.

The Rust unit test `license_gate::registered_deps_are_all_compliant` covers
the license-value side (every registered license must be in the allow-list);
this file covers the registration side (every shipped package must be
registered). Keep both in sync: run in CI (python-ffi job) and via
script/build.py --ffi.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_RS = os.path.join(ROOT, "translator-engine", "src", "license_gate.rs")
DENY_TOML = os.path.join(ROOT, "translator-engine", "deny.toml")

# Names allowed to skip explicit registration because they are pulled in
# transitively by a registered parent (transitive licenses are covered by the
# parent's audit). Kept empty by default: add an entry only with a reason.
TRANSITIVE_OK = set()

PKG_RE = re.compile(r'PackageReference\s+Include="([^"]+)"')
DEP_RE = re.compile(r'Dep\s*\{\s*name:\s*"([^"]+)",\s*license:\s*"([^"]+)"')
QUOTED_RE = re.compile(r'"([^"]+)"')
# A provenance note is any // comment line directly above a Dep entry that
# contains a URL or an explicit publisher name (heuristic, err on strict).
PROV_RE = re.compile(r"(https?://|publisher|upstream|official)", re.I)

# Shipped Python code lives in these directories; anything they import from
# outside the standard library has to clear the same registration bar as NuGet.
PY_DIRS = ("script", "sidecar", "tests")
REQUIREMENTS_TXT = os.path.join(ROOT, "sidecar", "requirements.txt")
IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.M)
REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[=<>~]")

# Import name -> PyPI distribution name, only where the two differ.
DIST_OF_MODULE = {"comet": "unbabel-comet"}


def _segment(text, marker):
    """Return the source slice from `marker` up to its terminating `];`."""
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"{marker} not found in license_gate.rs")
    end = text.find("];", start)
    if end < 0:
        raise AssertionError(f"unterminated list for {marker} in license_gate.rs")
    return text[start:end]


def _strip_line_comments(text):
    """Drop `//` comment prose so quoted strings inside comments are not read as data.

    Without this, a comment that names a license (e.g. documenting an upstream
    "AND Unicode-DFS-2016" expression) would be parsed as a list entry. The split
    is on the first `//` of a line, which is safe for these lists because none of
    their string literals contain `//`.
    """
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def parse_allowed(text):
    seg = _strip_line_comments(_segment(text, "ALLOWED_LICENSES"))
    return set(QUOTED_RE.findall(seg))


def parse_deny_allow(text):
    """Return the licence set from deny.toml's [licenses] allow list."""
    m = re.search(r"allow\s*=\s*\[(.*?)\]", text, re.S)
    return set(QUOTED_RE.findall(m.group(1))) if m else set()


def parse_registered(text):
    body = _segment(text, "REGISTERED_DEPS")
    lines = body.splitlines()
    deps = []
    for i, line in enumerate(lines):
        m = DEP_RE.search(line)
        if not m:
            continue
        # Look back up to 3 comment lines for the provenance note.
        prov = any(PROV_RE.search(lines[j]) for j in range(max(0, i - 3), i)
                   if lines[j].strip().startswith("//"))
        deps.append({"name": m.group(1), "license": m.group(2), "provenance": prov})
    return deps


def csproj_packages():
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ("bin", "obj", "target", "__pycache__", ".git")]
        for f in filenames:
            if f.endswith(".csproj"):
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for name in PKG_RE.findall(text):
                    found.append((os.path.relpath(path, ROOT), name))
    return found


def local_module_names():
    """Module names defined by this repository, so they are never third-party.

    Walked recursively: a helper imported from a nested directory (tests import
    script/verify/build_check.py, for one) is still first-party, and reporting it
    as an unregistered distribution would hand the next person a false violation
    to switch this gate off for.
    """
    names = set()
    for d in ("",) + PY_DIRS:
        base = os.path.join(ROOT, d) if d else ROOT
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames
                           if x not in ("__pycache__", ".git", "bin", "obj",
                                        "target", "node_modules")]
            for entry in filenames:
                if entry.endswith(".py") and entry != "__init__.py":
                    names.add(entry[:-3])
            for entry in dirnames:
                if os.path.exists(os.path.join(dirpath, entry, "__init__.py")):
                    names.add(entry)
    return names


def python_third_party_imports():
    """Map each shipped Python file to the third-party modules it imports."""
    known_local = local_module_names()
    std = set(getattr(sys, "stdlib_module_names", set()))
    found = {}
    for d in PY_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for f in filenames:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                mods = []
                for raw in IMPORT_RE.findall(text):
                    top = raw.split(".")[0]
                    if top in known_local or top in std:
                        continue
                    mods.append(top)
                if mods:
                    found[os.path.relpath(path, ROOT)] = sorted(set(mods))
    return found


def declared_requirements():
    """Distribution names listed in sidecar/requirements.txt."""
    if not os.path.exists(REQUIREMENTS_TXT):
        return []
    names = []
    with open(REQUIREMENTS_TXT, encoding="utf-8") as fh:
        for line in fh:
            m = REQ_RE.match(line)
            if m:
                names.append(m.group(1))
    return names


def run_checks():
    failures = []
    with open(GATE_RS, encoding="utf-8") as fh:
        gate_text = fh.read()
    allowed = parse_allowed(gate_text)
    registered = {d["name"]: d for d in parse_registered(gate_text)}

    # Check 1+2: every shipped NuGet package is registered with an allowed
    # license and a provenance note.
    for csproj, pkg in csproj_packages():
        if pkg in TRANSITIVE_OK:
            continue
        entry = registered.get(pkg)
        if entry is None:
            failures.append(
                f"{csproj}: package '{pkg}' is not registered in "
                f"license_gate.rs REGISTERED_DEPS (fail-closed)")
            continue
        if entry["license"] not in allowed:
            failures.append(
                f"package '{pkg}' has registered license '{entry['license']}' "
                f"outside ALLOWED_LICENSES")
        if not entry["provenance"]:
            failures.append(
                f"package '{pkg}' is registered without a provenance note "
                f"(comment above the Dep must name the official source)")

    # Check 3: no registered license string silently disappears — every
    # registered license must be a string the Rust allow-list understands.
    for name, entry in registered.items():
        if entry["license"] not in allowed:
            failures.append(f"registered dep '{name}' license "
                            f"'{entry['license']}' not in allow-list")

    # Check 5: the PyPI layer. Every third-party import in the shipped scripts,
    # and every declared requirement, must be registered with an allowed license
    # and a provenance note. Unknown imports fail closed rather than pass quietly.
    py_imports = python_third_party_imports()
    checked_dists = set()
    for rel, mods in sorted(py_imports.items()):
        for mod in mods:
            dist = DIST_OF_MODULE.get(mod, mod)
            checked_dists.add(dist)
            entry = registered.get(dist)
            if entry is None:
                failures.append(
                    f"{rel}: import '{mod}' (distribution '{dist}') is not "
                    f"registered in license_gate.rs REGISTERED_DEPS (fail-closed)")
                continue
            if entry["license"] not in allowed:
                failures.append(
                    f"python distribution '{dist}' has registered license "
                    f"'{entry['license']}' outside ALLOWED_LICENSES")
            if not entry["provenance"]:
                failures.append(
                    f"python distribution '{dist}' is registered without a "
                    f"provenance note (comment above the Dep must name the "
                    f"official source)")
    for dist in declared_requirements():
        checked_dists.add(dist)
        if dist not in registered:
            failures.append(
                f"sidecar/requirements.txt: '{dist}' is not registered in "
                f"license_gate.rs REGISTERED_DEPS (fail-closed)")

    # Check 6: the crate-layer gate must mirror the source-of-truth allow list.
    with open(DENY_TOML, encoding="utf-8") as fh:
        deny_allow = parse_deny_allow(fh.read())
    if deny_allow != allowed:
        missing = sorted(allowed - deny_allow) or "none"
        extra = sorted(deny_allow - allowed) or "none"
        failures.append("deny.toml allow list diverges from ALLOWED_LICENSES "
                        f"(missing in deny.toml: {missing}; "
                        f"only in deny.toml: {extra})")
    return failures


def main():
    failures = run_checks()
    if failures:
        print("component license gate: FAIL")
        for f in failures:
            print("  - " + f)
        return 1
    dists = {DIST_OF_MODULE.get(m, m) for mods in python_third_party_imports().values()
             for m in mods} | set(declared_requirements())
    print("component license gate: PASS "
          f"({len(csproj_packages())} NuGet reference(s), "
          f"{len(dists)} Python distribution(s)) audited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
