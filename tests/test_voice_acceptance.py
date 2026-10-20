# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for script/voice_acceptance.py -- the M4 human acceptance harness.

The rules under test are the ones that keep a human checklist honest:

  * only every-answer-yes exits 0; a skip or an unanswered question is `incomplete`, never a pass
    (D74: "nobody checked" must not read as "fine");
  * the evidence is self-describing (questions, answers, UTC timestamp, build hashes) and names no
    machine path (D70);
  * an unbuilt app or an existing evidence file is a refusal, not a quiet overwrite (evidence is
    history, not scratch).

No GUI, microphone or model is touched: `launch` is injected and the answers come from a file.
"""
import datetime
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import voice_acceptance as va  # noqa: E402

FIXED_NOW = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)


def _app(tmp, name="Wonslate.exe", body=b"MZ-fake-app"):
    path = pathlib.Path(tmp) / name
    path.write_bytes(body)
    return path


def _all_yes(**overrides):
    answers = {qid: {"result": "yes", "note": ""} for qid in va.QUESTION_IDS}
    for qid, value in overrides.items():
        answers[qid] = value if isinstance(value, dict) else {"result": value, "note": ""}
    return answers


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = _app(self.dir.name)
        self.out = pathlib.Path(self.dir.name) / "evidence.json"

    def test_all_yes_passes(self):
        verdict, code = va.accept(self.app, self.out, answers=_all_yes(), now=FIXED_NOW)
        self.assertEqual(("passed", 0), (verdict, code))
        payload = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual("passed", payload["verdict"])
        self.assertEqual("2026-10-20T12:00:00+00:00", payload["generated_at"])
        self.assertEqual(hashlib.sha256(b"MZ-fake-app").hexdigest(), payload["app"]["sha256"])
        self.assertEqual("Wonslate.exe", payload["app"]["name"])

    def test_any_no_fails_even_if_everything_else_passed(self):
        verdict, code = va.accept(
            self.app, self.out,
            answers=_all_yes(asr_text={"result": "no", "note": "识别成了空白"}), now=FIXED_NOW)
        self.assertEqual(("failed", 1), (verdict, code))
        payload = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(["asr_text"], payload["failed"])
        self.assertIn("识别成了空白", json.dumps(payload, ensure_ascii=False))

    def test_a_skip_is_incomplete_not_a_pass(self):
        verdict, code = va.accept(self.app, self.out,
                                  answers=_all_yes(playback="skip"), now=FIXED_NOW)
        self.assertEqual(("incomplete", 1), (verdict, code))
        payload = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(["playback"], payload["skipped"])

    def test_an_unanswered_question_is_incomplete_not_a_pass(self):
        answers = _all_yes()
        del answers["timings"]
        verdict, code = va.accept(self.app, self.out, answers=answers, now=FIXED_NOW)
        self.assertEqual(("incomplete", 1), (verdict, code))
        payload = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(["timings"], payload["unanswered"])
        self.assertEqual("unanswered", payload["answers"]["timings"]["result"])

    def test_evidence_names_no_machine_path_and_is_lf(self):
        va.accept(self.app, self.out, answers=_all_yes(), now=FIXED_NOW)
        raw = self.out.read_bytes()
        self.assertNotIn(self.dir.name.encode("utf-8"), raw)
        self.assertNotIn(b"\r\n", raw)
        self.assertNotIn("\\", self.out.read_text(encoding="utf-8").replace("\\\\", ""),
                         "no escaped backslash either")

    def test_engine_library_is_identified_when_present(self):
        (pathlib.Path(self.dir.name) / "translator_engine.dll").write_bytes(b"MZ-engine")
        va.accept(self.app, self.out, answers=_all_yes(), now=FIXED_NOW)
        payload = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual("translator_engine.dll", payload["engine_lib"]["name"])
        self.assertEqual(hashlib.sha256(b"MZ-engine").hexdigest(),
                         payload["engine_lib"]["sha256"])


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = _app(self.dir.name)
        self.out = pathlib.Path(self.dir.name) / "evidence.json"

    def test_an_unbuilt_app_is_refused_before_anything_is_written(self):
        verdict, code = va.accept(pathlib.Path(self.dir.name) / "nope.exe", self.out,
                                  answers=_all_yes())
        self.assertEqual(("refused", 2), (verdict, code))
        self.assertFalse(self.out.exists())

    def test_an_existing_evidence_file_is_not_overwritten(self):
        self.out.write_text("previous evidence", encoding="utf-8")
        verdict, code = va.accept(self.app, self.out, answers=_all_yes())
        self.assertEqual(("refused", 2), (verdict, code))
        self.assertEqual("previous evidence", self.out.read_text(encoding="utf-8"))

    def test_force_allows_overwriting(self):
        self.out.write_text("previous evidence", encoding="utf-8")
        verdict, code = va.accept(self.app, self.out, answers=_all_yes(), force=True,
                                  now=FIXED_NOW)
        self.assertEqual(("passed", 0), (verdict, code))

    def test_an_unknown_answer_id_is_refused(self):
        with self.assertRaises(ValueError):
            va._normalise_answers({"answers": {"recording": "yes"}})

    def test_a_bad_result_value_is_refused(self):
        for bad in ("ok", "", 1, {"result": "maybe"}):
            with self.assertRaises(ValueError):
                va._normalise_answers({"answers": {"asr_text": bad}})

    def test_the_launch_seam_is_used_and_injectable(self):
        calls = []
        va.accept(self.app, self.out, answers=_all_yes(), launch=lambda p: calls.append(p))
        self.assertEqual([str(self.app)], calls)

    def test_the_launch_seam_is_optional(self):
        verdict, _ = va.accept(self.app, self.out, answers=_all_yes(), launch=None)
        self.assertEqual("passed", verdict)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = _app(self.dir.name)
        self.out = pathlib.Path(self.dir.name) / "evidence.json"
        self.answers = pathlib.Path(self.dir.name) / "answers.json"

    def _write_answers(self, payload):
        self.answers.write_text(json.dumps(payload), encoding="utf-8")

    def test_non_interactive_without_answers_refuses(self):
        # A CI runner or a double-click with redirected stdin must not invent answers.
        code = va.main(["--app", str(self.app), "--out", str(self.out), "--no-launch"],
                       stdin_isatty=lambda: False)
        self.assertEqual(2, code)
        self.assertFalse(self.out.exists())

    def test_answers_file_drives_the_run_without_a_gui(self):
        self._write_answers({"answers": {q: "yes" for q in va.QUESTION_IDS},
                             "notes": "smoke"})
        code = va.main(["--app", str(self.app), "--out", str(self.out),
                        "--answers", str(self.answers), "--no-launch"])
        self.assertEqual(0, code)
        self.assertEqual("passed", json.loads(self.out.read_text(encoding="utf-8"))["verdict"])

    def test_unknown_id_in_the_answers_file_is_a_refusal(self):
        self._write_answers({"answers": {"recording": "yes"}})
        code = va.main(["--app", str(self.app), "--out", str(self.out),
                        "--answers", str(self.answers), "--no-launch"])
        self.assertEqual(2, code)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
