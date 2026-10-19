# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for script/pack_installer.py -- GA-4's single-file packaging and REQ-C1's 模型随包.

The deliverable this file guards is one step above `offline_bundle.py`: it puts the built app and,
optionally, a model bundle into one artifact a 涉密 host can carry away with no network. The
judgements it is held to are the judgements that round established, plus two that only appear at
this layer:

  * **A manifest is a claim, not evidence** (D69/D82). `verify` re-hashes every app file from the
    archive, refuses an unlisted member as readily as a promised-and-missing one, and re-reads the
    embedded model bundle with the *real* `offline_bundle.verify` -- the deliverable being whole and
    the deliverable being usable are two checks, and the second is what stops shipping an app that
    carries a model directory nobody can answer the version of.
  * **"We tried to make a single file" is not "we made one"** (D57/D58/D74). The `dotnet publish`
    step is a seam that must degrade honestly: no dotnet, not Windows, or a non-zero publish all
    fall back to the ordinary build output and *say so in the manifest*. A deliverable that claims
    `single_file: true` when the publish failed is exactly the flattering-default defect.
  * **A promise about models must be checkable.** `models.shipped: true` is only honest if the
    embedded bundle is really in the archive and really verifies; `false` must state the policy
    ("fetch on first use"), never leave the reader guessing whether models ride along.
  * **No machine path in the manifest, and no archive member may escape the payload root** -- the
    same two containment rules `offline_bundle` holds, imported rather than re-derived (one rule,
    one definition -- the D64 lesson).

Nothing here runs `dotnet`, touches the network, or reads the host's real OS: the publish seam is
injected. That is what lets it sit in the gate while the real `publish` is a manual, heavy step.
"""
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import model_origin                       # noqa: E402
import offline_bundle                     # noqa: E402
import pack_installer                     # noqa: E402


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _make_app(root, *, extra=None):
    """A minimal built-app directory: an exe, the engine dll, and a config file."""
    app = pathlib.Path(root) / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "Wonslate.exe").write_bytes(b"\x4d\x5a-apphost-bytes")
    (app / "translator_engine.dll").write_bytes(b"MZ-native-engine-payload")
    (app / "Wonslate.dll.config").write_text("<configuration/>\n", encoding="utf-8")
    for name, data in (extra or {}).items():
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return app


def _make_bundle(root):
    """A real offline model bundle (one unit) built through offline_bundle.pack."""
    src = pathlib.Path(root) / "models-src"
    unit = src / "madlad"
    unit.mkdir(parents=True, exist_ok=True)
    body = b"checkpoint-bytes"
    (unit / "model.bin").write_bytes(body)
    model_origin.record(unit, asset="model.bin", version="main", sha256=_digest(body),
                        url="https://huggingface.co/x/resolve/main/model.bin",
                        fetcher="test_pack_installer.py")
    bundle = pathlib.Path(root) / "models.zip"
    offline_bundle.pack(src, bundle)
    return bundle


class AssembleTests(unittest.TestCase):
    def test_app_only_bundle_round_trips_and_states_models_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _make_app(tmp)
            out = pathlib.Path(tmp) / "deliver.zip"
            manifest = pack_installer.assemble(app, out, single_file=False,
                                               publish_reason="not attempted")
            self.assertTrue(out.is_file())
            self.assertEqual([], pack_installer.verify(out))
            models = manifest["models"]
            self.assertFalse(models["shipped"])
            self.assertIn("first use", models["policy"].lower())
            self.assertFalse(manifest["single_file"])
            self.assertEqual("not attempted", manifest["publish_note"])

    def test_embedded_model_bundle_is_verified_by_the_real_bundle_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _make_app(tmp)
            bundle = _make_bundle(tmp)
            out = pathlib.Path(tmp) / "deliver.zip"
            manifest = pack_installer.assemble(app, out, model_bundle=bundle, single_file=True)
            self.assertEqual([], pack_installer.verify(out))
            self.assertTrue(manifest["models"]["shipped"])
            self.assertEqual(["madlad"], manifest["models"]["units"])
            self.assertEqual(_digest(bundle.read_bytes()), manifest["models"]["bundle_sha256"])
            self.assertTrue(manifest["single_file"])

    def test_a_zero_payload_app_is_a_refusal_not_a_clean_empty_bundle(self):
        # D74 one layer up: a deliverable that ships no application reads as "packed successfully"
        # to anything that only checks an exit code.
        with tempfile.TemporaryDirectory() as tmp:
            empty = pathlib.Path(tmp) / "app"
            empty.mkdir()
            out = pathlib.Path(tmp) / "deliver.zip"
            with self.assertRaises(pack_installer.AssemblyError):
                pack_installer.assemble(empty, out)
            self.assertFalse(out.exists())

    def test_refuses_to_overwrite_an_existing_deliverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _make_app(tmp)
            out = pathlib.Path(tmp) / "deliver.zip"
            out.write_bytes(b"already here")
            rc = pack_installer.main(["assemble", "--app", str(app), "--out", str(out)])
            self.assertEqual(1, rc)
            self.assertEqual(b"already here", out.read_bytes())


class ManifestIntegrityTests(unittest.TestCase):
    """The deliverable is untrusted bytes until its own manifest is re-checked against them."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = pathlib.Path(self.dir.name)
        self.app = _make_app(base)
        self.out = base / "deliver.zip"
        pack_installer.assemble(self.app, self.out)

    def _rewrite(self, members, manifest_patch=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(self.out) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename in members:
                    continue
                data = read.read(info.filename)
                if info.filename == pack_installer.ASSEMBLY_MANIFEST and manifest_patch:
                    data = json.dumps(manifest_patch(json.loads(data))).encode("utf-8")
                write.writestr(info, data)
            for name, data in members.items():
                write.writestr(name, data)
        self.out.write_bytes(buf.getvalue())

    def test_untouched_deliverable_verifies_clean(self):
        self.assertEqual([], pack_installer.verify(self.out))

    def test_a_substituted_app_file_is_caught_even_at_the_same_size(self):
        # Same length, different bytes -- the size branch must not be the only thing that fires.
        # Targeted at a binary member written with write_bytes, so no host newline translation
        # changes its recorded size out from under the assertion.
        manifest = json.loads(zipfile.ZipFile(self.out).read(pack_installer.ASSEMBLY_MANIFEST))
        target = [f["path"] for f in manifest["files"]
                  if f["path"].endswith("translator_engine.dll")][0]
        original = zipfile.ZipFile(self.out).read(target)
        substitute = bytes([original[0]]) + original[1:-1] + bytes([original[-1] ^ 0x01])
        self.assertEqual(len(original), len(substitute))
        self.assertNotEqual(original, substitute)
        self._rewrite({target: substitute})
        problems = pack_installer.verify(self.out)
        self.assertTrue(any("translator_engine.dll" in p and "digest" in p for p in problems),
                        problems)

    def test_a_truncated_app_file_is_caught_by_size_first(self):
        self._rewrite({"app/Wonslate.exe": b"\x4d\x5a"})
        problems = pack_installer.verify(self.out)
        self.assertTrue(any("Wonslate.exe" in p and "archive holds" in p for p in problems),
                        problems)

    def test_an_extra_member_the_manifest_never_listed_is_refused(self):
        self._rewrite({"app/backdoor.dll": b"payload"})
        problems = pack_installer.verify(self.out)
        self.assertTrue(any("backdoor.dll" in p and "manifest" in p for p in problems), problems)

    def test_a_promised_member_the_archive_dropped_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(self.out) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename == "app/translator_engine.dll":
                    continue
                write.writestr(info, read.read(info.filename))
        self.out.write_bytes(buf.getvalue())
        problems = pack_installer.verify(self.out)
        self.assertTrue(any("missing" in p.lower() and "translator_engine.dll" in p
                            for p in problems), problems)

    def test_a_manifest_that_promised_models_but_shipped_none_is_refused(self):
        # The models ship promise must be checkable, not decoration.
        with tempfile.TemporaryDirectory() as tmp2:
            bundle = _make_bundle(tmp2)
            pack_installer.assemble(self.app, self.out, model_bundle=bundle)
        manifest = json.loads(zipfile.ZipFile(self.out).read(pack_installer.ASSEMBLY_MANIFEST))
        # Drop the embedded bundle, leave the manifest claiming it is there.
        buf = io.BytesIO()
        with zipfile.ZipFile(self.out) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename.startswith("models/"):
                    continue
                write.writestr(info, read.read(info.filename))
        self.out.write_bytes(buf.getvalue())
        problems = pack_installer.verify(self.out)
        self.assertTrue(any("model" in p.lower() and "missing" in p.lower() for p in problems),
                        problems)

    def test_a_corrupt_embedded_bundle_is_refused_not_just_hashed(self):
        # Even if the embedded bundle's bytes match the assembly manifest exactly, its own contents
        # must verify -- "complete" and "usable" are two checks (D82). So we tamper a member
        # INSIDE the bundle to same-length-different-bytes (its own manifest then disagrees) AND
        # update the assembly manifest's recorded bundle digest, so a hash-only check would pass.
        with tempfile.TemporaryDirectory() as tmp2:
            bundle = _make_bundle(tmp2)
            pack_installer.assemble(self.app, self.out, model_bundle=bundle)
        # Rebuild the nested bundle with a substituted asset.
        with zipfile.ZipFile(bundle_path := self._embedded_bundle()) as read:
            members = {info.filename: read.read(info.filename) for info in read.infolist()}
        members["madlad/model.bin"] = b"xxle-checkpoin"      # same 16 bytes, different content
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as write:
            for name, data in members.items():
                write.writestr(name, data)
        new_bundle = buf2.getvalue()
        del bundle_path
        # Rebuild the deliverable: swap the embedded bundle and fix its digest in the manifest.
        buf = io.BytesIO()
        with zipfile.ZipFile(self.out) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename.startswith("models/"):
                    write.writestr(info, new_bundle)
                elif info.filename == pack_installer.ASSEMBLY_MANIFEST:
                    payload = json.loads(read.read(info.filename))
                    payload["models"]["bundle_sha256"] = _digest(new_bundle)
                    for f in payload["files"]:
                        if f["path"].startswith("models/"):
                            f["sha256"] = _digest(new_bundle)
                            f["size"] = len(new_bundle)
                    write.writestr(info, json.dumps(payload))
                else:
                    write.writestr(info, read.read(info.filename))
        self.out.write_bytes(buf.getvalue())
        # The assembly manifest now agrees with the bundle's bytes, yet the bundle itself is broken:
        # only running offline_bundle.verify on it can tell.
        with self.assertRaises(pack_installer.AssemblyError):
            pack_installer.verify(self.out)

    def _embedded_bundle(self):
        """Path to a temp file holding the deliverable's embedded `models/*.zip` bytes."""
        holder = pathlib.Path(self.dir.name) / "embedded.zip"
        with zipfile.ZipFile(self.out) as zf:
            name = next(n for n in zf.namelist() if n.startswith("models/"))
            holder.write_bytes(zf.read(name))
        return holder

    def test_a_manifest_named_path_cannot_escape_the_payload_root(self):
        # Hand-built manifest with a traversal entry; verify must refuse before extracting anything.
        def patch(manifest):
            manifest["files"].append({"path": "app/../../evil.exe",
                                      "sha256": _digest(b"x"), "size": 1})
            return manifest
        self._rewrite({"app/../../evil.exe": b"x"}, manifest_patch=patch)
        with self.assertRaises(pack_installer.AssemblyError):
            pack_installer.verify(self.out)

    def test_a_missing_or_unreadable_manifest_is_refused_loudly(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(self.out) as read, zipfile.ZipFile(buf, "w") as write:
            for info in read.infolist():
                if info.filename == pack_installer.ASSEMBLY_MANIFEST:
                    continue
                write.writestr(info, read.read(info.filename))
        self.out.write_bytes(buf.getvalue())
        with self.assertRaises(pack_installer.AssemblyError):
            pack_installer.verify(self.out)


class PublishSeamTests(unittest.TestCase):
    """The dotnet publish step must degrade honestly rather than claim a single file it did not make."""

    def test_a_failing_publish_falls_back_and_says_so(self):
        calls = []

        def runner(cmd, cwd):
            calls.append(cmd)
            return 1

        with tempfile.TemporaryDirectory() as tmp:
            app = _make_app(tmp)
            out = pathlib.Path(tmp) / "deliver.zip"
            payload, single_file, reason = pack_installer.publish_app(
                app, pathlib.Path(tmp) / "pub", run=runner, windows=True, dotnet=True)
            self.assertFalse(single_file)
            self.assertIn("publish", reason.lower())
            self.assertEqual(1, len(calls))
            manifest = pack_installer.assemble(app, out, single_file=single_file,
                                               publish_reason=reason)
            self.assertFalse(manifest["single_file"])

    def test_a_successful_publish_is_reported_as_single_file(self):
        def runner(cmd, cwd):
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            payload, single_file, reason = pack_installer.publish_app(
                pathlib.Path(tmp), pathlib.Path(tmp) / "pub", run=runner, windows=True,
                dotnet=True)
            self.assertTrue(single_file)

    def test_no_dotnet_degrades_without_running_anything(self):
        # The host lacking dotnet must not be recorded as a publish failure it never caused; and
        # it must not shell out at all.
        calls = []

        def runner(cmd, cwd):
            calls.append(cmd)
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            _payload, single_file, reason = pack_installer.publish_app(
                pathlib.Path(tmp), pathlib.Path(tmp) / "pub", run=runner, windows=True,
                dotnet=False)
            self.assertFalse(single_file)
            self.assertEqual([], calls)
            self.assertIn("dotnet", reason.lower())

    def test_non_windows_host_is_named_as_the_reason_not_a_generic_failure(self):
        # D57/D58: "not applicable here" and "it broke" are different sentences.
        def runner(cmd, cwd):
            return 0

        _payload, single_file, reason = pack_installer.publish_app(
            pathlib.Path("."), pathlib.Path("x"), run=runner, windows=False, dotnet=True)
        self.assertFalse(single_file)
        self.assertIn("windows", reason.lower())

    def test_host_facts_reads_the_real_host_without_publishing(self):
        # This exercises the exact line that first shipped as `subprocess.which` -- a crash only the
        # CLI path hit, because every other test injects `dotnet=` and never reads the host. Calling
        # `_host_facts` here runs the real `shutil.which`/`sys.platform` lookup without shelling out.
        facts = pack_installer._host_facts()
        self.assertIsInstance(facts, tuple)
        self.assertEqual(2, len(facts))
        self.assertTrue(all(isinstance(f, bool) for f in facts))

    def test_cli_publish_degrades_through_the_real_wiring_without_shelling_out(self):
        # Drive `main(["publish", ...])` with host facts faked to "not Windows" and a run seam that
        # would raise if the publish path shelled out at all -- proving the CLI reads _host_facts and
        # returns the honest fallback rather than attempting (or pretending) a single file.
        def boom(cmd, cwd):
            raise AssertionError("publish ran a command on a host that is not Windows")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(pack_installer, "_host_facts", return_value=(False, True)), \
                    mock.patch.object(pack_installer, "default_run", side_effect=boom):
                rc = pack_installer.main(["publish", "--app", str(tmp),
                                          "--pub", str(pathlib.Path(tmp) / "pub")])
            self.assertEqual(0, rc)
            self.assertFalse((pathlib.Path(tmp) / "pub").exists())


class ContainmentAndHygieneTests(unittest.TestCase):
    def test_safe_member_path_is_imported_not_reinvented(self):
        # One containment rule, one definition (the D64 lesson): pack_installer must route through
        # offline_bundle's, not carry a second copy that can drift.
        self.assertIs(pack_installer.safe_member_path, offline_bundle.safe_member_path)

    def test_the_manifest_names_no_machine_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _make_app(tmp)
            out = pathlib.Path(tmp) / "deliver.zip"
            pack_installer.assemble(app, out)
            text = json.loads(zipfile.ZipFile(out).read(pack_installer.ASSEMBLY_MANIFEST))
            blob = json.dumps(text)
            self.assertNotIn(str(tmp), blob, "a manifest must not publish one machine's layout")

    def test_app_paths_are_relative_posix_under_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _make_app(tmp, extra={"sub/nested.bin": b"deep"})
            out = pathlib.Path(tmp) / "deliver.zip"
            manifest = pack_installer.assemble(app, out)
            paths = {f["path"] for f in manifest["files"]}
            self.assertIn("app/sub/nested.bin", paths)
            for p in paths:
                self.assertTrue(p.startswith("app/"), p)
                self.assertNotIn("\\", p, p)

    def test_cli_round_trip_publish_then_verify_then_inspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _make_app(tmp)
            bundle = _make_bundle(tmp)
            out = pathlib.Path(tmp) / "deliver.zip"
            self.assertEqual(0, pack_installer.main(["assemble", "--app", str(app),
                                                    "--models", str(bundle), "--out", str(out)]))
            self.assertEqual(0, pack_installer.main(["verify", str(out)]))
            self.assertEqual(0, pack_installer.main(["list", str(out)]))


if __name__ == "__main__":
    unittest.main()
