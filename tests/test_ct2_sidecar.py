# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Contract tests for the real sidecar service (CT2 / Argos).

Pure stdlib unittest. Two layers are covered:

  * dependency-free: the backend abstraction, the HTTP contract (/translate,
    /health liveness, /readyz readiness, /languages enumeration), the split
    between permanent and recoverable failure (D22) and the explicit-degradation
    paths (missing deps, missing model dir, dir without packages, unsupported
    pair) -- always runs, no skips.
  * real inference: loads the CTranslate2 + sentencepiece stack and an unpacked
    Argos package from default_model_dir(). Runs wherever that model is present
    (install: sidecar/requirements.txt; download steps: docs/17) and skips with
    an explicit reason otherwise -- a missing model is an environment fact, not
    a passing test.

Run:  python -m unittest tests.test_ct2_sidecar -v
"""
import http.client
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
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
        # D22: for Argos the discovered directories ARE the whole coverage, so
        # the enumeration must claim to be exhaustive.
        capability = self.backend.languages()
        self.assertEqual(capability["pairs"], self.backend.available_pairs())
        self.assertTrue(capability["complete"])
        self.assertIn("zh", capability["target_codes"])

    def test_target_probe_needs_no_inference(self):
        self.assertTrue(self.backend.serves_target("zh"))
        self.assertFalse(self.backend.serves_target("jj"))

    def test_readiness_is_cold_before_any_pair_is_resident(self):
        # D23 at the Argos tier: packages are discovered eagerly, translators are
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
        # D22: `tl`-style answers must come from the checkpoint's own vocabulary,
        # cheaply, instead of by burning a request that then 5xx-fails.
        self.assertTrue(self.backend.serves_target("zh"))
        self.assertFalse(self.backend.serves_target("xx"))

    def test_readiness_is_cold_until_the_checkpoint_is_loaded(self):
        # D23: construction only reads the tokenizer; the 3B checkpoint is lazy,
        # which is precisely what /health cannot distinguish from "usable".
        fresh = MadladBackend(model_dir=self.model_dir)
        report = fresh.readiness()
        self.assertFalse(report["ready"])
        self.assertEqual(report["reason"], "model_not_loaded")

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


# ---- D22 / D23: permanent vs recoverable, readiness, enumeration ------------
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

    def __init__(self, error=None, ready=True, pairs=None, targets=None, complete=True):
        self.error = error
        self.ready = ready
        self.pairs = list(pairs or [])
        self.targets = list(targets or [])
        self.complete = complete
        self.translate_calls = 0
        self.warmed = False
        self.warmed_pairs = []

    def translate(self, text, source, target, glossary=None):
        self.translate_calls += 1
        if self.error is not None:
            raise self.error
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
    """D22: a code that can never work must not look like an engine that is down."""

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
    """D23: "the port answers" and "a request will not stall" are different facts."""

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


class LanguagesEndpointTests(unittest.TestCase):
    """D22: answer the capability question before a request spends a translation."""

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
    """D23 consumer side: somebody has to turn "warming" into "ready".

    /readyz only reports the truth; it does not make the model resident. Without
    an explicit warm-up the first real translate is what pays the load, and a
    desktop app that wants to say "ready" has no way to get there without burning
    a sentence. The endpoint must also be consistent with D22: asking to warm a
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
        # Same rule as /translate (D22): no retry makes this succeed.
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


if __name__ == "__main__":
    unittest.main()
