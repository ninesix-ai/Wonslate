#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""offline_bundle.py -- move model assets between machines that never both have a network.

Why this exists (GA-13's last zero). The three `script/fetch_*.py` downloaders answer "which
version is this and is there a newer one" now (see `model_origin.py` and `check_model_updates.py`),
but every one of them assumes the machine installing a model can reach the public upstream. An
enterprise that distributes to air-gapped or egress-restricted hosts has no way to run them at all,
so "对齐企业集中分发" was still zero: the only path from "we fetched it" to "they installed it" was
each target machine fetching it again itself.

This tool closes that gap without inventing a second, weaker integrity rule:

  * **A bundle is untrusted bytes until its manifest says otherwise.** Same shape as D69 -- a list
    of names is not a statement about the bytes behind them -- so `verify()` recomputes every
    digest and size from the archive itself rather than trusting `wonslate-bundle.json`'s own
    accounting, and refuses a member the manifest never listed as readily as one it promised and
    the archive dropped.
  * **Length is checked beside the digest, not folded into it.** D80's rule moved off the network:
    a bundle half-copied onto a USB stick is a *transport* failure (`CorruptError`) and an
    on-disk file that predates it is a *content* disagreement (`ConflictError`) -- opposite
    answers, so opposite types, and `--force` is only ever a fix for the second one.
  * **A corrupt provenance record is refused, not skipped.** D74's rule: a bundle whose own
    `wonslate-origin.json` cannot be parsed still opens fine and still hashes fine, so an
    integrity-only check would call it clean and install a directory whose version nobody can
    later answer. `verify()` therefore reads every embedded record with the real parser
    (`model_origin`) rather than inventing a lighter one that might miss what `read()` catches.
  * **Nothing is written before everything checks out.** AGENTS rule 8 / D38's shape: verify the
    whole archive, then check every destination for a conflict, then land files -- so a refusal
    never leaves half a model tree updated.
  * **The install receipt names no machine path.** §5.2's and D70's rule, applied to where a
    bundle came from rather than where it was packed: the bundle is identified by basename and
    its own sha256, and every timestamp is UTC with an offset.

This module never opens a network connection. That is the point, and it is also why
`script/check_name_guards.py` does not scan it -- that gate's rule is "every `fetch_*.py` routes
through `net_guard`", and this is not a downloader, so giving it that name would be a lie.

Usage:
    python script/offline_bundle.py pack --src <data_dir>/models --out bundle.zip
    python script/offline_bundle.py list bundle.zip
    python script/offline_bundle.py verify bundle.zip
    python script/offline_bundle.py install bundle.zip --dest <data_dir>/models [--force]

Exit code: 0 = the operation completed, 1 = it was refused (reason printed).
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "script"))

import model_origin                                   # noqa: E402  (the real parser, not a copy)

MANIFEST_NAME = "wonslate-bundle.json"
#: Written next to the installed tree, separate from every copied `wonslate-origin.json` on
#: purpose: those say when the bytes were fetched from upstream, which is not when they arrived
#: on *this* machine, and folding the two together is how D80's cache-hit path nearly lost the
#: distinction between "recorded" and "re-fetched".
RECEIPT_FILE = "wonslate-install.json"
FORMAT = 1
#: A path chunk that survives an archive round trip and still means "outside the destination."
_UNSAFE_SEGMENTS = {"", ".", ".."}
#: Checked by shape, not by `os.path.isabs`, whose answer differs per host: a single letter
#: followed by a colon and a slash is absolute to `ntpath` and just a filename to `posixpath`.
#: A bundler's containment rule must not depend on which machine packed or unpacked the
#: archive -- that is D36's "a suite that pinned one spelling of one variable" class, one
#: layer down.
_ABSOLUTE_SHAPE = re.compile(r"^(?:[A-Za-z]:[\\/]?|[\\/])")


class BundleError(Exception):
    """Something wrong with the bundle itself. Carries a reason an operator can act on."""


class CorruptError(BundleError):
    """The archive does not match its own manifest, or cannot be read at all.

    `--force` must never talk past this. It is the D80 `GuardError` half relocated: bytes that
    are not the artifact we recorded are worthless, and re-copying the bundle -- not overwriting
    the destination -- is what fixes it.
    """


class ConflictError(BundleError):
    """The archive is intact, but the destination already holds different bytes for one asset.

    This is the D80 `TruncatedError`'s opposite: the bundle is fine, a real decision is what is
    missing ("keep what is here" vs. "put the bundle's copy here no matter what"). Only this
    class -- never `CorruptError` -- is what `--force` exists to answer.
    """


def _digest_and_size(source, chunk=1 << 22):
    """(sha256, byte_count) for real bytes, a path, or an already-open stream.

    A path is opened and closed here rather than at every call site -- an unclosed handle on a
    multi-gigabyte checkpoint directory is not a warning to shrug off, it is a lock another
    process cannot get around.
    """
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as handle:
            return _digest_and_size(handle, chunk=chunk)
    digest = hashlib.sha256()
    total = 0
    if isinstance(source, (bytes, bytearray)):
        digest.update(source)
        return digest.hexdigest(), len(source)
    while True:
        block = source.read(chunk)
        if not block:
            break
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def find_units(src_root):
    """Relative posix paths of every directory under `src_root` that holds an origin record."""
    src_root = pathlib.Path(src_root).resolve()
    units = []
    for dirpath, _dirnames, filenames in os.walk(src_root):
        if model_origin.ORIGIN_FILE not in filenames:
            continue
        here = pathlib.Path(dirpath)
        units.append(here.relative_to(src_root).as_posix())
    return sorted(units)


def _looks_absolute(relname):
    """True for any member name that reads as absolute on *either* host the bundle might visit.

    Kept as its own function rather than inlined into `safe_member_path` because the incidental
    containment check further down happens to reject some of these shapes already -- on
    `ntpath` semantics, and on `ntpath` semantics only -- so inlining makes the regex untestable
    in isolation: deleting it looks like no behaviour change from a Windows host, which is
    exactly the D36 "green here, different there" trap this exists to avoid.
    """
    return bool(_ABSOLUTE_SHAPE.match(relname or ""))


def safe_member_path(dest_root, relname):
    """Resolve `relname` under `dest_root`, refusing anything that could land outside it.

    `zipfile` refuses absolute paths and drive letters on `extract()`, but not a `..` segment --
    that is the whole of "Zip Slip" -- so the containment has to be checked by name, before
    anything is written, exactly the rule `fetch_madlad_model.py`'s own `final_path()` enforces
    for one downloaded file at a time. Stated here once for a whole archive instead.

    Absolute-ness is decided by `_looks_absolute`, not `os.path.isabs`, because the two hosts
    that ever touch a bundle (the packing machine, the installing machine) may not share an
    answer for a Windows-shaped name -- see the comment at `_ABSOLUTE_SHAPE`.
    """
    if _looks_absolute(relname):
        raise CorruptError("archive names an absolute path, which cannot land anywhere as a "
                           "relative member: %r" % relname)
    parts = relname.replace("\\", "/").split("/")
    if any(part in _UNSAFE_SEGMENTS for part in parts):
        raise CorruptError("archive path %r contains a segment that is not a plain name"
                           % relname)
    root = pathlib.Path(dest_root).resolve()
    joined = (root / relname.replace("/", os.sep)).resolve()
    if joined != root and not str(joined).startswith(str(root) + os.sep):
        raise CorruptError("archive path escapes the destination: %r resolves to %s" % (relname,
                                                                                        joined))
    return joined


def _read_manifest(zf):
    try:
        raw = zf.read(MANIFEST_NAME)
    except KeyError:
        raise CorruptError("bundle has no %s -- there is nothing to check its contents against"
                           % MANIFEST_NAME)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CorruptError("%s is not readable JSON: %s" % (MANIFEST_NAME, exc))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("units"), list):
        raise CorruptError("%s is not a bundle manifest (no 'units' list)" % MANIFEST_NAME)
    return manifest


def _declared_files(manifest):
    """{arcname: {path, sha256, size}} over every unit, refusing duplicate declarations."""
    out = {}
    for unit in manifest["units"]:
        for entry in unit.get("files", []):
            name = entry.get("path")
            if name in out:
                raise CorruptError("%s is declared twice in the manifest" % name)
            out[name] = entry
    return out


def pack(src_root, out_path, *, report=None):
    """Write every origin-bearing directory under `src_root` into one archive + manifest.

    Returns the manifest. Raises `BundleError` when there is nothing to pack -- a zero-unit
    bundle opens, hashes clean, and installs nothing, which reads exactly like success to a
    script that only looks at an exit code (D74's shape, one more layer down).
    """
    src_root = pathlib.Path(src_root).resolve()
    out_path = pathlib.Path(out_path)
    units = find_units(src_root)
    if not units:
        raise BundleError("no directory under %s holds a %s -- there is nothing to bundle. Fetch "
                          "at least one model first, rather than shipping an empty bundle that "
                          "verifies green." % (src_root, model_origin.ORIGIN_FILE))
    manifest = {"format": FORMAT, "packed_at": _now(), "units": []}
    written = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_dir in units:
            files = []
            here = src_root / rel_dir
            for path in sorted(here.rglob("*")):
                if not path.is_file():
                    continue
                arcname = path.relative_to(src_root).as_posix()
                digest, size = _digest_and_size(path)
                files.append({"path": arcname, "sha256": digest, "size": size})
                zf.write(path, arcname)
                written += 1
            manifest["units"].append({"dir": rel_dir, "files": files})
        manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False,
                                    sort_keys=True).encode("utf-8")
        zf.writestr(MANIFEST_NAME, manifest_bytes)
    if report:
        report("pack", "%d file(s) across %d unit(s) -> %s" % (written, len(units),
                                                               out_path.name))
    return manifest


def list_assets(bundle_path):
    """(manifest, [problem, ...]) -- reads the archive without checking every byte."""
    try:
        with zipfile.ZipFile(bundle_path) as zf:
            manifest = _read_manifest(zf)
    except zipfile.BadZipFile as exc:
        raise CorruptError("%s is not a readable zip archive: %s" % (bundle_path, exc))
    return manifest


def verify(bundle_path, *, report=None):
    """Check the archive against its own manifest. Returns a list of problems; raises on the fatal ones.

    Fatal (raises `CorruptError`): the archive cannot be opened, the manifest is missing or
    unreadable, an embedded origin record does not parse, or a member names an unsafe path.
    Reported (returned, not raised): a digest or size that disagrees, a member the manifest never
    listed, a promised member that is absent -- each a full sentence naming the file, because an
    operator deciding whether to copy a bundle again needs to know which asset is the problem.
    """
    problems = []
    try:
        zf = zipfile.ZipFile(bundle_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise CorruptError("%s is not a readable zip archive: %s" % (bundle_path, exc))
    with zf:
        manifest = _read_manifest(zf)
        declared = _declared_files(manifest)
        actual = set(name for name in zf.namelist() if name != MANIFEST_NAME)
        for name in sorted(actual - set(declared)):
            problems.append("%s is in the archive but the manifest never listed it" % name)
        for name in sorted(set(declared) - actual):
            problems.append("%s is promised by the manifest but missing from the archive" % name)
        for name, entry in sorted(declared.items()):
            if name not in actual:
                continue
            safe_member_path(pathlib.Path.cwd(), name)      # destination-independent traversal check
            try:
                handle = zf.open(name)
            except KeyError:
                continue
            with handle:
                digest, size = _digest_and_size(handle)
            if size != entry["size"]:
                problems.append("%s: manifest says %d bytes, the archive holds %d"
                                % (name, entry["size"], size))
            elif digest != entry["sha256"]:
                problems.append("%s: manifest's digest does not match the archive's bytes" % name)

        # Every embedded provenance record must still answer "which version is this" on the far
        # side -- an archive that hashes clean but carries an unreadable record would install a
        # directory that then reports `incomplete` forever (D81's reader-side failure again).
        import tempfile
        for unit in manifest["units"]:
            origin_arcname = (unit["dir"] + "/" + model_origin.ORIGIN_FILE).replace("\\", "/")
            if origin_arcname not in actual:
                continue
            with tempfile.TemporaryDirectory() as tmp:
                pathlib.Path(tmp, model_origin.ORIGIN_FILE).write_bytes(zf.read(origin_arcname))
                state, _payload = model_origin.read(tmp, asset=None, allow_ambiguous=True)
            if state != "ok":
                raise CorruptError("%s in unit %r is not a readable origin record (state %r); "
                                   "the bundle cannot answer which version it carries"
                                   % (model_origin.ORIGIN_FILE, unit["dir"], state))

    if problems and report:
        for line in problems:
            report("verify", line)
    return problems


def _bundle_digest(path):
    with open(path, "rb") as handle:
        digest, _size = _digest_and_size(handle)
    return digest


def install(bundle_path, dest_root, *, force=False, report=None):
    """Verify everything first, then land every file, then stamp the receipt.

    `force` answers `ConflictError` only -- it never makes `CorruptError` passable, because a
    bundle whose bytes do not match its own manifest is not something an operator can authorize
    their way into trusting (the D80 two-type discipline, relocated).
    """
    dest_root = pathlib.Path(dest_root).resolve()
    problems = verify(bundle_path, report=report)
    if problems:
        raise CorruptError("%d integrity problem(s) in %s; nothing was installed"
                           % (len(problems), pathlib.Path(bundle_path).name))

    manifest = list_assets(bundle_path)
    declared = _declared_files(manifest)

    # Phase 2: check every destination before writing any of them (AGENTS rule 8 / D38).
    conflicts = []
    for name, entry in sorted(declared.items()):
        target = safe_member_path(dest_root, name)
        if not target.exists():
            continue
        digest, _size = _digest_and_size(target)
        if digest != entry["sha256"]:
            conflicts.append(name)
    if conflicts and not force:
        raise ConflictError("%d file(s) already on disk differ from the bundle: %s. Re-run with "
                            "--force to replace them, or do not install over a newer copy"
                            % (len(conflicts), ", ".join(conflicts[:5])))

    # Phase 3: land everything, each file under a temporary name until its own digest checks out.
    landed = 0
    with zipfile.ZipFile(bundle_path) as zf:
        for name, entry in sorted(declared.items()):
            target = safe_member_path(dest_root, name)
            if target.exists():
                digest, _size = _digest_and_size(target)
                if digest == entry["sha256"]:
                    continue                       # already exactly right; not a conflict, not a write
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.parent / (target.name + ".part")
            with zf.open(name) as source, open(staging, "wb") as out:
                while True:
                    block = source.read(1 << 22)
                    if not block:
                        break
                    out.write(block)
            got, _size = _digest_and_size(staging)
            if got != entry["sha256"]:
                staging.unlink()
                raise CorruptError("%s did not land as its manifest digest promised" % name)
            os.replace(staging, target)
            landed += 1

    receipt = {"bundle": pathlib.Path(bundle_path).name, "bundle_sha256": _bundle_digest(bundle_path),
               "bundle_format": manifest.get("format"), "packed_at": manifest.get("packed_at"),
               "installed_at": _now(), "units": [unit["dir"] for unit in manifest["units"]],
               "files_written": landed}
    dest_root.mkdir(parents=True, exist_ok=True)
    (dest_root / RECEIPT_FILE).write_text(json.dumps(receipt, indent=2, ensure_ascii=False,
                                                     sort_keys=True) + "\n",
                                          encoding="utf-8", newline="\n")
    if report:
        report("install", "%d file(s) landed; receipt written to %s" % (landed, RECEIPT_FILE))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Package and install model assets offline (GA-13).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pack = sub.add_parser("pack", help="bundle every origin record under --src")
    p_pack.add_argument("--src", required=True, help="a models/ root to read")
    p_pack.add_argument("--out", required=True, help="bundle path to write (must not exist yet)")

    p_list = sub.add_parser("list", help="print a bundle's manifest without reading every byte")
    p_list.add_argument("bundle")

    p_verify = sub.add_parser("verify", help="check a bundle against its own manifest")
    p_verify.add_argument("bundle")

    p_install = sub.add_parser("install", help="land a verified bundle under --dest")
    p_install.add_argument("bundle")
    p_install.add_argument("--dest", required=True, help="a models/ root to write into")
    p_install.add_argument("--force", action="store_true",
                           help="replace files already on disk that differ (never bypasses a "
                                "digest or size mismatch)")

    args = parser.parse_args(argv)

    def report(tag, msg):
        print("[{}] {}".format(tag, msg).rstrip(), flush=True)

    try:
        if args.command == "pack":
            out = pathlib.Path(args.out)
            if out.exists():
                report("pack", "%s already exists; refusing to overwrite it" % out.name)
                return 1
            manifest = pack(args.src, out, report=report)
            report("pack", "%d unit(s), packed_at %s" % (len(manifest["units"]),
                                                          manifest["packed_at"]))
            return 0
        if args.command == "list":
            manifest = list_assets(args.bundle)
            print(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "verify":
            problems = verify(args.bundle, report=report)
            if problems:
                report("verify", "%d problem(s) found" % len(problems))
                return 1
            report("verify", "clean -- every byte matches the manifest and every record parses")
            return 0
        if args.command == "install":
            install(args.bundle, args.dest, force=args.force, report=report)
            return 0
    except BundleError as exc:
        report("error", str(exc))
        return 1
    raise SystemExit("unreachable command: %r" % args.command)      # argparse should refuse first


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
