#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""The Rust engine against a real, gated MADLAD service: a busy refusal may cost seconds, never a translation.

`tests/test_sidecar_e2e.py` proves the chain works on the mock backend. `engine/sidecar.rs`'s
own tests prove the wait logic on scripted sockets. What neither can prove is the claim this
file exists for, because it needs the real decoder and the real timing:

    caller -> Rust `madlad` engine -> HTTP -> MADLAD-400 with `--decode-slots 1`

Three modes, sharing one harness so neither re-implements the other's assumptions:

    python tests/test_sidecar_busy_e2e.py
        pass/fail. Both episodes put the Rust call in front of a service whose single slot is
        demonstrably busy and differ in exactly one setting -- the shipped wait budget versus
        `*_SIDECAR_RETRY_MS=0`, the behaviour this repo shipped before the wait existed. With
        the budget the checkpoint answers; without it the pipeline answers from demo, i.e. the
        translation was *lost*. Skips with a named reason when there is no checkpoint, which is
        what CI does.

    python tests/test_sidecar_busy_e2e.py --sweep --out-json docs/evidence/busy-budget-sweep-madlad.json
        pricing. The same episode across a grid of (how long the slot is held) x (how big the
        budget is), so "should the gate ship on, and with what budget" is answered by a table
        instead of by a guess. Needs the checkpoint; fails loudly when it is missing, because an
        empty table is how a tool pretends it measured nothing.

    python tests/test_sidecar_busy_e2e.py --queue --out-json docs/evidence/queue-wait-madlad.json
        the other mechanism. `--queue-wait-ms` makes the *service* hold the caller on a
        semaphore instead of refusing it, which waits differently: a retrying caller can only
        sleep whole `Retry-After` steps, while a queued caller is woken the moment the slot
        frees. This prices that on the rung the budget grid could not reach -- a decode as long
        as the promise it would have to wait out.

Four things this harness had to learn the hard way:

1. **Nothing may compete for the slot.** The first draft probed `/translate` while the engine
   call was in flight to "prove" refusals -- and the probes won the slot the engine needed on its
   retry, so the shipped-budget episode failed against a gate the suite itself had made worse.
   Premises are read from `/health` (which takes no slot) plus the child's own `requests_served`
   delta; the one probe that does ask for a slot is used only while the slot is provably busy,
   so it is refused and therefore costs nothing.
2. **The service's counters, not the answer text, say which instance answered.** Two MADLAD
   processes return byte-identical translations. The first draft also forgot to pin
   `LT_MADLAD_URL`, so the engine quietly used the developer's ungated sidecar on 11436 and every
   assertion passed against a service that had no gate. `engine_served` is the guard against that
   class, and `tests/env_pin_coverage.rs` is the gate for the knob (defect D45).
3. **One assertion must not lean on another's output.** The no-budget episode used to compare its
   answer with the waiting episode's text; when the waiting episode also degraded, the reference
   *was* the demo string and a genuine loss became unrecognisable. Each episode now stands alone.
4. **The budget is not a duration, it is a number of promised waits.** `Retry-After` is the
   ceiling of the service's own p50, which drifts with whatever it served recently -- so every
   cell records the `Retry-After` and p50 it actually met, and `simulate()` re-derives what the
   shipped policy should have done from those. Prediction disagreeing with observation is a
   finding to report, not noise to smooth away.
"""
import json
import math
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

# The data-dir keys as the process found them, taken before anything in this file writes to
# os.environ. `default_model_dir()` reads `WONSLATE_DATA_DIR`, and every checkpoint publishes its
# own scratch data dir there -- so a *second* checkpoint would go looking for the model inside the
# directory the first one already deleted. Found the way these things are always found: the first
# service of a `--queue` run measured both of its cells and the second one reported "no MADLAD
# checkpoint under ...Temp\wonslate-busy-e2e-...", which is defect D36 wearing this file's own
# clothes -- the host environment, edited by the test, deciding what the test can do.
_ORIGINAL_DATA_ENV = {key: os.environ.get(key)
                      for key in ("LT_DATA_DIR", "WONSLATE_DATA_DIR")}

# The names of every check the pass/fail mode would run. Kept as data so a skipped run says
# *which* claims were not verified, instead of printing one line and looking like a smaller suite.
CHECKS = (
    "gate refuses a second decode on the real checkpoint",
    "the gated service proved which process it is",
    "the gated service is the one the engine dialed",
    "with the shipped budget the engine waits and the real checkpoint answers",
    "with no budget the same request loses its translation to demo",
    "two decodes never ran at once",
    "a budget that runs out still names the fallback instead of answering wrongly",
    "gated sidecar process reaped",
)

# The paired episodes hold the slot with this: measured ~2.2-2.4 s on CPU, i.e. shorter than the
# smallest `Retry-After` the service can promise (1 s) times a couple of retries, so a waiting
# caller is guaranteed to reach the slot inside the shipped budget.
SHORT_TEXT = "The sampler rate must match the reference track."

# A decode this long (~7-8 s measured) can outlast two promised waits, which is why the episode
# that uses it asserts only that a caller who gave up is told the truth.
LONG_TEXT = (
    "The reference track and the recording have to share one clock, because a sampler that "
    "drifts by even a fraction of a frame will place every transient later than the mix, and "
    "the listener hears a performance that no longer breathes with the vocal."
)

_POOL = (
    "The reference track and the recording have to share one clock.",
    "A drifting sampler places every transient later than the mix.",
    "The listener then hears a performance that no longer breathes with the vocal.",
    "Monitoring latency is a budget, not a feeling about the room.",
    "When the buffer is too small the transport stutters under load.",
    "Rendering the mix offline removes that pressure entirely.",
    "Timing data should be resampled to the session rate before analysis.",
    "A transient that lands between frames is still one event, not two.",
)


def occupancy_ladder():
    """Four occupiers of increasing decode cost, by word count. Named by length because the
    measured duration -- not the word count -- is what the table reports and buckets by."""
    out = []
    for words in (12, 28, 55, 110):
        parts, n = [], 0
        while n < words:
            parts.append(_POOL[len(parts) % len(_POOL)])
            n += len(parts[-1].split())
        out.append((words, " ".join(parts)))
    return out


BUDGETS_MS = (0, 4000, 8000, 12000, 20000, 30000)


def page_text() -> str:
    """The longest rung of the occupancy ladder (~27 s of decode, measured in
    `docs/evidence/busy-budget-sweep-madlad.json`). Reused rather than re-authored: the question
    being priced here is the one that table ended on -- a decode so long that no client budget
    can wait it out -- and it only stays the same question if it is the same workload."""
    return occupancy_ladder()[-1][1]


# (queue_wait_ms the service is started with, client busy_retry ms). The first two rows share one
# service and differ only in the budget, which is the paired half; the last two differ only in the
# queue. Nothing in here compares two settings that differ in two knobs.
QUEUE_CELLS = (
    (0, 0),
    (0, 12000),
    (20000, 0),
    (45000, 0),
)

QUEUE_CHECKS = (
    "queue=0 refuses at once: the caller loses the translation but learns immediately",
    "at page scale the shipped client budget behaves as its step model says and never answers wrongly",
    "an undersized queue costs its whole wait and still loses the translation",
    "a queue longer than the decode in front of it returns the real translation",
    "a queued caller wakes when the slot frees, it is not rounded up to a whole step",
    "every cell dialed the gated service and nothing ran two decodes at once",
    "gated sidecar process reaped",
)


class Precondition(Exception):
    """Something the host must provide is absent. The suite turns it into named skips; the
    sweep turns it into a loud failure, because a measurement that silently did nothing is the
    one outcome no reader can distinguish from a clean result."""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, text: str, timeout: float = 300.0):
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


def _wait_slot_busy(url: str, timeout: float = 20.0):
    """Block until the service says a decode is running. This is the premise of everything
    below, and it is read off `decodes_in_flight` rather than inferred from a sleep."""
    end = time.time() + timeout
    while time.time() < end:
        if _health(url).get("decodes_in_flight", 0) >= 1:
            return True
        time.sleep(0.02)
    return False


def _wait_slot_idle(url: str, timeout: float = 300.0) -> bool:
    """Between cells: a sweep whose next measurement started while the previous decode was still
    running would be measuring a queue, not a budget."""
    end = time.time() + timeout
    while time.time() < end:
        if _health(url).get("decodes_in_flight", 0) == 0:
            return True
        time.sleep(0.1)
    return False


def _retry_after_of(status: int, body: dict, headers: dict):
    """The seconds the service promised, or None when this was not a busy refusal. Guarded on
    the body as well as the status because that is what the client's own rule requires, and a
    sweep that counted a different 429 would be pricing the wrong thing."""
    if status != 429 or body.get("error") != "busy":
        return None
    raw = headers.get("Retry-After") or body.get("Retry-After") or "0"
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _restore_data_env(saved) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def locate_model_dir() -> pathlib.Path:
    """Where the MADLAD checkpoint lives, resolved against the environment the process started
    with rather than the scratch directories previous checkpoints of this run have published."""
    sys.path.insert(0, str(ROOT))
    from sidecar.ct2_sidecar import default_model_dir

    current = {key: os.environ.get(key) for key in _ORIGINAL_DATA_ENV}
    try:
        _restore_data_env(_ORIGINAL_DATA_ENV)
        return default_model_dir().parent / "madlad"
    finally:
        _restore_data_env(current)


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


def peek_retry_after(url: str, probe_text: str):
    """Ask the busy service what it would tell a caller to wait, without taking a slot.

    Legal only while a decode is provably in flight: a refused request starts no work, consumes
    no permit and is charged to no counter. If it is served instead, the slot was already free
    and this cell is invalid -- that is reported, not papered over.
    """
    status, body, headers = _post(url, probe_text, timeout=120)
    return {
        "status": status,
        "retry_after_s": _retry_after_of(status, body, headers),
        "served_instead": status == 200,
        "decodes_in_flight": body.get("decodes_in_flight"),
    }


def simulate(occupancy_s: float, arrival_s: float, retry_after_s, budget_ms: int) -> dict:
    """What the shipped policy must do, as arithmetic: a refused caller sleeps `Retry-After`
    seconds only when the *whole* step still fits what is left of the budget.

    Kept separate from the measurement so the sweep can say where model and observation diverge.
    `arrival_s` is when the caller reached a busy service, `occupancy_s` how long the decode in
    front of it ran, both measured -- nothing here is a guess about the other.
    """
    budget_s = budget_ms / 1000.0
    if not retry_after_s or retry_after_s <= 0:
        return {"predicted_recovered": False, "steps": 0, "reached_s": None}
    steps = int(budget_s // retry_after_s)
    for k in range(1, steps + 1):
        moment = arrival_s + k * retry_after_s
        if moment >= occupancy_s:
            return {"predicted_recovered": True, "steps": k, "reached_s": round(moment, 2)}
    return {"predicted_recovered": False, "steps": steps, "reached_s": None}


def simulate_queue(occupancy_s: float, arrival_s: float, wait_ms: int) -> dict:
    """What the service-side queue must do, as arithmetic -- and the point of writing it down is
    the thing that is *absent* from it.

    A queued caller sleeps on the service's semaphore and is woken when the slot is released, so
    its reachable moment is the instant the decode in front of it ends, with no rounding. The
    retrying caller above can only land on multiples of the promised step. At page scale those
    two models give opposite answers to the same question, which is exactly what `--queue` is for.
    """
    wait_s = wait_ms / 1000.0
    remaining = max(0.0, occupancy_s - arrival_s)
    if remaining <= wait_s:
        return {"predicted_recovered": True, "predicted_wake_s": round(remaining, 2),
                "predicted_refused_after_s": None}
    return {"predicted_recovered": False, "predicted_wake_s": None,
            "predicted_refused_after_s": round(wait_s, 2)}


class GatedCheckpoint:
    """One ephemeral MADLAD service started with a single decode slot, plus the shipped Rust
    client pointed at it.

    `queue_wait_ms` is a *start-up* setting, so a comparison across queue sizes means a new
    service per size -- which is also why every wait below is normalised by that service's own
    idle baseline instead of by another row's seconds (the lesson in `analyze()`'s docstring).

    The environment pinning lives here, in one place, on purpose: the first draft of this file
    pinned some slots and missed `LT_MADLAD_URL`, and a second copy of that block in a sweep
    would eventually be fixed in only one of them.
    """

    def __init__(self, queue_wait_ms: int = 0):
        self.queue_wait_ms = int(queue_wait_ms)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.store = tempfile.TemporaryDirectory(prefix="wonslate-busy-e2e-")
        self.proc = None
        self.eng = None
        self.up = False
        # Set once the service is proven ours: which port we drew and which pid answered. Read
        # by the assertions so a failure can name the instance instead of guessing about it.
        self.started_port = self.started_pid = None

    def __enter__(self):
        if self.up:
            # Reentrant on purpose: callers enter once to surface a `Precondition` outside the
            # `with`, then use the object as a context manager for the teardown. Entering twice
            # must not mean a second 2.9 GB checkpoint.
            return self
        self.up = True
        model_dir = locate_model_dir()
        if not model_dir.is_dir():
            raise Precondition(f"no MADLAD checkpoint under {model_dir}")

        env = os.environ.copy()
        for key in ("LT_ARGOS_URL", "WONSLATE_ARGOS_URL", "LT_OLLAMA_URL", "WONSLATE_OLLAMA_URL",
                    "OLLAMA_URL"):
            env[key] = DEAD
        env["WONSLATE_DATA_DIR"] = str(pathlib.Path(self.store.name) / "sidecar-data")

        # Start the gated service and prove the process answering is the one we started.
        #
        # `_free_port()` reserves a port and releases it; the child binds it afterwards, so in
        # between another process can take it. Health would then be served by someone else,
        # every counter read from the wrong instance, and the episode's assertions would fail in
        # a way that reads like engine misbehaviour (defect D72: this suite went red inside the
        # full gate and green on its own, which is what a port race looks like). Re-drawing the
        # port is the fix; silently trusting the first answer is what broke.
        reason = "gated service never answered /health"
        for _attempt in range(3):
            self.port = _free_port()
            self.url = f"http://127.0.0.1:{self.port}"
            cmd = [sys.executable, str(SIDECAR), "--backend", "madlad", "--port", str(self.port),
                   "--model-dir", str(model_dir), "--decode-slots", "1"]
            if self.queue_wait_ms:
                cmd += ["--queue-wait-ms", str(self.queue_wait_ms)]
            # Added only when non-zero, so the `queue_wait_ms=0` service is started with byte-identical
            # arguments to the one the budget grid measured: "off" has to be provably the old command
            # line, not a new one that happens to behave like the old.
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=env)
            if not _wait_health(self.url):
                reason = "gated service on port %d never answered /health" % self.port
                self._reap()
                continue
            answered_by = _health(self.url).get("pid")
            if not answered_by:
                # No pid published means no proof, and assuming "it's ours" is the mistake that
                # started this. Older sidecars do answer without it, so say which case we are in.
                reason = ("port %d answered /health without a pid, so the instance cannot be "
                          "identified" % self.port)
                self._reap()
                continue
            if answered_by == self.proc.pid:
                break
            reason = ("port %d was answered by pid %s while this episode started pid %s"
                      % (self.port, answered_by, self.proc.pid))
            self._reap()
        else:
            raise Precondition(reason)
        self.started_port, self.started_pid = self.port, answered_by

        # Pay the checkpoint load inside a call that is not part of any measurement.
        with urllib.request.urlopen(
                urllib.request.Request(self.url.rstrip("/") + "/warmup",
                                       data=json.dumps({"source": "en", "target": "zh"}).encode(),
                                       headers={"Content-Type": "application/json"}),
                timeout=300) as warm:
            warm.read()

        try:
            dll = ffi._find_dll()
        except FileNotFoundError as e:
            raise Precondition(f"engine library unavailable: {e}")
        self.eng = ffi.Engine(dll)
        os.environ["LT_DATA_DIR"] = str(pathlib.Path(self.store.name) / "data")
        os.environ["WONSLATE_DATA_DIR"] = os.environ["LT_DATA_DIR"]
        for name in ("LT_ARGOS_URL", "WONSLATE_ARGOS_URL", "LT_OLLAMA_URL",
                     "WONSLATE_OLLAMA_URL"):
            os.environ[name] = DEAD
        os.environ["LT_MADLAD_URL"] = os.environ["WONSLATE_MADLAD_URL"] = self.url
        os.environ["LT_SIDECAR_TIMEOUT_MS"] = os.environ["WONSLATE_SIDECAR_TIMEOUT_MS"] = "120000"
        self.eng.init("{}")
        return self

    def _reap(self):
        """Stop a service we are about to abandon, so a retry does not leave it holding a port."""
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            self.proc = None

    def __exit__(self, *_exc):
        if self.eng is not None:
            try:
                self.eng.shutdown()
            except Exception:
                pass
        self._reap()
        self.store.cleanup()
        return False

    def baseline_short(self):
        """One idle decode of the text the measured call uses, taken on this service.

        The queue's whole claim is that it wakes a caller instead of rounding it up, and that is a
        statement about *excess* time. Excess needs the service's own decode duration: carrying a
        number over from another service, or another run, is the cross-setting comparison this
        repo has already been burned by.
        """
        if not _wait_slot_idle(self.url):
            raise Precondition("the slot never drained before the baseline could be taken")
        started = time.perf_counter()
        status, _body, _headers = _post(self.url, SHORT_TEXT)
        took = round(time.perf_counter() - started, 2)
        print("  [baseline] queue_wait_ms=%s idle short decode=%.2fs status=%s" % (
            self.queue_wait_ms, took, status), flush=True)
        return {"seconds": took, "status": status, "served": status == 200}

    def episode(self, retry_ms: str, occupy_text: str, peek: bool = False):
        """Hold the single slot with exactly one decode of `occupy_text`, then ask the Rust
        engine for a translation, measuring enough to prove the premise afterwards."""
        for name in ("LT_SIDECAR_RETRY_MS", "WONSLATE_SIDECAR_RETRY_MS"):
            os.environ[name] = retry_ms
        measured = {"occupier_s": []}

        def hold():
            started = time.perf_counter()
            measured["hold_status"], measured["hold_body"], _ = _post(self.url, occupy_text)
            measured["occupier_s"].append(round(time.perf_counter() - started, 2))

        occupier = threading.Thread(target=hold, daemon=True)
        hold_started = time.perf_counter()
        occupier.start()
        if not _wait_slot_busy(self.url):
            occupier.join(600)
            return None
        # One sample, not four: `decodes_in_flight`, the latency percentiles and the served tally
        # are read together so the row describes one instant. Three separate reads could straddle
        # the moment the occupier finished, which would make the promise and the decode it describes
        # come from different service states.
        at_arrival = _health(self.url)
        busy_before = at_arrival.get("decodes_in_flight")
        p50_before = at_arrival.get("last_latency_p50")
        p99_before = at_arrival.get("last_latency_p99")
        # The service's own tally of *served* requests. A busy refusal is charged to nothing, so
        # the delta says how many times this ephemeral port did work -- which is what
        # distinguishes "my request was refused then retried" from "my request never met the
        # gate", and from "my request went to some other service".
        served_before = at_arrival.get("requests_served", 0)
        promised = peek_retry_after(self.url, SHORT_TEXT) if peek else None
        watcher = Watcher(self.url)
        watcher.start()
        started = time.perf_counter()
        # When the caller reached the gate, measured from the instant the decode in front of it
        # began. `simulate()` needs the same origin the occupier's duration is measured from.
        arrival_s = round(started - hold_started, 2)
        reply = self.eng.translate_full({
            "engine_id": "madlad", "input": SHORT_TEXT,
            "source_lang": "en", "target_lang": "zh",
            "mode": "full", "use_tm": False,
        })
        elapsed = time.perf_counter() - started
        occupier.join(600)
        watcher.stop_it.set()
        watcher.join(60)
        served_delta = _health(self.url).get("requests_served", 0) - served_before
        out = {
            "reply": reply,
            "retry_ms": int(retry_ms),
            "elapsed_s": round(elapsed, 2),
            "arrival_s": arrival_s,
            "occupier_s": measured["occupier_s"][0] if measured["occupier_s"] else None,
            "busy_before": busy_before,
            "p50_before_s": p50_before,
            "p99_before_s": p99_before,
            "peek": promised,
            "served_delta": served_delta,
            # Only the occupier, the engine and (when peeking was served) one probe dial this
            # port, so anything the service did that is neither belongs to nobody -- and the
            # probe case is marked invalid rather than counted.
            "engine_served": served_delta - len(measured["occupier_s"])
                             - (1 if (promised or {}).get("served_instead") else 0),
            "peak_in_flight": watcher.peak_in_flight,
            "recovered": reply.get("engine") == "madlad",
        }
        out["invalid"] = bool((promised or {}).get("served_instead")) or out["engine_served"] not in (0, 1)
        print("  [measure] retry_ms=%s engine=%r elapsed=%.2fs occupancy_s=%s arrival_s=%.2f "
              "p50=%s p99=%s RA=%s served_delta=%s engine_served=%s peak=%s%s"
              % (retry_ms, reply.get("engine"), elapsed, out["occupier_s"], arrival_s, p50_before,
                 p99_before, (promised or {}).get("retry_after_s"), served_delta,
                 out["engine_served"], watcher.peak_in_flight,
                 "  INVALID(premise)" if out["invalid"] else ""),
              flush=True)
        return out


def run_suite() -> int:
    ck = ffi.Checker()
    lab = GatedCheckpoint()
    try:
        lab.__enter__()
    except Precondition as reason:
        for name in CHECKS:
            ck.skip(name, str(reason))
        lab.__exit__()
        return ck.summary()

    with lab:
        # The port is drawn, released, and then bound by the child, so "health answered" is not
        # yet evidence that it was *our* service answering. Assert the identity before anything
        # else measures it -- this is the guard that D72 showed was missing.
        ck.ok("the gated service proved which process it is")

        # --- episode 1: the shipped budget, slot held for one short decode ----------------
        on = lab.episode("12000", SHORT_TEXT)
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
        if on["peak_in_flight"] <= 1:
            ck.ok("two decodes never ran at once")
        else:
            ck.fail("single-slot invariant",
                    f"health reported {on['peak_in_flight']} decodes in flight with "
                    "--decode-slots 1")

        madlad_out = (on["reply"].get("output") or "").strip()
        if (on["reply"].get("engine") == "madlad" and on["elapsed_s"] >= 1.0
                and any("\u4e00" <= ch <= "\u9fff" for ch in madlad_out)):
            ck.ok("with the shipped budget the engine waits and the real checkpoint answers")
        else:
            ck.fail("budget buys back the translation",
                    "expected madlad + CJK after waiting at least one Retry-After, got engine=%r "
                    "elapsed=%.2fs output=%r" % (on["reply"].get("engine"), on["elapsed_s"],
                                                 madlad_out))

        # --- episode 2: identical conditions, no wait budget ------------------------------
        off = lab.episode("0", SHORT_TEXT)
        if off is None:
            ck.skip("with no budget the same request loses its translation to demo",
                    "the service never reported a decode in flight")
        elif (off["busy_before"] and off["engine_served"] == 0
                and off["reply"].get("engine") == "demo"
                and "madlad" in (off["reply"].get("message") or "")):
            # Independent of episode 1 by design: it used to compare against that episode's
            # text, and the mutation sweep showed that when both degrade the reference is the
            # demo string, so "different from the real answer" became "different from itself".
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
        long = lab.episode("12000", LONG_TEXT)
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
        holder = threading.Thread(target=lambda: _post(lab.url, LONG_TEXT), daemon=True)
        holder.start()
        _wait_slot_busy(lab.url)
        status, body, headers = _post(lab.url, SHORT_TEXT)
        holder.join(600)
        if status == 429 and body.get("error") == "busy" and "text" not in body \
                and int(headers.get("Retry-After", "0") or 0) >= 1:
            ck.ok("gate refuses a second decode on the real checkpoint")
        else:
            ck.fail("busy refusal shape on the real backend",
                    f"expected 429 busy + Retry-After and no text, got {status} {body!r} "
                    f"{headers!r}")

    # Only reached when nothing raised: a service we terminated and reaped is a fact about the
    # child process, not about the gate.
    ck.ok("gated sidecar process reaped")
    return ck.summary()


def analyze(cells):
    """The decision numbers, computed next to the cells they come from.

    Kept in the tool rather than in a notebook so the evidence file and the figures quoted from
    it can be re-produced by anyone who runs the same command.
    """
    worst_ra = max([(c["peek"] or {}).get("retry_after_s") or 0 for c in cells], default=0)
    models = {}
    for label, pick in (("ra_as_peeked", lambda c: (c["peek"] or {}).get("retry_after_s")),
                        ("ra_as_worst_met_in_run", lambda c: worst_ra or None)):
        models[label] = sum(1 for c in cells
                            if simulate(c["occupier_s"] or 0.0, c["arrival_s"], pick(c),
                                        c["retry_ms"])["predicted_recovered"] != c["recovered"])
    per_budget = {}
    for budget in sorted({c["retry_ms"] for c in cells}):
        row = [c for c in cells if c["retry_ms"] == budget]
        per_budget[str(budget)] = {
            "recovered_up_to_s": max([c["occupier_s"] for c in row if c["recovered"]], default=None),
            "first_occupancy_lost_s": min([c["occupier_s"] for c in row if not c["recovered"]],
                                          default=None),
            "max_wait_s": max(c["elapsed_s"] for c in row),
        }
    silent_zero = sorted({c["retry_ms"] for c in cells
                          if 0 < c["retry_ms"] and c["elapsed_s"] < 0.05})
    formula_bad = []
    for c in cells:
        ra, p50 = (c["peek"] or {}).get("retry_after_s"), c["p50_before_s"]
        want = 1 if not p50 else max(1, int(math.ceil(p50)))
        if ra is not None and ra != want:
            formula_bad.append({"retry_ms": c["retry_ms"], "p50_s": p50, "promised": ra})
    return {
        "worst_retry_after_s": worst_ra,
        "retry_after_formula_violations": formula_bad,
        "model_disagreements": models,
        "per_budget": per_budget,
        "budgets_that_waited_zero_seconds": silent_zero,
        "reading": "a budget below one Retry-After step never sleeps at all, so it behaves like 0; "
                   "recovery is a whole number of steps of the *worst* promise met during the "
                   "wait, not of the one met on arrival.",
    }


def run_sweep(out_json) -> int:
    """Price the budget against the workload, rather than asserting a floor.

    Cells are (how long one decode holds the only slot) x (how long the caller may wait). The
    service's `Retry-After` is recorded per cell because it drifts with what the service last
    served -- which is the whole reason a fixed millisecond budget is hard to reason about.
    """
    cells = []
    lab = GatedCheckpoint()
    try:
        lab.__enter__()
    except Precondition as reason:
        print(f"[ABORT] cannot sweep: {reason}", flush=True)
        lab.__exit__()
        return 2

    ladder = occupancy_ladder()
    with lab:
        for retry_ms in BUDGETS_MS:
            for words, text in ladder:
                if not _wait_slot_idle(lab.url):
                    print("  [cell] ABORTED: the slot never drained between cells", flush=True)
                    return 2
                cell = lab.episode(str(retry_ms), text, peek=True)
                if cell is None:
                    print(f"  [cell] retry_ms={retry_ms} words={words}: never became busy", flush=True)
                    continue
                cell["words"] = words
                ra = (cell["peek"] or {}).get("retry_after_s")
                cell.update(simulate(cell["occupier_s"] or 0.0, cell["arrival_s"], ra, retry_ms))
                cell["predicted_vs_observed"] = (cell["predicted_recovered"] == cell["recovered"])
                # Keep the verdict, not the payload: the translated sentences would dominate the
                # file and prove nothing that `engine` / `message` do not already say.
                cell["answer"] = {"engine": cell["reply"].get("engine"),
                                   "source": cell["reply"].get("source"),
                                   "chars": len(cell["reply"].get("output") or ""),
                                   "message": cell["reply"].get("message")}
                cell.pop("reply")
                cells.append(cell)

    mismatches = [c for c in cells if not c["predicted_vs_observed"]]
    invalid = [c for c in cells if c["invalid"]]
    _print_table(cells, mismatches, invalid)
    analysis = analyze(cells)
    print(f"    worst Retry-After met: {analysis['worst_retry_after_s']} s; "
          f"model disagreements: {analysis['model_disagreements']}")
    print(f"    budgets that waited 0.00 s although non-zero: "
          f"{analysis['budgets_that_waited_zero_seconds']}")
    report = {
        "what": "how long a caller must be allowed to wait behind a busy refusal, measured on the "
                "real MADLAD checkpoint",
        "how": "python tests/test_sidecar_busy_e2e.py --sweep --out-json "
               "docs/evidence/busy-budget-sweep-madlad.json",
        "policy_under_test": "engine/sidecar.rs: waits only a 429 whose body says error=busy; a "
                             "sleep starts only if the whole Retry-After still fits the budget; a "
                             "refused request is charged to no counter",
        "preconditions": {
            "backend": "madlad en->zh, real checkpoint, --decode-slots 1, own ephemeral port",
            "client": "the shipped cdylib across FFI, engine_id=madlad, use_tm=False, identical "
                      "input text in every cell",
            "ambient_slots": "LT_/WONSLATE_ARGOS_URL, _OLLAMA_URL pinned dead and "
                             "LT_/WONSLATE_MADLAD_URL pinned to this child, so a developer's "
                             "long-lived sidecars cannot answer instead",
            "engine_per_request_timeout_ms": 120000,
        },
        "retry_after_is_load_dependent": sorted({(c["peek"] or {}).get("retry_after_s") for c in cells}),
        "cells": cells,
        "model": "simulate(): reachable moments are arrival + k*Retry-After for whole k with "
                 "k*Retry-After <= budget; recovery needs one such moment at or after the "
                 "occupier's measured duration",
        "predicted_vs_observed_mismatches": mismatches,
        "invalid_cells": invalid,
        "analysis": analysis,
        "what_this_does_not_show": [
            "one caller only: with several waiting callers the slot is shared by whoever's retry "
            "lands first, so a bigger budget does not buy the same thing per caller",
            "server-side queueing (--queue-wait-ms), which is a different mechanism with a "
            "different elapsed profile",
            "argos: only the madlad slot was measured, because that is the slot whose decodes are "
            "seconds long",
            "what the desktop UI should tolerate: this table says how long waiting recovers an "
            "answer, not how long a person will wait for one",
        ],
    }
    if out_json:
        path = pathlib.Path(out_json)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written as bytes: a text-mode write on Windows emits CRLF, so re-running the sweep would
        # show a whole-file diff against its own committed evidence -- and an evidence file that
        # cannot be reproduced byte for byte is a citation nobody can check. (Byte writing also
        # avoids depending on `Path.write_text(newline=...)`, which only exists from 3.10.)
        path.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        # No relative_to(): the caller may point this anywhere on disk, including outside the
        # repo, and a measurement that crashes *after* writing its own file makes the evidence
        # look untrustworthy for a reason that has nothing to do with the measurement.
        print(f"  [out] {path}: {len(cells)} cells", flush=True)
    return 0


def _print_table(cells, mismatches, invalid) -> None:
    ladder = [w for w, _ in occupancy_ladder()]
    print("")
    print("  occupancy(words)  " + "  ".join(f"{b:>7} ms" for b in BUDGETS_MS))
    for words in ladder:
        row = []
        for budget in BUDGETS_MS:
            got = [c for c in cells if c["words"] == words and c["retry_ms"] == budget]
            if not got:
                row.append(f"{'-':>9}")
                continue
            c = got[0]
            mark = "R" if c["recovered"] else "L"
            if not c["predicted_vs_observed"]:
                mark += "?"
            row.append(f"{mark + '(' + str(int(c['elapsed_s'])) + ')':>9}")
        occ = [c["occupier_s"] for c in cells if c["words"] == words and c["occupier_s"]]
        print(f"  {words:>6} w (~{max(occ):.1f}s)  " + "  ".join(row))
    print("    R = the checkpoint answered, L = fell back to demo, (n) = seconds the caller spent,")
    print("    ? = our model of the policy predicted the opposite, which is a finding")
    ras = sorted({(c["peek"] or {}).get("retry_after_s") for c in cells} - {None})
    print(f"    Retry-After met across the grid: {ras} s  (it follows the service's own p50, so it moves)")
    print(f"    invalid cells: {len(invalid)}   model disagreements: {len(mismatches)}")


def _answers_honestly(answer) -> bool:
    """Either the real checkpoint answered, or the caller was told the fallback it got.

    The invariant every mode of this file shares, and the only one that must hold no matter what
    the machine's timings turn out to be: a wait that fails may cost seconds or nothing at all,
    but it may not quietly hand back a different engine's text.
    """
    return (answer["engine"] == "madlad"
            or (answer["engine"] == "demo" and "madlad" in (answer["message"] or "")))


def _queue_agreements(cell) -> dict:
    """Both wait models against what this caller actually experienced.

    A cell that contradicts one model is a finding about that model. A cell that contradicts both
    is a finding about the harness, and only shows up if both are computed -- which is why the
    client model is evaluated here even on a service whose queue is the thing under test.
    """
    occupancy = cell["occupier_s"] or 0.0
    out = {}
    queue = simulate_queue(occupancy, cell["arrival_s"], cell["queue_wait_ms"])
    out["queue_model"] = queue
    out["queue_model_agrees"] = queue["predicted_recovered"] == cell["recovered"]
    if cell["retry_ms"]:
        client = simulate(occupancy, cell["arrival_s"],
                          (cell["peek"] or {}).get("retry_after_s"), cell["retry_ms"])
        out["client_model"] = client
        out["client_model_agrees"] = client["predicted_recovered"] == cell["recovered"]
    return out


def run_queue(out_json, waits=None) -> int:
    """Price `--queue-wait-ms` on the decode that no client budget can wait out.

    `--sweep` asks how long a caller may keep retrying; this asks how long a service may keep a
    caller, and the two mechanisms are not two sizes of the same thing. A retrying caller may
    only sleep a whole promised step, and the step is the ceiling of the service's p50 -- so on
    a page-scale decode the price of one try rises to roughly the duration it is waiting for,
    and buys it rounded up. A queued caller sleeps on the semaphore and is woken by the release.

    One variable per comparison. The three queue sizes run with the client budget pinned to `0`;
    the fourth cell shares the `queue=0` service with the first so its only difference is the
    budget. Absolute seconds are never compared across services -- each wait is read against that
    service's own idle baseline, taken here and stored in the evidence.
    """
    plan = [c for c in QUEUE_CELLS if waits is None or c[0] in waits]
    by_wait = {}
    for wait_ms, budget_ms in plan:
        by_wait.setdefault(wait_ms, []).append(budget_ms)

    ck = ffi.Checker()
    cells = []
    baselines = {}
    for wait_ms in sorted(by_wait):
        lab = GatedCheckpoint(queue_wait_ms=wait_ms)
        try:
            lab.__enter__()
        except Precondition as reason:
            # Exit code 2, not a green summary: a measurement interrupted halfway looks exactly
            # like a measurement that found nothing to complain about, and the cells already taken
            # would be quietly thrown away behind a `passed 0` line.
            print(f"[ABORT] cannot price the queue: {reason}", flush=True)
            for name in QUEUE_CHECKS:
                ck.skip(name, str(reason))
            lab.__exit__()
            print("    (a queue measurement that could not run is reported as a failure, not as "
                  "a skip: nothing below would have been measured)", flush=True)
            return 2
        with lab:
            try:
                baselines[wait_ms] = lab.baseline_short()
            except Precondition as reason:
                print(f"[ABORT] no baseline on queue_wait_ms={wait_ms}: {reason}", flush=True)
                return 2
            for budget_ms in by_wait[wait_ms]:
                if not _wait_slot_idle(lab.url):
                    print("  [cell] ABORTED: the slot never drained between cells", flush=True)
                    return 2
                # Peeking is legal only where a refusal is instant and unconditional. On a service
                # that queues, the probe joins the queue and can be served before the caller under
                # test -- the self-defeat the first draft of this file wrote itself.
                cell = lab.episode(str(budget_ms), page_text(), peek=(wait_ms == 0))
                if cell is None:
                    print(f"  [cell] wait={wait_ms} budget={budget_ms}: the page decode never "
                          f"became visible, so nothing was queued behind it", flush=True)
                    continue
                cell["queue_wait_ms"] = wait_ms
                cell["baseline_s"] = baselines[wait_ms]["seconds"]
                cell.update(_queue_agreements(cell))
                cell["wake_penalty_s"] = round(
                    cell["arrival_s"] + cell["elapsed_s"] - (cell["occupier_s"] or 0.0)
                    - cell["baseline_s"], 2)
                cell["answer"] = {"engine": cell["reply"].get("engine"),
                                  "source": cell["reply"].get("source"),
                                  "chars": len(cell["reply"].get("output") or ""),
                                  "message": cell["reply"].get("message"),
                                  "cjk": any("\u4e00" <= ch <= "\u9fff"
                                             for ch in (cell["reply"].get("output") or ""))}
                cell.pop("reply")
                cells.append(cell)

    if not cells:
        print("[ABORT] no cell was measured: a queue table with no rows is not a result",
              flush=True)
        return 2

    def pick(wait_ms, budget_ms):
        return next((c for c in cells if c["queue_wait_ms"] == wait_ms
                     and c["retry_ms"] == budget_ms), None)

    # --- 1: the shape a refusal has to keep ------------------------------------------------
    label, c = QUEUE_CHECKS[0], pick(0, 0)
    if c is None:
        ck.skip(label, "no cell ran with --queue-wait-ms 0 (restricted by --waits)")
    elif c["answer"]["engine"] == "demo" and c["elapsed_s"] < 2.0:
        ck.ok(label)
    else:
        ck.fail("queue=0 refuses at once",
                "with no queue and no budget the caller should be refused immediately and sent to "
                "demo, got engine=%r after %.2fs" % (c["answer"]["engine"], c["elapsed_s"]))

    # --- 2: the client mechanism, on the same rung, in the same session ----------------------
    label, c = QUEUE_CHECKS[1], pick(0, 12000)
    if c is None:
        ck.skip(label, "no cell ran with --queue-wait-ms 0 (restricted by --waits)")
    elif not c.get("client_model_agrees", True):
        ck.fail("the client step model predicts this rung",
                "budget=%d ms, Retry-After met=%s s, occupancy=%s s: the model says recovered=%s "
                "but the caller got engine=%r -- either the model or the measurement is wrong"
                % (c["retry_ms"], (c["peek"] or {}).get("retry_after_s"), c["occupier_s"],
                   c["client_model"]["predicted_recovered"], c["answer"]["engine"]))
    elif not _answers_honestly(c["answer"]):
        ck.fail("no silent degradation", "engine=%r message=%r" % (c["answer"]["engine"],
                                                                   c["answer"]["message"]))
    else:
        ck.ok(label)

    # --- 3 and 4: the queue, too small and big enough ---------------------------------------
    for index, wait_ms in ((2, 20000), (3, 45000)):
        label, c = QUEUE_CHECKS[index], pick(wait_ms, 0)
        if c is None:
            ck.skip(label, f"no cell ran with --queue-wait-ms {wait_ms} (restricted by --waits)")
            continue
        remaining = (c["occupier_s"] or 0.0) - c["arrival_s"]
        if wait_ms / 1000.0 < remaining:
            if index == 2:
                if c["answer"]["engine"] == "demo" and c["elapsed_s"] >= 0.8 * wait_ms / 1000.0:
                    ck.ok(label)
                else:
                    ck.fail("an undersized queue costs its whole wait",
                            "the decode in front took %.2fs but the queue was %.1fs, so the "
                            "caller should lose only after waiting it out: engine=%r after %.2fs"
                            % (remaining, wait_ms / 1000.0, c["answer"]["engine"],
                               c["elapsed_s"]))
            else:
                ck.skip(label, "this machine decoded the page rung in %.2fs, longer than the "
                               "%.1fs queue under test, so recovery cannot be asserted here"
                        % (remaining, wait_ms / 1000.0))
        elif index == 3:
            if c["answer"]["engine"] == "madlad" and c["answer"]["cjk"]:
                ck.ok(label)
            else:
                ck.fail("a queue longer than the decode returns the real translation",
                        "the decode in front took %.2fs and the queue allowed %.1fs, so the "
                        "checkpoint should have answered: engine=%r output=%d chars"
                        % (remaining, wait_ms / 1000.0, c["answer"]["engine"],
                           c["answer"]["chars"]))
        else:
            ck.skip(QUEUE_CHECKS[2], "the decode in front (%.2fs) fit inside the %.1fs queue, so "
                                    "there was nothing undersized to price" % (remaining,
                                                                               wait_ms / 1000.0))

    # --- 5: the claim that makes the queue a different mechanism ----------------------------
    label = QUEUE_CHECKS[4]
    woke = [c for c in cells if c["recovered"] and c["queue_wait_ms"] > 0]
    if not woke:
        ck.skip(label, "no queued cell recovered, so there was no wake to time")
    else:
        worst = max(woke, key=lambda c: c["wake_penalty_s"])
        limit = max(2.0, 0.5 * (worst["baseline_s"] or 1.0))
        if worst["wake_penalty_s"] <= limit:
            ck.ok(label)
        else:
            ck.fail("the queue wakes rather than rounds up",
                    "a queued caller took %.2fs over the decode in front plus its own decode "
                    "(limit %.2fs) -- that excess is what a whole Retry-After step would have "
                    "costed, not what the semaphore costs"
                    % (worst["wake_penalty_s"], limit))

    # --- 6: premises, every cell ------------------------------------------------------------
    label = QUEUE_CHECKS[5]
    bad = [c for c in cells if c["invalid"] or c["peak_in_flight"] > 1 or not c["busy_before"]
           or c["engine_served"] not in (0, 1)]
    if bad:
        ck.fail("the premise of every cell",
                "%d cell(s) where the gated service did not demonstrably hold one decode while "
                "the engine dialed it: %s" % (len(bad), [
                    {"wait": c["queue_wait_ms"], "budget": c["retry_ms"], "served": c["engine_served"],
                     "in_flight": c["busy_before"], "peak": c["peak_in_flight"],
                     "peek_served": bool((c["peek"] or {}).get("served_instead"))}
                    for c in bad]))
    else:
        ck.ok(label)

    _print_queue(cells, baselines)
    report = {
        "what": "how long a busy MADLAD service may hold a caller on its semaphore, measured on "
                "the decode length a client retry budget cannot wait out",
        "how": "python tests/test_sidecar_busy_e2e.py --queue --out-json "
               "docs/evidence/queue-wait-madlad.json",
        "policy_under_test": "sidecar/ct2_sidecar.py acquire_decode(): with --queue-wait-ms the "
                             "caller blocks on the slot and is refused only when its wait runs "
                             "out; engine/sidecar.rs: without it the caller is refused at once and "
                             "may only sleep whole Retry-After steps",
        "one_variable": "queue sizes share a client budget of 0; the two queue=0 cells share one "
                        "service and differ only in the budget. Waits are read against each "
                        "service's own idle baseline in `per_service`, never against another "
                        "service's seconds",
        "per_service": {str(w): b for w, b in baselines.items()},
        "cells": cells,
        "model": "simulate_queue(): a queued caller is served at the instant the decode in front "
                 "of it ends, with no rounding, and is refused at exactly its own wait; "
                 "simulate(): a retrying caller can only arrive at whole multiples of the "
                 "Retry-After it was promised",
        "model_disagreements": {
            "queue_model": sum(1 for c in cells if not c["queue_model_agrees"]),
            "client_model": sum(1 for c in cells
                                if "client_model_agrees" in c and not c["client_model_agrees"]),
        },
        "invalid_cells": [c for c in cells if c["invalid"]],
        "analysis": _queue_analysis(cells),
        "what_this_does_not_show": [
            "more than one waiting caller: every cell here has a single caller behind the decode "
            "it waits for, and the queue's shape with several waiters is a different question "
            "(BoundedSemaphore does not promise first-come-first-served, so who gets refused is "
            "not settled by arrival order -- `diag_sidecar_batch.py queue` is what measures that)",
            "the argos slot: madlad is the slot whose decodes are seconds long",
            "whether a desktop user should wait this long: that is a product decision, and the "
            "numbers here say what a queue costs, not what a person pays",
            "any absolute latency to certify: one machine, one checkpoint, decodes the local model "
            "is good at"
        ],
    }
    if out_json:
        path = pathlib.Path(out_json)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        print(f"  [out] {path}: {len(cells)} cells", flush=True)
    ck.ok(QUEUE_CHECKS[6])
    return ck.summary()


def _queue_analysis(cells):
    """The decision numbers, computed from the cells in this file rather than retyped elsewhere."""
    recovered = [c for c in cells if c["recovered"] and c["queue_wait_ms"] > 0]
    lost = [c for c in cells if not c["recovered"] and c["queue_wait_ms"] > 0]
    page = [c for c in cells if c["queue_wait_ms"] == 0 and c.get("peek")]
    return {
        "smallest_queue_that_recovered_ms": min([c["queue_wait_ms"] for c in recovered],
                                                default=None),
        "queues_that_lost_ms": sorted({c["queue_wait_ms"] for c in lost}),
        "seconds_a_refused_queue_caller_spent": {str(c["queue_wait_ms"]): c["elapsed_s"]
                                                 for c in lost},
        "wake_penalty_s_of_recovered_callers": {str(c["queue_wait_ms"]): c["wake_penalty_s"]
                                                for c in recovered},
        "retry_after_promised_at_page_scale_s": sorted({(c["peek"] or {}).get("retry_after_s")
                                                        for c in page} - {None}),
        "what_the_promised_step_describes": [
            # The row that matters for choosing a mechanism: the decode the caller is blocked
            # behind is the one *in flight*, while `Retry-After` is a percentile of the decodes
            # that already finished. On a uniform load those are the same number -- which is why
            # the budget grid read as "a step is a decode" -- and on a mixed load they are not.
            {"queue_wait_ms": c["queue_wait_ms"], "budget_ms": c["retry_ms"],
             "decode_actually_in_flight_s": c["occupier_s"],
             "p50_at_arrival_s": c["p50_before_s"], "p99_at_arrival_s": c["p99_before_s"],
             "retry_after_promised_s": (c["peek"] or {}).get("retry_after_s")} for c in cells],
        "reading": "a queue is priced by the decode in front of the caller, not by a step: it "
                   "recovers as soon as the slot frees, and if it is too short it spends its "
                   "whole wait before refusing. A refused caller at queue=0 spends nothing. The "
                   "retrying caller has no such guarantee: the number it is handed describes the "
                   "service's median, not the decode it is blocked behind.",
    }


def _print_queue(cells, baselines) -> None:
    print("")
    print("  queue   budget   decode in front   own decode   caller took   verdict")
    for c in cells:
        mark = "recovered" if c["recovered"] else "LOST"
        extra = ""
        if "client_model_agrees" in c:
            extra = "  (client model %s)" % ("agreed" if c["client_model_agrees"] else "DISAGREED")
        if not c["queue_model_agrees"]:
            extra += "  (queue model DISAGREED)"
        if c["invalid"]:
            extra += "  INVALID(premise)"
        print("  %6d %7d %16s %13s %12.2fs   %s%s" % (
            c["queue_wait_ms"], c["retry_ms"], c["occupier_s"],
            baselines.get(c["queue_wait_ms"], {}).get("seconds"), c["elapsed_s"], mark, extra),
            flush=True)
    analysis = _queue_analysis(cells)
    print("    Retry-After promised at this rung: %s s (a client budget has to buy one of these)"
          % analysis["retry_after_promised_at_page_scale_s"])
    print("    smallest queue that recovered: %s ms; queues that lost: %s; a refused queued "
          "caller spent %s s" % (analysis["smallest_queue_that_recovered_ms"],
                                 analysis["queues_that_lost_ms"],
                                 analysis["seconds_a_refused_queue_caller_spent"]))
    print("    wake penalty (over decode-in-front + own decode): %s s  <- the rounding a step "
          "would have added" % analysis["wake_penalty_s_of_recovered_callers"])


def main(argv) -> int:
    out = argv[argv.index("--out-json") + 1] if "--out-json" in argv else None
    if "--sweep" in argv:
        return run_sweep(out)
    if "--queue" in argv:
        waits = None
        if "--waits" in argv:
            waits = {int(raw) for raw in argv[argv.index("--waits") + 1].split(",") if raw.strip()}
        return run_queue(out, waits)
    return run_suite()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
