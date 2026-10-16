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
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                       # repo root
SIDECAR = ROOT / "sidecar" / "ct2_sidecar.py"

# Reuse the ctypes Engine wrapper + dll locator from the FFI smoke test.
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "script"))
import test_phase1_ffi as ffi  # noqa: E402
from sidecar_identity import SidecarIdentityError, wait_until_ours  # noqa: E402  (D73)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    ck = ffi.Checker()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    # Point the Rust `argos` engine (sidecar.rs reads LT_/WONSLATE_ARGOS_URL)
    # at our mock sidecar; set both spellings so the prefix chain resolves.
    os.environ["LT_ARGOS_URL"] = url
    os.environ["WONSLATE_ARGOS_URL"] = url

    proc = subprocess.Popen(
        [sys.executable, str(SIDECAR), "--backend", "mock", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Strict mode: a measurement must not accept a service that will not identify itself.
        # An answer from the port is not proof that the mock launched above is answering it --
        # if a stranger owns that port, every assertion below runs against something else.
        try:
            _label, detail = wait_until_ours(url, proc, timeout=10.0, interval=0.1,
                                             allow_unproven=False)
        except SidecarIdentityError as bad:
            ck.fail("sidecar start", str(bad))
            return ck.summary()
        ck.ok("mock sidecar ready at %s (%s)" % (url, detail))

        try:
            dll = ffi._find_dll()
        except FileNotFoundError as e:
            ck.fail("engine DLL", str(e))
            return ck.summary()
        eng = ffi.Engine(dll)
        # Hermetic run: pin the engine to a throw-away data dir, the same isolation
        # test_phase1_ffi applies to itself, so this suite neither reads nor writes
        # the developer's live TM/glossary.
        store = tempfile.TemporaryDirectory(prefix="wonslate-sidecar-e2e-")
        os.environ["LT_DATA_DIR"] = str(pathlib.Path(store.name) / "data")
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
                ck.ok(f"UI->Rust(argos)->sidecar end-to-end hit on the mock echo: {out!r}")
            else:
                ck.fail("argos->sidecar hit", f"expected '[zh] hello', got {r}")
        finally:
            eng.shutdown()
            store.cleanup()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        ck.ok("sidecar process reaped")

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
