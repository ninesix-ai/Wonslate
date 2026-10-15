#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""fetch_argos_models.py -- download and unpack Argos CTranslate2 packages.

Why: the real sidecar backend (sidecar/ct2_sidecar.py, --backend ct2) needs one
unpacked Argos package per language pair. This script resolves the official
archive for each pair, downloads it, unpacks it into the directory the backend
expects and verifies the layout -- so a fresh machine goes from "pip install"
to a working offline pair in one command.

Models land OUTSIDE the repository, under <data_dir>/models/argos, using the
same rule as the backend and translator-engine/src/config.rs (LT_DATA_DIR /
WONSLATE_DATA_DIR override, then the per-OS user data directory). Nothing here
writes into the git working tree.

Licenses: Argos model packages are MIT-licensed exports; archive links come from
the official argospm index. See translator-engine/src/license_gate.rs.

Fetch policy (defect D69): this script used to call urllib directly with no host or
integrity check, and the URL it fetches a package from is read out of the index it
fetched first -- so a replaced index silently changed which host the package arrived
from. Everything now goes through script/net_guard.py: https only, hosts restricted to
the tables below, redirects re-checked, and every package verified against a pinned
sha256 before it lands.

Why the index itself is fetched unpinned, and that is not a hole: the index is a living
list, so pinning its bytes would break every legitimate pair addition. The index only
chooses *where to ask*; the package still has to match its own digest, and every host it
names still has to be on PACKAGE_ALLOWED_HOSTS. A tampered index can therefore only
produce a refusal, never a substituted artifact -- unless it points at a host we already
trust for that exact digest.

Usage:
    python script/fetch_argos_models.py                  # en<->zh (default pair set)
    python script/fetch_argos_models.py --pairs en_zh,zh_en,en_ja
    python script/fetch_argos_models.py --list           # print the resolvable pairs
    python script/fetch_argos_models.py --dest <dir>   # override the target root
    python script/fetch_argos_models.py --force          # re-download existing archives

Exit code: 0 = every requested pair is present and verified, 1 = something failed.
"""
import argparse
import json
import pathlib
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

from sidecar.ct2_sidecar import default_model_dir  # noqa: E402  (single source of truth)
import net_guard                                    # noqa: E402  (the fetch policy)

# The index is small JSON; a CDN mirror is tried first because GitHub raw is
# unreliable on some networks. These two hosts serve *the index*, and are deliberately
# not the same list as the package hosts: jsDelivr can proxy any repository on GitHub,
# so trusting it for weights would be trusting the world.
INDEX_URLS = (
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
)
INDEX_ALLOWED_HOSTS = ("cdn.jsdelivr.net", "raw.githubusercontent.com")
PACKAGE_ALLOWED_HOSTS = ("argos-net.com",)

DEFAULT_PAIRS = ("en_zh", "zh_en")
USER_AGENT = "wonslate-fetch-argos/1.0"

# Declared for script/check_name_guards.py: the packages this script downloads today.
# Only the default pair set is listed because that is what a run fetches without --pairs;
# asking for another pair is not a silent gap -- it fails closed in net_guard.fetch as an
# UnpinnedAsset, and the fix is to verify that package once and add its digest here.
# Stating it this way means "unpinned" is always a decision someone wrote down, never an
# absence, and UNPINNED_EXCEPTIONS being empty is therefore a claim the gate can check.
DOWNLOADS = ("translate-en_zh-1_9.argosmodel", "translate-zh_en-1_9.argosmodel")
UNPINNED_EXCEPTIONS = ()

# Pinned digests, each with the provenance of the value stated next to it -- a hash
# without a source is a claim, not evidence. The live argospm index was read on
# 2026-10-15 and carries **no checksum field at all** (its entry keys are argos_version,
# code, from_code, from_name, links, package_version, to_code, to_name), so these two are
# local baselines computed from the copy in use on that machine. What they buy is drift
# and replacement detection from here on; they are NOT a proof of where the bytes
# originally came from, and a new pair cannot be fetched until its digest is added here
# (that is the point: `--allow-unpinned` exists but says out loud what it is skipping).
EXPECTED_SHA256 = {
    "translate-en_zh-1_9.argosmodel": (
        "433e7c4f034d87fbe2353161e05f18646d7999452f801a4e1f0378522b9850ab",
        "local baseline 2026-10-15 (upstream index publishes no hashes)"),
    "translate-zh_en-1_9.argosmodel": (
        "62e7af5a3a48b530e47b7b3e5c78c2de79073ecd815750d2bf3ab35b4a67da2d",
        "local baseline 2026-10-15 (upstream index publishes no hashes)"),
}


def say(tag, msg=""):
    print("[{}] {}".format(tag, msg).rstrip(), flush=True)


def http_get(url, timeout=60):
    """Read a small document through the policy. Kept for callers that only need bytes.

    It goes to a temporary file and back, rather than bypassing net_guard, so there is
    exactly one path to the network in this script.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="wonslate-argos-get-"))
    try:
        net_guard.fetch(url, tmp / "doc", INDEX_ALLOWED_HOSTS + PACKAGE_ALLOWED_HOSTS,
                        expected_sha256=None, allow_unpinned=True,
                        user_agent=USER_AGENT, timeout=timeout)
        return (tmp / "doc").read_bytes()
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def load_index():
    """Return {pair: url} from the argospm index, trying each mirror in turn.

    The index chooses *where to ask*, so the host it names is checked before anything is
    dialed. `links[0]` used to be taken blind. Measured on the live index (2026-10-14):
    100 entries carrying 125 links -- 100 https and 25 ``ipfs://`` -- and today every
    entry's first link happens to be the https one. So the old code was not downloading
    IPFS URIs; it was betting that the upstream author would keep ordering the list that
    way. That bet is exactly what "the guard does not look at the value" means, so the
    first allowlisted https link is chosen now and anything skipped is reported, never
    dropped in silence.
    """
    last = None
    for url in INDEX_URLS:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="wonslate-argos-index-"))
        try:
            net_guard.fetch(url, tmp / "index.json", INDEX_ALLOWED_HOSTS,
                            expected_sha256=None, allow_unpinned=True,
                            user_agent=USER_AGENT, timeout=60)
            entries = json.loads((tmp / "index.json").read_text(encoding="utf-8"))
        except (net_guard.GuardError, ValueError, OSError) as exc:
            last = "{}: {}".format(type(exc).__name__, exc)
            say("index", "mirror failed ({}): {}".format(url, last))
            continue
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)
        links = {}
        for entry in entries:
            source, target = entry.get("from_code"), entry.get("to_code")
            if not (source and target):
                continue
            pair = "{}_{}".format(source, target)
            chosen = pick_link(entry.get("links") or [], pair)
            if chosen:
                links[pair] = chosen
        say("index", "{} pair(s) resolvable via {}".format(len(links), url))
        return links
    raise SystemExit("could not read the Argos index; last error: {}".format(last))


def pick_link(urls, pair):
    """First https link on an allowlisted package host, or None. Says what it skipped."""
    skipped = []
    for candidate in urls:
        _host, reason = net_guard.check_url(candidate, PACKAGE_ALLOWED_HOSTS)
        if not reason:
            if skipped:
                say("index", "{}: ignored {} non-allowlisted link(s): {}".format(
                    pair, len(skipped), ", ".join(sorted({c.split(":", 1)[0] for c in skipped}))))
            return candidate
        skipped.append(candidate)
    if skipped:
        say("index", "{}: no usable link ({} candidate(s), all refused: {})".format(
            pair, len(skipped), net_guard.check_url(skipped[0], PACKAGE_ALLOWED_HOSTS)[1]))
    return None


def download(url, dest, attempts=4, allow_unpinned=False):
    """Fetch one package through the policy and verify it against its pinned digest.

    Which failures are retried matters: a missing pin, a refused host or a checksum
    mismatch will not fix themselves by downloading the same URL four more times, so those
    end the attempt loop immediately. Only transport errors retry.
    """
    name = pathlib.Path(dest).name
    pin = EXPECTED_SHA256.get(name)
    for i in range(1, attempts + 1):
        try:
            digest, size = net_guard.fetch(
                url, dest, PACKAGE_ALLOWED_HOSTS,
                expected_sha256=pin[0] if pin else None,
                digest_source=pin[1] if pin else None,
                allow_unpinned=allow_unpinned, user_agent=USER_AGENT)
            say("fetch", "{} ({:.1f} MB, sha256 {}{})".format(
                name, size / 1e6, digest[:16],
                "" if pin else " UNPINNED (--allow-unpinned)"))
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
            say("fetch", "attempt {}/{} failed: {}: {}".format(i, attempts, type(exc).__name__, exc))
            time.sleep(4)
        except (urllib.error.URLError, OSError) as exc:
            say("fetch", "attempt {}/{} failed: {}: {}".format(i, attempts, type(exc).__name__, exc))
            time.sleep(4)
    return False


def unpack(archive, target):
    """Extract and collapse the single top-level directory the archives carry."""
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)

    entries = list(target.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in sorted(inner.iterdir()):
            shutil.move(str(item), str(target / item.name))
        inner.rmdir()
        say("unpack", "flattened the archive's top-level directory")


def verify(target):
    """Return a problem description when the Argos layout is incomplete, else None."""
    if not (target / "metadata.json").is_file():
        return "metadata.json missing"
    if not (target / "model").is_dir():
        return "model/ directory missing"
    if not (target / "sentencepiece.model").is_file():
        return "sentencepiece.model missing"
    return None


def fetch_pair(pair, link, dest_root, force, allow_unpinned=False):
    archive = dest_root / link.rsplit("/", 1)[-1]
    # Name the unpacked directory after the archive (translate-en_zh-1_9),
    # matching Argos' own convention for unpacked packages.
    target = dest_root / archive.stem

    pin = EXPECTED_SHA256.get(archive.name)
    cached_ok = False
    if archive.is_file() and pin:
        # The already-cached path is the one every normal run takes, so it has to be
        # checked too: a size test alone accepts a replaced file of equal length, and
        # skipping the check entirely would leave the policy guarding only fresh downloads.
        got = net_guard.sha256_of(archive)
        if got == pin[0]:
            cached_ok = True
        else:
            say("fetch", "{} on disk is sha256 {}, pin says {} -- re-fetching".format(
                archive.name, got[:16], pin[0][:16]))
    elif archive.is_file():
        say("fetch", "{} exists but carries no pin to check it against".format(archive.name))

    if force or not cached_ok:
        say("fetch", "{} <- {}".format(pair, link))
        if not download(link, archive, allow_unpinned=allow_unpinned):
            return False
    else:
        say("fetch", "{} cached and digest-verified ({:.1f} MB)".format(
            pair, archive.stat().st_size / 1e6))

    if force and target.is_dir():
        shutil.rmtree(target)
    if not target.is_dir():
        target.mkdir(parents=True)
        unpack(archive, target)

    problem = verify(target)
    if problem:
        say("verify", "FAIL {}: {}".format(pair, problem))
        return False
    say("verify", "OK   {} -> {}".format(pair, target))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download and unpack Argos packages")
    parser.add_argument("--pairs", default=",".join(DEFAULT_PAIRS),
                        help="comma-separated Argos pair codes (default: en_zh,zh_en)")
    parser.add_argument("--dest", default=None,
                        help="target root (default: <data_dir>/models/argos)")
    parser.add_argument("--list", action="store_true", help="list the pairs the index offers")
    parser.add_argument("--force", action="store_true", help="re-download and re-unpack")
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="download an asset even when no sha256 is pinned for it "
                             "(prints which asset is unpinned; default refuses)")
    args = parser.parse_args(argv)

    index = load_index()

    if args.list:
        for pair in sorted(index):
            say("list", "{}  {}".format(pair, index[pair]))
        if DEFAULT_PAIRS[0] not in index:
            say("warn", "default pair {} is not in the index".format(DEFAULT_PAIRS[0]))
        return 0

    dest_root = pathlib.Path(args.dest) if args.dest else default_model_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    say("*", "target root: {}".format(dest_root))

    failed = []
    for pair in [p.strip() for p in args.pairs.split(",") if p.strip()]:
        link = index.get(pair)
        if not link:
            say("fetch", "SKIP {}: not offered by the index (see --list)".format(pair))
            failed.append(pair)
            continue
        if not fetch_pair(pair, link, dest_root, args.force, args.allow_unpinned):
            failed.append(pair)

    print("", flush=True)
    if failed:
        say("*", "FAILED: {}".format(", ".join(failed)))
        return 1
    say("*", "all requested pairs are in place; start the backend with "
              "--backend ct2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
