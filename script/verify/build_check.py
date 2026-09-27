#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""build_check.py -- local verification gate for the Wonslate WPF client.

Two stages that script/build.py cannot cover, ordered fail-fast:

  1. XAML static lint (no compilation needed, catches "builds fine but crashes
     on launch" before the build even runs)
       R1  DynamicResource inside a Binding expression
           BAML compiles it, WPF throws XamlParseException at runtime.
       R2  <Run Text="{Binding ...}"> without an explicit Mode
           Run.Text defaults to TwoWay; binding it to a read-only source throws
           during InitializeComponent, i.e. the window never appears.
  2. Launch smoke (Windows only)
       skip if an instance is already running (no single-instance mutex in the
       app, so a second launch would give a meaningless result), start the
       built exe, wait for a visible top-level window owned by that pid,
       require it to stay alive, then terminate the whole tree.

Machine policy is never a verdict: if the OS refuses to start a freshly built
unsigned binary, the smoke stage reports SKIP, not FAIL. See --help.

Usage:
    python script/verify/build_check.py                # lint + smoke
    python script/verify/build_check.py --lint-only     # text checks only
    python script/verify/build_check.py --build         # run script/build.py first
    python script/verify/build_check.py --exe path      # explicit exe to smoke
Exit code: 0 = all stages passed or skipped, 1 = at least one FAIL.

Standard library only.
"""

from __future__ import annotations

import argparse
import ctypes
import locale
import os
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
XAML_GLOB = "Wonslate.UI/**/*.xaml"
SKIP_DIR_PARTS = {"bin", "obj", ".vs", "publish", "__pycache__", ".git"}

SMOKE_WINDOW_WAIT = 20.0      # seconds to wait for the first visible window
SMOKE_STABLE_SECONDS = 2.0    # seconds it must then stay alive
EXE_HINT = r"Wonslate.UI\bin\Release\net10.0-windows\Wonslate.exe"

# Windows refuses to start a binary that application control / code integrity
# blocks. Those are machine policy differences, not product defects -> SKIP.
INTEGRITY_BLOCK_CODES = {
    4551, 1260, 225,               # Win32: app control / policy blocked
    0xC0E90002 & 0xFFFFFFFF,       # NTSTATUS seen when launching new hashes
}

IS_WINDOWS = os.name == "nt"


def say(tag: str, msg: str = "") -> None:
    print(f"[{tag}] {msg}".rstrip(), flush=True)


def decode_console(raw: bytes) -> str:
    """Prefer UTF-8; fall back to the system ANSI codepage (cn console is GBK)."""
    for enc in ("utf-8", locale.getpreferredencoding(False), "mbcs"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\ufffd" not in text:
            return text
    return raw.decode("utf-8", errors="replace")


def read_app_control_state() -> str:
    """off / enforcement / assessment / unknown, from the CI policy registry."""
    if not IS_WINDOWS:
        return "not-windows"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\CI\Policy") as key:
            val, _ = winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")
        return {0: "off", 1: "enforcement", 2: "assessment"}.get(int(val), "unknown")
    except OSError:
        return "unknown"


# --------------------------------------------------------------- stage 1: lint

BINDING_RE = re.compile(r"\{Binding[^}]*\}", re.M | re.S)
RUN_TEXT_RE = re.compile(r"<Run\b[^>]*\bText=\"\{Binding[^>]*>", re.M)


def lint_xaml(text: str) -> list[tuple[int, str, str]]:
    """Return (line, rule, detail) findings for one XAML document."""
    findings: list[tuple[int, str, str]] = []

    for match in BINDING_RE.finditer(text):
        if "DynamicResource" in match.group(0):
            line = text.count("\n", 0, match.start()) + 1
            findings.append((line, "R1", "DynamicResource inside a Binding: "
                             f"{match.group(0).splitlines()[0][:60]}"))

    for match in RUN_TEXT_RE.finditer(text):
        tag = match.group(0)
        if not re.search(r"\bMode\s*=\s*OneWay\b", tag):
            line = text.count("\n", 0, match.start()) + 1
            findings.append((line, "R2", "Run.Text binding without Mode=OneWay: "
                             f"{tag[:70]}"))
    return findings


def iter_xaml_files() -> list[Path]:
    return sorted(p for p in ROOT.glob(XAML_GLOB)
                  if not skip_path(p) and p.is_file())


def skip_path(path: Path) -> bool:
    return any(part in SKIP_DIR_PARTS for part in path.parts)


def stage_lint() -> str:
    files = iter_xaml_files()
    if not files:
        say("1", f"SKIP  no XAML found under {XAML_GLOB}")
        return "SKIP"
    total = 0
    for path in files:
        findings = lint_xaml(path.read_text(encoding="utf-8", errors="replace"))
        for line, rule, detail in findings:
            rel = path.relative_to(ROOT).as_posix()
            say("1", f"FAIL {rule} {rel}:{line}  {detail}")
            total += 1
    if total:
        say("1", f"FAIL  {total} finding(s) in {len(files)} file(s)")
        return "FAIL"
    say("1", f"PASS  {len(files)} XAML file(s) clean (R1 DynamicResource, R2 Run.Text mode)")
    return "PASS"


# -------------------------------------------------------------- stage 2: smoke

def _enum_top_windows_for_pid(pid: int) -> list[int]:
    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            found.append(hwnd)
        return True

    user32.EnumWindows(proto(cb), 0)
    return found


def _instances_running() -> int:
    """How many Wonslate.exe are alive; used to refuse a misleading smoke."""
    if not IS_WINDOWS:
        return 0
    name = Path(EXE_HINT).name
    r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                       capture_output=True)
    out = decode_console(r.stdout or b"")
    return sum(1 for line in out.splitlines() if name.lower() in line.lower())


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True)


def stage_smoke(exe: Path, stable: float) -> str:
    if not IS_WINDOWS:
        say("2", "SKIP  WPF smoke needs Windows")
        return "SKIP"
    if not exe.exists():
        say("2", f"SKIP  not built: {exe}")
        say("2", "      run: python script/build.py   (or: python script/verify/build_check.py --build)")
        return "SKIP"
    if _instances_running():
        say("2", "SKIP  a Wonslate.exe is already running; the app has no single")
        say("2", "      instance mutex, so a second launch cannot be attributed.")
        return "SKIP"

    err_fd, err_path = tempfile.mkstemp(prefix="wonslate-smoke-", suffix=".stderr")
    proc = None
    try:
        with os.fdopen(err_fd, "wb") as err_file:
            try:
                proc = subprocess.Popen([str(exe)], cwd=str(exe.parent),
                                        stdout=subprocess.DEVNULL, stderr=err_file)
            except OSError as exc:
                if getattr(exc, "winerror", None) in INTEGRITY_BLOCK_CODES:
                    say("2", f"SKIP  OS refused to start the new build (winerror {exc.winerror})")
                    say("2", f"      application control state: {read_app_control_state()}")
                    return "SKIP"
                raise

        deadline = time.monotonic() + SMOKE_WINDOW_WAIT
        window = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                code = proc.returncode & 0xFFFFFFFF
                if code in INTEGRITY_BLOCK_CODES:
                    say("2", f"SKIP  process refused by code integrity (exit 0x{code:08x})")
                    say("2", f"      application control state: {read_app_control_state()}")
                    return "SKIP"
                say("2", f"FAIL  exited before showing a window (exit 0x{code:08x})")
                _dump_stderr(err_path)
                return "FAIL"
            if _enum_top_windows_for_pid(proc.pid):
                window = proc.pid
                break
            time.sleep(0.25)

        if window is None:
            say("2", f"FAIL  no visible top-level window within {SMOKE_WINDOW_WAIT}s")
            _dump_stderr(err_path)
            return "FAIL"

        time.sleep(stable)
        if proc.poll() is not None:
            say("2", "FAIL  window appeared but the process then died")
            _dump_stderr(err_path)
            return "FAIL"
        if not _enum_top_windows_for_pid(proc.pid):
            say("2", "FAIL  window disappeared during the stability window")
            return "FAIL"

        say("2", f"PASS  window shown and stable for {stable:g}s (pid {proc.pid})")
        return "PASS"
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        Path(err_path).unlink(missing_ok=True)


def _dump_stderr(path: str, tail: int = 30) -> None:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if not lines:
        return
    say("2", f"      ---- captured stderr, last {min(tail, len(lines))} line(s) ----")
    for line in lines[-tail:]:
        say("2", f"      {line[:150]}")


# ------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", action="store_true",
                        help="run script/build.py first (release engine + WPF app)")
    parser.add_argument("--lint-only", action="store_true", help="skip the smoke stage")
    parser.add_argument("--smoke-only", action="store_true", help="skip the lint stage")
    parser.add_argument("--exe", default=None, help=f"exe to smoke (default {EXE_HINT})")
    parser.add_argument("--stable", type=float, default=SMOKE_STABLE_SECONDS,
                        help="seconds the window must stay alive (default 2)")
    args = parser.parse_args(argv)

    say("*", f"Wonslate build_check  root={ROOT}")
    results: dict[str, str] = {}

    if not args.smoke_only:
        results["lint"] = stage_lint()
        if results["lint"] == "FAIL":
            say("*", "STOP  fix the XAML findings before launching anything")
            return 1

    if not args.lint_only:
        if args.build:
            say("0", "building via script/build.py ...")
            rc = subprocess.run([sys.executable, str(ROOT / "script" / "build.py")]).returncode
            if rc != 0:
                say("0", "FAIL  build failed, nothing to smoke")
                return 1
        exe = Path(args.exe) if args.exe else ROOT / EXE_HINT
        results["smoke"] = stage_smoke(exe, args.stable)

    summary = ", ".join(f"{k}={v}" for k, v in results.items())
    say("*", summary)
    return 1 if "FAIL" in results.values() else 0


if __name__ == "__main__":
    raise SystemExit(main())
