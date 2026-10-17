# -*- coding: utf-8 -*-
"""Tests for the model version/upgrade half of GA-13 (origin records, baseline, update probes).

Three rules these tests exist to hold:

  * A record must never be trusted further than it goes. "No origin file", "a record missing its
    version", "no reference to compare against" and "the probe could not reach upstream" are four
    different facts, and collapsing any of them into "up to date" is defect D74 -- which lived in
    this file's own assertions until 2026-10-17, see `VerdictTests`.
  * Payload shapes come from what the upstreams actually answer, not from what the code expects.
    D75 and D76 were both found by calling for real: Hugging Face names its sha256 `lfs.oid`, and
    GitHub's model archives live under release *tags*, not under `/releases/latest`. The fixtures
    below are transcribed from captured responses (2026-10-17) for that reason.
  * A credential must not travel to a host that does not need it, so the opener records the
    requests it was handed.

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
import urllib.error
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import model_origin                                        # noqa: E402
import check_model_updates as updates                     # noqa: E402
import fetch_argos_models                                 # noqa: E402


def canned(payload):
    """An opener that answers with fixed JSON, and remembers which requests it was handed.

    The recorded requests are what let a test prove a bearer token never leaves toward a host that
    does not need it; a response alone cannot show that.
    """
    body = json.dumps(payload).encode("utf-8")
    seen = []

    def opener(req, timeout=None):
        seen.append(req)

        class Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        return Resp(body)

    opener.requests = seen
    return opener


def http_error(code=403):
    """An opener that fails the way urllib does for a rate-limited GitHub answer."""
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "Forbidden", None, None)

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

    def test_release_that_renamed_the_archive_is_a_finding_with_the_new_name(self):
        # D76's real shape: our file name lives under a release tag, and "newer" there means the
        # tag stopped listing our exact name while a same-family entry exists.
        with tempfile.TemporaryDirectory() as tmp:
            held = "kokoro-int8-multi-lang-v1_1.tar.bz2"
            self.record(tmp, asset=held, version=held, kind="tts-models")
            row = updates.check_voice(tmp, opener=canned(
                {"assets": [{"name": "kokoro-int8-multi-lang-v1_2.tar.bz2",
                             "digest": "sha256:" + "a" * 64}]}))
            self.assertEqual("newer", row["state"])
            self.assertEqual("name", row["basis"])       # the finding rests on the name, not bytes
            self.assertEqual("kokoro-int8-multi-lang-v1_2.tar.bz2", row["upstream"])

    def test_voice_digest_is_compared_when_the_name_is_still_listed(self):
        # Transcribed from the real `tts-models` tag: assets carry `digest: "sha256:<hex>"`. A
        # same-name archive can be replaced in place, which a name comparison cannot see.
        with tempfile.TemporaryDirectory() as tmp:
            held = "kokoro-int8-multi-lang-v1_1.tar.bz2"
            self.record(tmp, asset=held, version=held, kind="tts-models")
            same = updates.check_voice(tmp, opener=canned(
                {"assets": [{"name": held, "digest": "sha256:" + "d" * 64}]}))
            self.assertEqual("current", same["state"])
            self.assertEqual("d" * 64, same["upstream"])      # full digest, so it can be recorded
            moved = updates.check_voice(tmp, opener=canned(
                {"assets": [{"name": held, "digest": "sha256:" + "e" * 64}]}))
            self.assertEqual("newer", moved["state"])
            self.assertIn("replaced", moved["detail"])

    def test_upstream_publishing_no_digest_is_not_reported_as_current(self):
        # `sense-voice` really answers `digest: null`. "The name is still listed" is all that can
        # be proven, and calling that "current" would hide an in-place replacement -- the same
        # mistake as D74, one level down.
        with tempfile.TemporaryDirectory() as tmp:
            held = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
            self.record(tmp, asset=held, version=held, kind="asr-models")
            row = updates.check_voice(tmp, opener=canned(
                {"assets": [{"name": held, "digest": None}]}))
            self.assertEqual("incomparable", row["state"])

    def test_voice_without_a_release_tag_refuses_instead_of_guessing_latest(self):
        # `latest` is exactly the guess that produced D76: it answers with an app release that
        # holds none of the model archives.
        with tempfile.TemporaryDirectory() as tmp:
            held = "silero_vad.onnx"
            self.record(tmp, asset=held, version=held)
            row = updates.check_voice(tmp, opener=canned({"assets": []}))
            self.assertEqual("incomparable", row["state"])
            self.assertIn("no release tag", row["detail"])

    def test_a_rate_limited_github_answer_says_so_rather_than_httperror(self):
        # Anonymous callers on a shared IP get 403, and a reader has to be able to tell that from
        # "the repository moved" -- one is "wait or set GITHUB_TOKEN", the other is a real change.
        with tempfile.TemporaryDirectory() as tmp:
            held = "silero_vad.onnx"
            self.record(tmp, asset=held, version=held, kind="asr-models")
            row = updates.check_voice(tmp, opener=http_error(403))
            self.assertEqual("failed", row["state"])
            self.assertIn("HTTP 403", row["detail"])
            self.assertIn("GITHUB_TOKEN", row["detail"])

    def test_madlad_bytes_under_a_moving_revision_can_be_detected(self):
        # MADLAD publishes no version string, so the only honest signal is the digest shifting --
        # and Hugging Face calls that field `lfs.oid`, not `lfs.sha256` (D75: the first draft read
        # `sha256`, so every healthy install answered "upstream publishes no comparable digest").
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp, asset="model.bin", version="main", kind="main")
            other = "e" * 64
            row = updates.check_madlad(tmp, opener=canned(
                [{"path": "model.bin", "lfs": {"oid": other}}]))
            self.assertEqual("newer", row["state"])
            self.assertEqual(other, row["upstream"])      # full length, so it can be recorded
            same = updates.check_madlad(tmp, opener=canned(
                [{"path": "model.bin", "lfs": {"oid": "d" * 64}}]))
            self.assertEqual("current", same["state"])

    def test_a_non_lfs_git_oid_is_not_compared_as_a_sha256(self):
        # Files under Git LFS carry `lfs.oid` (sha256); others carry a top-level `oid` that is a
        # git sha1. Substituting one for the other would compare two different hash functions and
        # report a permanent difference.
        with tempfile.TemporaryDirectory() as tmp:
            self.record(tmp, asset="model.bin", version="main", kind="main")
            row = updates.check_madlad(tmp, opener=canned(
                [{"path": "model.bin", "oid": "1330b91bc697a7d4edfac67b99baa927aed29b97"}]))
            self.assertEqual("failed", row["state"])
            self.assertIn("no comparable digest", row["detail"])

    def record_in(self, tmp):
        model_origin.record(tmp, asset="en_zh", version="1.9", sha256="d" * 64,
                            url="https://x", fetcher="t")
        return tmp


class VerdictTests(unittest.TestCase):
    """What a collection of rows means. D74 lived here -- and so did this file's own wrong claim.

    The 2026-10-15 version of the test below asserted
    `verdict({"current", "unrecorded"}) == (0, "no newer upstream version found")`, which pinned
    the defect as the specification. Wiring up the scheduled check is what exposed it: a runner
    has no installed models, every row came back `unrecorded`, and the tool reported an all-clear
    it had not earned.
    """

    def recorded(self, tmp):
        model_origin.record(tmp, asset="en_zh", version="1.9", sha256="d" * 64,
                            url="https://x", fetcher="t")
        return tmp

    def test_a_failed_probe_cannot_be_reported_as_no_updates(self):
        # THE point of this file: exit 0 means "we looked", not "nothing went wrong while trying".
        with tempfile.TemporaryDirectory() as tmp:
            states = {updates.check_argos(self.recorded(tmp), opener=broken()).get("state")}
            self.assertEqual({"failed"}, states)
            code, message = updates.verdict(states)
            self.assertEqual(1, code)
            self.assertIn("UNKNOWN", message)

    def test_comparing_nothing_is_not_an_all_clear(self):
        # The defect itself, stated as a requirement: silence about updates is not "no newer
        # version", whatever the mix of reasons for the silence.
        for states in ({"unrecorded"}, {"incomparable"}, {"unrecorded", "incomparable"}):
            with self.subTest(states=states):
                code, message = updates.verdict(states)
                self.assertEqual(1, code, message)
                self.assertIn("UNKNOWN", message)
                self.assertNotIn("no newer upstream version found", message)

    def test_a_partial_comparison_keeps_the_number_and_adds_the_qualifier(self):
        # D70's trade-off, applied: hiding a partial run behind a bare 0 is a lie, and refusing to
        # print the number at all throws away information that is real.
        code, message = updates.verdict({"current", "unrecorded"}, skipped=["voice:asr"])
        self.assertEqual(0, code)
        self.assertIn("no newer upstream version found", message)
        self.assertIn("not compared: voice:asr", message)
        code, message = updates.verdict({"current", "newer", "incomparable"},
                                        skipped=["madlad"])
        self.assertEqual(2, code)
        self.assertIn("not compared: madlad", message)

    def test_failure_outranks_a_finding_but_a_finding_outranks_silence(self):
        self.assertEqual((2, "at least one upstream publishes something newer"),
                         updates.verdict({"current", "newer"}))
        code, _ = updates.verdict({"current", "newer", "failed"})
        self.assertEqual(1, code)

    def test_a_reference_without_a_value_is_incomparable_not_current(self):
        # D74's second face, reachable from `--baseline`: the old code fell through to CURRENT and
        # printed "index agrees" about an entry that had nothing to agree with. Each probe is fed
        # the one field it cannot work without, and no path is passed at all -- which is also the
        # proof that baseline mode reads no disk state.
        for probe, record in ((updates.check_argos, {"asset": "en_zh"}),
                              (updates.check_madlad, {"asset": "model.bin", "kind": "main"}),
                              (updates.check_voice, {"asset": "x.tar.bz2"})):
            with self.subTest(probe=probe.__name__):
                row = probe(None, opener=canned({}), record=record)
                self.assertEqual("incomparable", row["state"], row["detail"])

    def test_a_token_is_sent_only_to_the_host_that_needs_it(self):
        # The scheduled run gets GITHUB_TOKEN from the runner. It must not leave toward a CDN or
        # toward Hugging Face, where it buys nothing and could end up in somebody's access log.
        github = canned({"assets": []})
        updates.check_voice(None, opener=github,
                            record={"asset": "silero_vad.onnx", "kind": "asr-models",
                                    "sha256": "d" * 64}, token="sekrit")
        self.assertEqual("Bearer sekrit",
                         github.requests[0].get_header("Authorization", None))
        self.assertIn("api.github.com", github.requests[0].full_url)
        for probe, payload, record in ((updates.check_argos, [], {"asset": "en_zh",
                                                                  "version": "1.9"}),
                                       (updates.check_madlad, [], {"asset": "model.bin",
                                                                   "sha256": "d" * 64})):
            with self.subTest(probe=probe.__name__):
                opener = canned(payload)
                probe(None, opener=opener, token="sekrit", record=record)
                self.assertTrue(opener.requests, "the probe was never asked, so this proves "
                                                 "nothing")
                for request in opener.requests:
                    self.assertIsNone(request.get_header("Authorization", None))


class BaselineTests(unittest.TestCase):
    """The repository-level reference: what makes the check runnable where nothing is installed.

    A scheduled runner has no model directory, so `--baseline` is the only mode that can answer
    there. These tests keep it honest about the same three things D74 is about: it must refuse
    when its reference is unreadable, it must never record a blind spot, and it must not grow a
    second copy of the comparison.
    """

    def test_missing_and_invalid_are_not_ok_and_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, why = updates.read_baseline(pathlib.Path(tmp) / "nope.json")
            self.assertEqual("missing", state)
            self.assertIn("reason", why)
            path = pathlib.Path(tmp) / "bad.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual("invalid", updates.read_baseline(path)[0])
            path.write_text(json.dumps({"assets": {}}), encoding="utf-8")
            self.assertEqual("invalid", updates.read_baseline(path)[0],
                             "an empty asset list is a silent no-op, which D74 forbids")

    def test_main_refuses_to_pretend_when_the_baseline_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(1, updates.main(["--baseline", "--baseline-file",
                                              str(pathlib.Path(tmp) / "absent.json")]))

    def test_record_baseline_needs_baseline(self):
        # Recording from one machine's install would turn the repository's reference into a
        # snapshot of somebody's disk.
        with self.assertRaises(SystemExit) as ctx:
            updates.main(["--record-baseline"])
        self.assertIn("needs --baseline", str(ctx.exception))
        with self.assertRaises(SystemExit) as ctx:
            updates.main(["--baseline", "--dest", "x"])
        self.assertIn("cannot be used with it", str(ctx.exception))

    def test_an_unregistered_family_in_the_baseline_is_reported_not_skipped(self):
        rows = updates.baseline_rows({"argoo:x": {"version": "1"}}, opener=canned([]))
        self.assertEqual(["failed"], [row["state"] for row in rows])
        self.assertIn("no probe is registered", rows[0]["detail"])

    def test_baseline_mode_uses_the_same_probes_as_install_mode(self):
        # One comparison path, two references. If someone later writes a second set of per-family
        # logic for baseline mode, this is the test that says so -- D64's shape, two answers to
        # one question that eventually disagree.
        seen = []

        def spy(dest_dir, opener=None, timeout=30, record=None, token=None):
            seen.append((dest_dir, record, token))
            return updates._row("en_zh", "current", "1.9", "1.9", "spy", basis="version")

        saved = dict(updates.PROBES)
        updates.PROBES["argos"] = spy
        try:
            rows = updates.baseline_rows({"argos:en_zh": {"version": "1.9"}},
                                        opener=canned([]), token="tok")
        finally:
            updates.PROBES.clear()
            updates.PROBES.update(saved)
        self.assertEqual(["argos:en_zh"], [row["checked"] for row in rows])
        self.assertEqual([(None, {"asset": "en_zh", "version": "1.9"}, "tok")], seen,
                         "baseline mode must hand the entry to the family's own probe")

    def test_nothing_is_recorded_from_a_row_that_did_not_answer(self):
        for state in ("failed", "unrecorded", "incomparable"):
            with self.subTest(state=state):
                row = updates._row("en_zh", state, "1.9", "2.0", "x", basis="version")
                self.assertIsNone(updates.to_baseline_entry("argos:en_zh", row))

    def test_recording_writes_full_digests_and_only_for_the_rows_that_answered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "baseline.json"
            entries = {"madlad:model.bin": {"asset": "model.bin", "kind": "main",
                                            "sha256": "a" * 64},
                       "argos:en_zh": {"asset": "en_zh", "version": "1.9"}}
            rows = {"madlad:model.bin": updates._row("model.bin", "newer", "a" * 64, "b" * 64,
                                                     "bytes moved", basis="digest"),
                    "argos:en_zh": updates._row("en_zh", "failed", "1.9", None, "403"),
                    "voice:tts": updates._row("x", "newer", None, "y.tar.bz2", "renamed",
                                              basis="name")}
            changed = updates.write_baseline(entries, rows, path=path)
            self.assertEqual(1, changed, "only the row that answered may change the file")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("b" * 64, saved["assets"]["madlad:model.bin"]["sha256"],
                             "a truncated digest in the reference would fire the alert forever")
            self.assertEqual("1.9", saved["assets"]["argos:en_zh"]["version"],
                             "a failed probe must leave the previous value alone")
            self.assertNotIn("voice:tts", saved["assets"],
                             "a stray row must not invent a baseline line")
            self.assertIn("--record-baseline", saved["note"],
                          "the file says who may advance it; that text is load-bearing")

    def test_a_name_finding_never_lands_in_the_digest_field(self):
        # The two voice findings carry different evidence, and writing a file name into `sha256`
        # would create a reference that cannot match anything ever again.
        entry = updates.to_baseline_entry("voice:tts", updates._row(
            "old.tar.bz2", "newer", "old.tar.bz2", "new.tar.bz2", "renamed", basis="name"))
        self.assertEqual("new.tar.bz2", entry["asset"])
        self.assertNotIn("sha256", entry)
        entry = updates.to_baseline_entry("voice:tts", updates._row(
            "same.tar.bz2", "newer", "old" * 21, "f" * 64, "replaced", basis="digest"))
        self.assertEqual("f" * 64, entry["sha256"])


class ShippedBaselineTests(unittest.TestCase):
    """The committed baseline has to be usable by the code it is meant to feed.

    These read like configuration tests because that is what the file is: a typo in a key, a
    missing `kind` on a voice line or a short digest would each make the daily check answer a
    confident wrong thing forever, and no unit test of the probes would ever notice.
    """

    def test_the_shipped_baseline_parses_and_matches_the_probe_table(self):
        state, entries = updates.read_baseline()
        self.assertEqual("ok", state, entries)
        for key, entry in entries.items():
            with self.subTest(key=key):
                family = key.split(":", 1)[0]
                self.assertIn(family, updates.PROBES,
                              "%s names a family with no probe; such a line is only ever "
                              "reported as failed" % key)
                self.assertTrue(entry.get("asset") or entry.get("version") or entry.get("sha256"),
                                "%s has nothing to compare" % key)
                digest = entry.get("sha256")
                if digest:
                    self.assertEqual(64, len(digest), "%s holds a truncated digest" % key)
                    self.assertEqual(digest.lower(), digest)
                if family == "voice":
                    self.assertTrue(entry.get("kind"),
                                    "%s without a release tag can never be queried (D76)" % key)

    def test_the_shipped_baseline_produces_no_incomparable_row_from_its_own_shape(self):
        # Each entry must carry what its probe needs, or the daily report quietly loses an asset:
        # argos wants a version, voice a name plus a tag, madlad a digest.
        state, entries = updates.read_baseline()
        required = {"argos": ("version",), "voice": ("asset", "kind"), "madlad": ("sha256",)}
        for key, entry in entries.items():
            family = key.split(":", 1)[0]
            for field in required[family]:
                with self.subTest(key=key, field=field):
                    self.assertTrue(entry.get(field),
                                    "%s is missing %s and would read as incomparable" % (key,
                                                                                          field))

    def test_an_unconfirmed_entry_says_where_its_value_came_from(self):
        # Without this rule the file would read as "upstream said this" for values that were only
        # ever copied from a pin -- and the difference decides whether a NEWER row is news or an
        # artefact of comparing upstream against ourselves.
        state, entries = updates.read_baseline()
        self.assertEqual("ok", state)
        for key, entry in entries.items():
            with self.subTest(key=key):
                if entry.get("observed_at"):
                    self.assertIn("+00:00", entry["observed_at"],
                                  "%s was timestamped without an offset (D71)" % key)
                else:
                    self.assertTrue(entry.get("seeded_from"),
                                    "%s has no live observation and no stated source" % key)


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
