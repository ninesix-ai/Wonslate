#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""fetch_madlad_model.py -- download the MADLAD-400 CT2 checkpoint.

Why: the madlad sidecar backend (sidecar/ct2_sidecar.py, --backend madlad)
needs ONE multilingual checkpoint (model.bin + spiece.model) that serves every
supported pair. This script downloads it from a HuggingFace-compatible
endpoint and verifies the layout -- so a fresh machine goes from "pip install"
to an any-pair offline backend in one command.

Models land OUTSIDE the repository, under <data_dir>/models/madlad, using the
same rule as the backend and translator-engine/src/config.rs (LT_DATA_DIR /
WONSLATE_DATA_DIR override, then the per-OS user data directory). Nothing here
writes into the git working tree.

Endpoint policy: enforced by script/net_guard.py, the one fetch policy shared with
fetch_argos_models.py and fetch_voice_models.py -- https only, hosts restricted to an
allowlist of public HuggingFace endpoints, redirects to other hosts rejected, and each
host's addresses resolved and checked against private/loopback ranges before connecting
(the tool is SSRF-reachable because $HF_ENDPOINT lets a user point it at a mirror).

Integrity policy (defect D69): every file is checked against a pinned sha256 before it is
renamed into place, and verify() re-checks the digests of whatever is already in the
target directory, so a cache hit or a hand-copied file cannot skip the rule. Each pin
carries its provenance in EXPECTED_SHA256: two are upstream HuggingFace LFS oids that
were confirmed against the local copy, two are local baselines because the repo publishes
no sha256 for non-LFS files. That difference is stated rather than smoothed over.

Path policy: every write goes through a tempfile.mkstemp handle inside net_guard.fetch (no dynamic
path is ever opened), and the final os.replace target is normalized with
os.path.realpath, rejected if it contains "..", and verified to stay inside
the requested destination directory before the rename.

License: MADLAD-400 weights are Apache-2.0; the CT2 conversion downloaded here
(Heng666/madlad400-3b-mt-ct2-int8) is Apache-2.0 as well. See
translator-engine/src/license_gate.rs.

Usage:
    python script/fetch_madlad_model.py            # default repo + dest
    python script/fetch_madlad_model.py --dest <dir>       # override the target dir
    python script/fetch_madlad_model.py --force    # re-download everything

Exit code: 0 = every file is present and verified, 1 = something failed.
"""
import argparse
import os
import pathlib
import sys
import time
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

from sidecar.ct2_sidecar import default_madlad_model_dir  # noqa: E402  (single source of truth)
import net_guard                                          # noqa: E402  (the fetch policy)
import model_origin                                       # noqa: E402  (what version landed)

REPO = "Heng666/madlad400-3b-mt-ct2-int8"
# model.bin is the 2.9 GB int8 checkpoint; spiece.model is the tokenizer the
# backend loads eagerly; the other two are carried for completeness (CT2 reads
# the vocabulary from model metadata, but keeping them makes the directory
# self-describing and re-usable by other tooling).
FILE_NAMES = ("model.bin", "spiece.model", "shared_vocabulary.json", "config.json")
# What this script can download, stated for script/check_name_guards.py: it refuses a pin
# that names nothing downloadable, and a downloadable file that is neither pinned nor
# listed as an exception. Kept as an explicit tuple rather than `= FILE_NAMES` so that the
# day the two drift apart, the gate reports it instead of inheriting the mistake.
DOWNLOADS = ("model.bin", "spiece.model", "shared_vocabulary.json", "config.json")
UNPINNED_EXCEPTIONS = ()
MIN_SIZES = {"model.bin": 1_000_000_000, "spiece.model": 1_000_000}
FALLBACK_ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")
# Redirects and $HF_ENDPOINT may only land on HuggingFace's own public hosts: the two
# site domains plus the CDN hosts (cdn-lfs.hf.co, us.aws.cdn.hf.co, ...) that /resolve/
# redirects large files to. The policy itself lives in script/net_guard.py so all three
# downloaders answer to one set of rules (defect D69: this script was hardened while the
# Argos and voice ones were bare urlopen calls).
ALLOWED_HOSTS = frozenset({"hf-mirror.com", "huggingface.co"})
ALLOWED_HOST_SUFFIXES = (".hf.co", ".huggingface.co", ".hf-mirror.com")
USER_AGENT = "wonslate-fetch-madlad/1.0"

# Pinned digests with their provenance stated, because a hash with no source is a claim:
#   model.bin / spiece.model  -- the HuggingFace LFS oid published by the repo's tree API,
#     read 2026-10-15, and **confirmed against the copy already on this machine**: the
#     computed digests match byte for byte, so these are upstream-authoritative values.
#   shared_vocabulary.json / config.json -- small files that are NOT LFS objects, so the
#     tree API only offers a git blob sha1, which is a different function and cannot be
#     compared to a sha256. These two are local baselines: they detect drift and
#     replacement from here on, and do not prove where the bytes originally came from.
EXPECTED_SHA256 = {
    "model.bin": (
        "77b9fd9ab97c1259d07089b5f854393dad81bc5fb5647d3f9a5d101c94f40daa",
        "HuggingFace LFS oid 2026-10-15, matched against the local copy"),
    "spiece.model": (
        "ef11ac9a22c7503492f56d48dce53be20e339b63605983e9f27d2cd0e0f3922c",
        "HuggingFace LFS oid 2026-10-15, matched against the local copy"),
    "shared_vocabulary.json": (
        "c327551ce3ca6efc7b437e11a267f79979893332dda8a1d146e2c950815193f8",
        "local baseline 2026-10-15 (non-LFS file: upstream publishes only a git sha1)"),
    "config.json": (
        "90fb54962455a4e0a0bc7235c0f063d7e46d9c1a1ae003af8059809abd6aeece",
        "local baseline 2026-10-15 (non-LFS file: upstream publishes only a git sha1)"),
}


def say(tag, msg=""):
    print("[{}] {}".format(tag, msg).rstrip(), flush=True)


def _record_origin(dest_dir, leaf, digest, revision):
    """Write what landed, beside it. `version` is the revision we were told to fetch, which for
    `main` is deliberately weak -- so the digest is stored next to it and `check_model_updates.py`
    compares bytes rather than trusting the label.
    """
    model_origin.record(dest_dir, asset=leaf, version=revision, sha256=digest,
                        url="{}/{}/resolve/{}".format(FALLBACK_ENDPOINTS[0], REPO, revision),
                        fetcher="fetch_madlad_model.py", kind=revision)


def endpoints():
    """The bases $HF_ENDPOINT / the fallbacks allow, after the policy has looked at them."""
    override = os.environ.get("HF_ENDPOINT", "").strip().rstrip("/")
    ordered = ((override,) if override else ()) + FALLBACK_ENDPOINTS
    usable = []
    for base in ordered:
        _host, reason = net_guard.check_url(base + "/", ALLOWED_HOSTS, ALLOWED_HOST_SUFFIXES)
        if not reason:
            usable.append(base)
        else:
            say("endpoint", "skipping {}: {}".format(base or "(empty)", reason))
    return usable


def final_path(dest_dir, leaf):
    """Normalize dest_dir/leaf and refuse anything that could escape dest_dir.

    The leaf must be a plain filename from the FILE_NAMES table (no separators,
    no dot segments); the joined, realpath-normalized result must still have
    the realpath-normalized destination as its prefix.
    """
    if not leaf or os.path.basename(leaf) != leaf or leaf in (".", "..") or ".." in leaf:
        raise ValueError("unsafe file name: {!r}".format(leaf))
    root = os.path.realpath(dest_dir)
    joined = os.path.realpath(os.path.join(root, leaf))
    if ".." in joined.split(os.sep) or not joined.startswith(root + os.sep):
        raise ValueError("path escapes the target directory: {}".format(joined))
    return joined


def download(leaf, dest_dir, force, attempts=4, allow_unpinned=False, revision="main"):
    """Fetch one repo file through the shared policy, verifying its pinned digest.

    `revision` is the version knob (GA-13): the default follows the moving `main` branch, while a
    commit sha or tag pins the directory to one upstream state. Whatever was chosen gets written
    into `wonslate-origin.json` beside the file, because "we fetch from main" is not a fact a
    later reader can reconstruct -- the bytes under it change and nothing else records which
    generation this checkout belongs to.

    Policy refusals (host, scheme, private address, checksum, missing pin) end the loop
    at once: re-downloading the same URL cannot fix any of them, and four silent retries
    of a checksum mismatch would read like a network problem instead of what it is.
    """
    try:
        target = final_path(dest_dir, leaf)
    except ValueError as exc:
        say("verify", str(exc))
        return False

    pin = EXPECTED_SHA256.get(leaf)
    if os.path.isfile(target) and not force and pin:
        # The cached branch is what a normal run takes, so it is checked too -- a size
        # test alone would accept a replaced file of the same length.
        got = net_guard.sha256_of(target)
        if got == pin[0]:
            say("fetch", "{} cached and digest-verified ({:.1f} MB)".format(
                leaf, os.path.getsize(target) / 1e6))
            # Recorded here too: an existing checkout is exactly the case where nobody knows
            # which revision it came from, and the origin file is what makes it knowable.
            _record_origin(dest_dir, leaf, pin[0], revision)
            return True
        say("fetch", "{} on disk is sha256 {}, pin says {} -- re-fetching".format(
            leaf, got[:16], pin[0][:16]))
    elif os.path.isfile(target) and not force:
        say("fetch", "{} cached ({:.1f} MB) but unpinned: nothing to check it against".format(
            leaf, os.path.getsize(target) / 1e6))
        return True

    for i in range(1, attempts + 1):
        for base in endpoints():
            url = "{}/{}/resolve/{}/{}".format(base, REPO, revision, leaf)
            try:
                digest, size = net_guard.fetch(
                    url, target, ALLOWED_HOSTS, ALLOWED_HOST_SUFFIXES,
                    expected_sha256=pin[0] if pin else None,
                    digest_source=pin[1] if pin else None,
                    allow_unpinned=allow_unpinned, user_agent=USER_AGENT,
                    timeout=60, chunk=1 << 20)
                say("fetch", "{} ({:.1f} MB, sha256 {}{})".format(
                    leaf, size / 1e6, digest[:16],
                    "" if pin else " UNPINNED (--allow-unpinned)"))
                _record_origin(dest_dir, leaf, digest, revision)
                return True
            except net_guard.UnpinnedAsset as exc:
                say("fetch", "REFUSED: {}".format(exc))
                return False
            except net_guard.GuardError as exc:
                text = str(exc)
                if ("checksum mismatch" in text or "not allowlisted" in text
                        or "disallowed" in text or "not https" in text
                        or "non-public" in text):
                    say("fetch", "REFUSED: {}".format(exc))
                    return False
                say("fetch", "attempt {}/{} failed ({}): {}".format(i, base, exc))
            except (urllib.error.URLError, OSError) as exc:
                say("fetch", "attempt {}/{} failed ({}): {}".format(i, base, exc))
            time.sleep(4)
    return False


def verify(dest_dir):
    """Return a problem description when the layout is incomplete, else None.

    The digest check runs here as well as in download(): main() always calls verify, so a
    file that arrived by some other route (hand-copied, restored from a backup) still has
    to match the pin before the script says the checkpoint is in place.
    """
    for leaf in FILE_NAMES:
        try:
            target = final_path(dest_dir, leaf)
        except ValueError as exc:
            return str(exc)
        if not os.path.isfile(target):
            return "{} missing".format(leaf)
    for leaf, minimum in MIN_SIZES.items():
        size = os.path.getsize(final_path(dest_dir, leaf))
        if size < minimum:
            return "{} truncated ({} bytes)".format(leaf, size)
    for leaf, (expected, _source) in EXPECTED_SHA256.items():
        target = final_path(dest_dir, leaf)
        if os.path.isfile(target):
            got = net_guard.sha256_of(target)
            if got != expected:
                return "{} is sha256 {}, pin says {}".format(leaf, got[:16], expected[:16])
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download the MADLAD-400 CT2 checkpoint")
    parser.add_argument("--dest", default=None,
                        help="target directory (default: <data_dir>/models/madlad)")
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    parser.add_argument("--rev", default="main",
                        help="which upstream revision to fetch: a branch (default, it moves), a "
                             "tag, or a commit sha for a reproducible install. Whatever is used "
                             "is recorded in wonslate-origin.json next to the checkpoint")
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="fetch a file even when no sha256 is pinned for it "
                             "(prints which file is unpinned; default refuses)")
    args = parser.parse_args(argv)

    dest_dir = os.path.realpath(args.dest) if args.dest \
        else str(default_madlad_model_dir())
    if not endpoints():
        say("*", "no usable allowlisted endpoint; check network or $HF_ENDPOINT")
        return 1
    os.makedirs(dest_dir, exist_ok=True)
    say("*", "repo: {}".format(REPO))
    say("*", "target: {}".format(dest_dir))

    failed = [leaf for leaf in FILE_NAMES
              if not download(leaf, dest_dir, args.force, allow_unpinned=args.allow_unpinned,
                              revision=args.rev)]

    print("", flush=True)
    problem = verify(dest_dir)
    if problem:
        say("verify", "FAIL: {}".format(problem))
        return 1
    say("verify", "OK: MADLAD-400 checkpoint in place; start the backend with"
                  " `python -m sidecar.ct2_sidecar --backend madlad`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
