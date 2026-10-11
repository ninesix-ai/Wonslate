#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""The Rust engine against a real, gated MADLAD service: a busy refusal may cost seconds, never a translation.

`tests/test_sidecar_e2e.py` proves the chain works on the mock backend. `engine/sidecar.rs`'s
own tests prove the wait logic on scripted sockets. What neither can prove is the claim this
suite exists for, because it needs the real decoder and the real timing:

    caller -> Rust `madlad` engine -> HTTP -> MADLAD-400 with `--decode-slots 1`

and the claim is a **paired** one. Both episodes put the Rust call in front of a service whose
single slot is demonstrably busy, and differ in exactly one setting -- the shipped wait budget
versus `*_SIDECAR_RETRY_MS=0`, which is the behaviour this repo shipped before the wait existed:

  * with the budget, the engine waits and the real checkpoint answers;
  * with no budget, the pipeline answers from the demo engine: the translation was *lost*.

Saying "the retry works" from the first episode alone would be the unpaired mistake, and a
service that never refused would also return a translation. The second episode is what makes
the first one mean something, so each episode asserts its own premise on the service's numbers.

Three measurements this suite had to learn the hard way:

1. **Nothing may compete for the slot.** The first draft probed `/translate` while the engine
   call was in flight to "prove" refusals -- and the probes won the slot the engine needed on
   its retry, so the shipped-budget episode failed against a gate the suite itself had made
   worse. Premises are now read from `/health` (which does not take a slot) plus the child's
   own `requests_served` delta.
2. **The service's counters, not the answer text, say which instance answered.** Two MADLAD
   processes return byte-identical translations. The first draft also forgot to pin
   `LT_MADLAD_URL`, so the engine quietly used the developer's ungated sidecar on 11436 and
   every assertion passed against a service that had no gate. `engine_served()` below is the
   guard against that class, and `tests/env_pin_coverage.rs` is the gate for the knob.
3. **The occupancy must be shorter than any promised wait.** `Retry-After` is the ceiling of
   the service's own measured p50, so an 8-second occupier can put the slot out of reach for
   the caller's whole budget. The paired episodes therefore hold the slot with one short
   decode; the long-decode regime runs last and asserts only the honesty invariant, because
   that regime is where the shipped 12 s budget is a genuine boundary rather than a comfortable
   margin.

Needs the release engine built and the MADLAD checkpoint downloaded; skips with the reason
named otherwise (a skip is not a pass, so every planned check is reported).

Run (from the repo root):
    python tests/test_sidecar_busy_e2e.py
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SIDECAR = ROOT / "sidecar" / "ct2_sidecar.py"

sys.path.insert(0, str(HERE))
import test_phase1_ffi as ffi  # noqa: E402

DEAD = "http://127.0.0.1:1"

# The names of every check this suite would run. Kept as data so a skipped run says *which*
# claims were not verified, instead of printing one line and looking like a smaller suite.
CHECKS = (
    "gate refuses a second decode on the real checkpoint",
    "the gated service is the one the engine dialed",
    "with the shipped budget the engine waits and the real checkpoint answers",
    "with no budget the same request loses its translation to demo",
    "two decodes never ran at once",
    "a budget that runs out still names the fallback instead of answering wrongly",
    "gated sidecar process reaped",
)

# The paired episodes hold the slot with this: measured ~2.7 s on CPU, i.e. shorter than the
# smallest `Retry-After` the service can promise (1 s) times a couple of retries, so a waiting
# caller is guaranteed to reach the slot inside the shipped budget.
SHORT_TEXT = "The sampler rate must match the reference track."

# A decode this long (~8 s measured) can outlast two promised waits, which is why the episode
# that uses it asserts only that a caller who gave up is told the truth.
LONG_TEXT = (
    "The reference track and the recording have to share one clock, because a sampler that "
    "drifts by even a fraction of a frame will place every transient later than the mix, and "
    "the listener hears a performance that no longer breathes with the vocal."
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, text: str, timeout: float = 180.0):
    """One /translate call, keeping the status code. urllib raises on 4xx/5xx, and the status
    is the whole subject here, so the error object is the success path."""
    body = json.dumps({"text": text, "source": "en", "target": "zh"}).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/translate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers)


def _health(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def _wait_health(url: str, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _health(url).get("status") == "ok":
            return True
        time.sleep(0.2)
    return False


def _wait_slot_busy(url: str, timeout: float = 15.0):
    """Block until the service says a decode is running. This is the premise of everything
    below, and it is read off `decodes_in_flight` rather than inferred from a sleep."""
    end = time.time() + timeout
    while time.time() < end:
        if _health(url).get("decodes_in_flight", 0) >= 1:
            return True
        time.sleep(0.02)
    return False


class Watcher(threading.Thread):
    """Samples `/health` while the engine call is in flight and records the ceiling of
    `decodes_in_flight`. Polling the health endpoint takes no slot, which is the only reason
    this can be running at all: a probe that translated would steal the slot the caller is
    waiting for."""

    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url = url
        self.stop_it = threading.Event()
        self.peak_in_flight = 0
        self.samples = 0

    def run(self):
        while not self.stop_it.is_set():
            report = _health(self.url)
            if report:
                self.samples += 1
                self.peak_in_flight = max(self.peak_in_flight,
                                          int(report.get("decodes_in_flight", 0) or 0))
            time.sleep(0.05)


def main() -> int:
    ck = ffi.Checker()

    sys.path.insert(0, str(ROOT))
    from sidecar.ct2_sidecar import default_model_dir  # noqa: E402

    model_dir = default_model_dir().parent / "madlad"
    if not model_dir.is_dir():
        # CHECKS already contains the process claim: skipping one name twice would report a
        # different total here than on a host that ran them, and totals are how suites get compared.
        for name in CHECKS:
            ck.skip(name, f"no MADLAD checkpoint under {model_dir}")
        return ck.summary()

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    store = tempfile.TemporaryDirectory(prefix="wonslate-busy-e2e-")

    env = os.environ.copy()
    for key in ("LT_ARGOS_URL", "WONSLATE_ARGOS_URL", "LT_OLLAMA_URL", "WONSLATE_OLLAMA_URL",
                "OLLAMA_URL"):
        env[key] = DEAD
    env["WONSLATE_DATA_DIR"] = str(pathlib.Path(store.name) / "sidecar-data")
    proc = subprocess.Popen(
        [sys.executable, str(SIDECAR), "--backend", "madlad", "--port", str(port),
         "--model-dir", str(model_dir), "--decode-slots", "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )

    try:
        if not _wait_health(url):
            for name in CHECKS:
                ck.skip(name, "gated service never answered /health")
            return ck.summary()

        # Pay the checkpoint load inside a call that is not part of any measurement.
        with urllib.request.urlopen(
                urllib.request.Request(url.rstrip("/") + "/warmup",
                                       data=json.dumps({"source": "en", "target": "zh"}).encode(),
                                       headers={"Content-Type": "application/json"}),
                timeout=180) as warm:
            warm.read()

        try:
            dll = ffi._find_dll()
        except FileNotFoundError as e:
            for name in CHECKS:
                ck.skip(name, f"engine library unavailable: {e}")
            return ck.summary()
        eng = ffi.Engine(dll)
        os.environ["LT_DATA_DIR"] = str(pathlib.Path(store.name) / "data")
        os.environ["WONSLATE_DATA_DIR"] = os.environ["LT_DATA_DIR"]
        os.environ["LT_ARGOS_URL"] = os.environ["WONSLATE_ARGOS_URL"] = DEAD
        os.environ["LT_OLLAMA_URL"] = os.environ["WONSLATE_OLLAMA_URL"] = DEAD
        # Both spellings of the slot the engine under test dials. Missing this line made the
        # first draft of this suite measure the developer's own ungated sidecar on 11436: the
        # gated child was only ever touched by probes, so every "the wait recovered it"
        # assertion passed against a service with no gate at all. That is defect D36, and
        # `tests/env_pin_coverage.rs` exists because of exactly this class.
        os.environ["LT_MADLAD_URL"] = os.environ["WONSLATE_MADLAD_URL"] = url
        os.environ["LT_SIDECAR_TIMEOUT_MS"] = os.environ["WONSLATE_SIDECAR_TIMEOUT_MS"] = "120000"
        eng.init("{}")

        def episode(retry_ms: str, occupy_text: str):
            """Hold the single slot with exactly one decode of `occupy_text`, then ask the Rust
            engine for a translation, and measure enough to prove the premise afterwards."""
            os.environ["LT_SIDECAR_RETRY_MS"] = retry_ms
            os.environ["WONSLATE_SIDECAR_RETRY_MS"] = retry_ms
            durations = []

            def hold():
                started = time.perf_counter()
                _post(url, occupy_text)
                durations.append(round(time.perf_counter() - started, 2))

            occupier = threading.Thread(target=hold, daemon=True)
            occupier.start()
            if not _wait_slot_busy(url):
                occupier.join(180)
                return None
            busy_before = _health(url).get("decodes_in_flight")
            # The service's own tally of *served* requests. A busy refusal is charged to
            # nothing, so the delta says how many times this ephemeral port did work -- which
            # is what distinguishes "my request was refused then retried" from "my request
            # never met the gate", and from "my request went to some other service".
            served_before = _health(url).get("requests_served", 0)
            watcher = Watcher(url)
            watcher.start()
            started = time.perf_counter()
            r = eng.translate_full({
                "engine_id": "madlad", "input": SHORT_TEXT,
                "source_lang": "en", "target_lang": "zh",
                "mode": "full", "use_tm": False,
            })
            elapsed = time.perf_counter() - started
            occupier.join(180)
            watcher.stop_it.set()
            watcher.join(30)
            served_delta = _health(url).get("requests_served", 0) - served_before
            out = {"reply": r, "elapsed": elapsed, "watcher": watcher,
                   "occupier_s": list(durations), "busy_before": busy_before,
                   "served_delta": served_delta,
                   # Only the occupier and the engine dial this port, so anything the service
                   # did that is not the occupier's decode belongs to the engine.
                   "engine_served": served_delta - len(durations)}
            print("  [measure] retry_ms=%s engine=%r elapsed=%.2fs busy_before=%s "
                  "occupier_s=%s served_delta=%s engine_served=%s peak_in_flight=%s"
                  % (retry_ms, r.get("engine"), elapsed, busy_before, out["occupier_s"],
                     served_delta, out["engine_served"], watcher.peak_in_flight), flush=True)
            return out

        # --- episode 1: the shipped budget, slot held for one short decode ----------------
        on = episode("12000", SHORT_TEXT)
        if on is None:
            for name in CHECKS:
                ck.skip(name, "the service never reported a decode in flight, so nothing was gated")
            return ck.summary()

        if on["busy_before"] and on["engine_served"] == 1:
            ck.ok("the gated service is the one the engine dialed")
        else:
            ck.fail("engine dialed the gated service",
                    "a decode was in flight before the call (%s) but this service did %d of the "
                    "engine's requests -- it answered somewhere else"
                    % (on["busy_before"], on["engine_served"]))
        if on["watcher"].peak_in_flight <= 1:
            ck.ok("two decodes never ran at once")
        else:
            ck.fail("single-slot invariant",
                    f"health reported {on['watcher'].peak_in_flight} decodes in flight with "
                    "--decode-slots 1")

        madlad_out = (on["reply"].get("output") or "").strip()
        waited_longer_than_a_no_wait_call = on["elapsed"] >= 1.0
        if (on["reply"].get("engine") == "madlad" and waited_longer_than_a_no_wait_call
                and any("\u4e00" <= ch <= "\u9fff" for ch in madlad_out)):
            ck.ok("with the shipped budget the engine waits and the real checkpoint answers")
        else:
            ck.fail("budget buys back the translation",
                    "expected madlad + CJK after waiting at least one Retry-After, got engine=%r "
                    "elapsed=%.2fs output=%r" % (on["reply"].get("engine"), on["elapsed"],
                                                 madlad_out))

        # --- episode 2: identical conditions, no wait budget ------------------------------
        off = episode("0", SHORT_TEXT)
        if off is None:
            ck.skip("with no budget the same request loses its translation to demo",
                    "the service never reported a decode in flight")
        elif (off["busy_before"] and off["engine_served"] == 0
                and off["reply"].get("engine") == "demo"
                and "madlad" in (off["reply"].get("message") or "")):
            # Deliberately independent of episode 1's text. An earlier version compared this
            # answer against `madlad_out` to prove it was not the real translation, and the
            # mutation sweep caught the coupling: when the waiting episode also falls back,
            # `madlad_out` *is* the demo string, so "different from the real answer" became
            # "different from itself" and a genuine loss could not be recognised. Whether demo
            # answered and whether it said which engine gave up are facts this episode can
            # establish alone.
            ck.ok("with no budget the same request loses its translation to demo")
        else:
            ck.fail("no-wait caller loses the answer",
                    "expected the engine to be refused (0 served) and fall back to demo while "
                    "naming madlad in the message, got engine=%r served=%d in_flight=%s "
                    "message=%r"
                    % (off["reply"].get("engine"), off["engine_served"], off["busy_before"],
                       off["reply"].get("message")))

        # --- episode 3: the boundary the budget was not priced for ------------------------
        # An occupier that decodes for ~8 s can keep the slot out of reach for two promised
        # waits, so the shipped 12 s may run out. That is allowed; answering *wrongly* is not.
        # Run last, so its long samples do not move the p50 the paired episodes depend on.
        long = episode("12000", LONG_TEXT)
        if long is None:
            ck.skip("a budget that runs out still names the fallback instead of answering wrongly",
                    "the service never reported a decode in flight")
        else:
            answered = long["reply"].get("engine")
            honest = (answered == "madlad"
                      or (answered == "demo"
                          and "madlad" in (long["reply"].get("message") or "")))
            if honest:
                ck.ok("a budget that runs out still names the fallback instead of answering wrongly")
            else:
                ck.fail("no silent degradation",
                        f"engine={answered!r} message={long['reply'].get('message')!r}")

        # The gate itself, on the real backend: refused, with an honest promise, and no text.
        holder = threading.Thread(target=lambda: _post(url, LONG_TEXT), daemon=True)
        holder.start()
        _wait_slot_busy(url)
        status, body, headers = _post(url, SHORT_TEXT)
        holder.join(180)
        if status == 429 and body.get("error") == "busy" and "text" not in body \
                and int(headers.get("Retry-After", "0") or 0) >= 1:
            ck.ok("gate refuses a second decode on the real checkpoint")
        else:
            ck.fail("busy refusal shape on the real backend",
                    f"expected 429 busy + Retry-After and no text, got {status} {body!r} "
                    f"{headers!r}")

        eng.shutdown()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        store.cleanup()

    # Only reached when nothing raised: a service we terminated and reaped is a fact about the
    # child process, not about the gate.
    ck.ok("gated sidecar process reaped")
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
