# -*- coding: utf-8 -*-
"""Tests for the model version/upgrade half of GA-13 (model_origin + check_model_updates).

Two rules these tests exist to hold:

  * A record must never be trusted further than it goes. "No origin file" and "an origin file
    missing its version" and "the probe could not reach upstream" are three different facts, and
    collapsing any of them into "up to date" is the D57/D58 failure in a new costume.
  * The upgrade check must say UNKNOWN rather than 0 when it could not look. A tool that reports
    "no newer version" because its network call failed is worse than no tool.

Everything here is offline: probes take an `opener`, and the fetch scripts are exercised with a
monkeypatched `download`. Nothing in this file touches the network, so it can sit in the gate --
which `check_model_updates.py` itself cannot, because that one needs upstream by design.
"""
import io
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import model_origin                                        # noqa: E402
import check_model_updates as updates                     # noqa: E402
import fetch_argos_models                                 # noqa: E402


def canned(payload):
    """An opener that answers with fixed JSON, so a probe can be tested without upstream."""
    body = json.dumps(payload).encode("utf-8")

    def opener(req, timeout=None):
        class Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        return Resp(body)

    return opener


def broken(reason="boom"):
    def opener(req, timeout=None):
        raise OSError(reason)

    return opener


class OriginRecordTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_origin.record(tmp, asset="en_zh", version="1.9", sha256="a" * 64,
                               url="https://argos-net.com/x.zip", fetcher="t")
            state, payload = model_origin.read(tmp)
            self.assertEqual("ok", state)
            self.assertEqual("1.9", payload["version"])
            # UTC with an offset, for the same reason D71 required it of evidence.
            self.assertIn("+00:00", payload["fetched_at"])

    def test_a_missing_file_is_not_an_incomplete_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, payload = model_origin.read(tmp)
            self.assertEqual("missing", state)
            self.assertIsNone(payload)

    def test_a_record_without_its_version_is_not_trusted_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / model_origin.ORIGIN_FILE).write_text(
                json.dumps({"asset": "en_zh", "sha256": "b" * 64, "url": "u",
                            "fetched_at": "x", "fetcher": "f", "version": ""}),
                encoding="utf-8")
            state, payload = model_origin.read(tmp)
            self.assertEqual("incomplete", state)
            self.assertIn("version", payload["absent"])

    def test_garbage_is_reported_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / model_origin.ORIGIN_FILE).write_text("not json", encoding="utf-8")
            self.assertEqual("incomplete", model_origin.read(tmp)[0])
            (pathlib.Path(tmp) / model_origin.ORIGIN_FILE).write_text("[1, 2]", encoding="utf-8")
            self.assertEqual("incomplete", model_origin.read(tmp)[0])

    def test_no_machine_path_is_written(self):
        # The record travels with the model directory and may be pasted into a bug report; a
        # drive letter would publish one person's layout (the D70 rule, applied here).
        with tempfile.TemporaryDirectory() as tmp:
            model_origin.record(tmp, asset="x", version="1", sha256="c" * 64,
                                url="https://host/x", fetcher="t")
            text = (pathlib.Path(tmp) / model_origin.ORIGIN_FILE).read_text(encoding="utf-8")
            self.assertNotIn(tmp, text)
            self.assertNotIn("\\", text)


class UpdateClassificationTests(unittest.TestCase):
    def record(self, tmp, **kw):
        base = dict(asset="en_zh", version="1.9", sha256="d" * 64,
                    url="https://argos-net.com/x", fetcher="t")
        base.update(kw)
        model_origin.record(tmp, **base)
        return tmp

    def test_no_record_is_unrecorded_not_up_to_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = updates.check_argos(tmp, opener=canned([]))
            self.assertEqual("unrecorded", row["state"])

    def test_index_naming_another_version_is_reported_as_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp)
            row = updates.check_argos(tmp, opener=canned(
                [{"from_code": "en", "to_code": "zh", "package_version": "1.10"}]))
            self.assertEqual("newer", row["state"])
            self.assertEqual("1.10", row["upstream"])

    def test_agreement_is_current_and_not_a_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp)
            row = updates.check_argos(tmp, opener=canned(
                [{"from_code": "en", "to_code": "zh", "package_version": "1.9"}]))
            self.assertEqual("current", row["state"])

    def test_pair_gone_from_the_index_is_failure_not_current(self):
        # "I cannot find what you have" is not evidence that nothing newer exists.
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp)
            row = updates.check_argos(tmp, opener=canned([]))
            self.assertEqual("failed", row["state"])

    def test_release_listing_another_asset_is_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            held = "kokoro-int8-multi-lang-v1_1.tar.bz2"
            self.record(tmp, asset=held, version=held, kind="tts-models")
            row = updates.check_voice(tmp, opener=canned(
                {"assets": [{"name": "kokoro-int8-multi-lang-v1_2.tar.bz2"}]}))
            self.assertEqual("newer", row["state"])

    def test_madlad_bytes_under_a_moving_revision_can_be_detected(self):
        # MADLAD publishes no version string, so the only honest signal is the digest shifting.
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp, asset="model.bin", version="main", kind="main")
            other = "e" * 64
            row = updates.check_madlad(tmp, opener=canned(
                [{"path": "model.bin", "lfs": {"sha256": other}}]))
            self.assertEqual("newer", row["state"])
            same = updates.check_madlad(tmp, opener=canned(
                [{"path": "model.bin", "lfs": {"sha256": "d" * 64}}]))
            self.assertEqual("current", same["state"])

    def test_a_failed_probe_cannot_be_reported_as_no_updates(self):
        # THE point of this file: exit 0 means "we looked", not "nothing went wrong while trying".
        states = {updates.check_argos(self.record_in(tempfile.mkdtemp()),
                                      opener=broken()).get("state")}
        self.assertEqual({"failed"}, states)
        code, message = updates.verdict(states)
        self.assertEqual(1, code)
        self.assertIn("UNKNOWN", message)

    def record_in(self, tmp):
        model_origin.record(tmp, asset="en_zh", version="1.9", sha256="d" * 64,
                            url="https://x", fetcher="t")
        return tmp

    def test_verdict_prefers_newer_over_current_but_never_over_failure(self):
        self.assertEqual((2, "at least one upstream publishes something newer"),
                         updates.verdict({"current", "newer"}))
        self.assertEqual((1, "UNKNOWN -- a probe failed, so this run says nothing about updates"),
                         updates.verdict({"current", "newer", "failed"}))
        self.assertEqual((0, "no newer upstream version found"),
                         updates.verdict({"current", "unrecorded"}))


class WiringTests(unittest.TestCase):
    def test_every_fetch_script_records_what_landed(self):
        for name in ("fetch_argos_models.py", "fetch_madlad_model.py", "fetch_voice_models.py"):
            with self.subTest(name):
                text = (ROOT / "script" / name).read_text(encoding="utf-8")
                self.assertIn("model_origin.record(", text,
                              "%s fetches a model without recording its version" % name)

    def test_the_update_check_imports_urls_instead_of_restating_them(self):
        """A second copy of an upstream URL is a second truth waiting to drift."""
        text = (ROOT / "script" / "check_model_updates.py").read_text(encoding="utf-8")
        for needle in ("argospm-index", "k2-fsa/sherpa-onnx/releases/download",
                       "Heng666/madlad400"):
            with self.subTest(needle):
                self.assertNotIn(needle, text)
        self.assertIn("fetch_argos_models.INDEX_URLS[0]", text)

    def test_the_three_upstreams_are_all_known_to_the_tool(self):
        self.assertEqual({"argos", "voice", "madlad"}, set(updates.PROBES))

    def test_dest_requires_exactly_one_asset(self):
        # Refusing beats guessing which probe a directory belongs to and printing a verdict
        # nobody asked for.
        with self.assertRaises(SystemExit) as ctx:
            updates.main(["--dest", "x", "--only", "argos,voice"])
        self.assertIn("--dest takes exactly one", str(ctx.exception))

    def test_unknown_asset_name_is_refused(self):
        with self.assertRaises(SystemExit):
            updates.main(["--only", "gpt"])


class ArgosRecordsThroughRealPathTests(unittest.TestCase):
    PACKAGE = "translate-en_zh-1_9.argosmodel"
    LINK = "https://argos-net.com/" + PACKAGE

    def test_a_verified_package_leaves_an_origin_file(self):
        """Walk the real success path: the record must be a consequence of verification."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            archive = root / self.PACKAGE
            inner = root / "stage" / self.PACKAGE.replace(".argosmodel", "")
            (inner / "model").mkdir(parents=True)
            # An empty directory never reaches the zip (rglob yields files only), and verify()
            # requires model/ to exist as a directory -- so it needs a member inside it.
            (inner / "model" / "encoder.bin").write_text("m", encoding="utf-8")
            (inner / "metadata.json").write_text("{}", encoding="utf-8")
            (inner / "sentencepiece.model").write_text("x", encoding="utf-8")
            with zipfile.ZipFile(archive, "w") as zf:
                for path in sorted(inner.rglob("*")):
                    if path.is_file():
                        zf.write(path, path.relative_to(root / "stage"))

            saved = fetch_argos_models.download
            fetch_argos_models.download = lambda *a, **k: "f" * 64
            try:
                ok = fetch_argos_models.fetch_pair("en_zh", self.LINK, root,
                                                   True, version="1.9")
            finally:
                fetch_argos_models.download = saved
            self.assertTrue(ok)
            target = root / self.PACKAGE.replace(".argosmodel", "")
            state, payload = model_origin.read(target)
            self.assertEqual("ok", state)
            self.assertEqual("1.9", payload["version"])
            self.assertEqual("en_zh", payload["asset"])

    def test_a_failed_verification_writes_no_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            archive = root / self.PACKAGE
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("pkg/readme.txt", "no model here")   # layout will fail verify
            saved = fetch_argos_models.download
            fetch_argos_models.download = lambda *a, **k: "f" * 64
            try:
                ok = fetch_argos_models.fetch_pair("en_zh", self.LINK, root,
                                                   True, version="1.9")
            finally:
                fetch_argos_models.download = saved
            self.assertFalse(ok)
            self.assertEqual("missing",
                             model_origin.read(root / self.PACKAGE.replace(".argosmodel", ""))[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
