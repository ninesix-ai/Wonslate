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

Usage:
    python script/fetch_argos_models.py                  # en<->zh (default pair set)
    python script/fetch_argos_models.py --pairs en_zh,zh_en,en_ja
    python script/fetch_argos_models.py --list           # print the resolvable pairs
    python script/fetch_argos_models.py --dest D:\\models # override the target root
    python script/fetch_argos_models.py --force          # re-download existing archives

Exit code: 0 = every requested pair is present and verified, 1 = something failed.
"""
import argparse
import json
import pathlib
import shutil
import sys
import time
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidecar.ct2_sidecar import default_model_dir  # noqa: E402  (single source of truth)

# The index is small JSON; a CDN mirror is tried first because GitHub raw is
# unreliable on some networks.
INDEX_URLS = (
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
)

DEFAULT_PAIRS = ("en_zh", "zh_en")
USER_AGENT = "wonslate-fetch-argos/1.0"


def say(tag, msg=""):
    print("[{}] {}".format(tag, msg).rstrip(), flush=True)


def http_get(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout).read()


def load_index():
    """Return {pair: url} from the argospm index, trying each mirror in turn."""
    last = None
    for url in INDEX_URLS:
        try:
            entries = json.loads(http_get(url).decode("utf-8"))
        except Exception as exc:                      # noqa: BLE001 - report and try the next mirror
            last = "{}: {}".format(type(exc).__name__, exc)
            say("index", "mirror failed ({}): {}".format(url, last))
            continue
        links = {}
        for entry in entries:
            source, target = entry.get("from_code"), entry.get("to_code")
            urls = entry.get("links") or []
            if source and target and urls:
                links["{}_{}".format(source, target)] = urls[0]
        say("index", "{} pair(s) available via {}".format(len(links), url))
        return links
    raise SystemExit("could not read the Argos index; last error: {}".format(last))


def download(url, dest, attempts=4):
    for i in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=180) as response, open(dest, "wb") as out:
                total = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
            say("fetch", "{} ({:.1f} MB)".format(dest.name, total / 1e6))
            return True
        except Exception as exc:                      # noqa: BLE001 - transient network errors
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


def fetch_pair(pair, link, dest_root, force):
    archive = dest_root / link.rsplit("/", 1)[-1]
    # Name the unpacked directory after the archive (translate-en_zh-1_9),
    # matching Argos' own convention for unpacked packages.
    target = dest_root / archive.stem

    if force or not (archive.is_file() and archive.stat().st_size > 1_000_000):
        say("fetch", "{} <- {}".format(pair, link))
        if not download(link, archive):
            return False
    else:
        say("fetch", "{} cached ({:.1f} MB)".format(pair, archive.stat().st_size / 1e6))

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
        if not fetch_pair(pair, link, dest_root, args.force):
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
