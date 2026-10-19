# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for script/offline_bundle.py -- GA-13's last zero: enterprise distribution without a network.

The shape this tool is held to is the same shape the downloaders were held to, one layer down.

  * A bundle is untrusted bytes until its own manifest says otherwise -- D69's rule that "we got
    something" is not "we got the artifact whose digest we recorded", applied to a file someone
    copied onto a USB stick or an internal share instead of dialling out for.
  * Length is checked next to the digest, not folded into it -- D80's rule: a truncated copy is a
    transport failure with different consequences than a substituted file, so `CorruptError`
    (the archive is not the one that was packed) and `ConflictError` (the archive is fine but the
    disk already holds different bytes) must not be one type with one flag.
  * "Nothing was wrong" must be earned by having looked at something -- D74's rule: a bundle with
    no manifest, or an empty source tree that would produce a zero-asset bundle, is a refusal,
    not a silent success, because a timer or a script that reads exit 0 cannot tell the two apart.
  * No machine path is ever written into a record -- §5.2's and D70's rule, applied to the install
    receipt: the bundle is named by basename and digest, never by a drive-letter path or a home
    directory path -- the same shape `fetch_madlad_model.py`'s own seed comment warns about: a
    literal one-letter directory before a colon reads as a drive letter to any nearby check.

Nothing here touches the network, which is the whole point of the tool under test.
"""
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import model_origin                     # noqa: E402
import offline_bundle                   # noqa: E402


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _make_unit(dest_dir, *, asset="model.bin", body=b"checkpoint-bytes", version="1.9",
               url="https://example-cdn.test/repo/resolve/main/model.bin", extra_files=()):
    """One model directory: a real file, its origin record, and anything else the caller names.

    `extra_files` stands in for what an Argos package directory actually holds -- `metadata.json`,
    a `model/` folder of CT2 weights -- none of which the origin record names as an "asset",
    because the record tracks the *archive* it fetched, not every file it unpacked to. A bundler
    that only packed files the record names would install a directory that cannot load.
    """
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / asset).write_bytes(body)
    model_origin.record(dest_dir, asset=asset, version=version, sha256=_digest(body), url=url,
                        fetcher="test_offline_bundle.py")
    for name, data in extra_files:
        path = dest_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return dest_dir


class PackTests(unittest.TestCase):
    def test_a_unit_dir_and_its_record_both_land_in_the_bundle(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            unit = _make_unit(pathlib.Path(src) / "madlad", extra_files=[("config.json", b"{}")])
            bundle = pathlib.Path(out) / "b.zip"
            manifest = offline_bundle.pack(src, bundle)
            self.assertTrue(bundle.is_file())
            with zipfile.ZipFile(bundle) as zf:
                names = set(zf.namelist())
            self.assertIn((unit / "model.bin").relative_to(src).as_posix(), names)
            self.assertIn(model_origin.ORIGIN_FILE, "\n".join(names))
            self.assertEqual(1, manifest["format"])
            self.assertEqual(1, len(manifest["units"]))

    def test_a_file_the_record_never_names_is_still_packed(self):
        # The Argos shape: an origin record keyed on a pair code, beside a model/ folder it never
        # mentions. Dropping "unrecorded" files would produce a bundle that installs a directory
        # the sidecar cannot load, and verify() would say nothing is wrong -- the record and the
        # archive would agree with each other and both disagree with reality.
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            _make_unit(pathlib.Path(src) / "argos" / "translate-en_zh-1_9", asset="en_zh",
                       extra_files=[("model/model.bin", b"weights"), ("metadata.json", b"{}")])
            bundle = pathlib.Path(out) / "b.zip"
            offline_bundle.pack(src, bundle)
            with zipfile.ZipFile(bundle) as zf:
                self.assertIn("argos/translate-en_zh-1_9/model/model.bin", zf.namelist())
            self.assertEqual(0, len(offline_bundle.verify(bundle)))

    def test_several_units_under_one_root_are_all_found(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            _make_unit(pathlib.Path(src) / "madlad", asset="model.bin")
            _make_unit(pathlib.Path(src) / "voice" / "silero-vad", asset="silero_vad.onnx",
                       body=b"vad")
            bundle = pathlib.Path(out) / "b.zip"
            manifest = offline_bundle.pack(src, bundle)
            self.assertEqual({"madlad", "voice/silero-vad"},
                             {unit["dir"] for unit in manifest["units"]})

    def test_an_empty_source_tree_is_a_refusal_not_a_clean_bundle(self):
        # D74's shape, one layer down: "packed 0 assets successfully" reads exactly like a real
        # pack to anything that only checks the exit code.
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            bundle = pathlib.Path(out) / "b.zip"
            with self.assertRaises(offline_bundle.BundleError) as caught:
                offline_bundle.pack(src, bundle)
            self.assertIn("no", str(caught.exception).lower())
            self.assertFalse(bundle.exists())

    def test_a_directory_with_a_corrupt_record_is_reported_not_skipped_in_silence(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            unit = _make_unit(pathlib.Path(src) / "madlad")
            (unit / model_origin.ORIGIN_FILE).write_text("not json", encoding="utf-8")
            bundle = pathlib.Path(out) / "b.zip"
            problems = []
            offline_bundle.pack(src, bundle, report=lambda tag, msg: problems.append((tag, msg)))
            with self.assertRaises(offline_bundle.BundleError):
                offline_bundle.verify(bundle)


class ManifestIntegrityTests(unittest.TestCase):
    """Every way the archive can disagree with its own manifest must be caught, and named."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.src = pathlib.Path(self.dir.name) / "src"
        self.out = pathlib.Path(self.dir.name) / "out"
        self.src.mkdir()
        self.out.mkdir()
        self.unit = _make_unit(self.src / "madlad", body=b"whole-checkpoint")
        self.bundle = self.out / "b.zip"
        offline_bundle.pack(self.src, self.bundle)

    def _rewrite(self, members):
        """Rebuild the bundle with `members` ({arcname: bytes}) swapped in, manifest untouched."""
        buf = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename in members:
                    continue
                write.writestr(info, read.read(info.filename))
            for name, data in members.items():
                write.writestr(name, data)
        self.bundle.write_bytes(buf.getvalue())

    def test_untouched_bundle_verifies_clean(self):
        self.assertEqual([], offline_bundle.verify(self.bundle))

    def test_a_substituted_asset_is_caught_even_at_the_same_size(self):
        # Same length, different bytes: the D80 "size test alone" trap, moved off the network.
        # The substitution has to actually hold the size fixed -- an earlier draft of this test
        # shortened the body by two bytes while claiming to keep its length, and the size check
        # caught that instead of the digest check, so this asserted nothing about digest matching.
        self.assertEqual(len(b"whole-checkpoint"), len(b"whole-checkpoinT"))
        self._rewrite({"madlad/model.bin": b"whole-checkpoinT"})
        problems = offline_bundle.verify(self.bundle)
        self.assertTrue(any("model.bin" in p and "digest" in p for p in problems), problems)

    def test_a_truncated_asset_is_caught_by_size_before_digest(self):
        self._rewrite({"madlad/model.bin": b"whole"})
        problems = offline_bundle.verify(self.bundle)
        # "archive holds" is unique to the size sentence; matching on "bytes" alone would also
        # match the digest sentence ("does not match the archive's bytes"), so a mutation that
        # swapped the two checks would survive an assertion written that loosely.
        self.assertTrue(any("model.bin" in p and "archive holds" in p for p in problems), problems)

    def test_an_extra_member_the_manifest_never_listed_is_refused(self):
        # A rebuilt bundle smuggling in a file nobody recorded is exactly D69's shape: a name in
        # the list is not a statement about the bytes behind it.
        self._rewrite({"madlad/backdoor.bin": b"payload"})
        problems = offline_bundle.verify(self.bundle)
        self.assertTrue(any("backdoor.bin" in p and "manifest" in p for p in problems), problems)

    def test_a_member_the_manifest_promised_but_the_archive_dropped_is_refused(self):
        # `--rewrite` substitutes content, it cannot delete a member -- that needs rebuilding
        # the archive without it, which is what this does directly.
        buf = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename == "madlad/model.bin":
                    continue
                write.writestr(info, read.read(info.filename))
        self.bundle.write_bytes(buf.getvalue())
        problems = offline_bundle.verify(self.bundle)
        self.assertTrue(any("missing" in p.lower() and "model.bin" in p for p in problems),
                        problems)

    def test_a_bundle_without_a_manifest_is_refused_loudly(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename == offline_bundle.MANIFEST_NAME:
                    continue
                write.writestr(info, read.read(info.filename))
        self.bundle.write_bytes(buf.getvalue())
        with self.assertRaises(offline_bundle.BundleError) as caught:
            offline_bundle.verify(self.bundle)
        self.assertIn(offline_bundle.MANIFEST_NAME, str(caught.exception))

    def test_a_manifest_that_is_not_json_is_refused(self):
        self._rewrite({offline_bundle.MANIFEST_NAME: b"not json"})
        with self.assertRaises(offline_bundle.BundleError):
            offline_bundle.verify(self.bundle)

    def test_a_truncated_archive_is_caught_opening_it_not_midway_through_install(self):
        data = self.bundle.read_bytes()
        self.bundle.write_bytes(data[: len(data) // 2])
        with self.assertRaises(offline_bundle.BundleError):
            offline_bundle.verify(self.bundle)


class SafePathTests(unittest.TestCase):
    """`zipfile` guards against absolute paths on extract but not against `..` traversal."""

    def test_dot_segments_are_refused(self):
        with tempfile.TemporaryDirectory() as dest:
            with self.assertRaises(offline_bundle.BundleError):
                offline_bundle.safe_member_path(dest, "../escape.bin")
            with self.assertRaises(offline_bundle.BundleError):
                offline_bundle.safe_member_path(dest, "a/../../escape.bin")

    def test_a_bare_dot_or_empty_member_name_is_refused(self):
        # `resolve()` collapses every `..` shape, so those are caught by the containment check
        # alone -- the segment rule above is only load-bearing for the shapes resolution does
        # not move: "" and "." land exactly on the destination root, which the containment check
        # then reads as "still inside, fine" and would rename straight over the directory itself.
        with tempfile.TemporaryDirectory() as dest:
            for evil in ("", ".", "..", "madlad/."):
                with self.assertRaises(offline_bundle.BundleError):
                    offline_bundle.safe_member_path(dest, evil)

    def test_nested_paths_stay_inside(self):
        with tempfile.TemporaryDirectory() as dest:
            landed = offline_bundle.safe_member_path(dest, "madlad/model/weights.bin")
            self.assertTrue(str(landed).startswith(str(pathlib.Path(dest).resolve())))

    def test_absolute_shapes_are_recognized_by_shape_not_by_the_host(self):
        # `os.path.isabs` answers differently per platform for the drive-letter shapes, and the
        # containment check further down in `safe_member_path` happens to reject some of them on
        # its own -- but only under `ntpath` semantics, so a test going through that function
        # cannot tell the regex is even there. This calls the shape check directly, so its answer
        # is the same on the packing machine and on CI's ubuntu runner.
        for evil in ("/etc/passwd", "C:\\Windows\\evil.bin", "C:/mixed/evil",
                     "\\\\server\\share\\x"):
            self.assertTrue(offline_bundle._looks_absolute(evil), evil)
        for good in ("madlad/model.bin", "model.bin", "a/b/c.json"):
            self.assertFalse(offline_bundle._looks_absolute(good), good)

    def test_absolute_member_names_are_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as dest:
            for evil in ("/etc/passwd", "C:\\Windows\\evil.bin"):
                with self.assertRaises(offline_bundle.BundleError):
                    offline_bundle.safe_member_path(dest, evil)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = pathlib.Path(self.dir.name)
        self.src = base / "src"
        self.src.mkdir()
        self.bundle = base / "b.zip"
        self.body = b"whole-checkpoint"
        self.unit = _make_unit(self.src / "madlad", body=self.body)
        offline_bundle.pack(self.src, self.bundle)

    def test_install_to_an_empty_directory_reproduces_the_source(self):
        dest = pathlib.Path(self.dir.name) / "dest"
        offline_bundle.install(self.bundle, dest)
        got = (dest / "madlad" / "model.bin").read_bytes()
        self.assertEqual(self.body, got)
        state, entry = model_origin.read(dest / "madlad", asset="model.bin")
        self.assertEqual("ok", state, "the record must come along, not just the bytes")
        self.assertEqual(entry["sha256"], _digest(got))

    def test_the_install_receipt_names_no_machine_path(self):
        dest = pathlib.Path(self.dir.name) / "dest"
        offline_bundle.install(self.bundle, dest)
        receipt = pathlib.Path(dest) / offline_bundle.RECEIPT_FILE
        text = receipt.read_text(encoding="utf-8")
        self.assertNotIn(str(self.dir.name), text, "a receipt must not publish one machine's layout")
        self.assertNotIn("\\", text.replace("\\\\", ""), "no escaped backslash either")
        payload = json.loads(text)
        self.assertEqual("b.zip", payload["bundle"])
        self.assertEqual(_digest(self.bundle.read_bytes()), payload["bundle_sha256"])
        self.assertIn("+00:00", payload["installed_at"], "UTC with an offset, as everywhere else")

    def test_a_corrupt_bundle_installs_nothing_at_all(self):
        # Refuse before writing, not halfway through (AGENTS rule 8 / D38's shape): a bundle
        # whose second file is broken must not leave the first file already in place.
        buf = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename == "madlad/model.bin":
                    write.writestr(info, b"xxle-checkpoin")
                else:
                    write.writestr(info, read.read(info.filename))
        self.bundle.write_bytes(buf.getvalue())
        dest = pathlib.Path(self.dir.name) / "dest"
        with self.assertRaises(offline_bundle.CorruptError):
            offline_bundle.install(self.bundle, dest)
        self.assertFalse((dest / "madlad" / "model.bin").exists())

    def test_a_corrupt_member_stops_the_whole_install_not_just_its_own_file(self):
        # The test above corrupts the alphabetically-first file, so it cannot tell a batch-level
        # "verify everything before writing anything" guard from a per-file check that happens to
        # fail first anyway. Corrupting the last file instead is what separates the two: without
        # the batch guard, the two earlier files would already be in place by the time the last
        # one is caught, which is exactly the half-written model tree this whole file exists to
        # prevent -- and the mutation harness found that distinction by surviving a removal of
        # the batch guard under the original test alone.
        buf = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                write.writestr(info, read.read(info.filename))
            write.writestr("madlad/zzz_extra.bin", b"unrecorded trailing file")
        self.bundle.write_bytes(buf.getvalue())
        # Add it to the manifest so it is a *corrupt* member, not an *extra* one -- the two
        # branches must not be confused by an accidental shortcut here.
        manifest = json.loads(zipfile.ZipFile(self.bundle).read(offline_bundle.MANIFEST_NAME))
        manifest["units"][0]["files"].append({"path": "madlad/zzz_extra.bin",
                                               "sha256": _digest(b"promised-bytes"), "size": 14})
        buf2 = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf2, "w") as write:
            for info in read.infolist():
                if info.filename == offline_bundle.MANIFEST_NAME:
                    write.writestr(info, json.dumps(manifest))
                else:
                    write.writestr(info, read.read(info.filename))
        self.bundle.write_bytes(buf2.getvalue())

        dest = pathlib.Path(self.dir.name) / "dest"
        with self.assertRaises(offline_bundle.CorruptError):
            offline_bundle.install(self.bundle, dest)
        self.assertFalse((dest / "madlad" / "model.bin").exists(),
                         "the first file sorted must not have landed before the last one failed")
        self.assertFalse((dest / "madlad" / model_origin.ORIGIN_FILE).exists())

    def test_a_smuggled_member_does_not_escape_the_destination(self):
        manifest = json.loads(zipfile.ZipFile(self.bundle).read(offline_bundle.MANIFEST_NAME))
        manifest["units"][0]["files"].append(
            {"path": "madlad/../../escape.bin", "sha256": _digest(b"x"), "size": 1})
        buf = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename == offline_bundle.MANIFEST_NAME:
                    write.writestr(info, json.dumps(manifest))
                else:
                    write.writestr(info, read.read(info.filename))
            write.writestr("madlad/../../escape.bin", b"x")
        self.bundle.write_bytes(buf.getvalue())
        dest = pathlib.Path(self.dir.name) / "dest"
        dest.mkdir()
        with self.assertRaises(offline_bundle.CorruptError):
            offline_bundle.install(self.bundle, dest)
        self.assertFalse((pathlib.Path(self.dir.name) / "escape.bin").exists())
        self.assertEqual([], list(dest.iterdir()), "a refused install must leave nothing behind")

    def test_reinstalling_the_same_bundle_is_a_clean_no_op(self):
        dest = pathlib.Path(self.dir.name) / "dest"
        offline_bundle.install(self.bundle, dest)
        before = (dest / "madlad" / "model.bin").read_bytes()
        offline_bundle.install(self.bundle, dest)              # must not raise ConflictError
        self.assertEqual(before, (dest / "madlad" / "model.bin").read_bytes())

    def test_a_different_file_already_on_disk_is_a_conflict_not_a_silent_overwrite(self):
        dest = pathlib.Path(self.dir.name) / "dest"
        (dest / "madlad").mkdir(parents=True)
        (dest / "madlad" / "model.bin").write_bytes(b"newer-than-the-bundle")
        with self.assertRaises(offline_bundle.ConflictError) as caught:
            offline_bundle.install(self.bundle, dest)
        self.assertIn("model.bin", str(caught.exception))
        self.assertEqual(b"newer-than-the-bundle", (dest / "madlad" / "model.bin").read_bytes(),
                         "a refusal must not have replaced the newer file on the way out")

    def test_force_overwrites_the_conflict_and_says_so(self):
        dest = pathlib.Path(self.dir.name) / "dest"
        (dest / "madlad").mkdir(parents=True)
        (dest / "madlad" / "model.bin").write_bytes(b"newer-than-the-bundle")
        offline_bundle.install(self.bundle, dest, force=True)
        self.assertEqual(self.body, (dest / "madlad" / "model.bin").read_bytes())

    def test_a_conflict_is_never_a_corruption_and_vice_versa(self):
        # D80's two-type discipline moved off the network: "the archive is not what was packed"
        # and "the archive is fine but something newer is already here" are opposite answers, so
        # they must be opposite types -- one `--force` flag cannot be the fix for both.
        self.assertTrue(issubclass(offline_bundle.CorruptError, offline_bundle.BundleError))
        self.assertTrue(issubclass(offline_bundle.ConflictError, offline_bundle.BundleError))
        self.assertFalse(issubclass(offline_bundle.CorruptError, offline_bundle.ConflictError))
        dest = pathlib.Path(self.dir.name) / "dest"
        (dest / "madlad").mkdir(parents=True)
        (dest / "madlad" / "model.bin").write_bytes(b"different")
        buf = io.BytesIO()
        with zipfile.ZipFile(self.bundle) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename == "madlad/model.bin":
                    write.writestr(info, b"tampered-bytes")
                else:
                    write.writestr(info, read.read(info.filename))
        self.bundle.write_bytes(buf.getvalue())
        # Corruption wins the argument even with force given: a mismatched digest is not
        # something `--force` is allowed to talk itself into accepting.
        with self.assertRaises(offline_bundle.CorruptError):
            offline_bundle.install(self.bundle, dest, force=True)

    def test_partial_landing_is_atomic_per_file(self):
        # Every file lands under its final name only after its own digest checks out, matching
        # net_guard.fetch's discipline of never renaming an unverified staging file into place.
        dest = pathlib.Path(self.dir.name) / "dest"
        offline_bundle.install(self.bundle, dest)
        leftovers = [p.name for p in (dest / "madlad").iterdir() if p.name.endswith(".part")]
        self.assertEqual([], leftovers)


class CliTests(unittest.TestCase):
    def test_list_then_pack_then_verify_then_install_round_trip(self):
        with tempfile.TemporaryDirectory() as base:
            base = pathlib.Path(base)
            src = base / "src"
            src.mkdir()
            _make_unit(src / "madlad", body=b"checkpoint")
            bundle = base / "b.zip"
            dest = base / "dest"
            self.assertEqual(0, offline_bundle.main(["pack", "--src", str(src), "--out",
                                                    str(bundle)]))
            self.assertEqual(0, offline_bundle.main(["verify", str(bundle)]))
            self.assertEqual(0, offline_bundle.main(["install", str(bundle), "--dest", str(dest)]))
            self.assertTrue((dest / "madlad" / "model.bin").is_file())

    def test_pack_refuses_to_overwrite_an_existing_bundle(self):
        with tempfile.TemporaryDirectory() as base:
            base = pathlib.Path(base)
            src = base / "src"
            src.mkdir()
            _make_unit(src / "madlad")
            bundle = base / "b.zip"
            bundle.write_bytes(b"something already here")
            rc = offline_bundle.main(["pack", "--src", str(src), "--out", str(bundle)])
            self.assertEqual(1, rc)
            self.assertEqual(b"something already here", bundle.read_bytes())

    def test_install_reports_the_refusal_it_made_rather_than_exiting_silently_zero(self):
        # D74 one more time: a run that did nothing must not look like a run that did everything.
        with tempfile.TemporaryDirectory() as base:
            base = pathlib.Path(base)
            src = base / "src"
            src.mkdir()
            _make_unit(src / "madlad")
            bundle = base / "b.zip"
            offline_bundle.main(["pack", "--src", str(src), "--out", str(bundle)])
            dest = base / "dest"
            (dest / "madlad").mkdir(parents=True)
            (dest / "madlad" / "model.bin").write_bytes(b"newer")
            rc = offline_bundle.main(["install", str(bundle), "--dest", str(dest)])
            self.assertEqual(1, rc)


if __name__ == "__main__":
    unittest.main()
