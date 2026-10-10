#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Wonslate real sidecar translation service (local CT2 / Argos backend).

Contract (strictly aligned with Rust engine/sidecar.rs, .NET SidecarManager,
tests/mock_sidecar_server.py):
    POST /translate   body {"text" | "texts":[...]}, "source", "target"[, "glossary":[{"src","tgt"}]]
                      -> 200 {"text": "<translation>"} | {"texts": ["<translation>", ...]}
                         A line break is never handed to these checkpoints as part of one unit:
                         input is split, each line translated, and the answer rejoined keeping
                         the caller's line count (a blob makes the decoder emit repeated tokens
                         with no source content in it - external item 007). "failed_lines" names
                         the positions that came back unanswered; blank lines are structure.
    GET  /health      -> 200 {"status":"ok","backend":"<mock|ct2>","requests_served",...,
                         "requests_until_exit":N|null}   (LIVENESS only)
                         requests_until_exit is null unless the process was started with
                         --max-requests; it never invents a countdown it will not honour.
    GET  /readyz      -> 200 {"status":"ready","backend",...} | 503 {"status":"warming","error":"not_ready","reason":...}
                         501 {"error":"readiness_unknown"} when the backend cannot say
    GET  /languages   -> 200 {"backend","pairs","target_codes","complete","note"}
                         ?target=xx adds "supported": true | false | null (unknown)
    POST /warmup      body {"source","target"} optional -> 200 readiness report with
                         "warmed" and "load_s"; 422 unsupported_target; 501 unknown
    POST /reload      body {} or {"warm":bool,"source","target"} -> 200 {"discarded":[...],
                         "reloaded":bool,"status":"ready|warming","requests_served",...};
                         422/503 keep the same meanings as /translate and still report the
                         drop that happened; 501 {"error":"reload_unsupported"} when the
                         backend holds nothing to drop. Dropping is the default because a
                         reload that blocks on a model load reads as an outage to a caller
                         with a short HTTP timeout.
    --max-requests N  answer N requests then exit on its own, exit code 0, reason printed to
                         stdout, and the request that spent the budget carries
                         "shutting_down" (N=0/default means unbounded). Item 004's callers were
                         killing the process by hand because a degrading batch never failed in a
                         way they could act on; a budget they can predict replaces that.

Region tags: BCP-47 codes are folded to their primary subtag when that is
what the model or package can actually serve - pt-BR is served by pt, zh-Hans-CN
by zh. A code that folds to nothing is passed to the backend untouched, so the
422 still comes from the one component that knows its own coverage. Every
fold is echoed as "resolved_target" / "resolved_source" beside the requested code
so a caller can audit which code answered its text.

Warm-up: /readyz only tells the truth, it does not make the
checkpoint resident. /warmup is how a caller turns "warming" into "ready" without
spending a translation on it, and it is per-pair for Argos so warming one
direction never drags every installed package into memory.

Failure semantics: a language this backend can never serve is permanent and
answers 422 unsupported_target with the supported set attached; a missing
dependency or model is recoverable and keeps answering 503. Both used to be 503,
so a batch caller could not tell "restarting this will help" from "that code does
not exist" and burned a restart cycle on every rare language.

Pluggable backends:
    --backend mock  no third-party deps; echoes an identifiable pseudo-translation.
                    Used for integration debugging, CI and model-free environments.
    --backend ct2   real CTranslate2 inference (MADLAD-400 / Argos .argosmodel);
                    requires installing dependencies and downloading a model.

Design rules:
    - On a missing dependency / missing model raise MissingDependency explicitly
      with install guidance -- never crash bare, never degrade silently. A
      language that no model here can ever produce raises UnsupportedTarget.
    - /health proves the port answers; it does NOT prove a request will not stall
      on a cold model load. Say that on /readyz instead.
    - One request counter, one budget. /health's requests_served, the countdown and
      the self-exit all read the same tally, so a refused language code cannot be
      counted by one and ignored by another.
    - The service binds 127.0.0.1 only (privacy: translated text never leaves
      the machine).
    - Licenses: deps ctranslate2(MIT)/sentencepiece(Apache); models
      MADLAD-400(Apache-2.0)/Argos(MIT) -- all permissive and commercially usable.

Preparing a real CT2 environment (run these yourself; the script never
downloads or installs anything automatically):
    python -m pip install -r sidecar/requirements.txt
    # unpack Argos packages under <data_dir>/models/argos -- that default is
    # reported by default_model_dir(); pass --model-dir to point elsewhere
    #   (en_zh / zh_en .argosmodel from https://argos-net.com/v1/)
    # MADLAD-400 CT2 int8 (Apache-2.0, GB-scale): ONE checkpoint serves every
    # supported pair; fetch it with script/fetch_madlad_model.py, then
    python -m sidecar.ct2_sidecar --backend madlad --port 11436
    #   (or pass --model-dir to point at a directory holding model.bin +
    #   spiece.model, e.g. from huggingface-cli download <madlad400-ct2-repo>)
    python -m sidecar.ct2_sidecar --backend ct2 --model-dir <model_dir> --port 11435

The module is importable by unit tests (serve only blocks under __main__).
"""
import argparse
import collections
import gc
import json
import math
import os
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_ARGOS_PORT = 11435
DEFAULT_MADLAD_PORT = 11436

# Guards the lazy model loads below. The service is a ThreadingHTTPServer, so two
# clients hitting a cold backend can both pass the "not loaded yet" check and each
# build a translator -- duplicating a 2.95 GB MADLAD checkpoint inside one process.
# One lock for the whole module is deliberate: loads are rare and short compared to
# inference, and serialising them is cheaper than reasoning about per-instance
# lifecycle in two backends (external feedback item 003 proved the race corrupts
# nothing but memory; this keeps the memory honest too).
_LOAD_LOCK = threading.Lock()

# How many recent translation latencies /health describes. Fixed size on purpose:
# an unbounded history would be exactly the per-request accumulation this service
# was accused of (defect D24), and a percentile over the last N calls is the
# number that answers "is it slowing down right now?".
_LATENCY_WINDOW = 200


def _percentile(ordered, fraction):
    """Nearest-rank percentile over the recorded window; None when empty.

    Null rather than 0.0 when nothing has run yet: a batch client that reads a
    fast percentile out of a stopwatch that never started is the exact failure
    these fields exist to prevent.
    """
    if not ordered:
        return None
    rank = max(1, int(math.ceil(fraction * len(ordered))))
    return round(ordered[min(rank, len(ordered)) - 1], 6)


def process_rss_bytes():
    """Resident memory of this process, or None where the platform hides it.

    stdlib only by deliberate choice: psutil is registered as an operator-side
    diagnostic dependency and the shipped runtime stays free of it, so this reads
    the OS directly and reports "unknown" instead of reaching for a new dep.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            # The signatures are declared, not assumed: GetCurrentProcess returns
            # the pseudo-handle -1, so leaving restype/argtypes unset truncates it
            # to 32 bits on a 64-bit host and GetProcessMemoryInfo fails. Measured
            # that way on Python 3.10 / Windows 11 -- it reported None, not an error.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                                   ctypes.POINTER(_Counters),
                                                   wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                          ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        except Exception:
            return None
    try:                                    # Linux: resident pages from procfs
        statm = pathlib.Path("/proc/self/statm").read_text().split()
        return int(statm[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:
        pass
    try:                                    # macOS and anything else
        import resource
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # ru_maxrss is bytes on macOS, kilobytes on Linux and the BSDs.
        return value if sys.platform == "darwin" else value * 1024
    except Exception:
        return None


class HealthSignals:
    """What /health reports next to "ok": served count, recent latency, memory.

    External item 004 measured a long batch that kept returning 200 while it
    slowed down, so a caller following the "restart on failure" rule had nothing
    to fire on: there never was a failure. These three numbers are what lets it
    tell fine from degraded without timing its own wall clock.

    `max_requests` belongs here rather than on the server because the countdown has
    to be read off the same counter that /health already publishes. Two tallies would
    eventually disagree about whether a refused language code cost anything.
    """

    def __init__(self, window=_LATENCY_WINDOW, max_requests=None):
        self._lock = threading.Lock()
        self._latencies = collections.deque(maxlen=window)
        self._served = 0
        self._max_requests = max_requests

    def record_served(self):
        """One /translate reached the engine, however it ended."""
        with self._lock:
            self._served += 1

    def over_budget(self):
        """Whether the request just counted was the last permitted one."""
        with self._lock:
            if self._max_requests is None:
                return False
            return self._served >= self._max_requests

    def record_latency(self, seconds):
        """One translation completed; failures are deliberately not sampled."""
        with self._lock:
            self._latencies.append(float(seconds))

    def snapshot(self):
        with self._lock:
            served = self._served
            ordered = sorted(self._latencies)
            remaining = (None if self._max_requests is None
                         else max(0, self._max_requests - served))
        return {"requests_served": served,
                "requests_until_exit": remaining,
                "latency_samples": len(ordered),
                "last_latency_p50": _percentile(ordered, 0.50),
                "last_latency_p99": _percentile(ordered, 0.99),
                "rss_bytes": process_rss_bytes()}


def resolve_max_requests(raw):
    """Normalise a request budget: a positive int, or None for "unbounded".

    Zero and negatives mean unbounded rather than "exit after the first call", because a
    flag left at its default must never silently shorten a batch. The caller that meant
    `--max-requests 0` gets a process that outlives the batch, which is recoverable; one
    that exits on the first sentence looks like a crash, which is not.
    """
    if raw is None:
        return None
    value = int(raw)
    return value if value > 0 else None


def primary_subtag(code):
    """Reduce a BCP-47 tag to its language subtag: pt-BR -> pt, zh-Hans-CN -> zh.

    Product-side codes are region-qualified almost everywhere, so this belongs in
    front of the vocabulary rather than in every integrator's loop - the failure
    mode of the alternative is that one caller forgets and errors in production.
    Deliberately conservative: only the first subtag is kept, case is lowered to
    match how model vocabularies and Argos metadata spell codes, and nothing is
    guessed for an empty or all-separator tag.
    """
    raw = (code or "").strip()
    if not raw:
        return ""
    return raw.split("-")[0].strip().lower()


class MissingDependency(RuntimeError):
    """Raised when a third-party library or model required by a real backend
    is absent; the message carries actionable install guidance.

    Recoverable in principle -- fetching a model or installing a wheel makes the
    next request work -- which is why the service answers 503. A request naming a
    language that no downloadable model covers is the other kind; use
    UnsupportedTarget so the caller can give up on that pair instead of
    restarting a healthy engine."""


class UnsupportedTarget(MissingDependency):
    """The requested language or pair can never be served by this backend.

    Subclasses MissingDependency so every existing `except MissingDependency`
    site keeps catching it, while the HTTP layer can single it out and answer 4xx
    with the supported set attached."""


# ---- Backend abstraction --------------------------------------------------

class MockBackend:
    """Dependency-free echo backend: produces an identifiable pseudo-translation
    for integration debugging and contract tests."""

    name = "mock"

    def translate(self, text, source, target, glossary=None):
        out = "[{}] {}".format(target, text)
        if glossary:
            out += " +gloss"
        return out

    def languages(self):
        """It echoes anything, so it has no coverage to report -- and must not
        imply that an empty list means 'nothing is supported'."""
        return {"pairs": [], "target_codes": [], "complete": False,
                "note": "mock backend echoes any pair; it declares no coverage"}

    def serves_target(self, target):
        return True

    def resolve_target(self, target):
        """The mock echoes anything, so whatever it is handed is servable."""
        return target or None

    def resolve_source(self, source):
        return source or None

    def readiness(self):
        return {"ready": True, "reason": None, "loaded": ["mock (nothing to load)"]}

    def warm(self, source=None, target=None):
        """Nothing to load; already: True keeps load_s at zero for callers."""
        return {"ready": True, "already": True, "loaded": ["mock (nothing to load)"]}


# SentencePiece marks word boundaries with U+2581. The Argos packages carry it as
# an ordinary piece, so decode() leaves the marker in the text; the detokenizer
# maps it back to a space.
_SPM_SPACE = "\u2581"

# Beam width used per model call (Argos' own default).
_BEAM_SIZE = 4


def default_data_dir():
    """Per-OS user data location, overridable by LT_DATA_DIR / WONSLATE_DATA_DIR
    (legacy spelling wins). Mirrors translator-engine/src/config.rs."""
    override = None
    for key in ("LT_DATA_DIR", "WONSLATE_DATA_DIR"):
        value = os.environ.get(key)
        if value and value.strip():
            override = value.strip()
            break

    if override:
        return pathlib.Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return pathlib.Path(local) / "Wonslate" if local else pathlib.Path("./lt-data")
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Wonslate"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "share"
    return base / "wonslate"


def default_model_dir():
    """Conventional location for unpacked Argos packages."""
    return default_data_dir() / "models" / "argos"


def default_madlad_model_dir():
    """Conventional location for the MADLAD-400 CT2 checkpoint directory."""
    return default_data_dir() / "models" / "madlad"


class CT2Backend:
    """Real CTranslate2 backend over unpacked Argos packages.

    A model root holds one directory per language pair, each in the Argos
    layout: metadata.json (from_code / to_code), model/ (CTranslate2) and
    sentencepiece.model. Pairs are discovered from metadata.json and loaded
    lazily, so an unused direction costs nothing.

    Raises MissingDependency -- never a bare crash and never a silently wrong
    answer -- when the dependencies, the model root or the requested pair is
    unavailable. Glossary injection is not supported by these checkpoints; the
    Rust pipeline owns glossary handling for engines that accept a prompt.
    """

    name = "ct2"

    def __init__(self, model_dir=None):
        try:
            import ctranslate2
            import sentencepiece
        except ImportError as e:
            raise MissingDependency(
                "ct2 backend is missing dependencies: {}. Run"
                " `python -m pip install -r sidecar/requirements.txt` first.".format(e))

        if not model_dir:
            model_dir = default_model_dir()
        root = pathlib.Path(model_dir)
        if not root.is_dir():
            raise MissingDependency(
                "ct2 backend model dir does not exist: {}. Download and unpack"
                " Argos packages into it (see the guide at the top of this"
                " file), or pass --model-dir.".format(root))

        self._ct2 = ctranslate2
        self._spm = sentencepiece
        self._root = root
        self._packages = {}   # (source, target) -> package directory
        self._loaded = {}     # (source, target) -> (translator, processor)

        for pkg in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (pkg / "model").is_dir():
                continue
            codes = self._pair_codes(pkg / "metadata.json")
            if codes:
                self._packages[codes] = pkg

        if not self._packages:
            raise MissingDependency(
                "ct2 backend found no language packages under {}. Expected"
                " <pair>/metadata.json plus <pair>/model/ (unpacked"
                " .argosmodel).".format(root))

    @staticmethod
    def _pair_codes(metadata):
        """Read (from_code, to_code) from an Argos metadata.json, else None."""
        if not metadata.is_file():
            return None
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        source, target = data.get("from_code"), data.get("to_code")
        return (source, target) if source and target else None

    def available_pairs(self):
        """Sorted 'source->target' strings for the packages found on disk."""
        return sorted("{0}->{1}".format(s, t) for s, t in self._packages)

    def languages(self):
        """For Argos the packages on disk ARE the whole coverage: complete."""
        return {"pairs": self.available_pairs(),
                "target_codes": sorted({t for _, t in self._packages}),
                "complete": True,
                "note": "enumerated from the installed Argos packages"}

    def serves_target(self, target):
        """Whether any installed package writes its output in `target`."""
        return self.resolve_target(target) is not None

    def resolve_target(self, target):
        """Fold a region-qualified code onto a package this service has."""
        targets = {t for _, t in self._packages}
        if target in targets:
            return target
        base = primary_subtag(target)
        return base if base and base in targets else None

    def resolve_source(self, source):
        """Same fold on the source side: Argos packages name exact from_codes."""
        sources = {s for s, _ in self._packages}
        if source in sources:
            return source
        base = primary_subtag(source)
        return base if base and base in sources else None

    def reload(self):
        """Release every translator this service has materialised.

        Package discovery stays: it is a directory listing, not memory worth dropping. The
        loaded pairs are translators plus their processors, and those are the resident
        megabytes a long batch is accused of accumulating (external item 004).
        """
        dropped = sorted("{}->{}".format(pair[0], pair[1]) for pair in self._loaded)
        self._loaded = {}
        gc.collect()
        return {"discarded": dropped}

    def readiness(self):
        """Ready only once a pair has actually been loaded.

        Discovery is eager, inference is not: a translator materialises on the
        first request for its pair, so right after start a request still pays the
        model load. `loaded` names what is resident so a caller can see the why."""
        loaded = sorted("{0}->{1}".format(s, t) for s, t in self._loaded)
        return {"ready": bool(loaded),
                "reason": None if loaded else "model_not_loaded",
                "loaded": loaded}

    def _load(self, source, target):
        """Return the pair's translator and whether *this* call built it.

        The second value is decided inside the lock, not before it: three clients can
        each see a cold store and then wait their turn, and if the flag were read
        outside the lock all three would claim they paid for the one load.
        """
        key = (source, target)
        pkg = self._packages.get(key)
        if pkg is None:
            raise UnsupportedTarget(
                "ct2 backend has no model for {}->{}; available: {}".format(
                    source, target, ", ".join(self.available_pairs()) or "none"))
        if key in self._loaded:
            return self._loaded[key], False
        with _LOAD_LOCK:                    # double-checked: one load per pair
            if key not in self._loaded:
                processor = self._spm.SentencePieceProcessor(
                    model_file=str(pkg / "sentencepiece.model"))
                self._loaded[key] = (self._ct2.Translator(str(pkg / "model"), device="cpu"),
                                     processor)
                return self._loaded[key], True
            return self._loaded[key], False

    def _resolve_pair(self, source, target):
        """Fold both codes or raise - one answer per question, whoever is asking.

        The HTTP layer folds as well (so it can echo what it resolved to), but the
        rule has to live here too: otherwise a direct importer of this class gets
        "no model for en->pt-BR" while /translate answers the very same pair, and
        two implementations of one capability question disagreeing is exactly the
        class of bug this rule exists to prevent. The error names the codes the caller
        sent, because that is what it needs to see to fix its side.
        """
        resolved_source = self.resolve_source(source)
        resolved_target = self.resolve_target(target)
        if not resolved_source or not resolved_target:
            raise UnsupportedTarget(
                "ct2 backend has no model for {}->{}; available: {}".format(
                    source, target, ", ".join(self.available_pairs()) or "none"))
        return resolved_source, resolved_target

    def _pair(self, source, target):
        """Resolve (region fold) then load, giving the hot path one way in."""
        return self._load(*self._resolve_pair(source, target))[0]

    def warm(self, source=None, target=None):
        """Load one pair, or nothing at all when no pair was named.

        Refusing to load every installed package on a bare request is the point:
        a user with 40 directions would otherwise pay for all of them to serve one
        sentence. Callers that want a specific direction pass source and target.
        """
        loaded_now = False
        if source and target:
            _, loaded_now = self._load(*self._resolve_pair(source, target))
        loaded = sorted("{0}->{1}".format(s, t) for s, t in self._loaded)
        return {"ready": bool(loaded),
                "already": not loaded_now,
                "loaded": loaded,
                "reason": None if loaded else "model_not_loaded",
                "note": "pass source and target to warm one Argos direction"}

    def translate(self, text, source, target, glossary=None):
        text = (text or "").strip()
        if not text:
            return ""
        translator, processor = self._pair(source, target)
        pieces = processor.encode(text, out_type=str)
        if not pieces:
            return ""
        result = translator.translate_batch([pieces], beam_size=_BEAM_SIZE)
        return self._detokenize(processor, result[0].hypotheses[0])

    @staticmethod
    def _detokenize(processor, hypothesis):
        """Turn a hypothesis into text and restore the word-boundary markers."""
        return processor.decode(hypothesis).replace(_SPM_SPACE, " ").strip()


class MadladBackend:
    """MADLAD-400 backend: ONE multilingual T5-style checkpoint serves every
    supported pair.

    Unlike Argos (one small Marian package per direction), MADLAD-400 is a
    single model covering 400+ languages; the direction is chosen at inference
    time by prefixing the source with the `<2xx>` target-language token. The
    model directory therefore holds model.bin + spiece.model (+ optionally
    shared_vocabulary.json / config.json) straight from a CT2 conversion, with
    no per-pair layout.

    Runs on CPU on purpose: the GPU slot belongs to the ollama upgrade engine,
    and CT2's CUDA path would add a cuDNN requirement for little gain at 3B
    int8 on a modern 8-core laptop. Glossary injection is not supported by
    these checkpoints (same as Argos); the Rust pipeline owns glossary
    handling for engines that accept a prompt.
    """

    name = "madlad"

    # Shown in "unsupported pair" messages; the full MADLAD-400 list has 450+ codes.
    _COMMON_CODES = ("en", "zh", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar")

    def __init__(self, model_dir=None):
        try:
            import ctranslate2
            import sentencepiece
        except ImportError as e:
            raise MissingDependency(
                "madlad backend is missing dependencies: {}. Run"
                " `python -m pip install -r sidecar/requirements.txt` first.".format(e))

        if not model_dir:
            model_dir = default_madlad_model_dir()
        root = pathlib.Path(model_dir)
        weights = root / "model.bin"
        tokenizer = root / "spiece.model"
        if not tokenizer.is_file():  # tolerate the alternate tokenizer name some repos use
            tokenizer = root / "sentencepiece.model"
        if not weights.is_file() or not tokenizer.is_file():
            raise MissingDependency(
                "madlad backend model dir {} is incomplete: need model.bin and"
                " spiece.model (fetch with `python script/fetch_madlad_model.py`,"
                " or point --model-dir at a MADLAD-400 CT2 conversion"
                " directory).".format(root))

        self._ct2 = ctranslate2
        self._spm = sentencepiece
        self._root = root
        self._processor = self._spm.SentencePieceProcessor(model_file=str(tokenizer))
        self._translator = None          # lazily: first request pays the load
        self._known_targets = set()

    def _known_target(self, target):
        """Whether MADLAD-400 has a `<2xx>` token for this target code.

        A known code encodes to exactly that one special piece (plus, if the
        tokenizer adds it, the dummy word-boundary marker); an unknown one
        falls back to per-character pieces. Data-driven, so the check follows
        the checkpoint rather than a hardcoded language list.
        """
        if target not in self._known_targets:
            token = "<2{}>".format(target)
            pieces = [p for p in self._processor.encode(token, out_type=str) if p != _SPM_SPACE]
            if pieces == [token]:
                self._known_targets.add(target)
            else:
                return False
        return True

    def _ensure_engine(self):
        """Return the translator and whether this call built it (decided in the lock)."""
        if self._translator is not None:
            return self._translator, False
        with _LOAD_LOCK:                    # double-checked: one checkpoint only
            if self._translator is None:
                self._translator = self._ct2.Translator(str(self._root), device="cpu")
                return self._translator, True
            return self._translator, False

    def _engine(self):
        return self._ensure_engine()[0]

    def reload(self):
        """Drop the 3B checkpoint so the next request loads a fresh one.

        Measured on this build: releasing the translator took resident memory from 2926.7 MB
        to 79.6 MB, so this returns the megabytes rather than only reassigning a name. The
        tokenizer stays, and so do the target codes probed into `_known_targets`: both are
        facts about the vocabulary, which a reset must not make the service re-derive by
        guessing. In-flight requests keep the reference they already hold, so a reload cannot
        pull the engine out from under a sentence mid-decode; it only means the next one pays
        the load (external item 004, defect D30).
        """
        dropped = ["translator"] if self._translator is not None else []
        self._translator = None
        gc.collect()
        return {"discarded": dropped}

    def warm(self, source=None, target=None):
        """Materialise the checkpoint now instead of on the first sentence.

        An unknown target still raises UnsupportedTarget, so warming a language the
        vocabulary does not cover is answered the same permanent way as translating
        into it would be.
        """
        if target is not None and not self._known_target(target):
            raise UnsupportedTarget(
                "madlad backend has no target language {!r}; probe"
                " GET /languages?target={}".format(target, target))
        _, built_now = self._ensure_engine()
        return {"ready": True, "already": not built_now,
                "loaded": [] if built_now else ["checkpoint"]}

    def languages(self):
        """Coverage comes from the checkpoint's vocabulary, which has 450+ codes
        and no list to hand out, so `complete` is False: only codes actually
        probed are named. A `?target=` probe is the real answer."""
        codes = set(self._known_targets)
        codes.update(code for code in self._COMMON_CODES if self._known_target(code))
        return {"pairs": [], "target_codes": sorted(codes), "complete": False,
                "note": "MADLAD covers 450+ codes; only the ones probed so far are listed"}

    def serves_target(self, target):
        """Decide by vocabulary lookup, not by paying for a translation."""
        return self.resolve_target(target) is not None

    def resolve_target(self, target):
        """Fold `pt-BR` to the `pt` token the checkpoint actually owns.

        The vocabulary is the authority: `pt-BR` is not a <2xx> token, so a request
        naming it is only servable through its primary subtag. Source needs no fold
        here - the <2xx> prefix chooses the direction, the source text is whatever
        the tokenizer reads.
        """
        if not target:
            return None
        if self._known_target(target):
            return target
        base = primary_subtag(target)
        return base if base and base != target and self._known_target(base) else None

    def resolve_source(self, source):
        """Every source is accepted; MADLAD decides by target alone."""
        return source or None

    def readiness(self):
        """The 3B checkpoint is materialised on first use, so right after start a
        request still pays the load -- exactly what /health cannot express."""
        resident = self._translator is not None
        return {"ready": resident,
                "reason": None if resident else "model_not_loaded",
                "loaded": ["checkpoint"] if resident else []}

    def translate(self, text, source, target, glossary=None):
        text = (text or "").strip()
        if not text:
            return ""
        # resolve_target folds `pt-BR` onto the <2pt> token the checkpoint really
        # owns; an unknown code stays the permanent error it was before.
        resolved = self.resolve_target(target)
        if resolved is None:
            raise UnsupportedTarget(
                "madlad backend has no target language {!r}. Supported codes"
                " follow the MADLAD-400 vocabulary (450+ languages; common"
                " ones: {}).".format(target, " ".join(self._COMMON_CODES)))
        pieces = self._processor.encode("<2{}> {}".format(resolved, text), out_type=str)
        if not pieces:
            return ""
        eos = self._processor.eos_id()
        if eos is not None and eos >= 0:
            pieces.append(self._processor.id_to_piece(eos))
        result = self._engine().translate_batch(
            [pieces], beam_size=_BEAM_SIZE, max_decoding_length=300)
        return self._detokenize(result[0].hypotheses[0])

    def _detokenize(self, hypothesis):
        """Decode a hypothesis, dropping the special pieces a T5 decoder can emit."""
        pieces = [p for p in hypothesis
                  if p not in ("<pad>", "</s>", "<unk>") and not (p.startswith("<2") and p.endswith(">"))]
        if not pieces:
            return ""
        return self._processor.decode(pieces).replace(_SPM_SPACE, " ").strip()


def make_backend(kind, model_dir=None):
    kind = (kind or "mock").lower()
    if kind == "mock":
        return MockBackend()
    if kind == "ct2":
        return CT2Backend(model_dir)
    if kind == "madlad":
        return MadladBackend(model_dir)
    raise ValueError("unknown backend: {!r} (available: mock / ct2 / madlad)".format(kind))


# ---- Output sanitisation ----------------------------------------------------

# External item 009 measured a Lao translation returning `LAO LAO U+200B LAO ...`: invisible
# on screen, fatal to every string comparison downstream (dedup, search, cache keys, audit
# scripts). Wiping *every* zero-width codepoint is the other bug, and the reporter flagged
# it themselves -- the Persian half-space IS U+200C, and Khmer and Myanmar segment words
# with U+200B. So a mark survives when it is data: either the caller's own source text
# carried that codepoint, or the target script writes with it. Everything else goes.
#
# Keyed by primary subtag, so `fa-IR` is judged as `fa` (the handler already passes the code
# the engine actually served, but a lookup must not be fooled by a qualifier either).
ZERO_WIDTH_SCRIPTS = {
    # word separator
    "\u200b": frozenset({"km", "my"}),
    # half-space (Persian branch) and conjunct control (Indic branch)
    "\u200c": frozenset({"fa", "ps", "ckb", "ug", "ku", "sd",
                        "hi", "mr", "ne", "sa", "bn", "gu", "pa", "or", "as",
                        "kn", "ml", "te"}),
    # explicit conjunct / Arabic ligature control / emoji sequences
    "\u200d": frozenset({"hi", "mr", "ne", "sa", "bn", "gu", "pa", "or", "as",
                        "kn", "ml", "te", "ar"}),
}

# No script uses any of these to write a word, and a bidi control inside delivered text can
# change what a reader sees without changing the string's visible length.
STRIP_ALWAYS = "\u00ad\u2060\ufeff\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"

# Line structure is not noise: item 007 is still open, and callers who send a multi-line
# string must not have the sanitiser flatten it for them.
_CONTROL_KEEP = "\t\n\r"


def sanitize_output(text, target, source=""):
    """Return `text` with invisible characters that a translation should never ship.

    Presence-based rather than counting: if the source carries a codepoint at all, the
    output may use it freely, because a script can legitimately double a separator and
    guessing at counts would delete real text. Never rewrites anything visible, and is
    idempotent -- the Rust pipeline and the batch callers see the same string either way.
    """
    if not text:
        return text or ""
    base = primary_subtag(target or "")
    source = source or ""
    keep = {cp for cp, codes in ZERO_WIDTH_SCRIPTS.items() if base in codes or cp in source}
    out = []
    for ch in text:
        if ch in keep:
            out.append(ch)
        elif ch in STRIP_ALWAYS or ch in ZERO_WIDTH_SCRIPTS:
            continue                      # a mark we were not given a reason to keep
        elif (ord(ch) < 0x20 or 0x7f <= ord(ch) < 0xa0) and ch not in _CONTROL_KEEP:
            continue                      # C0/C1 and DEL control noise
        else:
            out.append(ch)
    return "".join(out)


# ---- HTTP service ----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            # Liveness only, by contract: a 200 here says the port answers, not
            # that a translation will come back promptly. GET /readyz for that.
            # The signal fields are additive, so an existing consumer reading
            # "status" gets exactly what it got before (defect D30).
            report = {"status": "ok", "backend": self.server.backend.name}
            report.update(self.server.signals.snapshot())
            self._send(200, report)
        elif parsed.path == "/readyz":
            self._readyz()
        elif parsed.path == "/languages":
            self._languages(parse_qs(parsed.query))
        else:
            self._send(404, {"error": "not found"})

    def _capability(self):
        """What this backend can serve, or an honest 'it does not enumerate'.

        Backends are duck-typed, so a third-party or older one may predate the
        contract; claiming an empty set would then read as 'nothing supported'."""
        fn = getattr(self.server.backend, "languages", None)
        if fn is None:
            return {"pairs": [], "target_codes": [], "complete": False,
                    "note": "backend does not enumerate its language coverage"}
        return fn()

    def _fold_codes(self, source, target):
        """Fold BCP-47 tags onto codes this backend says it can serve.

        Returns (source, target, echoes). Only a fold the backend itself reports as
        servable is applied; anything else goes through untouched and the backend's
        own UnsupportedTarget answers - the handler must not invent error text about
        a capability it does not own, or the capability answer would end up with two voices. Every
        fold is echoed with the requested code beside it so a caller can audit what
        actually served its text. A backend without these hooks (older or
        third-party) is passed through unchanged.
        """
        backend = self.server.backend
        echoes = {}
        fold = getattr(backend, "resolve_target", None)
        if fold is not None and target:
            resolved = fold(target)
            if resolved:
                if resolved != target:
                    echoes["requested_target"] = target
                    echoes["resolved_target"] = resolved
                target = resolved
        fold_source = getattr(backend, "resolve_source", None)
        if fold_source is not None and source:
            resolved = fold_source(source)
            if resolved:
                if resolved != source:
                    echoes["requested_source"] = source
                    echoes["resolved_source"] = resolved
                source = resolved
        return source, target, echoes

    def _languages(self, query):
        body = dict(self._capability())
        body["backend"] = self.server.backend.name
        target = (query.get("target") or [""])[0].strip()
        if target:
            body["target"] = target
            fold = getattr(self.server.backend, "resolve_target", None)
            probe = getattr(self.server.backend, "serves_target", None)
            if fold is not None:
                # Answer through the fold, then say which code it landed on: this is
                # the probe a batch caller uses to decide whether to bother the
                # engine at all, and pt-BR is worth sending even though no <2pt-BR>
                # token exists.
                resolved = fold(target)
                body["supported"] = resolved is not None
                if resolved and resolved != target:
                    body["resolved_target"] = resolved
            elif probe is None:
                # Unknown must stay apart from unsupported, or a caller that
                # treats them alike silently drops a working language.
                body["supported"] = None
                body["note"] = "backend cannot answer a target-code probe"
            else:
                body["supported"] = bool(probe(target))
        self._send(200, body)

    def _readyz(self):
        fn = getattr(self.server.backend, "readiness", None)
        if fn is None:
            # 200 would claim readiness and 503 would claim a cold model; neither
            # is known here, so say the thing is not implemented (501).
            self._send(501, {"error": "readiness_unknown",
                             "backend": self.server.backend.name,
                             "note": "backend does not report its model load state"})
            return
        body = dict(fn())
        # `ready` is the backend's internal verb; the wire says it once, as the
        # status code plus "status". A null reason would only be noise.
        ready = bool(body.pop("ready", False))
        body["backend"] = self.server.backend.name
        body["status"] = "ready" if ready else "warming"
        if ready:
            body.pop("reason", None)
            self._send(200, body)
        else:
            body["error"] = "not_ready"       # reason says which half is missing
            self._send(503, body)

    def _translate_lines(self, lines, source, target, glossary):
        """Translate each line on its own and report which ones did not come back.

        Line breaks are not a unit these checkpoints can translate. Measured against the
        real MADLAD-400 checkpoint (external item 007): three lines in came back as one
        run-on line of repeated tokens (``10000000...`` followed by source fragments), with
        none of the three answers present and a 200 on the wire. The tokenizer is not the
        problem - it keeps the ``\n`` piece - and neither is the transport; the decoder
        simply degenerates. So the service splits, joins back in the caller's structure, and
        names any line the engine could not answer instead of shipping an empty slot that
        reads like a translation of nothing (REQ-B2).

        Blank lines are structure, not work: they are echoed unchanged and never reach the
        engine, so a caller splitting a UI string on ``\n\n`` gets its paragraph break back.
        """
        answers = []
        failed = []
        for index, line in enumerate(lines):
            if not line.strip():
                answers.append(line)
                continue
            answer = self.server.backend.translate(line, source, target, glossary)
            answer = sanitize_output(answer or "", target, line)
            if not answer.strip():
                failed.append(index)
                answers.append("")
            else:
                answers.append(answer)
        return answers, failed

    def _units_from_request(self, req):
        """Normalise ``text`` / ``texts`` into entries, or send a 400 and return None.

        Exactly one shape per request: accepting both would make the answer depend on key
        order in a JSON object, which is the kind of ambiguity a batch caller cannot audit.
        """
        text, batch = req.get("text"), req.get("texts")
        if text is not None and batch is not None:
            self._send(400, {"error": "text and texts are mutually exclusive"})
            return None
        if batch is not None:
            if not isinstance(batch, list) or not batch:
                self._send(400, {"error": "'texts' must be a non-empty list"})
                return None
            if not all(isinstance(u, str) for u in batch):
                self._send(400, {"error": "'texts' must contain only strings"})
                return None
            if not any(u.strip() for u in batch):
                self._send(400, {"error": "no text to translate"})
                return None
            return [u.split("\n") for u in batch]
        if not text or not str(text).strip():
            self._send(400, {"error": "missing 'text'"})
            return None
        return [str(text).split("\n")]

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/translate", "/warmup", "/reload"):
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        if path == "/warmup":
            self._warmup(req)
            return
        if path == "/reload":
            self._reload(req)
            return
        entries = self._units_from_request(req)
        if entries is None:
            return
        as_array = req.get("texts") is not None
        flat = [line for entry in entries for line in entry]
        folded_source_lang, folded_target, echoes = self._fold_codes(
            str(req.get("source") or ""), str(req.get("target") or ""))
        started = time.perf_counter()
        try:
            answers, failed_lines = self._translate_lines(
                flat, folded_source_lang, folded_target, req.get("glossary"))
        except UnsupportedTarget as e:
            # Permanent for this code, and ordered before MissingDependency
            # because it IS one. Hand back the supported set so a batch caller can
            # retire the direction instead of restarting a healthy engine.
            spent = self.server.count_served()
            payload = {"error": "unsupported_target", "message": str(e),
                       "supported": self._capability()}
            self._send(422, self._mark_exit(payload, spent))
            return
        except MissingDependency as e:
            spent = self.server.count_served()
            self._send(503, self._mark_exit({"error": "backend_unavailable",
                                             "message": str(e)}, spent))
            return
        # Counted and sampled together only on this path: an error is fast, and
        # letting it into the percentile would report a healthy engine at the
        # exact moment the engine is failing.
        spent = self.server.count_served()
        self.server.signals.record_latency(time.perf_counter() - started)
        # Regroup in the caller's own shape: one entry per array element, one text for a
        # plain request, and always the same number of lines the entry had.
        joined = []
        cursor = 0
        for entry in entries:
            joined.append("\n".join(answers[cursor:cursor + len(entry)]))
            cursor += len(entry)
        # `failed_lines` counts flat lines; the caller holds entries, so a batch maps each
        # failed line back to the entry it came from, and a single text reports line numbers
        # directly. Naming the position is what keeps a partial answer honest (REQ-B2).
        if as_array:
            bounds, cursor = [], 0
            for entry in entries:
                bounds.append((cursor, cursor + len(entry)))
                cursor += len(entry)
            failed_units = sorted({i for i, (first, last) in enumerate(bounds)
                                   for line in failed_lines if first <= line < last})
        else:
            failed_units = list(failed_lines)
        body = {"texts": joined} if as_array else {"text": joined[0]}
        if failed_units:
            body["failed_lines"] = failed_units
            body["note"] = ("%d %s did not come back translated; the rest of the answer is"
                            " intact and the position is named so it can be retried"
                            % (len(failed_units), "entries" if as_array else "lines"))
        body.update(echoes)
        self._send(200, self._mark_exit(body, spent))

    def _mark_exit(self, body, spent_last_permit):
        """Announce a scheduled exit inside the answer that caused it.

        Item 004's callers had to discover a dying process by timing their own wall clock and
        then taskkill it. A budget is only useful if the request that spends it says so, so the
        caller can relaunch on its own terms; it rides on error answers too, because an exit the
        caller cannot predict is the same silence the original report was about. Nothing is
        claimed about memory being reclaimed -- that is a measured question, not a promise.
        """
        if spent_last_permit:
            body["shutting_down"] = {"reason": "max_requests",
                                     "requests_served":
                                         self.server.signals.snapshot()["requests_served"],
                                     "note": "this process has spent its request budget and is "
                                             "exiting; relaunch to continue the batch"}
        return body

    def _warmup(self, req):
        """Load on demand, then answer with the same report /readyz would give.

        Readiness decides the status code, not whether the call ran: a warm that
        left the backend cold (Argos with no pair named) must not read as success,
        or a caller polling this endpoint would keep believing it was ready.
        """
        fn = getattr(self.server.backend, "warm", None)
        if fn is None:
            self._send(501, {"error": "warmup_unknown",
                             "backend": self.server.backend.name,
                             "note": "backend does not support explicit warm-up"})
            return
        source, target, echoes = self._fold_codes(
            str(req.get("source") or "").strip(), str(req.get("target") or "").strip())
        source = source or None
        target = target or None
        started = time.perf_counter()
        try:
            report = fn(source, target)
        except UnsupportedTarget as e:
            # Warming a language the backend can never produce is
            # permanent, so it gets the same 422 the translate path gives.
            self._send(422, {"error": "unsupported_target", "message": str(e),
                             "supported": self._capability()})
            return
        except MissingDependency as e:
            self._send(503, {"error": "backend_unavailable", "message": str(e)})
            return
        warmed = not bool(report.get("already"))
        ready = bool(report.get("ready"))
        body = {k: v for k, v in report.items() if k not in ("ready", "already")}
        body["backend"] = self.server.backend.name
        body["status"] = "ready" if ready else "warming"
        body["warmed"] = warmed
        # Report a cost only when this call actually paid one; a no-op warm should
        # not teach callers to read a stopwatch that never ran.
        body["load_s"] = round(time.perf_counter() - started, 3) if warmed else 0.0
        body.update(echoes)          # say which code was warmed, if it was folded
        if ready:
            self._send(200, body)
        else:
            body["error"] = "not_ready"
            self._send(503, body)

    def _readiness_word(self):
        """The same two words /readyz uses, or no word at all if the backend cannot tell.

        "warming" would be a claim about a cold model that this backend never made, and that is
        the failure mode /readyz exists to stop (defect D23).
        """
        fn = getattr(self.server.backend, "readiness", None)
        if fn is None:
            return None
        return "ready" if fn().get("ready") else "warming"

    def _reload(self, req):
        """Drop the resident engine, and only reload it if the caller asked to pay for that.

        Item 004's callers were restarting the whole process per language -- kill, wait for the
        port, relaunch, verify with a real translation -- because that was the only way they
        found to reset a degrading engine. This keeps the port and the process and discards the
        engine instead. Measured on this build's MADLAD-400 checkpoint: releasing the translator
        took resident memory from 2926.7 MB to 79.6 MB, so the drop is real rather than a report
        written over a live engine; re-materialising it cost 4.2-5.5 s here, which is why it is
        opt-in -- the desktop client probes this service with a 1500 ms HTTP timeout, so a
        synchronous reload would read as an outage to exactly the callers this exists to help.

        Whatever the answer says, it never claims memory came back: RSS after a drop is the
        number above, and a promise would outlive any single measurement.
        """
        backend = self.server.backend
        fn = getattr(backend, "reload", None)
        if fn is None:
            # 200 would claim a reset that never happened and 503 would claim an outage; this is
            # neither, so it gets the same 501 shape as warmup (defect D23's vocabulary).
            self._send(501, {"error": "reload_unsupported", "backend": backend.name,
                             "note": "backend holds no reloadable engine"})
            return
        report = fn() or {}
        body = {"backend": backend.name,
                "discarded": list(report.get("discarded") or []),
                "reloaded": False,
                "requests_served": self.server.signals.snapshot()["requests_served"]}
        word = self._readiness_word()
        if word is not None:
            body["status"] = word
        if not req.get("warm"):
            self._send(200, body)
            return
        warm_fn = getattr(backend, "warm", None)
        if warm_fn is None:
            # The drop really happened, so failing the call would hide it; what is missing is
            # the reload, and that gets said as plainly as it is.
            body["note"] = ("engine was discarded but this backend cannot warm on demand; "
                            "the next request pays the load")
            self._send(200, body)
            return
        source, target, echoes = self._fold_codes(
            str(req.get("source") or "").strip(), str(req.get("target") or "").strip())
        started = time.perf_counter()
        try:
            loaded = warm_fn(source or None, target or None)
        except UnsupportedTarget as e:
            # The drop already happened, so the permanent-failure answer has to carry it: a 422
            # that hid the reset would leave the caller believing its engine was still resident.
            body.update(echoes)
            body.update({"error": "unsupported_target", "message": str(e),
                         "supported": self._capability()})
            self._send(422, body)
            return
        except MissingDependency as e:
            body.update(echoes)
            body.update({"error": "backend_unavailable", "message": str(e)})
            self._send(503, body)
            return
        already = bool(loaded.get("already"))
        # Read readiness again: the word above described the instant after the drop, and an
        # answer that warmed the engine while still reporting "warming" would send the caller
        # off to poll /readyz for a fact this response already changed.
        word = self._readiness_word()
        if word is not None:
            body["status"] = word
        # "reloaded" is only true if this call both did the work and left the service able to
        # answer: Argos with no pair named warms nothing, and reporting that as a reload would
        # teach the caller to trust a field that invents its own content.
        body["reloaded"] = not already and word == "ready"
        if body["reloaded"]:
            body["load_s"] = round(time.perf_counter() - started, 3)
        else:
            body["load_s"] = 0.0
        body.update(echoes)
        self._send(200, body)

    def log_message(self, *_):  # silence the access log
        pass


class SidecarServer(ThreadingHTTPServer):
    """The service plus the facts a batch caller needs about the process itself.

    `exit_requested` is a flag rather than a direct kill because the request that spends the
    budget still has to answer: retiring the process is the point, losing that last answer
    would not be.
    """

    daemon_threads = True

    def count_served(self):
        """Charge one request to the process budget; True if this was its last permit.

        Both halves happen together on purpose. A permit spent on a refused language code has
        to retire the process exactly like one spent on an answer, otherwise a batch of bad
        codes keeps a degrading engine alive forever -- and the budget would then disagree with
        /health, which already counted that request (defect D30, external item 004).
        """
        self.signals.record_served()
        if not self.signals.over_budget():
            return False
        if not self.exit_requested:
            self.exit_requested = True
            self.exit_reason = ("max_requests: served {} of {} permitted requests; exiting "
                                "so the next request is answered by a fresh process"
                                .format(self.signals.snapshot()["requests_served"],
                                        self.max_requests))
            # Ask the listening loop to stop from another thread: calling shutdown() here would
            # block this handler until the loop actually returns, and the response above is
            # what the caller is waiting for.
            threading.Thread(target=self.shutdown, daemon=True).start()
        return True


def build_server(backend, host="127.0.0.1", port=0, max_requests=None):
    srv = SidecarServer((host, port), Handler)
    srv.backend = backend
    srv.max_requests = resolve_max_requests(max_requests)
    srv.signals = HealthSignals(max_requests=srv.max_requests)
    srv.exit_requested = False
    srv.exit_reason = None
    return srv


def serve_in_thread(backend, host="127.0.0.1", port=0, max_requests=None):
    """Start the service on a background thread (for tests / embedding);
    returns (server, bound_port)."""
    srv = build_server(backend, host, port, max_requests)
    bound = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, bound


def main():
    ap = argparse.ArgumentParser(description="Wonslate sidecar translation server")
    ap.add_argument("--backend", default="mock", choices=["mock", "ct2", "madlad"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None,
                    help="default: 11435 for mock/ct2 (argos slot), 11436 for madlad")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--max-requests", type=int, default=0,
                    help="exit after serving this many requests instead of running forever "
                         "(0, the default, means unbounded); the request that spends the "
                         "budget is answered normally and carries shutting_down, so a batch "
                         "caller can relaunch on schedule rather than by taskkill")
    args = ap.parse_args()

    if args.port is None:
        args.port = DEFAULT_MADLAD_PORT if args.backend == "madlad" else DEFAULT_ARGOS_PORT
    backend = make_backend(args.backend, args.model_dir)
    srv = build_server(backend, args.host, args.port, max_requests=args.max_requests)
    budget = "unbounded" if srv.max_requests is None else "{} requests".format(srv.max_requests)
    print("sidecar[{}] listening on http://{}:{}/translate (budget: {})"
          .format(backend.name, args.host, args.port, budget))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        # Close the listening socket whichever way the loop ended, so the port is free for the
        # process that follows. A bound exit that leaves the port held would recreate the one
        # symptom item 005 was filed about.
        srv.server_close()
    if srv.exit_requested:
        # Printed, not raised: a batch that reached its budget finished successfully, and an
        # exit code reading as a crash would teach the supervisor to fear the mechanism it asked
        # for. The reason goes to stdout so a wrapper log can attribute the restart.
        print("sidecar[{}] exiting on budget: {}".format(backend.name, srv.exit_reason))


if __name__ == "__main__":
    main()
