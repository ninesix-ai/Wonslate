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
#   python script/build.py --ffi      # build + Python FFI smoke only
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
    return run(["dotnet", "test", str(proj), "-c", "Release", "--nologo"])


def run_ffi_smoke() -> int:
    rc = 0
    for name in ("test_phase1_ffi.py", "test_sidecar_e2e.py"):
        script = ROOT / "tests" / name
        if not script.exists():
            print(f"  [SKIP] tests/{name} not present", flush=True)
            continue
        if shutil.which("python") is None:
            print("  [SKIP] python not on PATH", flush=True)
            return rc
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
