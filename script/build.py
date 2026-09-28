# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
# One-click build/test entrypoint for Wonslate (cross-platform).
# Replaces build.ps1; thin build.bat / build.sh launchers forward all args here.
# Lives under script/; the repo root is the parent of this file's directory.
#
# Usage (via build.bat / build.sh, or directly):
#   python script/build.py            # build only: Rust engine (release) + .NET WPF (Windows only)
#   python script/build.py --run      # build, then launch the app (Windows)
#   python script/build.py --test     # build + run all tests (Rust + .NET + Python FFI)
#   python script/build.py --unit     # build + Rust & .NET tests only
#   python script/build.py --ffi      # build + Python smoke only (FFI, sidecar e2e, ct2 unit, XAML + hook lint)
#
# Application control note: on a host where Smart App Control (or another code
# integrity policy) blocks the freshly built native library, the two smoke tests
# that cross the FFI boundary are reported as SKIP instead of FAIL. That is an
# environment condition rather than a product defect; see docs/09.
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "translator-engine"
APP = ROOT / "Wonslate.UI"
IS_WIN = os.name == "nt"


def native_lib_name() -> str:
    if IS_WIN:
        return "translator_engine.dll"
    if sys.platform == "darwin":
        return "libtranslator_engine.dylib"
    return "libtranslator_engine.so"


def run(cmd, cwd=None) -> int:
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None).returncode


def run_teeing(cmd, cwd=None) -> tuple[int, str]:
    """Run a command, echo its output as it arrives, and also return it.

    Needed where the caller must inspect the output (application control blocks
    surface as text) without hiding it from the operator.
    """
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    chunks: list[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            chunks.append(line)
            print(line, end="", flush=True)
    proc.wait()
    return proc.returncode, "".join(chunks)


def build_rust() -> Path:
    print("==> [1/2] Building Rust engine (release)...", flush=True)
    if run(["cargo", "build", "--release"], cwd=ENGINE) != 0:
        raise SystemExit("Rust build failed")
    lib = ENGINE / "target" / "release" / native_lib_name()
    if not lib.exists():
        raise SystemExit(f"engine library not found: {lib}")
    print(f"    library: {lib}", flush=True)
    return lib


def build_dotnet(lib: Path):
    # WPF (net10.0-windows) can only be built on Windows.
    if not IS_WIN:
        print("==> [2/2] .NET WPF app skipped (non-Windows host).", flush=True)
        return None
    print("==> [2/2] Building .NET WPF app...", flush=True)
    shutil.copy2(lib, APP / lib.name)
    if run(["dotnet", "build", "-c", "Release"], cwd=APP) != 0:
        raise SystemExit(".NET build failed")
    return APP / "bin" / "Release" / "net10.0-windows" / "Wonslate.exe"


def run_rust_tests() -> int:
    print("  -- Rust unit + integration (cargo test --release) --", flush=True)
    return run(["cargo", "test", "--release"], cwd=ENGINE)


def run_dotnet_tests() -> int:
    proj = ROOT / "Wonslate.UI.Tests" / "Wonslate.UI.Tests.csproj"
    if not IS_WIN:
        print("  [SKIP] .NET tests require Windows", flush=True)
        return 0
    if not proj.exists():
        print("  [SKIP] Wonslate.UI.Tests not present", flush=True)
        return 0
    print("  -- .NET xUnit tests (dotnet test) --", flush=True)
    rc, out = run_teeing(["dotnet", "test", str(proj), "-c", "Release", "--nologo"])
    if rc != 0 and app_control_blocked_output(out):
        print(f"  [SKIP] dotnet test blocked by application control "
              f"(state={app_control_state()})", flush=True)
        print("         No test result is trustworthy on this host.", flush=True)
        print_app_control_hint()
        return 0
    return rc


# Windows application control (Smart App Control) block codes. A policy block
# on the native library is an environment condition, not a product defect.
SAC_BLOCK_CODES = {4551, 1260, 225}

# Smoke tests that load the Rust cdylib across the FFI boundary; they cannot run
# when application control blocks that library.
NATIVE_LIB_TESTS = {"test_phase1_ffi.py", "test_sidecar_e2e.py"}

# Markers that identify an application control block inside subprocess output.
# dotnet reports the policy HRESULT rather than a WinError and the surrounding
# sentence is localized, so several spellings are matched. Deliberately no bare
# "4551": a plain number can occur in unrelated test output and would mask real
# failures.
SAC_OUTPUT_MARKERS = (
    "0x800711c7",                  # ERROR_BLOCKED_BY_APPLICATION_CONTROL_POLICY
    "0x11c7",
    "winerror 4551",
    "application control policy",
    "应用程序控制策略",
)


def app_control_blocked_output(text: str) -> bool:
    """Return True when command output shows a code integrity policy block."""
    low = text.lower()
    return any(marker in low for marker in SAC_OUTPUT_MARKERS)


def app_control_state() -> str:
    """Return Smart App Control state: 'off' / 'enforcement' / 'assessment' / 'unknown'."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\CI\Policy") as key:
            val, _ = winreg.QueryValueEx(key, "VerifiedAndReputablePolicyState")
        return {0: "off", 1: "enforcement", 2: "assessment"}.get(int(val), "unknown")
    except (OSError, ImportError):
        return "unknown"


def print_app_control_hint() -> None:
    """Explain how to clear an application control block on a dev machine."""
    if app_control_state() == "off":
        print("         The setting reads off but the policy is still enforced; "
              "a reboot is required after changing it.", flush=True)
    print("         Fix: Windows Security > App & browser control > "
          "Smart App Control > Off", flush=True)


def native_lib_blocked() -> int | None:
    """Return the WinError if application control blocks the engine library.

    Any other load failure (missing dependency, wrong architecture) returns None
    so the real defect still surfaces as a FAIL.
    """
    lib = ENGINE / "target" / "release" / native_lib_name()
    if not lib.exists():
        return None
    try:
        import ctypes
        ctypes.CDLL(str(lib))
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        return code if code in SAC_BLOCK_CODES else None
    return None


def run_ffi_smoke() -> int:
    rc = 0
    blocked = native_lib_blocked()
    if blocked is not None:
        print(f"  [SKIP] application control blocks {native_lib_name()} "
              f"(WinError {blocked}, state={app_control_state()})", flush=True)
        print("         FFI smoke cannot run on this host.", flush=True)
        print_app_control_hint()
    for name in ("test_phase1_ffi.py", "test_sidecar_e2e.py",
                 "test_ct2_sidecar.py", "test_xaml_lint.py",
                 "test_pre_commit_hook.py", "test_component_licenses.py",
                 "test_source_lint.py"):
        script = ROOT / "tests" / name
        if not script.exists():
            print(f"  [SKIP] tests/{name} not present", flush=True)
            continue
        if shutil.which("python") is None:
            print("  [SKIP] python not on PATH", flush=True)
            return rc
        if blocked is not None and name in NATIVE_LIB_TESTS:
            print(f"  [SKIP] {name} needs {native_lib_name()} (blocked)", flush=True)
            continue
        print(f"  -- {name} --", flush=True)
        rc |= run([sys.executable or "python", str(script)], cwd=ROOT)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/test Wonslate cross-platform.")
    parser.add_argument("--test", action="store_true", help="build + run all tests")
    parser.add_argument("--unit", action="store_true", help="build + Rust/.NET tests")
    parser.add_argument("--ffi", action="store_true", help="build + Python FFI smoke")
    parser.add_argument("--run", action="store_true", help="build, then launch the app (Windows)")
    args = parser.parse_args()

    lib = build_rust()
    exe = build_dotnet(lib)

    failures = 0
    if args.test or args.unit or args.ffi:
        print("\n==> [3/3] Running tests...", flush=True)
        if not args.ffi:  # runUnit
            failures += 1 if run_rust_tests() != 0 else 0
            failures += 1 if run_dotnet_tests() != 0 else 0
        if not args.unit:  # runFfi
            failures += 1 if run_ffi_smoke() != 0 else 0

    print("", flush=True)
    if failures:
        print(f"{failures} test phase(s) FAILED", flush=True)
        return 1

    if args.test or args.unit or args.ffi:
        print("ALL DONE, tests PASSED", flush=True)
    else:
        print("Build complete.", flush=True)

    if args.run:
        if exe and Path(exe).exists():
            print(f"Launching {exe}", flush=True)
            return subprocess.run([str(exe)]).returncode
        print("  [WARN] --run requested but app executable is unavailable on this host", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
