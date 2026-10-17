#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""fetch_voice_models.py -- download and unpack the offline voice stack (N-10).

Why: the end-to-end voice pipeline needs three things on disk before it can run
at all -- a voice-activity detector, an ASR model and a TTS model. Without them
the pipeline can only degrade, so a fresh machine has no way to tell "not wired
up" from "models missing". This script fetches the official sherpa-onnx releases
and verifies the layout the C# side expects.

Models land OUTSIDE the repository, under <data_dir>/models/<name>, using the
same resolution rule as the rest of the stack (LT_DATA_DIR / WONSLATE_DATA_DIR
override, then the per-OS user data directory). Nothing here writes into the git
working tree, and the weights are never committed: they are large, they carry
their own upstream terms, and a checkout should not need a gigabyte to build.

Licenses: SenseVoice (FunAudioLLM), Kokoro (hexgrad), Silero VAD (snakers4) and
the sherpa-onnx runtime are Apache-2.0 / MIT; see license_gate.rs for the
registered dependency list.

Sizes: VAD 0.6 MB, Kokoro TTS 140 MB, SenseVoice ASR 999 MB. Fetch the ASR model
only when you intend to run the pipeline end to end.

Usage:
    python script/fetch_voice_models.py              # vad + tts (small, ~141 MB)
    python script/fetch_voice_models.py --only asr   # add the 999 MB ASR model
    python script/fetch_voice_models.py --only all
    python script/fetch_voice_models.py --dest <dir>
    python script/fetch_voice_models.py --force

Exit code: 0 = every requested component is present and verified, 1 = something failed.
"""
import argparse
import pathlib
import sys
import tarfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

from sidecar.ct2_sidecar import default_model_dir  # noqa: E402  (data-dir single source of truth)
import net_guard                                   # noqa: E402  (the fetch policy)
import model_origin                                # noqa: E402  (which version landed, GA-13)

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
USER_AGENT = "wonslate-fetch-voice/1.0"
# Release assets are served from github.com and redirected to a per-upload host under
# objects.githubusercontent.com, so the suffix is what makes redirects land; both are
# re-checked hop by hop by net_guard rather than trusted once (defect D69: this script used
# to be a bare urlopen with neither host nor integrity checks).
ALLOWED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com"})
ALLOWED_HOST_SUFFIXES = (".githubusercontent.com",)

# Declared for script/check_name_guards.py. sense-voice sits in UNPINNED_EXCEPTIONS
# because the release API returned a null digest for that 1.0 GB asset and it is not on
# this machine to baseline from -- so it is written down as a known gap instead of looking
# covered. `--only asr` therefore fails closed with UnpinnedAsset unless --allow-unpinned
# is passed, and that flag prints which asset it is skipping.
DOWNLOADS = ("silero_vad.onnx",
             "kokoro-int8-multi-lang-v1_1.tar.bz2",
             "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2")
UNPINNED_EXCEPTIONS = ("sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",)

# Pins with the provenance of each value stated. As of 2026-10-15 the GitHub release API
# publishes a sha256 `digest` per asset, and:
#   silero_vad.onnx -- upstream digest, **confirmed against the copy on this machine**;
#   kokoro-int8-multi-lang-v1_1.tar.bz2 -- upstream digest. Not re-verifiable locally,
#     because only the unpacked tree is kept here and the archive is gone: the pin still
#     guards every future fetch, and says so rather than pretending it was checked now.
# sherpa-onnx-sense-voice-...tar.bz2 is deliberately **absent**: the API returned a null
# digest for that 1.0 GB asset and it is not on this machine to baseline from. An empty
# entry would read as "checked", which is the opposite of what happened; fetching it
# therefore requires --allow-unpinned, and that flag prints which asset is unpinned.
EXPECTED_SHA256 = {
    "silero_vad.onnx": (
        "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
        "GitHub release asset digest 2026-10-15, matched against the local copy"),
    "kokoro-int8-multi-lang-v1_1.tar.bz2": (
        "a1e94694776049035c4f2c6529f003aaece993c76aae9a78995831c3c4dcafc6",
        "GitHub release asset digest 2026-10-15 (archive not on disk to re-verify)"),
}

# name -> (kind, url, archive?, required files once unpacked)
COMPONENTS = {
    "vad": (
        "silero-vad",
        RELEASE + "/asr-models/silero_vad.onnx",
        False,
        ["silero_vad.onnx"],
    ),
    "tts": (
        "kokoro-int8-multi-lang-v1_1",
        RELEASE + "/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2",
        True,
        ["model.int8.onnx", "voices.bin", "tokens.txt", "espeak-ng-data"],
    ),
    "asr": (
        "sense-voice",
        RELEASE + "/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
        True,
        ["model.int8.onnx", "tokens.txt"],
    ),
}

DEFAULT_ONLY = ("vad", "tts")


def say(tag, msg=""):
    print("[{}] {}".format(tag, msg).rstrip(), flush=True)


def download(url, dest, attempts=4, allow_unpinned=False, chunk=1 << 20):
    """Fetch one asset through the shared policy, verifying its pinned digest.

    Returns the digest on success, False on failure -- the caller records which bytes landed,
    and handing back the value already computed beats hashing a 1 GB archive twice.

    Policy refusals stop the retry loop at once -- a missing pin or a checksum mismatch
    is not something another four attempts at the same URL can resolve.
    """
    name = pathlib.Path(dest).name
    pin = EXPECTED_SHA256.get(name)
    for i in range(1, attempts + 1):
        try:
            digest, size = net_guard.fetch(
                url, dest, ALLOWED_HOSTS, ALLOWED_HOST_SUFFIXES,
                expected_sha256=pin[0] if pin else None,
                digest_source=pin[1] if pin else None,
                allow_unpinned=allow_unpinned, user_agent=USER_AGENT,
                timeout=300, chunk=chunk, report=say, progress_every=50 << 20)
            say("fetch", "{} ({:.1f} MB, sha256 {}{})".format(
                name, size / 1e6, digest[:16],
                "" if pin else " UNPINNED (--allow-unpinned)"))
            return digest
        except net_guard.UnpinnedAsset as exc:
            say("fetch", "REFUSED: {}".format(exc))
            return False
        except net_guard.GuardError as exc:
            text = str(exc)
            if ("checksum mismatch" in text or "not allowlisted" in text
                    or "disallowed" in text or "not https" in text or "non-public" in text):
                say("fetch", "REFUSED: {}".format(exc))
                return False
            say("fetch", "attempt {}/{} failed: {}".format(i, attempts, exc))
        except (urllib.error.URLError, OSError) as exc:
            say("fetch", "attempt {}/{} failed: {}".format(i, attempts, exc))
        time.sleep(5)
    return False


def unpack(archive, target):
    """Extract a .tar.bz2 and collapse the single top-level directory it carries.

    Members are checked before extraction: `filter="data"` is only available from
    Python 3.12, and this project targets 3.10, so the traversal guard is done by
    hand rather than left to the stdlib default.
    """
    with tarfile.open(archive, "r:bz2") as tf:
        for member in tf.getmembers():
            name = pathlib.PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise SystemExit("refusing to extract unsafe member: {}".format(member.name))
            if not (member.isfile() or member.isdir()):
                raise SystemExit("refusing to extract non-file member: {}".format(member.name))
        tf.extractall(target)

    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in sorted(inner.iterdir()):
            item.rename(target / item.name)
        inner.rmdir()
        say("unpack", "flattened the archive's top-level directory")


def verify(target, required):
    """Return a problem description when the layout is incomplete, else None."""
    for name in required:
        if not (target / name).exists():
            return "{} missing".format(name)
    return None


def fetch(key, dest_root, force, allow_unpinned=False):
    folder, url, is_archive, required = COMPONENTS[key]
    target = dest_root / folder
    target.mkdir(parents=True, exist_ok=True)
    _before, name = url.rsplit("/", 1)
    archive = dest_root / name
    pin = EXPECTED_SHA256.get(name)

    if verify(target, required) is None and not force:
        # Be exact about what a cache hit proves. The layout is always checked; the digest
        # is re-checked only when the archive is still on disk -- for an archive component
        # the unpacked tree is what gets kept, so there is nothing left to hash. Printing
        # "cached" over that difference would claim a verification that did not happen.
        extra = "; cached archive digest re-verified"
        if archive.is_file() and pin:
            got = net_guard.sha256_of(archive)
            if got != pin[0]:
                say("verify", "FAIL {} cached archive is sha256 {}, pin says {}".format(
                    key, got[:16], pin[0][:16]))
                return False
            # A cache hit whose bytes were just re-hashed is a fact worth recording: this is
            # the case where a directory has been sitting on disk since before GA-13 existed.
            _record_origin(target, url, name, got)
        elif pin:
            extra = "; digest was enforced at download time only (archive no longer present)"
        else:
            extra = "; UNPINNED asset, nothing to verify"
        say("cached", "{} -> {} (layout verified{})".format(key, target, extra))
        return True

    if force or not (archive.is_file() and archive.stat().st_size > 100_000):
        say("fetch", "{} <- {}".format(key, url))
        digest = download(url, archive, allow_unpinned=allow_unpinned)
        if not digest:
            return False
    elif pin:
        # A file of the right size is not the right file: check it before trusting it.
        got = net_guard.sha256_of(archive)
        if got != pin[0]:
            say("fetch", "{} on disk is sha256 {}, pin says {} -- re-fetching".format(
                name, got[:16], pin[0][:16]))
            digest = download(url, archive, allow_unpinned=allow_unpinned)
            if not digest:
                return False
        else:
            digest = got
            say("fetch", "{} cached and digest-verified ({:.1f} MB)".format(
                name, archive.stat().st_size / 1e6))
    else:
        digest = None
        say("fetch", "{} cached ({:.1f} MB) but UNPINNED: no digest to check it against".format(
            name, archive.stat().st_size / 1e6))

    if is_archive:
        if force:
            for item in sorted(target.iterdir()):
                item.unlink() if item.is_file() else __import__("shutil").rmtree(item)
        unpack(archive, target)
    else:
        # Single-file component: move it into place rather than copying.
        archive.replace(target / name)

    problem = verify(target, required)
    if problem:
        say("verify", "FAIL {}: {}".format(key, problem))
        return False
    if digest:
        _record_origin(target, url, name, digest)
    say("verify", "OK   {} -> {}".format(key, target))
    return True


def _record_origin(target, url, name, digest):
    """Record what landed, in the component directory it belongs to.

    These assets have no version scheme of their own -- the release tag is a long-lived bucket
    (`asr-models`, `tts-models`) and the version is spelled inside the file name -- so both are
    kept: the name as the version, the tag as `kind`, plus the bytes actually received.
    """
    tag = url.split("/releases/download/", 1)[1].split("/", 1)[0] if "/releases/download/" in url else None
    model_origin.record(target, asset=name, version=name, sha256=digest, url=url,
                        fetcher="fetch_voice_models.py", kind=tag)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download the offline voice stack")
    parser.add_argument("--only", default=",".join(DEFAULT_ONLY),
                        help="comma-separated subset of vad,tts,asr (or 'all')")
    parser.add_argument("--dest", default=None,
                        help="target root (default: <data_dir>/models)")
    parser.add_argument("--force", action="store_true", help="re-download and re-unpack")
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="fetch an asset even when no sha256 is pinned for it "
                             "(prints which asset is unpinned; default refuses)")
    args = parser.parse_args(argv)

    only = [k.strip() for k in args.only.split(",") if k.strip()]
    if "all" in only:
        only = list(COMPONENTS)
    unknown = [k for k in only if k not in COMPONENTS]
    if unknown:
        raise SystemExit("unknown component(s): {} (choose from {})".format(
            unknown, ", ".join(COMPONENTS)))

    # default_model_dir() is <data_dir>/models/argos; the voice models are its siblings.
    dest_root = pathlib.Path(args.dest) if args.dest else default_model_dir().parent
    dest_root.mkdir(parents=True, exist_ok=True)
    say("*", "target root: {}".format(dest_root))

    failed = [k for k in only
              if not fetch(k, dest_root, args.force, args.allow_unpinned)]
    print("", flush=True)
    if failed:
        say("*", "FAILED: {}".format(", ".join(failed)))
        return 1
    say("*", "voice stack ready: {}".format(", ".join(only)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
