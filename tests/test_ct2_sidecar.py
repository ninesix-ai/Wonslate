# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Contract tests for the real sidecar service (CT2 / Argos).

Pure stdlib unittest, zero third-party deps, runs on a dev machine. Focuses on
everything verifiable WITHOUT a real model: the backend abstraction, the HTTP
contract (/health, /translate), and graceful degradation when dependencies are
missing. Real CT2 inference needs ctranslate2 installed plus a model download
-- authorized environment steps outside this unit test.

Run:  python -m unittest tests.test_ct2_sidecar -v
"""
import http.client
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sidecar.ct2_sidecar import (  # noqa: E402
    MockBackend, MissingDependency, make_backend, serve_in_thread,
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

    def test_ct2_backend_without_deps_raises_missing_dependency(self):
        # With ctranslate2 not installed locally, make_backend must raise
        # MissingDependency (with install guidance), not a bare ImportError.
        try:
            import ctranslate2  # noqa: F401
            self.skipTest("ctranslate2 is installed; skipping the missing-dep branch")
        except ImportError:
            with self.assertRaises(MissingDependency):
                make_backend("ct2", model_dir=None)


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


if __name__ == "__main__":
    unittest.main()
