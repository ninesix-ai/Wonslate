# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
# Install the Wonslate local git hooks (currently: pre-commit).
#
# Copies every file under ci/hooks/ into the repository's active hooks directory,
# normalizes line endings to LF (so hooks run under bash even on Windows checkouts)
# and marks them executable. Cross-platform (Windows / Linux / macOS); run once per
# clone. Re-running overwrites, so updates under ci/hooks/ take effect immediately.
#
# Usage:
#   python script/install_hooks.py
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ci" / "hooks"


def git(*args) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True
    )
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def hooks_dir() -> Path:
    # `--git-path hooks` resolves the active hooks directory, honoring worktrees and
    # a custom core.hooksPath; the returned path may be relative to the repo root.
    p = Path(git("rev-parse", "--git-path", "hooks"))
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def mark_executable(dst: Path) -> None:
    mode = dst.stat().st_mode
    dst.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> int:
    if shutil.which("git") is None:
        raise SystemExit("git is not on PATH")
    if not SRC.is_dir():
        raise SystemExit(f"hook source not found: {SRC}")

    dst_dir = hooks_dir()
    installed = []
    for src in sorted(SRC.iterdir()):
        if not src.is_file():
            continue
        dst = dst_dir / src.name
        # Force LF endings; a CRLF shebang line breaks execution under bash.
        dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))
        if os.name != "nt":
            # Git for Windows runs hooks through its bundled bash and does not
            # require the executable bit; chmod exec bits is a no-op on Windows.
            mark_executable(dst)
        installed.append(dst.name)

    if not installed:
        print(f"No hooks found in {SRC}")
        return 1

    print(f"Installed {len(installed)} hook(s) into {dst_dir}:")
    for name in installed:
        print(f"  - {name}")
    print("Tip: bypass once with `git commit --no-verify`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
