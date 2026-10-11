# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Contract tests for the real sidecar service (CT2 / Argos).

Pure stdlib unittest. Two layers are covered:

  * dependency-free: the backend abstraction, the HTTP contract (/translate,
    /health liveness, /readyz readiness, /languages enumeration), the split
    between permanent and recoverable failure and the explicit-degradation
    paths (missing deps, missing model dir, dir without packages, unsupported
    pair) -- always runs, no skips.
  * real inference: loads the CTranslate2 + sentencepiece stack and an unpacked
    Argos package from default_model_dir(). Runs wherever that model is present
    (install: sidecar/requirements.txt; download steps: docs/17) and skips with
    an explicit reason otherwise -- a missing model is an environment fact, not
    a passing test.

Run:  python -m unittest tests.test_ct2_sidecar -v
"""
import contextlib
import http.client
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sidecar import ct2_sidecar  # noqa: E402  (module handle for symbols under test)
from sidecar.ct2_sidecar import (  # noqa: E402
    CT2Backend, MadladBackend, MockBackend, MissingDependency, default_model_dir, make_backend,
    serve_in_thread,
)


# ---- Backend abstraction (pure logic, no network) ---------------------------

class MockBackendTests(unittest.TestCase):
    def test_translate_returns_target_tagged_text(self):
        out = MockBackend().translate("hello", "en", "zh")
        self.assertIn("hello", out)
        self.assertIn("zh", out)

    def test_translate_flags_glossary_when_present(self):
        plain = MockBackend().translate("hello", "en", "zh")
        with_gloss = MockBackend().translate("hello", "en", "zh", glossary=[{"src": "hello", "tgt": "你好"}])
        self.assertNotEqual(plain, with_gloss)
        self.assertIn("gloss", with_gloss)

    def test_name_is_mock(self):
        self.assertEqual(MockBackend().name, "mock")


class BackendFactoryTests(unittest.TestCase):
    def test_unknown_backend_raises_value_error(self):
        with self.assertRaises(ValueError):
            make_backend("no-such-backend")

    def test_mock_backend_created(self):
        self.assertEqual(make_backend("mock").name, "mock")

    def test_ct2_backend_rejects_a_missing_model_dir(self):
        # Must be an explicit MissingDependency with guidance -- never a bare
        # ImportError and never a silent empty translation. Unconditional:
        # whether or not ctranslate2 is installed, a missing directory is an
        # explicit failure.
        missing = os.path.join(tempfile.gettempdir(), "wonslate-no-such-model-dir")
        with self.assertRaises(MissingDependency):
            make_backend("ct2", model_dir=missing)

    def test_ct2_backend_rejects_a_dir_without_packages(self):
        # Also unconditional: an empty dir fails with "no language packages"
        # when the stack is installed, and with the dependency message when it
        # is not -- both are MissingDependency.
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(MissingDependency):
                make_backend("ct2", model_dir=empty)

    def test_madlad_backend_rejects_a_missing_model_dir(self):
        # Same explicit-degradation contract as ct2: an absent or incomplete
        # MADLAD directory is a MissingDependency with guidance, never a crash.
        missing = os.path.join(tempfile.gettempdir(), "wonslate-no-such-madlad-dir")
        with self.assertRaises(MissingDependency):
            make_backend("madlad", model_dir=missing)

    def test_madlad_backend_rejects_an_incomplete_dir(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(MissingDependency):
                make_backend("madlad", model_dir=empty)


# ---- Real inference (needs the deps plus an unpacked Argos package) ---------

class RealInferenceTests(unittest.TestCase):
    """Exercises the actual CTranslate2 + sentencepiece path end to end."""

    @classmethod
    def setUpClass(cls):
        model_dir = default_model_dir()
        if not model_dir.is_dir():
            raise unittest.SkipTest(
                "no Argos model under {}; install sidecar/requirements.txt and "
                "follow the download steps (docs/17)".format(model_dir))
        try:
            cls.backend = CT2Backend(model_dir=str(model_dir))
        except MissingDependency as exc:
            raise unittest.SkipTest("ct2 stack unavailable: {}".format(exc))

    def test_discovers_the_english_chinese_pair(self):
        self.assertIn("en->zh", self.backend.available_pairs())

    def test_english_to_chinese_returns_cjk(self):
        out = self.backend.translate("Hello, world.", "en", "zh")
        self.assertTrue(out)
        self.assertNotEqual(out, "[zh] Hello, world.")  # not the mock backend
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in out), out)

    def test_chinese_to_english_returns_latin(self):
        out = self.backend.translate("你好，世界。", "zh", "en")
        self.assertTrue(out)
        self.assertTrue(any(ch.isascii() and ch.isalpha() for ch in out), out)

    def test_unsupported_pair_is_explicit(self):
        with self.assertRaises(MissingDependency):
            self.backend.translate("hello", "en", "ja")

    def test_languages_reports_the_packages_on_disk(self):
        # For Argos the discovered directories ARE the whole coverage, so
        # the enumeration must claim to be exhaustive.
        capability = self.backend.languages()
        self.assertEqual(capability["pairs"], self.backend.available_pairs())
        self.assertTrue(capability["complete"])
        self.assertIn("zh", capability["target_codes"])

    def test_target_probe_needs_no_inference(self):
        self.assertTrue(self.backend.serves_target("zh"))
        self.assertFalse(self.backend.serves_target("jj"))

    def test_region_fold_follows_the_installed_packages(self):
        # Region folding at the Argos tier: packages carry exact codes from metadata.json, so a
        # BCP-47 code has to fold before the lookup or a UI that says `zh-CN` cannot
        # reach the one direction sitting on disk. No package for Portuguese means
        # no fold invents one.
        self.assertEqual(self.backend.resolve_target("zh-Hans"), "zh")
        self.assertEqual(self.backend.resolve_source("en-US"), "en")
        self.assertIsNone(self.backend.resolve_target("pt-BR"))
        # and the pair resolution the hot path uses agrees with those answers
        self.assertEqual(self.backend._resolve_pair("en-US", "zh-Hans"), ("en", "zh"))
        with self.assertRaises(MissingDependency):
            self.backend._resolve_pair("en", "pt-BR")

    def test_readiness_is_cold_before_any_pair_is_resident(self):
        # Readiness at the Argos tier: packages are discovered eagerly, translators are
        # loaded per pair on first use, so a fresh backend must not claim ready.
        fresh = CT2Backend(model_dir=str(default_model_dir()))
        self.assertFalse(fresh.readiness()["ready"])

    def test_warm_loads_only_the_direction_named(self):
        # The memory argument for per-pair warming, measured: warming en->zh must
        # leave the other installed direction untouched, or warm-up would cost more
        # than the lazy load it is replacing.
        fresh = CT2Backend(model_dir=str(default_model_dir()))
        report = fresh.warm("en", "zh")
        self.assertTrue(report["ready"])
        self.assertEqual(report["loaded"], ["en->zh"])
        self.assertFalse(report["already"])      # this call paid the load
        self.assertEqual(len(fresh.available_pairs()), 2)   # both exist on disk

    def test_http_translate_uses_the_real_backend(self):
        srv, port = serve_in_thread(self.backend, host="127.0.0.1", port=0)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            conn.request("POST", "/translate",
                         body=json.dumps({"text": "Hello, world.",
                                          "source": "en", "target": "zh"}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["text"])
            self.assertNotIn("[zh]", payload["text"])
        finally:
            srv.shutdown()
            srv.server_close()


# ---- Real MADLAD inference (needs the deps plus the CT2 checkpoint) ---------

class RealMadladInferenceTests(unittest.TestCase):
    """Exercises the MADLAD-400 checkpoint across several pairs end to end."""

    @classmethod
    def setUpClass(cls):
        model_dir = default_model_dir().parent / "madlad"
        if not model_dir.is_dir():
            raise unittest.SkipTest(
                "no MADLAD checkpoint under {}; run "
                "script/fetch_madlad_model.py and install sidecar/requirements.txt"
                .format(model_dir))
        try:
            cls.backend = MadladBackend(model_dir=str(model_dir))
        except MissingDependency as exc:
            raise unittest.SkipTest("madlad stack unavailable: {}".format(exc))
        cls.model_dir = str(model_dir)

    def test_target_probe_follows_the_vocabulary(self):
        # `tl`-style answers must come from the checkpoint's own vocabulary,
        # cheaply, instead of by burning a request that then 5xx-fails.
        self.assertTrue(self.backend.serves_target("zh"))
        self.assertFalse(self.backend.serves_target("xx"))

    def test_readiness_is_cold_until_the_checkpoint_is_loaded(self):
        # Construction only reads the tokenizer; the 3B checkpoint is lazy,
        # which is precisely what /health cannot distinguish from "usable".
        fresh = MadladBackend(model_dir=self.model_dir)
        report = fresh.readiness()
        self.assertFalse(report["ready"])
        self.assertEqual(report["reason"], "model_not_loaded")

    def test_region_code_folds_onto_the_real_vocabulary(self):
        # Region folding against the actual MADLAD-400 vocabulary rather than a fake: `pt-BR`
        # is not a <2xx> token while `pt` is, so only a fold makes the request
        # servable - and the folded answer must be the same translation, not a
        # best-effort guess at something else.
        self.assertTrue(self.backend.serves_target("pt-BR"))
        self.assertEqual(self.backend.resolve_target("pt-BR"), "pt")
        self.assertEqual(self.backend.resolve_target("zh-Hans-CN"), "zh")
        self.assertIsNone(self.backend.resolve_target("xx-YY"))
        folded = self.backend.translate("Hello, world.", "en", "pt-BR")
        self.assertTrue(folded)
        self.assertEqual(folded, self.backend.translate("Hello, world.", "en", "pt"))

    def test_english_to_chinese_returns_cjk(self):
        out = self.backend.translate("Hello, world.", "en", "zh")
        self.assertTrue(out)
        self.assertNotIn("[zh]", out)
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in out), out)

    def test_english_to_japanese_returns_kana_or_kanji(self):
        out = self.backend.translate("Good morning.", "en", "ja")
        self.assertTrue(out)
        self.assertTrue(any(
            "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in out), out)

    def test_chinese_to_english_returns_latin(self):
        out = self.backend.translate("你好，世界。", "zh", "en")
        self.assertTrue(out)
        self.assertTrue(any(ch.isascii() and ch.isalpha() for ch in out), out)

    def test_unknown_target_language_is_explicit(self):
        with self.assertRaises(MissingDependency):
            self.backend.translate("hello", "en", "xx")

    def test_empty_input_is_empty(self):
        self.assertEqual(self.backend.translate("   ", "en", "zh"), "")


# ---- HTTP contract (real server, random port, mock backend) -----------------

class HttpContractTests(unittest.TestCase):
    def setUp(self):
        self.server, self.port = serve_in_thread(MockBackend(), host="127.0.0.1", port=0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=(json.dumps(body) if body is not None else None),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        return resp.status, data

    def test_health_returns_ok(self):
        status, data = self._req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["status"], "ok")

    def test_translate_valid_request(self):
        status, data = self._req("POST", "/translate",
                                 {"text": "hello world", "source": "en", "target": "zh"})
        self.assertEqual(status, 200)
        self.assertIn("text", json.loads(data))
        self.assertIn("hello world", json.loads(data)["text"])

    def test_translate_missing_text_is_4xx(self):
        status, _ = self._req("POST", "/translate", {"source": "en", "target": "zh"})
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)

    def test_unknown_path_is_404(self):
        status, _ = self._req("GET", "/nope")
        self.assertEqual(status, 404)


# ---- permanent vs recoverable, readiness, enumeration ------------
#
# Three separate questions a batch caller has to be able to ask, none of which
# the contract used to answer (external feedback pack, items 001 and 002):
#
#   * is this language something the engine could ever serve? (4xx + the
#     supported set, not a 503 that reads as an outage worth retrying);
#   * is it up, or up *and* able to answer without a cold model load?
#     (/health stays liveness, /readyz tells the truth);
#   * what does it serve at all? (/languages, plus a cheap ?target= probe so
#     the answer costs no translation).

class _StubBackend:
    """Duck-typed backend that fails, warms or enumerates on command."""

    name = "stub"

    def __init__(self, error=None, ready=True, pairs=None, targets=None, complete=True,
                 sources=None):
        self.error = error
        self.ready = ready
        self.pairs = list(pairs or [])
        self.targets = list(targets or [])
        self.sources = list(sources or ([p.split("->")[0] for p in (pairs or [])]))
        self.complete = complete
        self.translate_calls = 0
        self.warmed = False
        self.warmed_pairs = []
        self.served = []

    def translate(self, text, source, target, glossary=None):
        self.translate_calls += 1
        self.served.append((source, target))
        if self.error is not None:
            raise self.error
        if self.targets and target not in self.targets:
            # Mirror the real backends: an unservable target is permanent and comes
            # from the backend, so a 422 here proves the handler passed an unfolded
            # code through instead of quietly dropping the region itself.
            raise ct2_sidecar.UnsupportedTarget(
                "stub has no target language {!r}; available: {}".format(
                    target, ", ".join(self.targets)))
        return "ok: {}".format(target)

    def languages(self):
        return {"pairs": self.pairs, "target_codes": self.targets,
                "complete": self.complete, "note": "stub"}

    def serves_target(self, target):
        return target in self.targets

    def readiness(self):
        return {"ready": self.ready, "reason": "stub", "loaded": []}

    def warm(self, source=None, target=None):
        """Load on demand; reports whether this call was the one that loaded."""
        if target and target not in self.targets and self.targets:
            raise ct2_sidecar.UnsupportedTarget(
                "stub backend has no target language {!r}".format(target))
        if source and target:
            self.warmed_pairs.append((source, target))
        was = self.ready
        self.ready = True
        self.warmed = self.warmed or not was
        return {"ready": True, "already": was}

    def resolve_target(self, target):
        """Fold a region-qualified code onto one this stub can serve."""
        if target in self.targets:
            return target
        base = ct2_sidecar.primary_subtag(target)
        return base if base in self.targets else None

    def resolve_source(self, source):
        if source in self.sources:
            return source
        base = ct2_sidecar.primary_subtag(source)
        return base if base in self.sources else None


class _LegacyBackend:
    """A backend that predates the /languages + /readyz contract and reports
    neither. The service must say so instead of inventing a capability."""

    name = "legacy"

    def translate(self, text, source, target, glossary=None):
        return "ok"


class _ColdAfterWarmBackend:
    """Warmable, but still cold afterwards - Argos with no pair named."""

    name = "coldish"

    def translate(self, text, source, target, glossary=None):
        return "ok"

    def warm(self, source=None, target=None):
        return {"ready": False, "already": True, "reason": "model_not_loaded",
                "note": "pass source and target"}


class ErrorSeparationTests(unittest.TestCase):
    """A code that can never work must not look like an engine that is down."""

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _post(self, backend, target="xx"):
        srv, port = serve_in_thread(backend, host="127.0.0.1", port=0)
        self.servers.append(srv)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/translate", body=json.dumps(
            {"text": "hello", "source": "en", "target": target}),
            headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_a_code_that_can_never_work_is_4xx_not_503(self):
        # No download and no restart fixes an absent language, so 503 -- the code
        # the audit saw -- tells the caller to nurse a healthy engine back to
        # life: measured there it cost a kill + port wait + reload per code.
        status, payload = self._post(_StubBackend(
            error=ct2_sidecar.UnsupportedTarget("madlad has no target language 'xx'"),
            targets=["zh", "ja"]))
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "unsupported_target")
        self.assertIn("no target language", payload["message"])

    def test_permanent_failure_hands_back_the_supported_set(self):
        # So a caller can mark one direction dead without parsing prose or
        # re-deriving coverage from the error text.
        status, payload = self._post(_StubBackend(
            error=ct2_sidecar.UnsupportedTarget("no such target"),
            pairs=["en->zh"], targets=["zh"]))
        self.assertEqual(status, 422)
        self.assertEqual(payload["supported"]["target_codes"], ["zh"])
        self.assertEqual(payload["supported"]["pairs"], ["en->zh"])

    def test_a_recoverable_absence_is_still_503(self):
        # The other half of the split, pinned so the fix cannot collapse every
        # failure into 4xx: a model that is merely not fetched yet IS an outage
        # for this process and keeps its existing code and error value.
        status, payload = self._post(_StubBackend(
            error=ct2_sidecar.MissingDependency("model dir does not exist")))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "backend_unavailable")

    def test_unsupported_target_stays_a_missing_dependency(self):
        # Every existing `except MissingDependency` site keeps working, including
        # the two real-inference cases below that spell the failure as
        # MissingDependency and the CLI's install-guidance contract.
        self.assertTrue(
            issubclass(ct2_sidecar.UnsupportedTarget, ct2_sidecar.MissingDependency))


class ReadinessContractTests(unittest.TestCase):
    """Liveness ("the port answers") and readiness ("a request will not stall") are different facts."""

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _get(self, backend, path):
        srv, port = serve_in_thread(backend, host="127.0.0.1", port=0)
        self.servers.append(srv)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_health_is_liveness_and_stays_200_when_cold(self):
        # Existing consumers (the .NET health probe) rely on this shape; the fix
        # must not silently repurpose /health.
        status, payload = self._get(_StubBackend(ready=False), "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_readyz_refuses_to_claim_readiness_of_a_cold_backend(self):
        status, payload = self._get(_StubBackend(ready=False), "/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "not_ready")
        self.assertEqual(payload["status"], "warming")
        self.assertEqual(payload["backend"], "stub")
        self.assertIn("reason", payload)          # why it is not usable yet

    def test_readyz_confirms_a_resident_backend(self):
        status, payload = self._get(_StubBackend(ready=True), "/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        # Say the verb once: the code plus "status" already carry it, and an
        # empty reason field would only teach callers to parse two answers.
        self.assertNotIn("ready", payload)
        self.assertNotIn("reason", payload)

    def test_a_backend_that_reports_nothing_gets_501_not_a_guess(self):
        # Neither 200 (claims readiness) nor 503 (claims cold) is true for a
        # backend that never said. 501 keeps the signal honest for the caller.
        status, payload = self._get(_LegacyBackend(), "/readyz")
        self.assertEqual(status, 501)
        self.assertEqual(payload["error"], "readiness_unknown")

    def test_mock_backend_declares_both_signals(self):
        # The dependency-free backend everyone starts with must satisfy the
        # contract itself: it has nothing to load, and it serves anything, so
        # readiness is true and enumeration is explicitly not exhaustive.
        mock = MockBackend()
        self.assertTrue(mock.readiness()["ready"])
        self.assertFalse(mock.languages()["complete"])


class _SlowStubBackend(_StubBackend):
    """Pays a real cost inside translate, so the latency window has something to see."""

    COST = 0.05

    def translate(self, text, source, target, glossary=None):
        time.sleep(self.COST)
        return _StubBackend.translate(self, text, source, target, glossary)


class HealthSignalsTests(unittest.TestCase):
    """A batch client must be able to see that it is degrading (defect D30).

    External item 004 measured a long batch that got slower and slower while
    every call returned 200 and /health kept answering ok. Nothing in the
    response body let the caller tell "fine" from "degraded", so its recovery
    strategy (restart on failure) could not fire: there was never a failure.

    These fields are additive. /health stays a liveness probe with the same
    meaning it had before (defect D23 fixed what readiness means, and the .NET
    client depends on this shape), so an old consumer keeps working untouched.
    Percentiles are null with zero samples instead of 0.0: a stopwatch that
    never ran must not look like a fast one.
    """

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _serve(self, backend):
        srv, port = serve_in_thread(backend, host="127.0.0.1", port=0)
        self.servers.append(srv)
        return port

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def _translate(self, port, text="hello", target="zh"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/translate", body=json.dumps(
            {"text": text, "source": "en", "target": target}),
            headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_health_advertises_the_signals_without_inventing_values(self):
        port = self._serve(_StubBackend())
        status, payload = self._get(port, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")          # unchanged semantics
        self.assertEqual(payload["backend"], "stub")       # unchanged fields
        self.assertEqual(payload["requests_served"], 0)
        self.assertEqual(payload["latency_samples"], 0)
        self.assertIsNone(payload["last_latency_p50"],
                          "no sample was taken, so a percentile must not be 0.0")
        self.assertIsNone(payload["last_latency_p99"])
        # rss_bytes must be a real reading wherever the platform exposes one; the
        # product's own platform is Windows, so a silent None there is a failure.
        self.assertIsNotNone(payload["rss_bytes"], "this platform can report RSS")
        self.assertGreater(payload["rss_bytes"], 0)

    def test_served_translations_are_counted_and_timed(self):
        port = self._serve(_SlowStubBackend())
        for _ in range(3):
            self.assertEqual(self._translate(port), 200)
        # Probing health must not pollute the very numbers it reports.
        self._get(port, "/health")
        self._get(port, "/health")
        _, payload = self._get(port, "/health")
        self.assertEqual(payload["requests_served"], 3)
        self.assertEqual(payload["latency_samples"], 3)
        # Asserted against the cost the backend actually paid, not against 0.0:
        # "positive" would pass on a stopwatch that timed nothing but the probe.
        self.assertGreaterEqual(payload["last_latency_p50"], _SlowStubBackend.COST * 0.8)
        self.assertGreaterEqual(payload["last_latency_p99"], payload["last_latency_p50"])

    def test_a_failing_translation_counts_but_produces_no_latency_sample(self):
        # A permanent 422 or a 503 costs microseconds; letting it into the
        # percentile would report a healthy engine precisely when the engine is
        # failing, so the latency window tracks completed work only.
        port = self._serve(_StubBackend(
            error=ct2_sidecar.MissingDependency("model dir does not exist")))
        self.assertEqual(self._translate(port), 503)
        _, payload = self._get(port, "/health")
        self.assertEqual(payload["requests_served"], 1)
        self.assertEqual(payload["latency_samples"], 0)
        self.assertIsNone(payload["last_latency_p99"])


class LanguagesEndpointTests(unittest.TestCase):
    """Answer the capability question before a request spends a translation."""

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _get(self, backend, path):
        srv, port = serve_in_thread(backend, host="127.0.0.1", port=0)
        self.servers.append(srv)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_languages_enumerates_the_backend(self):
        backend = _StubBackend(pairs=["en->zh", "zh->en"], targets=["zh", "en"])
        status, payload = self._get(backend, "/languages")
        self.assertEqual(status, 200)
        self.assertEqual(payload["backend"], "stub")
        self.assertEqual(payload["pairs"], ["en->zh", "zh->en"])
        self.assertTrue(payload["complete"])

    def test_target_probe_answers_false_without_spending_a_translation(self):
        backend = _StubBackend(pairs=["en->zh"], targets=["zh"])
        status, payload = self._get(backend, "/languages?target=xx")
        self.assertEqual(status, 200)          # an answer, not an error
        self.assertIs(payload["supported"], False)
        self.assertEqual(backend.translate_calls, 0)

    def test_target_probe_answers_true_for_a_covered_code(self):
        status, payload = self._get(_StubBackend(pairs=["en->zh"], targets=["zh"]),
                                    "/languages?target=zh")
        self.assertEqual(status, 200)
        self.assertIs(payload["supported"], True)

    def test_probe_without_enumeration_answers_null_not_a_false(self):
        # "unknown" must stay distinguishable from "unsupported": a caller that
        # treats the two alike would drop a working language.
        status, payload = self._get(_LegacyBackend(), "/languages?target=tl")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["supported"])
        self.assertIn("note", payload)

    def test_legacy_backend_admits_it_does_not_enumerate(self):
        status, payload = self._get(_LegacyBackend(), "/languages")
        self.assertEqual(status, 200)
        self.assertFalse(payload["complete"])   # empty lists are not a full set
        self.assertIn("note", payload)


class WarmupContractTests(unittest.TestCase):
    """Consumer side of readiness: somebody has to turn "warming" into "ready".

    /readyz only reports the truth; it does not make the model resident. Without
    an explicit warm-up the first real translate is what pays the load, and a
    desktop app that wants to say "ready" has no way to get there without burning
    a sentence. It must also follow the /translate rule: asking to warm a
    language the backend cannot produce is permanent, so it is 422, not 503.
    """

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _post(self, backend, body=None):
        srv, port = serve_in_thread(backend, host="127.0.0.1", port=0)
        self.servers.append(srv)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/warmup",
                     body=(json.dumps(body) if body is not None else "{}"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_warming_backend_becomes_ready_and_reports_what_it_paid(self):
        backend = _StubBackend(ready=False)
        status, payload = self._post(backend, None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(backend.warmed)          # the load actually happened here
        self.assertIn("load_s", payload)         # and its cost is measurable

    def test_warming_an_already_resident_backend_is_cheap_and_idempotent(self):
        backend = _StubBackend(ready=True)
        status, payload = self._post(backend, None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertFalse(backend.warmed)         # nothing to do, nothing claimed
        self.assertEqual(payload["load_s"], 0.0)

    def test_warming_a_pair_only_loads_that_pair(self):
        # Argos is per direction: warming en->zh must not drag every other package
        # into memory, or the flag costs more than the lazy load it replaces.
        backend = _StubBackend(ready=False, pairs=["en->zh", "zh->en"])
        status, payload = self._post(backend, {"source": "en", "target": "zh"})
        self.assertEqual(status, 200)
        self.assertEqual(backend.warmed_pairs, [("en", "zh")])

    def test_unwarmable_language_is_422_not_503(self):
        # Same rule as /translate: no retry makes this succeed.
        backend = _StubBackend(ready=False, targets=["zh"])
        status, payload = self._post(backend, {"source": "en", "target": "xx"})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "unsupported_target")

    def test_backend_that_cannot_warm_says_so_instead_of_claiming_ready(self):
        status, payload = self._post(_LegacyBackend(), None)
        self.assertEqual(status, 501)
        self.assertEqual(payload["error"], "warmup_unknown")

    def test_mock_backend_warms_without_loading_anything(self):
        mock = MockBackend()
        self.assertTrue(mock.warm()["ready"])

    def test_a_warm_that_stayed_cold_is_not_reported_as_success(self):
        # Readiness, not "the handler ran", decides the code. Otherwise a caller that
        # treats 200 as "warm now" would be lied to by the bare-Argos case above.
        status, payload = self._post(_ColdAfterWarmBackend(), None)
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "not_ready")
        self.assertEqual(payload["status"], "warming")
        self.assertIn("note", payload)


class ConcurrentLoadTests(unittest.TestCase):
    """The service is threaded, so two first requests must not build two models.

    Both real backends materialise their translator lazily with a check-then-
    assign sequence. Under ThreadingHTTPServer two clients hitting a cold service
    concurrently can each pass the check and each construct a translator -- a
    2.95 GB MADLAD checkpoint duplicated in one process. External item 003 proved
    the output is not corrupted by it, so this is robustness, not correctness;
    it is fixed together with /warmup because warm-up is precisely the moment the
    duplicate load becomes likely (a desktop app may warm and translate at once).
    """

    class _CountingCt2:
        """Stands in for ctranslate2: counts Translator constructions."""

        def __init__(self):
            self.built = 0

        class _Translator:
            def __init__(self, owner, path, **kwargs):
                time.sleep(0.05)     # widen the race window on purpose
                owner.built += 1

        def Translator(self, path, **kwargs):
            return self._Translator(self, path, **kwargs)

    class _CountingSp:
        """Stands in for sentencepiece; also counts, so shared setup is visible."""

        def __init__(self):
            self.built = 0

        def SentencePieceProcessor(self, model_file=None):
            self.built += 1
            return object()

    def _race(self, build_once, attempts=6):
        results = []
        barrier = threading.Barrier(attempts)

        def worker():
            barrier.wait()          # all threads arrive as close as possible
            results.append(build_once())

        threads = [threading.Thread(target=worker) for _ in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_madlad_checkpoint_is_built_once_under_concurrent_first_requests(self):
        backend = MadladBackend.__new__(MadladBackend)   # skip the model check on disk
        backend._ct2 = self._CountingCt2()
        backend._root = "."
        backend._translator = None
        results = self._race(lambda: backend._engine())
        self.assertEqual(backend._ct2.built, 1,
                         "built %d translators for %d racing requests" %
                         (backend._ct2.built, len(results)))
        self.assertEqual(len(set(map(id, results))), 1)

    def test_argos_pair_is_built_once_under_concurrent_first_requests(self):
        backend = CT2Backend.__new__(CT2Backend)
        backend._ct2 = self._CountingCt2()
        backend._spm = self._CountingSp()
        backend._packages = {("en", "zh"): pathlib.Path(".")}
        backend._loaded = {}
        results = self._race(lambda: backend._pair("en", "zh"))
        self.assertEqual(backend._ct2.built, 1,
                         "built %d translators for %d racing requests" %
                         (backend._ct2.built, len(results)))
        self.assertEqual(len(set(map(id, results))), 1)

    def test_only_one_concurrent_warmer_claims_it_loaded_the_checkpoint(self):
        # Found by the live round trip, not by the tests above: with "was it cold when
        # I looked?" read outside the lock, every racing caller reported warmed=True
        # for the single load that only one of them actually paid for.
        backend = MadladBackend.__new__(MadladBackend)
        backend._ct2 = self._CountingCt2()
        backend._root = "."
        backend._translator = None
        reports = self._race(lambda: backend.warm())
        self.assertEqual(1, sum(1 for r in reports if not r["already"]),
                         "concurrent warmers all claimed the load: %r" % (reports,))

    def test_only_one_concurrent_warmer_claims_it_loaded_the_pair(self):
        backend = CT2Backend.__new__(CT2Backend)
        backend._ct2 = self._CountingCt2()
        backend._spm = self._CountingSp()
        backend._packages = {("en", "zh"): pathlib.Path(".")}
        backend._loaded = {}
        reports = self._race(lambda: backend.warm("en", "zh"))
        self.assertEqual(1, sum(1 for r in reports if not r["already"]))
        self.assertEqual(backend._ct2.built, 1)


class AbandonedRequestTests(unittest.TestCase):
    """A client that gives up has not stopped the handler, so "single flight" overlaps.

    Defect D24 was reported as a ladder of slowdown under a strictly sequential
    batch client, which reads as impossible until you remember that
    ThreadingHTTPServer keeps serving a socket nobody is reading any more: the
    retry overlaps the handler it replaced, and in the revision without
    _LOAD_LOCK every overlapping handler built its own 2.95 GB checkpoint.

    ConcurrentLoadTests pins the lock by calling _engine() directly. This pins it
    through a real socket and a real timeout -- the place where the overlap is
    actually produced, so a refactor that moves the load off the request path or
    hands each request its own backend is caught here instead of silently
    shipping. Needs no model and no third-party stack, so it runs on every
    platform and never skips.
    """

    LOAD_SECONDS = 0.6         # how long a checkpoint "takes" to materialise
    CLIENT_TIMEOUT = 0.05      # far shorter, so the client always gives up first

    class _SlowCt2:
        """ctranslate2 stand-in: a Translator construction is slow and counted."""

        def __init__(self, load_seconds):
            self.load_seconds = load_seconds
            self.built = 0      # finished constructions

        class _Translator:
            def __init__(self, owner):
                time.sleep(owner.load_seconds)
                owner.built += 1

        def Translator(self, path, **kwargs):
            return self._Translator(self)

    @staticmethod
    def _warm(port, timeout):
        """One POST /warmup over its own connection, with a client-side deadline."""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("POST", "/warmup", body=json.dumps({}),
                         headers={"Content-Type": "application/json"})
            return json.loads(conn.getresponse().read())
        finally:
            conn.close()

    def test_retries_after_client_timeouts_build_the_checkpoint_once(self):
        backend = MadladBackend.__new__(MadladBackend)   # skip the model check on disk
        ct2 = self._SlowCt2(self.LOAD_SECONDS)
        backend._ct2 = ct2
        backend._root = pathlib.Path(".")
        backend._translator = None

        srv, port = ct2_sidecar.serve_in_thread(backend)
        # A handler that is still working when its client hung up used to print a traceback for
        # it, and this test silenced `handle_error` to keep the suite output tidy -- which also
        # silenced the defect option A is about. The service now records that write quietly, so
        # the override is gone: if the noise ever comes back, this suite is where it shows.
        timeouts = 0
        built_at_last_timeout = None
        try:
            for _ in range(3):                          # strictly sequential: one in flight
                try:
                    self._warm(port, self.CLIENT_TIMEOUT)
                except (TimeoutError, socket.timeout):
                    timeouts += 1
                    built_at_last_timeout = ct2.built
            final = self._warm(port, self.LOAD_SECONDS + 2.0)
        finally:
            srv.shutdown()
            srv.server_close()

        # Anti-rot guards first: without them this test could quietly stop
        # exercising an overlap while still reporting green.
        self.assertGreaterEqual(timeouts, 2,
                                "no client timed out, so nothing was abandoned and "
                                "no handler ever overlapped")
        self.assertEqual(0, built_at_last_timeout,
                         "the abandoned handler had already finished loading; the "
                         "retry never met it, so retune LOAD_SECONDS/CLIENT_TIMEOUT")
        # The property D24 needs.
        self.assertEqual(1, ct2.built,
                         "%d handlers built %d checkpoints for one cold service"
                         % (timeouts + 1, ct2.built))
        self.assertEqual("ready", final.get("status"))
        self.assertFalse(final.get("warmed"),
                         "a later caller claimed a load it did not pay for: %r" % (final,))


class RegionCodeTests(unittest.TestCase):
    """pt-BR / zh-TW / es-419 have to fold, not fail.

    Product-side language codes are BCP-47 almost everywhere, so without a fold
    every integrator re-implements `tgt.split('-')[0]` and whichever one they
    forget errors in production - "one line in the engine, one line per caller and
    somebody always misses it". The fold is echoed, because a caller that asked
    for pt-BR and was served pt is entitled to see that in the response, and
    nothing is invented: when no fold resolves, the original code is passed to the
    backend and the outcome stays the backend's honest answer.
    """

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _send(self, backend, method, path, body=None):
        srv, port = serve_in_thread(backend, host="127.0.0.1", port=0)
        self.servers.append(srv)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path,
                     body=(json.dumps(body) if body is not None else None),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def _post(self, backend, path, body=None):
        return self._send(backend, "POST", path, body)

    def _get(self, backend, path):
        return self._send(backend, "GET", path)

    def test_primary_subtag_takes_the_language_subtag(self):
        cases = {"pt-BR": "pt", "zh-TW": "zh", "es-419": "es", "zh-Hans-CN": "zh",
                 "pt": "pt", "PT-br": "pt", "  ": "", "-BR": "", "pt-": "pt"}
        for code, want in cases.items():
            self.assertEqual(ct2_sidecar.primary_subtag(code), want, code)

    def test_translate_folds_a_region_code_it_cannot_serve(self):
        backend = _StubBackend(pairs=["en->pt"], targets=["pt"], sources=["en"])
        status, payload = self._post(
            backend, "/translate",
            {"text": "hello", "source": "en-US", "target": "pt-BR"})
        self.assertEqual(status, 200)
        self.assertEqual(backend.served, [("en", "pt")])   # the fold reached the backend
        self.assertEqual(payload.get("resolved_target"), "pt")
        self.assertEqual(payload.get("resolved_source"), "en")

    def test_an_exact_code_is_not_echoed(self):
        # Echo only what changed, or every response grows two keys nobody reads.
        backend = _StubBackend(pairs=["en->pt"], targets=["pt"], sources=["en"])
        status, payload = self._post(
            backend, "/translate", {"text": "hello", "source": "en", "target": "pt"})
        self.assertEqual(status, 200)
        self.assertNotIn("resolved_target", payload)
        self.assertNotIn("resolved_source", payload)

    def test_unfoldable_code_still_answers_422(self):
        backend = _StubBackend(pairs=["en->pt"], targets=["pt"], sources=["en"])
        status, payload = self._post(
            backend, "/translate", {"text": "hello", "source": "en", "target": "xx-YY"})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "unsupported_target")

    def test_nothing_is_invented_when_the_backend_cannot_fold(self):
        # targets=[] means "only the backend itself knows"; the handler must pass the
        # requested code through untouched rather than guessing pt-BR -> pt.
        backend = _StubBackend()
        status, payload = self._post(
            backend, "/translate", {"text": "hello", "source": "en-US", "target": "pt-BR"})
        self.assertEqual(status, 200)
        self.assertEqual(backend.served, [("en-US", "pt-BR")])
        self.assertNotIn("resolved_target", payload)

    def test_an_unfoldable_code_is_refused_by_the_backend_not_the_handler(self):
        # One voice about capability: the 422 must carry the message of the
        # component that owns the coverage list, so the handler has to forward the
        # original code rather than compose its own error string.
        backend = _StubBackend(pairs=["en->pt"], targets=["pt"], sources=["en"])
        status, payload = self._post(
            backend, "/translate", {"text": "hello", "source": "en", "target": "xh-XG"})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "unsupported_target")
        self.assertIn("no target language", payload["message"])
        self.assertEqual(backend.served, [("en", "xh-XG")])

    def test_probe_reports_the_folded_capability_as_supported(self):
        # 006's practical case: a batch caller deciding whether to bother the engine.
        backend = _StubBackend(pairs=["en->pt"], targets=["pt"], sources=["en"])
        status, payload = self._get(backend, "/languages?target=pt-BR")
        self.assertEqual(status, 200)
        self.assertIs(payload["supported"], True)
        self.assertEqual(payload.get("resolved_target"), "pt")

    def test_warmup_folds_too(self):
        # Otherwise /warmup {"target":"pt-BR"} would answer 422 while /translate on the
        # same code works - two endpoints disagreeing about one capability.
        backend = _StubBackend(pairs=["en->pt"], targets=["pt"], sources=["en"])
        status, payload = self._post(
            backend, "/warmup", {"source": "en-US", "target": "pt-BR"})
        self.assertEqual(status, 200)
        self.assertEqual(backend.warmed_pairs, [("en", "pt")])
        self.assertEqual(payload.get("resolved_target"), "pt")

    def test_a_backend_without_the_folding_hook_keeps_working(self):
        # _LegacyBackend exposes nothing new: the contract must stay additive.
        status, payload = self._post(
            _LegacyBackend(), "/translate",
            {"text": "hello", "source": "en", "target": "pt-BR"})
        self.assertEqual(status, 200)
        self.assertNotIn("resolved_target", payload)


class _BatchResult:
    """A ctranslate2 batch result; the translate path only reads .hypotheses[0]."""

    def __init__(self, hypothesis):
        self.hypotheses = [hypothesis]


class ConcurrentColdStartTests(unittest.TestCase):
    """Several clients on one cold service must still build one translator.

    Three shapes reach the same lock and only two were pinned. ConcurrentLoadTests
    races the loader in-process; AbandonedRequestTests gets there through a retry that
    overlapped a handler its client had abandoned. This is the shape a batch run starts
    in, and the first thing external item 004 did: several connections open at once,
    nobody timing out, nothing retried, and the 2.95 GB MADLAD checkpoint not resident
    yet. In the revision before _LOAD_LOCK every one of those handlers built its own.

    The checkpoint is a fake whose construction sleeps and counts, so this needs no
    model and no ctranslate2: it runs on every host and never skips, which is what
    REQ-F3's runtime-precondition column exists to make visible.
    """

    LOAD_SECONDS = 0.8       # long enough that a late-scheduled handler still arrives cold
    CLIENTS = 3
    SENTENCE = "Hello, please import the audio file."

    class _Ct2:
        """ctranslate2 stand-in: slow constructions, counted, and every decode counted.

        `built` is the property under test. `decodes` is the guard against winning it
        the wrong way -- a service that answered one request and dropped the other two
        would also report a single build.
        """

        def __init__(self, load_seconds):
            self.load_seconds = load_seconds
            self.built = 0
            self.live = 0
            self.peak = 0
            self.decodes = 0
            self._lock = threading.Lock()

        def Translator(self, path, **kwargs):
            return self._Translator(self)

        class _Translator:
            def __init__(self, owner):
                self.owner = owner
                with owner._lock:
                    owner.live += 1
                    owner.peak = max(owner.peak, owner.live)
                time.sleep(owner.load_seconds)      # what a checkpoint really costs
                with owner._lock:
                    owner.built += 1
                    owner.live -= 1

            def translate_batch(self, tokens, **kwargs):
                with self.owner._lock:
                    self.owner.decodes += 1
                return [_BatchResult(["<2zh>", "ok"])]

    class _Sp:
        """sentencepiece stand-in: enough for the vocabulary probe and one decode."""

        def SentencePieceProcessor(self, model_file=None):
            return self._Proc()

        class _Proc:
            def encode(self, text, out_type=str):
                head = text.split(" ", 1)[0]
                if head.startswith("<2") and head.endswith(">"):
                    return [head] + text.split(" ", 1)[1:]
                return text.split()

            def eos_id(self):
                return None

            def id_to_piece(self, piece_id):
                return "</s>"

            def decode(self, pieces):
                return " ".join(pieces)

    class _ObservingMadlad(MadladBackend):
        """MadladBackend that tallies the callers that reached it while still cold.

        Built this way because the premise can rot: on a single-threaded service the
        second request is only read after the first finished loading, so one build
        happens for a reason that has nothing to do with the lock, and the assertion
        below would stay green while testing nothing. The tally is taken on the real
        request path, before any waiting, so it says how many handlers met a cold
        service -- which is the whole point of the test.

        __init__ is not inherited: the parent's would demand ctranslate2 and a
        checkpoint on disk, and neither is what is under review here.
        """

        def __init__(self):
            self._ct2 = None
            self._spm = None
            self._root = pathlib.Path(".")
            self._translator = None     # cold, and every client is about to find it so
            self._known_targets = set()
            self.cold_entrants = 0
            self._probe_lock = threading.Lock()

        def translate(self, text, source, target, glossary=None):
            if self._translator is None:
                with self._probe_lock:
                    self.cold_entrants += 1
            return MadladBackend.translate(self, text, source, target, glossary)

    def test_concurrent_cold_requests_build_one_translator(self):
        backend = self._ObservingMadlad()
        ct2 = self._Ct2(self.LOAD_SECONDS)
        sp = self._Sp()
        backend._ct2 = ct2
        backend._spm = sp
        backend._processor = sp.SentencePieceProcessor()

        srv, port = ct2_sidecar.serve_in_thread(backend)
        gate = threading.Barrier(self.CLIENTS)
        record = threading.Lock()
        seen = []                      # (status, payload) per client

        def client():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            gate.wait()                # set off together, on separate connections
            try:
                conn.request("POST", "/translate",
                             body=json.dumps({"text": self.SENTENCE, "source": "en",
                                              "target": "zh"}),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                payload = json.loads(resp.read().decode("utf-8"))
                with record:
                    seen.append((resp.status, payload))
            finally:
                conn.close()

        threads = [threading.Thread(target=client) for _ in range(self.CLIENTS)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
        finally:
            srv.shutdown()
            srv.server_close()

        alive = [t for t in threads if t.is_alive()]
        self.assertFalse(alive, "clients never came back: %r" % (alive,))

        # Premise first, then the property -- in that order, so a future failure reads
        # as "this stopped testing an overlap" rather than as a broken lock.
        self.assertGreaterEqual(
            backend.cold_entrants, 2,
            "only %d of %d clients reached the service while it was cold, so no two"
            " handlers ever competed for the load and a single build here proves"
            " nothing (raise LOAD_SECONDS, or check the service is still threaded)"
            % (backend.cold_entrants, self.CLIENTS))

        self.assertEqual(self.CLIENTS, len(seen), "a cold burst lost a request: %r" % (seen,))
        self.assertEqual([200] * self.CLIENTS, sorted(code for code, _ in seen),
                         "every client must be served, not just the lucky first one: %r"
                         % (seen,))
        self.assertEqual(1, ct2.built,
                         "%d handlers met a cold service and built %d checkpoints of 2.95"
                         " GB each -- the duplicate load external item 004 measured"
                         % (backend.cold_entrants, ct2.built))
        self.assertEqual(1, ct2.peak,
                         "two translators were materialising at once even though one"
                         " survived: %r" % (ct2,))
        self.assertEqual(self.CLIENTS, ct2.decodes,
                         "one checkpoint served %d of %d requests: sharing the engine must"
                         " not mean dropping the work" % (ct2.decodes, self.CLIENTS))
        for _, payload in seen:
            self.assertTrue(payload.get("text"), "a client got an empty translation")


# ---- Multi-line and batch input (external item 007) -------------------------

class _PerLineStubBackend:
    """Answers one line at a time, and goes quiet on a poisoned line.

    The stub can only satisfy a multi-line request if the service splits the text before
    handing it over -- which is the point: measured against the real checkpoint, a blob with
    newlines comes back as a single run-on line of repeated tokens (item 007), and a 200
    with garbage in it is worse than an error.
    """

    name = "perline"

    def __init__(self):
        self.units = []                     # exactly what the service asked us to translate

    def translate(self, text, source, target, glossary=None):
        self.units.append(text)
        if "MISSING" in text:
            return ""                       # a segment the engine did not answer
        return "[%s] %s" % (target, text.strip())


class MultiLineContractTests(unittest.TestCase):
    def setUp(self):
        self.backend = _PerLineStubBackend()
        self.server, self.port = serve_in_thread(self.backend, host="127.0.0.1", port=0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/translate", json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        return resp.status, (json.loads(data) if data else {})

    def test_each_line_is_translated_as_its_own_unit(self):
        status, _ = self._post({"text": "one\n\nthree", "source": "en", "target": "zh"})
        self.assertEqual(200, status)
        self.assertEqual(["one", "three"], self.backend.units,
                         "the engine must be handed one line at a time; a blob is what makes"
                         " MADLAD run away with repeated tokens (item 007)")

    def test_line_structure_survives_a_multi_line_request(self):
        status, body = self._post({"text": "one\ntwo\n\nfour", "source": "en", "target": "zh"})
        self.assertEqual(200, status)
        lines = body["text"].split("\n")
        self.assertEqual(4, len(lines), "the answer must keep the caller's line count: %r" % (lines,))
        self.assertEqual("", lines[2], "a blank line is structure, not a gap to close")
        self.assertEqual(["[zh] one", "[zh] two", "", "[zh] four"], lines)

    def test_a_segment_that_came_back_empty_is_reported_not_left_silent(self):
        # REQ-B2: an unanswered line must not look like a translation of nothing.
        status, body = self._post(
            {"text": "fine\nMISSING\nalso fine", "source": "en", "target": "zh"})
        self.assertEqual(200, status)
        self.assertEqual([1], body.get("failed_lines"),
                         "the caller has to see which line the engine did not answer: %r" % (body,))
        self.assertEqual(3, len(body["text"].split("\n")))

    def test_whitespace_only_text_is_still_rejected(self):
        status, _ = self._post({"text": "   \n  ", "source": "en", "target": "zh"})
        self.assertEqual(400, status, "blank lines are structure, but a blank request is not work")

    def test_array_input_keeps_order_and_count(self):
        status, body = self._post({"texts": ["b", "", "a"], "source": "en", "target": "zh"})
        self.assertEqual(200, status)
        self.assertEqual(["[zh] b", "", "[zh] a"], body["texts"],
                         "a batch answer must line up index by index with the request")
        self.assertEqual(["b", "a"], self.backend.units, "the empty entry is not a model call")

    def test_the_batch_names_a_failing_segment_by_index(self):
        status, body = self._post({"texts": ["ok", "MISSING"], "source": "en", "target": "zh"})
        self.assertEqual(200, status)
        self.assertEqual(["[zh] ok", ""], body["texts"])
        self.assertEqual([1], body["failed_lines"])

    def test_text_and_texts_together_are_rejected(self):
        status, body = self._post({"text": "a", "texts": ["b"], "source": "en", "target": "zh"})
        self.assertEqual(400, status, "one request, one shape of input, never both")
        self.assertIn("error", body)

    def test_array_items_must_be_strings(self):
        status, _ = self._post({"texts": ["a", 3], "source": "en", "target": "zh"})
        self.assertEqual(400, status)

    def test_an_empty_array_is_rejected(self):
        status, _ = self._post({"texts": [], "source": "en", "target": "zh"})
        self.assertEqual(400, status)

    def test_a_permanent_target_stays_permanent_across_a_batch(self):
        # One bad target code is a target-wide fact, not a per-line accident: it must not be
        # downgraded to a partial success (defect D22's distinction, applied to a batch).
        class _RejectingBackend(_PerLineStubBackend):
            def translate(self, text, source, target, glossary=None):
                raise ct2_sidecar.UnsupportedTarget("no such target")

        self.server.shutdown()
        self.server.server_close()
        rejecting = _RejectingBackend()
        self.server, self.port = serve_in_thread(rejecting, host="127.0.0.1", port=0)
        status, body = self._post({"texts": ["a", "b"], "source": "en", "target": "xx"})
        self.assertEqual(422, status, body)
        self.assertEqual("unsupported_target", body["error"])


class RealMadladLineTests(unittest.TestCase):
    """Item 007 measured on the actual checkpoint, through the wire.

    MADLAD decodes deterministically (verified for this build: identical input twice gives
    identical output), so a line translated inside a multi-line request has to read exactly
    as it does when sent alone. That is a strong equality, and before the fix it failed
    spectacularly: three lines in came back as one line of repeated tokens with none of the
    source content present.
    """

    LINES = ["The buffer is too small for one frame.", "Sample rate must match the device."]

    @classmethod
    def setUpClass(cls):
        model_dir = default_model_dir().parent / "madlad"
        if not model_dir.is_dir():
            raise unittest.SkipTest(
                "no MADLAD checkpoint under {}; run script/fetch_madlad_model.py"
                .format(model_dir))
        try:
            cls.backend = MadladBackend(model_dir=str(model_dir))
        except MissingDependency as exc:
            raise unittest.SkipTest("madlad stack unavailable: {}".format(exc))

    def test_multi_line_request_matches_line_by_line_on_the_real_model(self):
        solo = [self.backend.translate(line, "en", "zh") for line in self.LINES]
        server, port = serve_in_thread(self.backend, host="127.0.0.1", port=0)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
            conn.request("POST", "/translate",
                         json.dumps({"text": "\n".join(self.LINES),
                                     "source": "en", "target": "zh"}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(200, resp.status, body)
        lines = body["text"].split("\n")
        self.assertEqual(len(self.LINES), len(lines),
                         "a two-line request answered with %d lines: %r" % (len(lines), lines))
        self.assertEqual(solo, lines,
                         "each line must read as it does when sent alone: solo=%r blob=%r"
                         % (solo, lines))
        self.assertFalse(body.get("failed_lines"), body)

    def test_batch_array_matches_the_same_answers_on_the_real_model(self):
        solo = [self.backend.translate(line, "en", "zh") for line in self.LINES]
        server, port = serve_in_thread(self.backend, host="127.0.0.1", port=0)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
            conn.request("POST", "/translate",
                         json.dumps({"texts": self.LINES, "source": "en", "target": "zh"}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(200, resp.status, body)
        self.assertEqual(solo, body["texts"],
                         "one round trip must not change what any segment becomes: %r" % (body,))


# ---- Output sanitisation (external item 009) ----------------------------------

class OutputSanitizationTests(unittest.TestCase):
    """An invisible codepoint in a translation silently poisons everything downstream
    (dedup, search, cache keys, audit scripts) -- but some of those codepoints *are* the
    orthography of the language we were asked to write, so a blanket wipe is its own bug.

    The rule under test: a zero-width or formatting mark survives if the caller's own
    source text carried that codepoint, or if the target script needs it; everything
    else goes. Presence-based rather than counting, because a script may legitimately
    double a separator and guessing the count would delete real text.
    """

    ZWSP = "\u200b"
    ZWNJ = "\u200c"
    ZWJ = "\u200d"
    WJ = "\u2060"
    BOM = "\ufeff"
    SHY = "\u00ad"
    RLO = "\u202e"
    LRI = "\u2066"

    def test_the_reported_lao_case_loses_its_zwsp(self):
        # 009 verbatim, codepoint evidence rather than console rendering: target='lo',
        # "Hello" -> LAO LAO ZERO LAO LAO LAO LAO LAO. Reproduced with no concurrency,
        # so the model emits it; it is not decoder crosstalk.
        self.assertEqual(
            "\u0e82\u0ecd\u0e82\u0ead\u0e9a\u0ec3\u0e88",
            ct2_sidecar.sanitize_output("\u0e82\u0ecd" + self.ZWSP + "\u0e82\u0ead\u0e9a\u0ec3\u0e88",
                                        "lo", "Hello"))

    def test_ordinary_text_comes_back_byte_for_byte(self):
        for target, text in (("zh", "\u4f60\u597d\uff0c\u4e16\u754c\u3002"),
                             ("en", "Hello, world!"),
                             ("ja", "\u304a\u306f\u3088\u3046\u3054\u3056\u3044\u307e\u3059"),
                             ("ru", "\u041f\u0440\u0438\u0432\u0435\u0442, \u043c\u0438\u0440!"),
                             ("ar", "\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645"),
                             ("de", "Stra\u00dfe \u2013 Auto")):
            self.assertEqual(text, ct2_sidecar.sanitize_output(text, target, "Hello, world!"),
                             "%s: sanitising must not rewrite anything visible" % target)

    def test_the_wipe_list_goes_for_every_language(self):
        dirty = "a" + self.BOM + self.WJ + self.SHY + self.RLO + self.LRI + "b"
        for target in ("en", "lo", "fa", "km", "ml"):
            self.assertEqual("ab", ct2_sidecar.sanitize_output(dirty, target, "ab"),
                             "%s: BOM, word joiner, soft hyphen and bidi marks carry no"
                             " text in any script" % target)

    def test_control_characters_go_but_line_structure_stays(self):
        # Item 007 is still open: callers do send multi-line text, and the sanitiser must
        # not quietly become the thing that flattens it.
        self.assertEqual("ab\tc\nd", ct2_sidecar.sanitize_output(
            "a\u0001b\tc\nd\u009f", "zh", "a\u0001b\tc\nd\u009f"))

    def test_the_persian_half_space_survives_because_the_script_needs_it(self):
        # 009's own warning, and it holds even when the source had no ZWNJ at all:
        # deleting U+200C from fa/ps/ckb/ug is an orthographic error, not a cleanup.
        text = "\u06a9\u062a\u0627\u0628" + self.ZWNJ + "\u062e\u0627\u0646\u0647"
        for target in ("fa", "ps", "ckb", "ug"):
            self.assertEqual(text, ct2_sidecar.sanitize_output(text, target, "library office"),
                             "%s: the zero-width non-joiner IS the half-space" % target)

    def test_khmer_and_myanmar_word_separators_survive(self):
        # "Strip every ZWSP" is exactly what 009 proposes, and for these two scripts it
        # would write them wrong: U+200B is their conventional word separator.
        text = "\u1780\u17d2\u1798\u1796\u17bb\u179f\u17b6" + self.ZWSP + "\u1798\u17a1\u17b6\u1787\u17d2\u1793"
        for target in ("km", "my"):
            self.assertEqual(text, ct2_sidecar.sanitize_output(text, target, "Cambodia people"),
                             "%s: ZWSP is word segmentation here" % target)

    def test_a_joiner_the_source_already_had_is_data_not_noise(self):
        # A family emoji in a UI string carries a real ZWJ, and Indic conjuncts use the
        # same codepoint; "the caller's text has it" is what keeps both.
        emoji = "\U0001F468" + self.ZWJ + "\U0001F469"
        self.assertEqual(emoji, ct2_sidecar.sanitize_output(emoji, "en", emoji))
        self.assertEqual("a" + self.ZWJ + "b" + self.ZWJ,
                         ct2_sidecar.sanitize_output("a" + self.ZWJ + "b" + self.ZWJ, "en", "x" + self.ZWJ),
                         "presence-based, not counting: one joiner in the source does not"
                         " cap the output at one")

    def test_a_joiner_a_script_cannot_use_is_still_dropped(self):
        self.assertEqual("\u4f60\u597d", ct2_sidecar.sanitize_output(
            "\u4f60" + self.ZWJ + "\u597d", "zh", "hello"))

    def test_the_region_tag_is_folded_before_deciding(self):
        # A qualifier must not turn a protected script into an unprotected one, and the
        # handler passes the code the engine actually served anyway.
        fa = "\u06a9\u062a\u0627\u0628" + self.ZWNJ + "\u062e\u0627\u0646\u0647"
        self.assertEqual(fa, ct2_sidecar.sanitize_output(fa, "fa-IR", "book"))
        self.assertEqual("ab", ct2_sidecar.sanitize_output("a" + self.ZWSP + "b", "zh-CN", "ab"))

    def test_nothing_to_clean_and_nothing_to_return(self):
        self.assertEqual("", ct2_sidecar.sanitize_output("", "en", ""))
        self.assertEqual("", ct2_sidecar.sanitize_output("", "fa", "\u06a9\u062a\u0627\u0628"))

    def test_cleaning_twice_changes_nothing(self):
        dirty = "\u0e82\u0ecd" + self.ZWSP + "x" + self.SHY + self.BOM
        once = ct2_sidecar.sanitize_output(dirty, "lo", "Hello")
        self.assertEqual(once, ct2_sidecar.sanitize_output(once, "lo", "Hello"))


class _DirtyOutputBackend:
    """Answers with the invisible characters external item 009 measured, so the wire is
    what gets tested and not only the helper next to it."""

    name = "dirty"

    def translate(self, text, source, target, glossary=None):
        if target == "lo":
            return "\u0e82\u0ecd\u200b\u0e82\u0ead\u0e9a\u0ec3\u0e88"
        if target == "fa":
            return "\u06a9\u062a\u0627\u0628\u200c\u062e\u0627\u0646\u0647"
        if target == "km":
            return "\u1780\u17d2\u1798\u1796\u17bb\u179f\u17b6\u200b\u1798\u17a1\u17b6\u1787\u17d2\u1793"
        return "a\u200b\u202e\ufeffb"


class OutputSanitizationOverHttpTests(unittest.TestCase):
    """The batch caller's contract: nothing invisible comes back, nothing the script
    needs goes missing -- decided at the response, so every backend is covered at once."""

    def setUp(self):
        self.server, self.port = serve_in_thread(_DirtyOutputBackend(), host="127.0.0.1", port=0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _translate(self, text, target):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/translate",
                     json.dumps({"text": text, "source": "en", "target": target}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return body

    def test_the_wire_is_clean_for_the_reported_case(self):
        self.assertEqual("\u0e82\u0ecd\u0e82\u0ead\u0e9a\u0ec3\u0e88", self._translate("Hello", "lo")["text"])

    def test_the_wire_keeps_the_orthography_that_needs_zero_width(self):
        self.assertEqual("\u06a9\u062a\u0627\u0628\u200c\u062e\u0627\u0646\u0647",
                         self._translate("library", "fa")["text"])
        self.assertEqual("\u1780\u17d2\u1798\u1796\u17bb\u179f\u17b6\u200b\u1798\u17a1\u17b6\u1787\u17d2\u1793",
                         self._translate("Cambodia", "km")["text"])

    def test_the_wipe_list_never_reaches_the_wire(self):
        for target in ("en", "zh", "de"):
            self.assertEqual("ab", self._translate("hello", target)["text"],
                             "%s: ZWSP, bidi override and BOM must not ship" % target)


# ---- batch lifecycle: reload and self-exit (defect D30 items 3 and 4) -------

class _ReloadableBackend:
    """A backend with a resident engine that can be dropped on command.

    It mirrors what MadladBackend and Ct2Backend really are -- one loaded object whose
    absence the next request pays to rebuild -- so the assertions below are about the
    service's behaviour, not about a call being counted.
    """

    name = "reloadable"

    def __init__(self):
        self.resident = False
        self.discarded = []
        self.reloads = 0
        self.load_calls = 0
        self.served = 0

    def translate(self, text, source, target, glossary=None):
        if not self.resident:
            self.load_calls += 1
            self.resident = True
        self.served += 1
        return "ok: {}".format(target)

    def readiness(self):
        return {"ready": self.resident,
                "reason": None if self.resident else "model_not_loaded",
                "loaded": ["engine"] if self.resident else []}

    def warm(self, source=None, target=None):
        was = self.resident
        if not was:
            self.load_calls += 1
            self.resident = True
        return {"ready": True, "already": was}

    def reload(self):
        """Drop what is resident and say what went."""
        self.reloads += 1
        dropped = ["engine"] if self.resident else []
        self.resident = False
        self.discarded.append(dropped)
        return {"discarded": dropped}


class ReloadEndpointTests(unittest.TestCase):
    """Item 004 asks for a way to reset an engine without killing the process.

    Its callers were restarting the sidecar per language -- kill, wait for the port, relaunch,
    verify with a real translation -- and that loop is pure cost they wanted the service to
    carry. `POST /reload` is that: drop the resident checkpoint, keep the port.

    Two things are deliberately not offered. Reloading is not warm by default: re-materialising
    this checkpoint measured 4.2-5.5 s on this build, and the desktop client probes the service
    with a 1500 ms HTTP timeout, so a synchronous reload would read as an outage to exactly the
    callers this endpoint exists to help. And the answer never claims memory came back --
    whether the OS reclaims it is a measured question, not a promise.
    """

    def setUp(self):
        self.backend = _ReloadableBackend()
        self.server, self.port = serve_in_thread(self.backend, host="127.0.0.1", port=0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, body

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, body

    def test_a_backend_that_cannot_reload_says_so_and_keeps_its_model(self):
        # Same honesty shape as warmup_unknown: 200 would claim a reset that never happened
        # and 503 would claim an outage.
        self.server.backend = _LegacyBackend()
        status, body = self._post("/reload", {})
        self.assertEqual(501, status, body)
        self.assertEqual("reload_unsupported", body["error"])
        self.assertEqual("legacy", body["backend"])

    def test_reload_drops_the_resident_engine_and_says_what_went(self):
        self.assertEqual(200, self._post("/translate",
                        {"text": "hello", "source": "en", "target": "zh"})[0])
        self.assertTrue(self.backend.resident, "the request above must have loaded it")
        status, body = self._post("/reload", {})
        self.assertEqual(200, status, body)
        self.assertEqual(["engine"], body["discarded"],
                         "the caller has to see that something was actually dropped: %r" % body)
        self.assertFalse(self.backend.resident,
                         "reload must not have reloaded it behind the caller's back")
        # Reported with /readyz vocabulary rather than a third word for the same fact.
        self.assertEqual("warming", body["status"])
        self.assertEqual("reloadable", body["backend"])
        self.assertEqual(1, self.backend.reloads)
        ready_status, ready_body = self._get("/readyz")
        self.assertEqual(503, ready_status, ready_body)
        self.assertEqual("model_not_loaded", ready_body["reason"])

    def test_after_a_reload_the_service_answers_and_the_engine_comes_back(self):
        # The failure mode this guards against is a reset that takes the service down with it.
        self._post("/translate", {"text": "hello", "source": "en", "target": "zh"})
        self._post("/reload", {})
        status, body = self._post("/translate", {"text": "again", "source": "en", "target": "zh"})
        self.assertEqual(200, status, body)
        self.assertEqual("ok: zh", body["text"])
        self.assertTrue(self.backend.resident)
        self.assertEqual(2, self.backend.load_calls,
                         "two cold loads: the first request and the one after the reload")

    def test_an_explicit_warm_pays_the_load_inside_the_call_and_proves_it(self):
        self._post("/translate", {"text": "hello", "source": "en", "target": "zh"})
        status, body = self._post("/reload", {"warm": True})
        self.assertEqual(200, status, body)
        self.assertEqual("ready", body["status"])
        self.assertTrue(body["reloaded"], "a warm reload did reload: %r" % body)
        self.assertTrue(self.backend.resident)
        # The stopwatch only runs when this call paid a load (the /warmup discipline).
        self.assertGreaterEqual(body["load_s"], 0.0)

    def test_a_reload_with_nothing_resident_is_not_reported_as_a_drop(self):
        # Nothing was loaded yet, so claiming a discard would teach the caller to read a
        # report that invents its own content.
        status, body = self._post("/reload", {})
        self.assertEqual(200, status, body)
        self.assertEqual([], body["discarded"])
        self.assertEqual("warming", body["status"])

    def test_a_warm_that_loaded_nothing_does_not_claim_a_reload(self):
        # Argos deliberately refuses to load every installed package on a bare request, so a warm
        # can honestly leave the service cold. Calling that a reload would hand the caller a
        # field that invents its own content -- the same reason /warmup keeps load_s at zero.
        class _RefusesToLoadAnything(_ReloadableBackend):
            def warm(self, source=None, target=None):
                return {"ready": False, "already": False, "reason": "no pair named"}

        self.server.backend = _RefusesToLoadAnything()
        status, body = self._post("/reload", {"warm": True})
        self.assertEqual(200, status, body)
        self.assertFalse(body["reloaded"],
                         "nothing became ready, so no reload happened: %r" % body)
        self.assertEqual(0.0, body["load_s"])
        self.assertEqual("warming", body["status"])

    def test_a_reloadable_backend_without_warm_says_which_half_happened(self):
        # Third-party duck-typed backends exist in this contract already (that is why /readyz
        # answers 501 instead of guessing). One that can drop but not reload must not fail the
        # call -- the drop is real and the caller has to learn it happened.
        class _DropOnlyBackend:
            name = "droponly"

            def __init__(self):
                self.resident = True

            def readiness(self):
                return {"ready": self.resident, "reason": None, "loaded": []}

            def reload(self):
                self.resident = False
                return {"discarded": ["engine"]}

        self.server.backend = _DropOnlyBackend()
        status, body = self._post("/reload", {"warm": True})
        self.assertEqual(200, status, body)
        self.assertEqual(["engine"], body["discarded"])
        self.assertFalse(body["reloaded"], "nothing was reloaded: %r" % body)
        self.assertIn("cannot warm", body["note"])

    def test_reload_is_post_only(self):
        status, body = self._get("/reload")
        self.assertEqual(404, status, body)

    def test_a_permanent_code_stays_permanent_through_a_reload(self):
        # Warming on reload goes through the same resolution, so an impossible target must
        # keep answering 422 instead of becoming a fresh 503 -- the D22 distinction has to
        # survive the new endpoint rather than be re-implemented worse next to it.
        class _TargetRejecting(_ReloadableBackend):
            def warm(self, source=None, target=None):
                raise ct2_sidecar.UnsupportedTarget("no such target")

        self.server.backend = _TargetRejecting()
        status, body = self._post("/reload", {"warm": True, "target": "xx"})
        self.assertEqual(422, status, body)
        self.assertEqual("unsupported_target", body["error"])


class BatchBudgetTests(unittest.TestCase):
    """A bounded batch, so a degrading engine retires itself instead of hanging.

    Item 004 measured 130 s, then 280 s, then over 900 s for the same workload, and the
    caller's only recovery was to notice the wall clock and taskkill the process. A
    request budget means a long batch can be run against a process that is known to be
    healthy for exactly N calls, and the exit is announced in the answer that carries it.

    The budget is deliberately opt-in (no default), because the desktop client supervises
    its own sidecars and an unannounced exit would read as a crash there. And it reads the
    same counter /health already reports, rather than keeping a second tally that could
    disagree with the first about whether a 422 cost anything.
    """

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _serve(self, backend, max_requests=None):
        srv, port = serve_in_thread(backend, host="127.0.0.1", port=0,
                                   max_requests=max_requests)
        self.servers.append(srv)
        return srv, port

    def _translate(self, port, target="zh"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/translate", json.dumps(
            {"text": "hello", "source": "en", "target": target}),
            headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, body

    def _health(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return body

    def test_no_budget_means_no_countdown_invented(self):
        srv, port = self._serve(_StubBackend(ready=True, targets=["zh"]))
        body = self._health(port)
        self.assertIsNone(body["requests_until_exit"],
                          "an unbounded service must not report a countdown it will never use")
        self._translate(port)
        self.assertFalse(srv.exit_requested)
        self.assertIsNone(srv.exit_reason)

    def test_the_countdown_lives_in_the_same_counter_health_already_reports(self):
        srv, port = self._serve(_StubBackend(ready=True, targets=["zh"]), max_requests=3)
        self.assertEqual(3, self._health(port)["requests_until_exit"])
        self._translate(port)
        self.assertEqual(2, self._health(port)["requests_until_exit"])
        self._translate(port)
        self.assertEqual(1, self._health(port)["requests_until_exit"])
        self.assertFalse(srv.exit_requested, "the last permit is the third call, not the second")

    def test_the_last_permit_is_announced_in_the_answer_that_uses_it(self):
        srv, port = self._serve(_StubBackend(ready=True, targets=["zh"]), max_requests=2)
        _status, first = self._translate(port)
        self.assertNotIn("shutting_down", first,
                         "a call that is not the last must not read like one: %r" % first)
        _status, last = self._translate(port)
        self.assertIn("shutting_down", last,
                      "the caller cannot schedule a relaunch from a signal it never sees")
        self.assertEqual("max_requests", last["shutting_down"]["reason"])
        self.assertEqual(2, last["shutting_down"]["requests_served"])
        self.assertEqual("ok: zh", last["text"], "the announcement is additive, not a refusal")
        self.assertTrue(srv.exit_requested)
        self.assertIn("max_requests", srv.exit_reason)

    def test_a_refused_target_spends_the_same_budget_as_an_answer(self):
        # One tally, one truth: a 422 reaches the engine and is counted by /health today,
        # so it must also spend the permit. A second counter would disagree about whether a
        # rejected code costs anything, and the caller would be left guessing.
        srv, port = self._serve(_StubBackend(ready=True, targets=["zh"]), max_requests=1)
        status, body = self._translate(port, target="xx")
        self.assertEqual(422, status, body)
        self.assertTrue(srv.exit_requested,
                        "a spent budget must retire the process even if the answer was an error")

    def test_zero_and_negative_budgets_mean_unbounded_not_immediate_exit(self):
        # "--max-requests 0" reading as "exit after this very call" is the kind of surprise
        # that stops a batch nobody meant to bound.
        self.assertIsNone(ct2_sidecar.resolve_max_requests(0))
        self.assertIsNone(ct2_sidecar.resolve_max_requests(None))
        self.assertIsNone(ct2_sidecar.resolve_max_requests(-5))
        self.assertEqual(1500, ct2_sidecar.resolve_max_requests(1500))


class RealMadladReloadTests(unittest.TestCase):
    """The reset on the checkpoint that item 004 was actually run against.

    The stub proves the endpoint's shape; this proves the one thing a caller cares about:
    after a reload the service still answers correctly, and it is honest about being cold
    in between -- on the 2.95 GB MADLAD-400 engine whose restart cost the reporter was
    paying by hand.
    """

    @classmethod
    def setUpClass(cls):
        model_dir = default_model_dir().parent / "madlad"
        if not model_dir.is_dir():
            raise unittest.SkipTest(
                "no MADLAD checkpoint under {}; run script/fetch_madlad_model.py"
                .format(model_dir))
        try:
            cls.backend = MadladBackend(model_dir=str(model_dir))
        except MissingDependency as exc:
            raise unittest.SkipTest("madlad stack unavailable: {}".format(exc))

    def test_a_reload_leaves_the_real_engine_able_to_answer_again(self):
        question = "The buffer is too small for one frame."
        before = self.backend.translate(question, "en", "zh")
        self.assertTrue(self.backend.readiness()["ready"])
        server, port = serve_in_thread(self.backend, host="127.0.0.1", port=0)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
            conn.request("POST", "/reload", json.dumps({}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            self.assertEqual(200, resp.status, body)
            self.assertEqual("warming", body["status"])
            self.assertTrue(body["discarded"], "the checkpoint was resident, so something went: %r"
                            % body)
            self.assertFalse(self.backend.readiness()["ready"],
                             "the drop must be real, not a report over a live engine")

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
            conn.request("POST", "/translate",
                         json.dumps({"text": question, "source": "en", "target": "zh"}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            after = json.loads(resp.read().decode("utf-8"))
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(200, resp.status, after)
        self.assertEqual(before, after["text"],
                         "the same sentence must read the same after a reset: before=%r after=%r"
                         % (before, after.get("text")))
        self.assertTrue(self.backend.readiness()["ready"])


# ---- Concurrent-decode gate (defect D30 item 2) -----------------------------

class _GatedBackend:
    """A backend whose decode the test can hold open.

    A gate is only observable if a decode is provably still running when the next request
    arrives. Blocking on an event the test controls is what makes that checkable: sleeping
    would turn the suite into a bet on how fast the machine happens to be, which is how the
    "flat on a busy host" mistake in the batch probe was made.
    """

    name = "gated"

    def __init__(self, fail=False):
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()
        self.fail = fail
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.calls = 0

    def wait_for_entry(self, count=1):
        """Block until `count` decodes have actually started (never guess a schedule)."""
        for _ in range(count):
            self.entered.acquire(timeout=30)

    def translate(self, text, source, target, glossary=None):
        with self._lock:
            self.calls += 1
            self.live += 1
            self.peak = max(self.peak, self.live)
        self.entered.release()
        self.release.wait(30)
        with self._lock:
            self.live -= 1
        if self.fail:
            raise ct2_sidecar.MissingDependency("model went away")
        return "ok: {0}".format(target)

    def readiness(self):
        return {"ready": True, "reason": None, "loaded": ["gated"]}


class DecodeGateTests(unittest.TestCase):
    """Bound the number of decodes running at once, and say so instead of queueing.

    Measured cause (D30 item 2, `docs/tech/16`): a request whose caller timed out keeps
    decoding -- 73% of a full decode is still spent after the caller is gone -- and every
    one of those abandoned decodes slows the work that is still wanted, up to 4.61x at five
    in flight. That is the positive feedback item 004 reported as a ladder; "restart per
    language" only worked because it killed the in-flight decodes with the process.

    Choosing the limit needs measurements, so the gate ships switchable and OFF by default:
    an unrequested serialisation would change latency for every existing caller, and the
    desktop client is not built to expect a 429. Overflow is refused rather than queued by
    default because a queued caller is still waiting on the other side of its own timeout --
    which is the exact failure this gate exists to stop.
    """

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _serve(self, backend, decode_slots=0, queue_wait_ms=0, max_requests=None):
        srv, port = ct2_sidecar.serve_in_thread(backend, host="127.0.0.1", port=0,
                                               max_requests=max_requests,
                                               decode_slots=decode_slots,
                                               queue_wait_ms=queue_wait_ms)
        self.servers.append(srv)
        return srv, port

    def _post(self, port, text="hello", timeout=20):
        started = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("POST", "/translate", json.dumps(
                {"text": text, "source": "en", "target": "zh"}),
                headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            headers = dict(resp.getheaders())
        finally:
            conn.close()
        return resp.status, body, headers, time.monotonic() - started

    def _health(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def test_no_gate_is_the_default_and_overlapping_decodes_both_run(self):
        backend = _GatedBackend()
        _srv, port = self._serve(backend)
        done = {}
        first = threading.Thread(target=lambda: done.setdefault("a", self._post(port)))
        first.start()
        backend.wait_for_entry()
        second = threading.Thread(target=lambda: done.setdefault("b", self._post(port)))
        second.start()
        backend.wait_for_entry(1)          # the second decode really started too
        backend.release.set()
        first.join(timeout=30)
        second.join(timeout=30)
        self.assertEqual(2, backend.peak,
                         "without a gate the service must still run decodes concurrently")
        self.assertEqual([200, 200], [done["a"][0], done["b"][0]])

    def test_one_slot_turns_the_second_caller_away_while_the_first_decodes(self):
        backend = _GatedBackend()
        _srv, port = self._serve(backend, decode_slots=1)
        results = {}
        held = threading.Thread(target=lambda: results.setdefault("slow", self._post(port)))
        held.start()
        backend.wait_for_entry()
        status, body, headers, seconds = self._post(port)
        # The rejection came back while the first decode is still blocked -- a caller that
        # timed out must not have to wait out the work it already gave up on.
        self.assertEqual(1, backend.live,
                         "the rejection arrived after the held decode finished, so it waited")
        self.assertEqual(429, status, body)
        self.assertEqual("busy", body["error"])
        self.assertEqual(1, body["decode_slots"])
        self.assertEqual(1, body["decodes_in_flight"],
                         "the answer has to name what is holding the engine")
        self.assertIn("Retry-After", headers,
                      "a rejection without a retry hint teaches callers to hammer")
        self.assertLess(seconds, 5.0, "busy must be answered fast, not after a queue: %.2fs" % seconds)
        self.assertNotIn("text", body, "a refused request must not look like an empty translation")
        backend.release.set()
        held.join(timeout=30)
        self.assertEqual(200, results["slow"][0], results["slow"][1])

    def test_two_slots_run_two_decodes_and_refuse_the_third(self):
        backend = _GatedBackend()
        _srv, port = self._serve(backend, decode_slots=2)
        results = {}
        threads = [threading.Thread(target=lambda i=i: results.setdefault(
            i, self._post(port, "sentence %d" % i))) for i in (0, 1)]
        for t in threads:
            t.start()
        backend.wait_for_entry(2)
        # The third is asked from the main thread: both slots are provably held already, so a
        # refusal must come back at once. Waiting for it to "enter" a decode would be waiting
        # for something the gate is designed to prevent -- which is exactly how a first draft of
        # this test hung for 30 s and then reported a timeout as if it were the product's.
        results[2] = self._post(port, "third")
        self.assertEqual(429, results[2][0], results[2][1])
        self.assertEqual(2, results[2][1]["decodes_in_flight"])
        self.assertEqual(2, backend.peak, "two slots must allow two decodes, not one")
        backend.release.set()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual([200, 200], [results[0][0], results[1][0]],
                         "a held decode ended badly: %r %r" % (results[0][1], results[1][1]))

    def test_a_waiting_caller_gets_served_when_a_slot_opens(self):
        backend = _GatedBackend()
        _srv, port = self._serve(backend, decode_slots=1, queue_wait_ms=10000)
        results = {}
        held = threading.Thread(target=lambda: results.setdefault("slow", self._post(port)))
        held.start()
        backend.wait_for_entry()
        waiter = threading.Thread(target=lambda: results.setdefault("wait", self._post(port)))
        waiter.start()
        # Free the slot rather than wait for the waiter to be refused: whether it had already
        # reached the gate or reaches it a moment later, either way it must be served, and the
        # peak below is what proves it never decoded alongside the first.
        backend.release.set()
        held.join(timeout=30)
        waiter.join(timeout=30)
        self.assertEqual(200, results["wait"][0],
                         "a queueing caller was asked to give up too early: %r" % results["wait"][1])
        self.assertEqual(1, backend.peak,
                         "a one-slot gate let two decodes run at once (peak %d)" % backend.peak)

    def test_a_decode_that_fails_gives_its_slot_back(self):
        # A leaked permit would be the worst kind of regression here: the service would look
        # healthy on /health and refuse everything forever after one broken model.
        backend = _GatedBackend(fail=True)
        _srv, port = self._serve(backend, decode_slots=1)
        results = {}
        held = threading.Thread(target=lambda: results.setdefault("bad", self._post(port)))
        held.start()
        backend.wait_for_entry()
        backend.release.set()
        held.join(timeout=30)
        self.assertEqual(503, results["bad"][0], results["bad"][1])
        ok = _GatedBackend()
        ok.release.set()
        _srv.backend = ok
        status, body, _headers, _t = self._post(port)
        self.assertEqual(200, status, "the slot was not returned after a failure: %r" % body)

    def test_a_busy_rejection_does_not_spend_the_request_budget(self):
        # The budget exists to retire a degrading process. Charging it for work that never
        # happened would let a busy service exit while still healthy -- and a caller retrying
        # into it would be killing the engine it is waiting for.
        backend = _GatedBackend()
        _srv, port = self._serve(backend, decode_slots=1, max_requests=2)
        results = {}
        held = threading.Thread(target=lambda: results.setdefault("slow", self._post(port)))
        held.start()
        backend.wait_for_entry()
        # The held decode has not finished, so nothing is charged yet; the number to compare a
        # refusal against is the one beside it, not a guess about when the first gets counted.
        before = self._health(port)["requests_served"]
        status, _body, _h, _t = self._post(port)
        self.assertEqual(429, status)
        self.assertEqual(before, self._health(port)["requests_served"],
                         "a refusal was counted as a served request")
        self.assertFalse(_srv.exit_requested, "a busy answer must not retire a healthy process")
        backend.release.set()
        held.join(timeout=30)
        self.assertEqual(200, results["slow"][0], results["slow"][1])
        self.assertEqual(before + 1, self._health(port)["requests_served"],
                         "the decode that did run should be the only one charged")

    def test_zero_slots_means_no_gate_not_a_service_that_serves_nothing(self):
        # Same shape as the budget rule: a flag left at its default, or set to a nonsense
        # value, must never quietly change how a working service behaves.
        self.assertIsNone(ct2_sidecar.resolve_decode_slots(0))
        self.assertIsNone(ct2_sidecar.resolve_decode_slots(None))
        self.assertIsNone(ct2_sidecar.resolve_decode_slots(-3))
        self.assertEqual(2, ct2_sidecar.resolve_decode_slots(2))
        self.assertEqual(0.0, ct2_sidecar.resolve_queue_wait_ms(None))
        self.assertEqual(0.0, ct2_sidecar.resolve_queue_wait_ms(-1))
        self.assertEqual(2.5, ct2_sidecar.resolve_queue_wait_ms(2500))

    def test_in_flight_is_measured_and_never_invented(self):
        backend = _GatedBackend()
        _srv, port = self._serve(backend)          # gate off: the count still has to be true
        self.assertEqual(0, self._health(port)["decodes_in_flight"])
        results = {}
        held = threading.Thread(target=lambda: results.setdefault("slow", self._post(port)))
        held.start()
        backend.wait_for_entry()
        self.assertEqual(1, self._health(port)["decodes_in_flight"],
                         "/health must show the decode that is running right now")
        backend.release.set()
        held.join(timeout=30)
        self.assertEqual(0, self._health(port)["decodes_in_flight"],
                         "the count leaked after the decode ended")


# ---- Answers written to callers that already left (defect D30 item 2, option A) ----

class _RaisingFile:
    """A `wfile` that fails in one chosen way, to pin which write errors are swallowed."""

    def __init__(self, error):
        self.error = error
        self.writes = 0

    def write(self, data):
        self.writes += 1
        raise self.error

    def flush(self):
        return None


class GoneClientTests(unittest.TestCase):
    """A caller that hung up is a fact, not a stack trace -- and only that fact is quiet.

    Measured before this existed: one `gate` run wrote 47 complete tracebacks (127 KB of stderr)
    because `socketserver` treats any exception from a handler as a crash, and every one of them
    was the service finishing an answer to a socket nobody was reading. On a long batch that is
    not an incident per request, it is the ordinary end state of an abandoned one -- and the noise
    is what hides the two numbers worth having: how much *finished translation* was thrown away,
    and how many *refusals* went unlistened to. Those are counted apart because only the first
    one cost a decode.

    The guard that matters most here is the third test: silencing the wrong exception would make
    the log quiet by making the service blind, so a real engine failure still has to print.
    """

    def setUp(self):
        self.servers = []
        self.captured = io.StringIO()
        redirect = contextlib.redirect_stderr(self.captured)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _serve(self, backend, **over):
        srv, port = ct2_sidecar.serve_in_thread(backend, host="127.0.0.1", port=0, **over)
        self.servers.append(srv)
        # Note what is NOT done here: `handle_error` is left alone. Overriding it is how the
        # abandoned-request test used to keep its output tidy, and that is exactly the
        # silence-under-test.
        return srv, port

    def _counters(self, srv):
        return srv.signals.writes_to_gone_clients()

    def _leave_a_caller_behind(self, port, read_timeout, out):
        """Send one request from a thread and hang up before the answer arrives.

        Returns once the socket is provably closed, so the service's later write is the event
        under test rather than a race against it.
        """
        def worker():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=read_timeout)
            try:
                conn.request("POST", "/translate", json.dumps(
                    {"text": "hello", "source": "en", "target": "zh"}),
                    headers={"Content-Type": "application/json"})
                conn.getresponse().read()
                out["answered"] = True
            except OSError:
                out["answered"] = False            # the expected path: we gave up first
            finally:
                conn.close()
                out["gone"].set()

        t = threading.Thread(target=worker)
        t.start()
        return t

    def test_a_finished_translation_written_to_a_gone_caller_is_counted_not_printed(self):
        backend = _GatedBackend()
        srv, port = self._serve(backend)
        out = {"gone": threading.Event()}
        caller = self._leave_a_caller_behind(port, 0.2, out)
        backend.wait_for_entry()                   # the decode is genuinely in flight
        caller.join(timeout=30)
        self.assertTrue(out["gone"].wait(10), "the caller thread never closed its socket")
        self.assertFalse(out.get("answered"),
                         "the caller was answered, so nothing was abandoned and the assertion "
                         "below would prove nothing")
        # The socket is closed; now let the service finish the work nobody is waiting for.
        backend.release.set()
        deadline = time.monotonic() + 15.0
        while self._counters(srv)["answers"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        counts = self._counters(srv)
        self.assertEqual(1, counts["answers"],
                         "the write to the hung-up caller was not recorded: %r / stderr=%s"
                         % (counts, self.captured.getvalue()[-400:]))
        self.assertEqual(0, counts["refusals"], "a translation answer is not a refusal")
        stderr = self.captured.getvalue()
        self.assertNotIn("Traceback", stderr,
                         "one abandoned caller still printed a traceback: %s" % stderr[-500:])

    def test_a_refusal_written_to_a_gone_caller_is_counted_apart(self):
        # This is the 11-of-47 case from the measurement: a queued caller gave up long before
        # the gate stopped waiting, so the service spent a refusal on an empty socket. It carries
        # no compute, and lumping it with thrown-away translations would inflate the waste.
        backend = _GatedBackend()
        srv, port = self._serve(backend, decode_slots=1, queue_wait_ms=600)
        out = {"gone": threading.Event()}

        results = {}
        held = threading.Thread(target=lambda: results.setdefault(
            "held", self._post(port, "the one holding the slot")))
        held.start()
        backend.wait_for_entry()
        caller = self._leave_a_caller_behind(port, 0.15, out)
        caller.join(timeout=30)
        self.assertTrue(out["gone"].wait(10))
        deadline = time.monotonic() + 15.0
        while self._counters(srv)["refusals"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        backend.release.set()
        held.join(timeout=30)
        counts = self._counters(srv)
        self.assertEqual(1, counts["refusals"],
                         "the busy answer never reached anyone and was not recorded: %r / %s"
                         % (counts, self.captured.getvalue()[-400:]))
        self.assertEqual(0, counts["answers"],
                         "a refusal spent no decode, so it must not be counted as waste")
        self.assertNotIn("Traceback", self.captured.getvalue())

    def test_a_real_engine_failure_still_prints(self):
        # The whole risk of this change is buying a quiet log with a blind one.
        class _Crashing:
            name = "crashing"

            def translate(self, text, source, target, glossary=None):
                raise ZeroDivisionError("a real defect in the engine")

            def readiness(self):
                return {"ready": True, "reason": None, "loaded": []}

        srv, port = self._serve(_Crashing())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", "/translate", json.dumps(
                {"text": "hello", "source": "en", "target": "zh"}),
                headers={"Content-Type": "application/json"})
            try:
                conn.getresponse().read()
            except OSError:
                pass                              # the handler died mid-response; expected
        finally:
            conn.close()
        stderr = self.captured.getvalue()
        self.assertIn("Traceback", stderr,
                      "an engine crash was silenced along with the hung-up callers")
        self.assertIn("ZeroDivisionError", stderr)
        self.assertEqual(self._counters(srv), {"answers": 0, "refusals": 0},
                         "a crash is not a gone-client write; the counters must stay clean")

    def test_only_the_hung_up_family_is_swallowed_when_writing(self):
        # Both directions, without depending on how a given OS notices a closed socket.
        class _Stub:
            _send = ct2_sidecar.Handler._send

            def __init__(self, error, signals):
                self.wfile = _RaisingFile(error)
                self.server = types.SimpleNamespace(signals=signals)

            def send_response(self, code):
                pass

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass

        for error in (ConnectionAbortedError(10053, "aborted"),
                      ConnectionResetError(10054, "reset"),
                      BrokenPipeError(32, "broken pipe")):
            signals = ct2_sidecar.HealthSignals()
            _Stub(error, signals)._send(200, {"text": "x"})       # must not raise
            self.assertEqual(1, signals.writes_to_gone_clients()["answers"],
                             "%s is a caller that left and should be recorded quietly"
                             % type(error).__name__)

        for error in (OSError(28, "no space left on device"), ValueError("a bug")):
            signals = ct2_sidecar.HealthSignals()
            with self.assertRaises(type(error)):
                _Stub(error, signals)._send(200, {"text": "x"})
            self.assertEqual({"answers": 0, "refusals": 0},
                             signals.writes_to_gone_clients(),
                             "%s is not a hung-up caller and must not be swallowed" % type(error).__name__)

        # Which side of 300 the code falls on is a claim, so it is pinned: an answer is an answer
        # at 204 as much as at 200, and a 429/503 is a message that cost no work.
        signals = ct2_sidecar.HealthSignals()
        _Stub(ConnectionAbortedError(10053, "aborted"), signals)._send(204, {})
        _Stub(ConnectionAbortedError(10053, "aborted"), signals)._send(503, {"error": "x"})
        self.assertEqual({"answers": 1, "refusals": 1}, signals.writes_to_gone_clients(),
                         "an empty 2xx is an answer and a 5xx is a refusal: %r"
                         % (signals.writes_to_gone_clients(),))

    def test_healthy_traffic_leaves_the_counters_and_the_endpoint_alone(self):
        # A control, and a scope pin: exposing these numbers on /health is still an open
        # decision (`docs/tech/16` section 7), so nothing may appear there quietly.
        class _Instant:
            name = "instant"

            def translate(self, text, source, target, glossary=None):
                return "ok: {0}".format(target)

            def readiness(self):
                return {"ready": True, "reason": None, "loaded": []}

        srv, port = self._serve(_Instant())
        status, body = self._post(port, "hello")
        self.assertEqual(200, status, body)
        self.assertEqual({"answers": 0, "refusals": 0}, self._counters(srv))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/health")
        health = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        self.assertNotIn("answers_to_gone_clients", health,
                         "the self-report is a pending decision, not a free addition")
        self.assertIn("decodes_in_flight", health)

    def _post(self, port, text):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", "/translate", json.dumps(
                {"text": text, "source": "en", "target": "zh"}),
                headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
