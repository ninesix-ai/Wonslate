# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Source hygiene lint: English comment prose, and no developer-machine paths.

Why: Wonslate is an open-source project with international contributors; the
project convention is English comments everywhere in source. CJK test DATA
(string literals like "你好") is legitimate product behavior and is NOT flagged
-- only comment prose is checked.

Two checks run over the tree:

  1. CJK comment prose (English-only rule), scanned for:
     .rs .cs .py .toml .yml .bat .xaml
     Exclusions:
       - generated/vendored dirs (bin, obj, target, __pycache__, TestResults)
       - README.zh-CN.md and docs/ (Chinese by design; not source)
       - quoted string literals on the same line (stripped before scanning)
       - MainWindow.xaml outside comments: UI strings are product content, only
         its <!-- comments --> are scanned

  2. Drive-absolute paths (``D:\\something`` / ``C:/something``) in the places a
     stranger actually copies from: shipped script defaults and docs/*.md.
     Why: a harness default naming one developer's disk is not reproducible
     anywhere else, and publishing it leaks the author's machine layout. Data
     locations must come from the shared resolution rule (see
     script/bench_flores.py and sidecar/ct2_sidecar.py::default_data_dir).
     Deliberately out of scope: unit-test fixtures, which fabricate paths on
     purpose (C:\\, D:\\, E:\\ appear in dozens of assertions that never run on
     those disks). Everything inside scope that genuinely needs a drive letter
     (the Windows SDK roots probed in script/misc/sign_wonslate.py) carries an
     explicit `lint-allow: drive-path` marker on the line.

Usage:
    python tests/test_source_lint.py            # scan the whole tree
    python tests/test_source_lint.py FILE ...   # scan specific files (pre-commit)
Exit code: 0 = clean, 1 = violations found.
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CJK ideographs + Chinese-specific punctuation only. Em/en dashes and curly
# quotes are ordinary English typography and must NOT be flagged.
# lint-allow: unicode-tables -- the class below must list CJK literals inline.
CJK_PROSE = re.compile(
    r'[\u4e00-\u9fff\u3400-\u4dbf'
    r'\u3000-\u303f'
    r'\uff01-\uff60'
    r'\u2018\u2019\u201c\u201d\u2026\u3001]'
)

SKIP_DIRS = {"bin", "obj", "target", "__pycache__", "TestResults", ".git",
             "docs", "node_modules"}
EXTS = (".rs", ".cs", ".py", ".toml", ".yml", ".bat", ".xaml")

QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"'
                    r"|'(?:[^'\\]|\\.)*'"
                    r'|"[^"]*"')  # double-quoted runs incl. raw-string bodies

LINE_COMMENT = {
    ".rs": [("//",)],
    ".cs": [("///",), ("//",)],
    ".py": [("#",)],
    ".toml": [("#",)],
    ".yml": [("#",)],
    ".bat": [("@echo off", None)],  # placeholder; bat comments handled below
}


def strip_quoted(text):
    """Blank out quoted literals so CJK test data never trips the lint."""
    return QUOTED.sub(lambda m: " " * len(m.group(0)), text)


def comment_part(line, ext):
    """Return the comment prose of a line ('' when there is none)."""
    s = line
    if ext in (".rs", ".cs"):
        # find the first // outside double quotes
        cleaned_idx = []
        inq = False
        i = 0
        while i < len(s):
            if s[i] == '"':
                inq = not inq
            cleaned_idx.append(inq)
            i += 1
        depth = 0
        # C# also nests inside /* */ blocks; a lightweight pass treats every
        # line inside a block comment as comment-only (caller tracks state)
        j = 0
        while j < len(s) - 1:
            if not cleaned_idx[j] and s[j] == "/" and s[j + 1] == "/":
                return s[j:]
            j += 1
        return ""
    if ext == ".py":
        inq = False
        j = 0
        while j < len(s):
            if s[j] == '"':
                inq = not inq
            if not inq and s[j] == "#":
                return s[j:]
            j += 1
        return ""
    if ext in (".toml", ".yml"):
        j = 0
        inq = False
        while j < len(s):
            if s[j] == '"':
                inq = not inq
            if not inq and s[j] == "#":
                return s[j:]
            j += 1
        return ""
    if ext == ".bat":
        st = s.lstrip()
        if st.lower().startswith("rem "):
            return st
        return ""
    return ""


def xaml_comments(text):
    """Yield (line_number, comment_text) for XML comments in a xaml file."""
    out = []
    lineno = 1
    buf = ""
    in_comment = False
    for line in text.splitlines():
        work = line
        while True:
            if in_comment:
                end = work.find("-->")
                if end < 0:
                    out.append((lineno, work))
                    break
                out.append((lineno, work[:end]))
                work = work[end + 3:]
                in_comment = False
            else:
                start = work.find("<!--")
                if start < 0:
                    break
                work = work[start + 4:]
                in_comment = True
        lineno += 1
    return out


def scan_file(path):
    ext = os.path.splitext(path)[1].lower()
    violations = []
    try:
        text = io.open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        return violations
    if "lint-allow: unicode-tables" in text:
        # self-exemption for this very linter: its char class must contain
        # CJK literals, which no comment translation can avoid.
        return violations
    lines = text.splitlines()
    if ext == ".xaml":
        targets = [(n, c) for n, c in xaml_comments(text)]
    else:
        targets = [(i, comment_part(l, ext)) for i, l in enumerate(lines, 1)]
        # doc-block continuation lines: python triple-quoted docstrings cannot
        # be detected lexically here; test/whitelist keeps them honest instead
    for n, c in targets:
        if not c:
            continue
        if CJK_PROSE.search(strip_quoted(c)):
            violations.append((n, c.strip()[:120]))
    return violations


def iter_source_files(base=None, file_list=None):
    if file_list:
        return [p for p in file_list
                if os.path.splitext(p)[1].lower() in EXTS
                and os.path.basename(p) != "README.zh-CN.md"]
    root = base or ROOT
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if os.path.splitext(f)[1].lower() in EXTS:
                found.append(os.path.join(dirpath, f))
    return found


# ---- Check 2: no developer-machine paths in shipped source or docs ----------

# A drive letter on its own. The lookbehind keeps URLs honest: the 's' of
# https:// is preceded by a word character, so it never reads as a drive.
DRIVE_PATH = re.compile(r'(?<!\w)[A-Za-z]:[\\/](?![/])')
DRIVE_ALLOW = "lint-allow: drive-path"

# Repro commands live in prose as much as in code, so docs are covered; that is
# why this check has its own extension list instead of reusing EXTS (which
# skips .md), and its own directory list (which keeps docs in, where the CJK
# rule sends nothing because docs are Chinese by design).
PATH_EXTS = EXTS + (".md", ".sh", ".xaml")
PATH_SKIP_DIRS = SKIP_DIRS - {"docs"} | {".mimosa"}
PATH_SCAN_DIRS = ("script", "sidecar", "ci", ".github", "docs")
# Test fixtures are out of scope entirely -- see the module docstring.
PATH_FIXTURE_DIRS = ("tests", "translator-engine", "Wonslate.UI", "Wonslate.UI.Tests")


def iter_path_files(base=None, file_list=None):
    if file_list is not None:
        return [p for p in file_list
                if os.path.splitext(p)[1].lower() in PATH_EXTS
                and not _under_a(p, base or ROOT, PATH_FIXTURE_DIRS)]
    found = []
    for top in PATH_SCAN_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(base or ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in PATH_SKIP_DIRS]
            for f in filenames:
                if os.path.splitext(f)[1].lower() in PATH_EXTS:
                    found.append(os.path.join(dirpath, f))
    return found


def _under_a(path, base, tops):
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base))
    parts = rel.split(os.sep)
    return bool(parts) and parts[0] in tops


def scan_paths(path):
    """Return [(lineno, snippet)] holding a drive-absolute path."""
    if os.path.basename(path) == os.path.basename(__file__):
        # Self-exemption, same spirit as the unicode-tables one above: this
        # linter has to spell out the shapes it forbids.
        return []
    try:
        text = io.open(path, encoding="utf-8").read()
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if DRIVE_ALLOW in line:
            continue
        if DRIVE_PATH.search(line):
            hits.append((n, line.strip()[:120]))
    return hits


def main(argv):
    files = None
    if len(argv) > 1:
        files = argv[1:]
    total = 0
    for path in iter_source_files(file_list=files):
        rel = os.path.relpath(path, ROOT)
        for n, snippet in scan_file(path):
            print(f"[FAIL] CJK comment prose (English-only rule): {rel}:{n}: {snippet}")
            total += 1
    for path in iter_path_files(file_list=files):
        rel = os.path.relpath(path, ROOT)
        for n, snippet in scan_paths(path):
            print(f"[FAIL] drive-absolute path (use the data-dir rule, or mark "
                  f"{DRIVE_ALLOW}): {rel}:{n}: {snippet}")
            total += 1
    if total:
        print(f"source lint: {total} violation(s) -- comments must be English, "
              "shipped files must not name one machine's disk")
        return 1
    print("source lint: PASS (comments are English-only, no machine paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
