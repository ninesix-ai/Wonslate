#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""End-to-end wiring check for the argos/madlad sidecar path (Phase 2, mock backend).

This does NOT test a real translation model. It proves the full chain works:
    caller -> Rust `argos` engine -> local HTTP sidecar (mock backend) -> echoed pseudo-translation

A mock hit is uniquely identifiable: MockBackend returns "[<target>] <text>", so
"[zh] hello" can only come from the sidecar (the demo engine would return "你好",
and an absent sidecar would fall back to demo / error).

Run (needs the release engine built first, e.g. `python script/build.py`):
    python tests/test_sidecar_e2e.py
"""
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                       # repo root
SIDECAR = ROOT / "sidecar" / "ct2_sidecar.py"

# Reuse the ctypes Engine wrapper + dll locator from the FFI smoke test.
sys.path.insert(0, str(HERE))
import test_phase1_ffi as ffi  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(base: str, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def main() -> int:
    ck = ffi.Checker()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    # Point the Rust `argos` engine (sidecar.rs reads LT_ARGOS_URL) at our mock sidecar.
    os.environ["LT_ARGOS_URL"] = url

    proc = subprocess.Popen(
        [sys.executable, str(SIDECAR), "--backend", "mock", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_health(url):
            ck.fail("sidecar 启动", f"mock sidecar 未在 {url} 就绪")
            return ck.summary()
        ck.ok(f"mock sidecar 就绪于 {url}")

        try:
            dll = ffi._find_dll()
        except FileNotFoundError as e:
            ck.fail("引擎 DLL", str(e))
            return ck.summary()
        eng = ffi.Engine(dll)
        eng.init("{}")
        try:
            req = {
                "engine_id": "argos",       # lock to the sidecar engine, bypass routing
                "input": "hello",
                "source_lang": "en",
                "target_lang": "zh",
                "mode": "full",
                "use_tm": False,
            }
            r = eng.translate_full(req)
            out = (r.get("output") or "").strip()
            if r.get("ok") and out == "[zh] hello":
                ck.ok(f"UI→Rust(argos)→sidecar 端到端命中 mock 回显: {out!r}")
            else:
                ck.fail("argos→sidecar 命中", f"expected '[zh] hello', got {r}")
        finally:
            eng.shutdown()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        ck.ok("sidecar 进程已回收")

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
