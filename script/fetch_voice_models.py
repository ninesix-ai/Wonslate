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
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidecar.ct2_sidecar import default_model_dir  # noqa: E402  (data-dir single source of truth)

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
USER_AGENT = "wonslate-fetch-voice/1.0"

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


def download(url, dest, attempts=4, chunk=1 << 20):
    """Download with resumable-ish retries; reports progress every 50 MB."""
    for i in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=300) as response, open(dest, "wb") as out:
                total, mark = 0, 0
                while True:
                    block = response.read(chunk)
                    if not block:
                        break
                    out.write(block)
                    total += len(block)
                    if total - mark >= 50 << 20:
                        mark = total
                        say("fetch", "  {} {:.0f} MB".format(dest.name, total / 1e6))
            say("fetch", "{} ({:.1f} MB)".format(dest.name, total / 1e6))
            return True
        except Exception as exc:                      # noqa: BLE001 - transient network errors
            say("fetch", "attempt {}/{} failed: {}: {}".format(
                i, attempts, type(exc).__name__, exc))
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


def fetch(key, dest_root, force):
    folder, url, is_archive, required = COMPONENTS[key]
    target = dest_root / folder
    target.mkdir(parents=True, exist_ok=True)

    if verify(target, required) is None and not force:
        say("cached", "{} -> {}".format(key, target))
        return True

    _, name = url.rsplit("/", 1)
    archive = dest_root / name
    if force or not (archive.is_file() and archive.stat().st_size > 100_000):
        say("fetch", "{} <- {}".format(key, url))
        if not download(url, archive):
            return False
    else:
        say("fetch", "{} cached ({:.1f} MB)".format(name, archive.stat().st_size / 1e6))

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
    say("verify", "OK   {} -> {}".format(key, target))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download the offline voice stack")
    parser.add_argument("--only", default=",".join(DEFAULT_ONLY),
                        help="comma-separated subset of vad,tts,asr (or 'all')")
    parser.add_argument("--dest", default=None,
                        help="target root (default: <data_dir>/models)")
    parser.add_argument("--force", action="store_true", help="re-download and re-unpack")
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

    failed = [k for k in only if not fetch(k, dest_root, args.force)]
    print("", flush=True)
    if failed:
        say("*", "FAILED: {}".format(", ".join(failed)))
        return 1
    say("*", "voice stack ready: {}".format(", ".join(only)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
