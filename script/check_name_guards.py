#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""check_name_guards.py -- keeps the D68/D69 pattern from coming back.

Both defects had the same shape: a decision was made about a *name*, while the thing the
name stands for was chosen somewhere the decision never looked.

  D68  `is_local_engine("argos")` said local, while argos' address came from
       ARGOS_URL -- so a privacy request dialed another host.
  D69  `REGISTERED_DEPS` records "translate-*.argosmodel is MIT", while the bytes that
       name stands for were fetched by a bare urlopen from a host read out of an index.

Those two are fixed. This gate is what stops the third one: it does not try to detect the
pattern in general (that is a reviewer's judgement), it enforces the four properties that
would have had to hold and did not, each of them mechanically checkable.

  G1  every engine id that the privacy whitelist calls local, and that has an address of
      its own somewhere in the tree, must answer for that address (`reaches_only_loopback`
      / `endpoint` overridden on the implementing type);
  G2  every script under script/fetch_*.py routes its downloads through net_guard.fetch
      and never calls urlopen itself;
  G3  every pinned digest is a real 64-hex sha256, with a non-empty provenance string --
      an empty placeholder reads as "checked" in a diff and means the opposite;
  G4  every pinned filename is one the script can actually download, and every asset the
      script can download is either pinned or named in its unpinned exception list.
      A pin for a file nobody fetches is decoration, and decoration is what D69 looked like.

Exit code: 0 = all four hold; 1 = at least one violation (named, with file and reason).
`--self-test` seeds one violation of every class and requires all four to fire.
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

FETCH_GLOB = "script/fetch_*.py"
ENGINE_DIR = ROOT / "translator-engine" / "src" / "engine"


class Finding(str):
    """A violation. Kept as a str so the report reads naturally."""


def parse_local_whitelist():
    """The ids `is_local_engine` treats as local, or None if the shape moved."""
    path = ENGINE_DIR / "mod.rs"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"fn is_local_engine\([^)]*\)[^{]*\{.*?matches!\s*\(\s*\w+\s*,\s*([^)]*)\)",
                      text, re.S)
    if not match:
        return None
    return re.findall(r"\"([^\"]+)\"", match.group(1))


def collect_engine_facts():
    """Per engine file: which ids answer for their own address, and which have an address.

    Facts are gathered once and reused by G1. "Answers for an address" means the file
    overrides reaches_only_loopback() *and* routes it through url_host_is_loopback(), so a
    stub returning a constant does not count -- that is the difference between a guard and
    a placeholder, and D68 is what the placeholder version cost.
    """
    overrides, bearers = set(), set()
    for path in sorted(set(ENGINE_DIR.rglob("*.rs"))):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "fn reaches_only_loopback" in text and "url_host_is_loopback" in text:
            overrides.update(re.findall(r"from_env\(\"(\w+)\"", text))
            overrides.update(re.findall(r"with_url\(\"(\w+)\"", text))
        if re.search(r"(?:_URL|_CANDIDATES|first_env)\b", text):
            for ident in re.findall(r"([A-Za-z][A-Za-z]+)_CANDIDATES", text):
                bearers.add(ident.lower())
            for ident in re.findall(r"\"(?:LT|WONSLATE)_([A-Z]+)_URL\"", text):
                bearers.add(ident.lower())
    return overrides, bearers


def check_privacy_whitelist(findings):
    """G1."""
    ids = parse_local_whitelist()
    if ids is None:
        findings.append(Finding("G1  is_local_engine's whitelist shape is unrecognised -- "
                                "the gate cannot read it, which is not the same as it passing"))
        return
    overrides, bearers = collect_engine_facts()
    for ident in ids:
        if ident == "demo":
            continue                      # in-process dictionary: it has no address
        if ident in bearers and ident not in overrides:
            findings.append(Finding(
                "G1  engine id %-12s is called local by is_local_engine and has an "
                "env-overridable address, but its type never overrides "
                "reaches_only_loopback() -- exactly D68" % ident))


def declared_assets(text, name):
    """Read a tuple of strings declared as a module constant; None when absent."""
    match = re.search(r"%s\s*=\s*\(([^)]*)\)" % name, text, re.S)
    if not match:
        return None
    return set(re.findall(r"[\"']([\w.\-]+)[\"']", match.group(1)))


def check_downloaders(findings):
    """G2, G3, G4 over every script/fetch_*.py."""
    for path in sorted((ROOT / "script").glob("fetch_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")

        if "net_guard.fetch(" not in text:
            findings.append(Finding("G2  %s does not call net_guard.fetch()" % rel))
        for line_no, line in enumerate(text.split("\n"), 1):
            if "urlopen(" in line and not line.lstrip().startswith("#"):
                findings.append(Finding("G2  %s:%d dials the network directly: %s"
                                        % (rel, line_no, line.strip()[:70])))

        pins = parse_pins(text)
        if pins is None:
            findings.append(Finding("G3  %s has no EXPECTED_SHA256 table" % rel))
            continue
        for leaf, value in pins.items():
            if not isinstance(value, tuple) or len(value) != 2:
                findings.append(Finding("G3  %s: pin %s is not a (digest, source) pair"
                                        % (rel, leaf)))
                continue
            digest, source = value
            if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
                findings.append(Finding("G3  %s: pin %s digest is %r, not 64 hex chars"
                                         % (rel, leaf, (digest or "")[:12])))
            if not source or not re.search(r"20\d\d-\d\d-\d\d", source):
                findings.append(Finding("G3  %s: pin %s carries no dated provenance -- a hash "
                                        "without a source is a claim" % (rel, leaf)))

        # G4 is a set equation over what the script *declares*, not a guess at it. An
        # earlier version inferred "downloadable" from filenames appearing in the source,
        # and immediately produced three false alarms: `voices.bin` and
        # `sentencepiece.model` are members of an unpacked archive, never fetch targets.
        # Inferring that boundary from text is not possible, so the script states it.
        declared = declared_assets(text, "DOWNLOADS")
        unpinned = declared_assets(text, "UNPINNED_EXCEPTIONS")
        if declared is None:
            findings.append(Finding("G4  %s declares no DOWNLOADS tuple: the gate cannot tell "
                                    "a decorative pin from a used one" % rel))
        elif unpinned is None:
            findings.append(Finding("G4  %s declares no UNPINNED_EXCEPTIONS (an empty tuple is "
                                    "the answer when everything is pinned)" % rel))
        else:
            covered = set(pins) | unpinned
            for leaf in sorted(declared - covered):
                findings.append(Finding("G4  %s can download %s but it is neither pinned nor "
                                        "declared unpinned" % (rel, leaf)))
            for leaf in sorted(covered - declared):
                findings.append(Finding("G4  %s pins/declares %s but does not list it in "
                                        "DOWNLOADS -- a pin for something never fetched is "
                                        "decoration" % (rel, leaf)))
            overlap = set(pins) & unpinned
            if overlap:
                findings.append(Finding("G4  %s lists %s as both pinned and unpinned" % (
                    rel, ", ".join(sorted(overlap)))))


def parse_pins(text):
    """The EXPECTED_SHA256 literal, as {name: (digest, source)} or None."""
    match = re.search(r"EXPECTED_SHA256\s*=\s*\{(.*?)\n\}", text, re.S)
    if not match:
        return None
    body = match.group(1)
    pins = {}
    # Entries look like:  "name": ("digest", "source"),  -- with either part wrapped onto
    # the next line, so match pairs rather than whole entries.
    for name, digest, source in re.findall(
            r"\"([\w.\-]+)\":\s*\(\s*\n?\s*\"([0-9a-fA-F]*)\"\s*,\s*\n?\s*\"(.*?)\"\s*\)", body):
        pins[name] = (digest, source)
    return pins


def check_net_guard_used_by_name(findings):
    """The shared policy must exist and be importable by the three downloaders (D65's lesson:
    a validator that nothing runs is worse than no validator, because it reads as coverage)."""
    guard = ROOT / "script" / "net_guard.py"
    if not guard.exists():
        findings.append(Finding("G2  script/net_guard.py is missing"))
        return
    text = guard.read_text(encoding="utf-8", errors="replace")
    for symbol in ("def check_url", "def require_public_address", "def fetch"):
        if symbol not in text:
            findings.append(Finding("G2  script/net_guard.py lost %s()" % symbol[4:]))


def check(argv):
    findings = []
    check_privacy_whitelist(findings)
    check_net_guard_used_by_name(findings)
    check_downloaders(findings)
    for line in findings:
        print("!! %s" % line)
    print("%d violation(s). name-based guards: privacy whitelist + 3 downloaders audited."
          % len(findings))
    return 1 if findings else 0


# ---- self test: seed one violation per class, and require every one to fire  ----

def _seed_tree(tmp):
    """A miniature repo whose four properties are each broken in exactly one way."""
    engine = tmp / "translator-engine" / "src" / "engine"
    engine.mkdir(parents=True)
    (engine / "mod.rs").write_text(
        'pub fn is_local_engine(id: &str) -> bool {\n'
        '    matches!(id, "demo" | "phantom" | "argos")\n}\n', encoding="utf-8", newline="\n")
    # argos is fine (overrides and answers for its address); phantom is D68 reborn.
    (engine / "phantom.rs").write_text(
        'pub const PHANTOM_CANDIDATES: &[&str] = &["LT_PHANTOM_URL"];\n'
        'impl Translator for PhantomTranslator {\n'
        '    fn from_env("phantom", PHANTOM_CANDIDATES) -> Self { self.url }\n}\n',
        encoding="utf-8", newline="\n")
    (engine / "sidecar.rs").write_text(
        'pub const ARGOS_CANDIDATES: &[&str] = &["LT_ARGOS_URL"];\n'
        'impl Translator for SidecarTranslator {\n'
        '    fn reaches_only_loopback(&self) -> bool {\n'
        '        crate::engine::url_host_is_loopback(&self.url)\n    }\n}\n'
        'fn argos() { Self::from_env("argos", ARGOS_CANDIDATES, DEFAULT_ARGOS_URL) }\n',
        encoding="utf-8", newline="\n")
    script = tmp / "script"
    script.mkdir()
    (script / "net_guard.py").write_text(
        "def check_url():\n    pass\ndef require_public_address():\n    pass\n"
        "def fetch():\n    pass\n", encoding="utf-8", newline="\n")
    # A downloader that dials the network itself, with one bad pin and one pin for a file
    # it never fetches.
    (script / "fetch_demo_model.py").write_text(
        'FILE_NAMES = ("wanted.bin",)\n'
        'DOWNLOADS = ("wanted.bin", "ghost.bin")\n'
        'UNPINNED_EXCEPTIONS = ()\n'
        'EXPECTED_SHA256 = {\n'
        '    "wanted.bin": ("deadbeef", ""),\n'
        '    "never_fetched.bin": ("0000000000000000000000000000000000000000000000000000000000000000",\n'
        '                         "source 2026-01-01"),\n'
        '}\n'
        'def go(url, dest):\n'
        # Seed text only: it has to contain a real urlopen call for G2 to have something
        # to catch. The handle is spelled out rather than shortened -- with a one-letter
        # variable, the letter sits alone before the colon that opens an escape sequence,
        # and tests/test_source_lint.py correctly reads that pair as a Windows drive
        # letter. It is the right rule to have; the first version of this comment walked
        # straight into it by showing the offending shape instead of describing it.
        '    with urllib.request.urlopen(url) as r, open(dest, "wb") as fh:\n'
        '        fh.write(r.read())\n',
        encoding="utf-8", newline="\n")


def self_test():
    import tempfile
    global ROOT, ENGINE_DIR
    keep = (ROOT, ENGINE_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        _seed_tree(tmp)
        ROOT, ENGINE_DIR = tmp, tmp / "translator-engine" / "src" / "engine"
        findings = []
        check_privacy_whitelist(findings)
        check_net_guard_used_by_name(findings)
        check_downloaders(findings)
    ROOT, ENGINE_DIR = keep

    text = "\n".join(findings)
    want = {
        "G1 the phantom id fires": "G1" in text and "phantom" in text,
        "G1 does not cry wolf on argos": "argos" not in text,
        "G2 direct urlopen fires": "G2" in text and "urlopen" in text,
        "G2 missing policy call fires": "does not call net_guard.fetch" in text,
        "G3 short digest fires": "not 64 hex" in text,
        "G3 missing provenance fires": "no dated provenance" in text,
        "G4 a declared download with no pin fires": "neither pinned nor declared unpinned" in text,
        "G4 a pin nobody downloads fires": "is decoration" in text,
    }
    fired = 0
    for label, ok in want.items():
        print("  %-38s %s" % (label, "fired" if ok else "!! DID NOT FIRE"))
        fired += 1 if ok else 0
    print("%d/%d seeded violations detected (net %d findings)"
          % (fired, len(want), len(findings)))
    return 0 if fired == len(want) else 1


def main(argv):
    parser = argparse.ArgumentParser(description="name-based-guard gate (D68/D69)")
    parser.add_argument("--self-test", action="store_true", help="seed one violation per class")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    return check(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
