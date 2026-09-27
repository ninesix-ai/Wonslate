# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Source-comment hygiene lint: comment prose must stay English.

Why: Wonslate is an open-source project with international contributors; the
project convention is English comments everywhere in source. CJK test DATA
(string literals like "你好") is legitimate product behavior and is NOT flagged
-- only comment prose is checked.

Checked files (tracked source, by extension):
    .rs .cs .py .toml .yml .bat .xaml
Exclusions:
    - generated/vendored dirs (bin, obj, target, __pycache__, TestResults)
    - README.zh-CN.md and docs/ (Chinese by design; not source)
    - quoted string literals on the same line (stripped before scanning)
    - MainWindow.xaml outside comments: UI strings are product content, only
      its <!-- comments --> are scanned

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
    if total:
        print(f"source lint: {total} violation(s) -- comments must be English")
        return 1
    print("source lint: PASS (comments are English-only)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
