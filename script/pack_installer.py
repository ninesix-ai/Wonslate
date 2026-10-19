#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""pack_installer.py -- assemble a distributable offline application bundle (GA-4 + REQ-C1).

Why this exists. GA-4's second half -- "single-file distribution" -- and REQ-C1's "离线整合包
(模型随包)" are the same missing deliverable: an artifact a 涉密/air-gapped host can carry away and
run with no network. `offline_bundle.py` already moves *model assets* between machines; this file
wraps that into a single delivery that also carries the *application*, so the customer is not told
"install the app from a share, then fetch the models from the internet" -- which is precisely the
sentence the 涉密 scenario forbids.

The judgements it holds are the ones the model-bundler established, imported rather than
re-derived, plus two that only appear at this layer:

  * **A manifest is a claim, not evidence** (D69 / D82). `verify` re-hashes and re-measures every
    archived member against `wonslate-assembly.json` rather than trusting it, refuses an unlisted
    member as readily as a promised-and-dropped one, and refuses any recorded path that would land
    outside the payload root. The containment rule is `offline_bundle.safe_member_path` -- the
    same function object, not a second copy that could drift (the D64 lesson).
  * **Complete and usable are two checks** (D82). If models are shipped, `verify` extracts the
    embedded bundle and runs the *real* `offline_bundle.verify` on it: a deliverable whose embedded
    model archive hashes exactly like the manifest still promised, but whose contents no longer
    answer "which version is this", is refused -- shipping an app carrying an unusable model tree is
    worse than shipping no models and saying so.
  * **"We tried to make a single file" is not "we made one"** (D57/D58/D74). The `dotnet publish`
    step is an injectable seam. When the host is not Windows, has no `dotnet`, or the publish exits
    non-zero, it falls back to the ordinary build output and records *why* in the manifest. A
    deliverable that claims `single_file: true` when the publish never succeeded is exactly the
    flattering-default defect this repository keeps paying to remove.
  * **A promise about models must be checkable.** `models.shipped` is only `true` when the embedded
    bundle is present and verifies; when it is `false`, the manifest states the policy
    ("fetch on first use") instead of leaving the reader to guess whether weights ride along.

One integrity error type here, deliberately: `offline_bundle` splits `CorruptError` from
`ConflictError` because `--force` answers only the second. A deliverable is written once, not
installed over an existing tree, so there is no "keep the newer copy" decision -- a second type
would be a distinction with no consequence, and inventing one would repeat the very thing D82's
third bullet warns against.

Usage:
    python script/pack_installer.py publish --app <build output> --pub <publish dir>   # optional
    python script/pack_installer.py assemble --app <dir> [--models <bundle.zip>] --out deliver.zip
    python script/pack_installer.py list deliver.zip
    python script/pack_installer.py verify deliver.zip

Exit code: 0 = the operation completed, 1 = it was refused (reason printed).
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "script"))

import model_origin                                   # noqa: E402
import offline_bundle                                 # noqa: E402

# One containment rule, one definition: reuse the bundler's, not a copy that can drift.
safe_member_path = offline_bundle.safe_member_path

ASSEMBLY_MANIFEST = "wonslate-assembly.json"
APP_PREFIX = "app"
MODELS_PREFIX = "models"
FORMAT = 1
#: The model-shipping policy stated when no bundle rides along, so "false" is a sentence, not a gap
#: the reader has to interpret (D57/D58: "not applicable" and "it is missing" must not read alike).
NO_MODEL_POLICY = "models not shipped: each host fetches them on first use"


class AssemblyError(Exception):
    """The deliverable cannot be trusted. Carries a reason an operator can act on."""


def _digest_and_size(source, chunk=1 << 22):
    """(sha256, byte_count) for a path or an open stream, without buffering a large dll whole."""
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as handle:
            return _digest_and_size(handle, chunk=chunk)
    if isinstance(source, (bytes, bytearray)):
        return hashlib.sha256(source).hexdigest(), len(source)
    digest = hashlib.sha256()
    total = 0
    while True:
        block = source.read(chunk)
        if not block:
            break
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _host_facts():
    """(is_windows, dotnet_present) read from the real host, in one testable seam.

    Kept as its own function so the CLI `publish` branch -- which reads the host -- can be driven
    under test without running a multi-minute self-contained publish: the first version inlined
    `subprocess.which("dotnet")` in `main`, a path no test reached, and `subprocess` has no
    `which` (it is `shutil.which`) -- a crash that only the real CLI hit, which is exactly the
    "reachable only from a consumer nobody exercises" shape AGENTS rule 12 guards.
    """
    return (sys.platform.startswith("win"), shutil.which("dotnet") is not None)


def default_run(cmd, cwd):
    """The real seam for `dotnet publish`: run it, echo, and hand back the exit code."""
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None).returncode


def publish_app(app_src, pub_dir, *, run=None, windows=True, dotnet=True,
                runtime="win-x64", project=None):
    """Attempt a single-file self-contained publish; degrade honestly on every refusal.

    Returns ``(payload_dir, single_file, reason)``. ``single_file`` is true ONLY when the publish
    command was actually run and exited zero; every other path returns the plain build output with
    a reason that names which precondition failed, so the manifest can never claim a single file
    that was not made.
    """
    run = run or default_run
    if not windows:
        return pathlib.Path(app_src), False, ("single-file publish only produces a Windows app; "
                                              "this host is not Windows, using the build output")
    if not dotnet:
        return pathlib.Path(app_src), False, ("no dotnet on PATH; cannot run a single-file publish, "
                                              "using the build output")
    project = pathlib.Path(project) if project else (ROOT / "Wonslate.UI" / "Wonslate.UI.csproj")
    pub_dir = pathlib.Path(pub_dir)
    pub_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["dotnet", "publish", str(project), "-c", "Release", "-r", runtime,
           "--self-contained", "true", "-p:PublishSingleFile=true",
           "-p:IncludeNativeLibrariesForSelfExtract=true", "-o", str(pub_dir)]
    rc = run(cmd, pub_dir)
    if rc != 0:
        return pathlib.Path(app_src), False, ("dotnet publish exited %d; falling back to the "
                                              "build output (single file not produced)" % rc)
    return pub_dir, True, "dotnet publish produced a single-file, self-contained app"


def _models_summary(bundle):
    """{shipped, file, bundle_sha256, units} for an embedded offline bundle."""
    bundle = pathlib.Path(bundle)
    manifest = offline_bundle.list_assets(bundle)
    digest, _size = _digest_and_size(bundle)
    return {"shipped": True,
            "file": "%s/%s" % (MODELS_PREFIX, bundle.name),
            "bundle_sha256": digest,
            "units": sorted(unit["dir"] for unit in manifest["units"]),
            "policy": None}


def assemble(app_dir, out_zip, *, model_bundle=None, single_file=False, publish_reason=None,
             report=None):
    """Write the application (and an optional model bundle) into one verified-later deliverable.

    Returns the manifest. Raises `AssemblyError` on an empty application payload or when a supplied
    model bundle does not itself verify -- a deliverable built on an untrustworthy input is refused
    at build time, not discovered by the customer at run time.
    """
    app_dir = pathlib.Path(app_dir).resolve()
    out_zip = pathlib.Path(out_zip)

    app_members = []
    app_sources = []                       # (arcname, real file) paired, not re-derived later
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file():
            continue
        arcname = "%s/%s" % (APP_PREFIX, path.relative_to(app_dir).as_posix())
        digest, size = _digest_and_size(path)
        app_members.append({"path": arcname, "sha256": digest, "size": size})
        app_sources.append((arcname, path))
    if not app_members:
        raise AssemblyError("no application payload under %s -- refusing to build an empty "
                            "deliverable that would verify green" % app_dir)

    files = list(app_members)
    model_summary = {"shipped": False, "file": None, "bundle_sha256": None, "units": [],
                     "policy": NO_MODEL_POLICY}
    embedded_bytes = None
    if model_bundle is not None:
        problems = offline_bundle.verify(model_bundle)
        if problems:
            raise AssemblyError("the model bundle to be shipped does not verify (%d problem(s), "
                                "first: %s); refusing to assemble a deliverable around it"
                                % (len(problems), problems[0]))
        embedded_bytes = pathlib.Path(model_bundle).read_bytes()
        model_summary = _models_summary(model_bundle)
        files.append({"path": model_summary["file"],
                      "sha256": _digest_and_size(embedded_bytes)[0],
                      "size": len(embedded_bytes)})

    manifest = {"format": FORMAT, "assembled_at": _now(), "single_file": single_file,
                "publish_note": publish_reason if publish_reason is not None else "not attempted",
                "files": files, "models": model_summary}

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, src in app_sources:
            zf.write(src, arcname)
        if embedded_bytes is not None:
            zf.writestr(model_summary["file"], embedded_bytes)
        zf.writestr(ASSEMBLY_MANIFEST,
                    json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True))
    if report:
        report("assemble", "%d app file(s)%s -> %s"
               % (len(app_members), " + models" if embedded_bytes is not None else "",
                  out_zip.name))
    return manifest


def _read_manifest(zf):
    try:
        raw = zf.read(ASSEMBLY_MANIFEST)
    except KeyError:
        raise AssemblyError("deliverable has no %s -- there is nothing to check it against"
                            % ASSEMBLY_MANIFEST)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AssemblyError("%s is not readable JSON: %s" % (ASSEMBLY_MANIFEST, exc))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise AssemblyError("%s is not an assembly manifest (no 'files' list)" % ASSEMBLY_MANIFEST)
    return manifest


def list_assets(deliverable):
    try:
        with zipfile.ZipFile(deliverable) as zf:
            return _read_manifest(zf)
    except zipfile.BadZipFile as exc:
        raise AssemblyError("%s is not a readable zip archive: %s" % (deliverable, exc))


def verify(deliverable, *, report=None):
    """Check the deliverable against its own manifest. Returns integrity problems; raises the fatal ones.

    Fatal (raises `AssemblyError`): the archive cannot be opened, the manifest is missing or
    unreadable, a recorded path could land outside the payload root, or the embedded model bundle
    does not verify. Reported (returned): a digest or size that disagrees, a member the manifest
    never listed, a promised member the archive dropped -- each a full sentence naming the file.
    """
    problems = []
    try:
        zf = zipfile.ZipFile(deliverable)
    except (zipfile.BadZipFile, OSError) as exc:
        raise AssemblyError("%s is not a readable zip archive: %s" % (deliverable, exc))
    with zf:
        manifest = _read_manifest(zf)
        declared = {}
        for entry in manifest["files"]:
            # A recorded path must be a plain nested name; refuse traversal before anything else.
            try:
                safe_member_path(pathlib.Path.cwd(), entry["path"])
            except offline_bundle.BundleError as exc:
                raise AssemblyError("manifest names an unsafe path: %s" % exc)
            declared[entry["path"]] = entry
        actual = set(n for n in zf.namelist() if n != ASSEMBLY_MANIFEST)
        for name in sorted(actual - set(declared)):
            problems.append("%s is in the archive but the manifest never listed it" % name)
        for name in sorted(set(declared) - actual):
            problems.append("%s is promised by the manifest but missing from the archive" % name)
        for name, entry in sorted(declared.items()):
            if name not in actual:
                continue
            with zf.open(name) as handle:
                digest, size = _digest_and_size(handle)
            if size != entry["size"]:
                problems.append("%s: manifest says %d bytes, the archive holds %d"
                                % (name, entry["size"], size))
            elif digest != entry["sha256"]:
                problems.append("%s: manifest's digest does not match the archive's bytes" % name)

        models = manifest.get("models") or {}
        if models.get("shipped") and models.get("file") in actual:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(zf.read(models["file"]))
                staged = tmp.name
            try:
                inner = offline_bundle.verify(staged)
            finally:
                try:
                    os.unlink(staged)
                except OSError:
                    pass
            if inner:
                raise AssemblyError("the embedded model bundle is complete but not usable "
                                    "(%d problem(s), first: %s); refusing to ship it"
                                    % (len(inner), inner[0]))
    if problems and report:
        for line in problems:
            report("verify", line)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Assemble and verify an offline application bundle (GA-4 / REQ-C1).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pub = sub.add_parser("publish", help="run dotnet publish; prints where the payload landed")
    p_pub.add_argument("--app", required=True, help="the ordinary build output directory (fallback)")
    p_pub.add_argument("--pub", required=True, help="directory to publish the single file into")

    p_asm = sub.add_parser("assemble", help="bundle the app (and optional models) into a deliverable")
    p_asm.add_argument("--app", required=True, help="a built-application directory")
    p_asm.add_argument("--models", default=None, help="an offline_bundle zip to ship alongside")
    p_asm.add_argument("--out", required=True, help="deliverable path to write (must not exist)")
    p_asm.add_argument("--single-file", action="store_true",
                       help="record that the app payload is a dotnet single-file publish")
    p_asm.add_argument("--publish-note", default=None, help="why single_file is true/false")

    p_list = sub.add_parser("list", help="print a deliverable's manifest without reading every byte")
    p_list.add_argument("deliverable")

    p_verify = sub.add_parser("verify", help="check a deliverable against its own manifest")
    p_verify.add_argument("deliverable")

    args = parser.parse_args(argv)

    def report(tag, msg):
        print("[{}] {}".format(tag, msg).rstrip(), flush=True)

    try:
        if args.command == "publish":
            windows, dotnet = _host_facts()
            payload, single_file, reason = publish_app(
                args.app, args.pub, windows=windows, dotnet=dotnet)
            report("publish", "%s -> %s" % ("single-file" if single_file else "fallback", payload))
            report("publish", reason)
            return 0
        if args.command == "assemble":
            out = pathlib.Path(args.out)
            if out.exists():
                report("assemble", "%s already exists; refusing to overwrite it" % out.name)
                return 1
            manifest = assemble(args.app, out, model_bundle=args.models,
                                single_file=args.single_file, publish_reason=args.publish_note,
                                report=report)
            report("assemble", "single_file=%s models_shipped=%s at %s"
                   % (manifest["single_file"], manifest["models"]["shipped"],
                      manifest["assembled_at"]))
            return 0
        if args.command == "list":
            print(json.dumps(list_assets(args.deliverable), indent=2, ensure_ascii=False,
                             sort_keys=True))
            return 0
        if args.command == "verify":
            problems = verify(args.deliverable, report=report)
            if problems:
                report("verify", "%d problem(s) found" % len(problems))
                return 1
            report("verify", "clean -- every byte matches the manifest and any embedded bundle is "
                              "usable")
            return 0
    except AssemblyError as exc:
        report("error", str(exc))
        return 1
    raise SystemExit("unreachable command: %r" % args.command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
