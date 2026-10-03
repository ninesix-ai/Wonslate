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

Endpoint policy (SSRF-hardened for a tool that users may point at mirrors via
$HF_ENDPOINT): https only, hosts restricted to an allowlist of public
HuggingFace endpoints, redirects to other hosts rejected, and each host's
addresses are resolved and checked against private/loopback ranges before
connecting.

Path policy: every write goes through a tempfile.mkstemp handle (no dynamic
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
import ipaddress
import os
import pathlib
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidecar.ct2_sidecar import default_madlad_model_dir  # noqa: E402  (single source of truth)

REPO = "Heng666/madlad400-3b-mt-ct2-int8"
# model.bin is the 2.9 GB int8 checkpoint; spiece.model is the tokenizer the
# backend loads eagerly; the other two are carried for completeness (CT2 reads
# the vocabulary from model metadata, but keeping them makes the directory
# self-describing and re-usable by other tooling).
FILE_NAMES = ("model.bin", "spiece.model", "shared_vocabulary.json", "config.json")
MIN_SIZES = {"model.bin": 1_000_000_000, "spiece.model": 1_000_000}
FALLBACK_ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")
# Redirects and $HF_ENDPOINT may only land on HuggingFace's own public hosts:
# the two site domains plus the CDN hosts (cdn-lfs.hf.co, us.aws.cdn.hf.co,
# ...) that /resolve/ redirects large files to.
ALLOWED_HOST_SUFFIXES = (".hf.co", ".huggingface.co", ".hf-mirror.com")
ALLOWED_HOSTS = frozenset({"hf-mirror.com", "huggingface.co"})
USER_AGENT = "wonslate-fetch-madlad/1.0"


def host_allowed(hostname):
    return hostname in ALLOWED_HOSTS or hostname.endswith(ALLOWED_HOST_SUFFIXES)


def say(tag, msg=""):
    print("[{}] {}".format(tag, msg).rstrip(), flush=True)


def endpoint_allowed(url):
    """https + allowlisted host + no private/loopback resolved address."""
    try:
        parts = urllib.request.urlparse(url)
    except ValueError:
        return False, "unparseable URL"
    if parts.scheme != "https" or parts.hostname is None or not host_allowed(parts.hostname):
        return False, "host {} is not allowlisted".format(parts.hostname)
    try:
        infos = socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, "cannot resolve {}: {}".format(parts.hostname, exc)
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return False, "{} resolves to a non-public address ({})".format(parts.hostname, address)
    return True, ""


def endpoints():
    override = os.environ.get("HF_ENDPOINT", "").strip().rstrip("/")
    ordered = ((override,) if override else ()) + FALLBACK_ENDPOINTS
    usable = []
    for base in ordered:
        ok, reason = endpoint_allowed(base)
        if ok:
            usable.append(base)
        else:
            say("endpoint", "skipping {}: {}".format(base or "(empty)", reason))
    return usable


class _AllowlistedRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that leave the allowlist instead of silently following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, _ = endpoint_allowed(newurl)
        if not ok:
            raise urllib.error.URLError("redirect to a non-allowlisted host: {}".format(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    # Python 3.10's HTTPRedirectHandler predates 308 support and raises plain
    # HTTPError; hf-mirror answers with 308, so teach it the 307 behaviour.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_307


_OPENER = urllib.request.build_opener(_AllowlistedRedirect)


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


def download(leaf, dest_dir, force, attempts=4):
    """Download one repo file into dest_dir (atomic mkstemp -> os.replace)."""
    try:
        target = final_path(dest_dir, leaf)
    except ValueError as exc:
        say("verify", str(exc))
        return False
    if os.path.isfile(target) and not force and os.path.getsize(target) >= MIN_SIZES.get(leaf, 1):
        say("fetch", "{} cached ({:.1f} MB)".format(leaf, os.path.getsize(target) / 1e6))
        return True

    for i in range(1, attempts + 1):
        for base in endpoints():
            url = "{}/{}/resolve/main/{}".format(base, REPO, leaf)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".part")
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with _OPENER.open(request, timeout=60) as response, \
                        os.fdopen(fd, "wb") as out:
                    total = 0
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        total += len(chunk)
                os.replace(tmp_path, target)
                say("fetch", "{} ({:.1f} MB)".format(leaf, total / 1e6))
                return True
            except Exception as exc:              # noqa: BLE001 - report and try the next endpoint
                say("fetch", "attempt {}/{} failed ({}): {}: {}".format(
                    i, attempts, base, type(exc).__name__, exc))
                try:
                    os.close(fd)                  # fdopen already closed it on success paths only
                except OSError:
                    pass
                finally:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
            time.sleep(4)
    return False


def verify(dest_dir):
    """Return a problem description when the layout is incomplete, else None."""
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
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download the MADLAD-400 CT2 checkpoint")
    parser.add_argument("--dest", default=None,
                        help="target directory (default: <data_dir>/models/madlad)")
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    args = parser.parse_args(argv)

    dest_dir = os.path.realpath(args.dest) if args.dest \
        else str(default_madlad_model_dir())
    if not endpoints():
        say("*", "no usable allowlisted endpoint; check network or $HF_ENDPOINT")
        return 1
    os.makedirs(dest_dir, exist_ok=True)
    say("*", "repo: {}".format(REPO))
    say("*", "target: {}".format(dest_dir))

    failed = [leaf for leaf in FILE_NAMES if not download(leaf, dest_dir, args.force)]

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
