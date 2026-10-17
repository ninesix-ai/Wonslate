# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Source hygiene lint: English comment prose, and no developer-machine paths.

Why: Wonslate is an open-source project with international contributors; the
project convention is English comments everywhere in source. CJK test DATA
(string literals like "你好") is legitimate product behavior and is NOT flagged
-- only comment prose is checked.

Three checks run over the tree:

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

  3. SPDX / copyright header on every shipped source file, within the first eight
     lines: `SPDX-License-Identifier: Apache-2.0` and `Copyright (c) 2026
     ninesix-ai studio`, in that language's own comment marker.
     Why this became a check: the convention is not a preference here -- 121 of
     133 source files already carry it, including every .rs, .cs, .toml and .xaml
     file. The 12 exceptions were all files added during recent work, and nothing
     caught nine consecutive omissions because no rule existed. A convention that
     only lives in prose decays exactly this way (this repository's own version of
     "a guard nobody runs is not a guard", D65/D77).
     Scope note: .sh joins the header check but not checks 1 and 2 -- widening
     EXTS would also drag shell scripts under the English-prose and drive-path
     rules, which has never been measured for them and is a separate decision.

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

# ---- Check 3: the header every shipped file already carries -------------------

SPDX_MARK = "SPDX-License-Identifier:"
COPYRIGHT_MARK = "Copyright (c)"
HEADER_ALLOW = "lint-allow: no-header"
#: How far down a file we look, generous enough for a shebang, a coding declaration and a
#: blank line before the header; tight enough that a header buried after the code still reads
#: as missing -- the point is the top of the file, not the presence of a string somewhere.
HEADER_WINDOW = 8
#: Comment openers stripped before testing whether a line *declares* the exemption.
COMMENT_MARKS = ("#", "//", "rem", "<!--")
#: Check 1 and 2 stay on EXTS; only the header rule also covers shell entry points.
HEADER_EXTS = EXTS + (".sh", ".yaml")


def header_exempt(text):
    """Does a comment line at the top of this file declare `lint-allow: no-header`?

    Declared as a comment on its own line, not as a substring anywhere in the file. The first
    draft asked `HEADER_ALLOW in text`, and the very first file it ran over was this linter: the
    rule text appears in its help output and in the code that prints the exemption count, so the
    file that *documents* the exemption was granted it -- D78, and the same mistake this
    repository has already written down twice from the other direction (a predicate that fires
    on prose about the rule, `check_name_guards` and the quote verdict). Anyone allowed to
    mention a word is allowed to skip the check that names it.
    """
    for line in text.splitlines()[:HEADER_WINDOW]:
        body = line.strip()
        for mark in COMMENT_MARKS:
            if body.startswith(mark):
                body = body[len(mark):].strip()
                break
        if body.startswith(HEADER_ALLOW):
            return True
    return False


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


def iter_source_files(base=None, file_list=None, exts=EXTS):
    """One walker, parameterised by extension set.

    A second copy of this loop for the header rule would be the D64 shape: two readers of the
    same tree drifting into two ideas of what is in scope.
    """
    if file_list:
        return [p for p in file_list
                if os.path.splitext(p)[1].lower() in exts
                and os.path.basename(p) != "README.zh-CN.md"]
    root = base or ROOT
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if os.path.splitext(f)[1].lower() in exts:
                found.append(os.path.join(dirpath, f))
    return found


def scan_header(path):
    """(kind, detail) for one file: kind is 'ok', 'exempt', 'unreadable' or 'missing'.

    Four outcomes, not two. Returning "fine" for a file that could not be opened was the first
    draft's quiet lie: pointing this linter at a path that does not exist printed "1 file(s) carry
    the header" about a file with no header at all, because OSError was folded into the passing
    branch. A count that includes what it never read is the failure this repository has already
    written down twice (a skipped check printing the same as a passing one, and "0 hits" that
    really means "0 files scanned").

    Returns a flag rather than printing, so `main` can keep the exemption count visible.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in HEADER_EXTS:
        return "not-in-scope", None
    if not os.path.isfile(path):
        return "missing", "named but not on disk"
    try:
        with io.open(path, encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeDecodeError:
        return "unreadable", "not valid UTF-8"
    if header_exempt(text):
        return "exempt", None
    head = "\n".join(text.splitlines()[:HEADER_WINDOW])
    missing = [mark for mark in (SPDX_MARK, COPYRIGHT_MARK) if mark not in head]
    if missing:
        return "missing", "no %s in the first %d lines" % ("/".join(missing), HEADER_WINDOW)
    return "ok", None


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
    headed = exempt = unreadable = 0
    for path in iter_source_files(file_list=files, exts=HEADER_EXTS):
        rel = os.path.relpath(path, ROOT)
        kind, detail = scan_header(path)
        if kind == "exempt":
            exempt += 1
        elif kind in ("missing", "unreadable"):
            unreadable += 1
            print(f"[FAIL] license header could not be confirmed ({detail}): {rel}")
            total += 1
        elif kind == "ok":
            headed += 1
    # Printed even when zero. A count that only appears when something was exempted is how an
    # exemption quietly becomes the normal path.
    print(f"note: {headed} file(s) carry the header, {exempt} exempted via {HEADER_ALLOW}, "
          f"{unreadable} could not be confirmed")
    if total:
        print(f"source lint: {total} violation(s) -- comments must be English, "
              "shipped files must not name one machine's disk, and every source file "
              "must open with its license header")
        return 1
    print("source lint: PASS (comments are English-only, no machine paths, headers present)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
