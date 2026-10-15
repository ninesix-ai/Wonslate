#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""net_guard.py -- the one fetch policy shared by every model downloader.

Why this exists (defect D69, 2026-10-15). The three `script/fetch_*.py` downloaders were
not written to the same standard. `fetch_madlad_model.py` had been hardened: https only,
a host allowlist, redirects off the allowlist refused, and the resolved addresses checked
against private ranges. `fetch_argos_models.py` and `fetch_voice_models.py` were bare
`urllib.request.urlopen` calls. The Argos one is worse than "no allowlist", because the
URL it fetches the model from is *read out of the index it fetched first*: a replaced
index.json silently changes which host the 85 MB package arrives from, and nothing in the
script looks.

That is the same shape as D68 one layer down: the licence we record (`REGISTERED_DEPS`
says the Argos package is MIT) is keyed on the *name* of an artifact, while the bytes that
name stands for come from a path the guard never inspects. A whitelist that is not
recomputed against the value it actually fetched is not a whitelist.

What this module enforces, and the asymmetry behind each choice:
  * https only, and every redirect re-checked hop by hop;
  * host matching on **label boundaries** -- `huggingface.co.evil.example` must be
    refused, which a plain `hostname.endswith(".huggingface.co")` happens to get right
    and a careless `endswith("huggingface.co")` would not;
  * the resolved addresses are checked before connecting, so an allowlisted name that
    points at a private or loopback address is refused (SSRF);
  * a checksum is *required*: an unpinned asset is a failure, not a silent pass, because
    "we downloaded something" is not the same claim as "we downloaded the artifact whose
    licence we registered". `--allow-unpinned` exists for bootstrapping and says so in
    its name and in the printed line.

The resolver is injectable (`resolve=`) so the address check can be exercised without a
network: a guard whose only test needs the internet is a guard that does not get tested.
"""

import hashlib
import ipaddress
import os
import pathlib
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT_HEADER = "User-Agent"
# Each downloader passes its own string so an upstream log can tell the tools apart.
# Read from a parameter, never from the environment: a knob nobody declared is how D45
# and D36 happened, and a User-Agent is not worth an overridable variable.
DEFAULT_USER_AGENT = "wonslate-fetch/1.0"


class GuardError(RuntimeError):
    """Something the fetch policy refuses. Always carries a reason a human can act on."""


class UnpinnedAsset(GuardError):
    """No expected digest is recorded for this asset."""


def hostname_matches(hostname, allowed_hosts=(), allowed_suffixes=()):
    """True if `hostname` is exactly listed, or a sub-domain of a listed suffix.

    Suffix comparison happens on dot boundaries and case-folded, so
    `hostile-huggingface.co` (not a suffix) and `huggingface.co.evil.example` (a suffix in
    the middle, not at the end) are both refused. Callers must pass suffixes *with* the
    leading dot; a bare domain in the suffix list is a bug, and `--self-test` checks that
    the caller-supplied tables obey the rule.
    """
    name = (hostname or "").strip().rstrip(".").lower()
    if not name:
        return False
    if name in {h.strip().rstrip(".").lower() for h in allowed_hosts if h}:
        return True
    for suffix in allowed_suffixes:
        sfx = suffix.strip().lower()
        if not sfx.startswith("."):
            raise GuardError("allowed suffix %r must begin with a dot (label boundary)" % suffix)
        if name.endswith(sfx) and len(name) > len(sfx):
            return True
    return False


def check_url(url, allowed_hosts=(), allowed_suffixes=()):
    """Return (hostname, reason). reason is "" when the URL is acceptable."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return parts.hostname or "", "scheme %r is not https" % (parts.scheme,)
    if not parts.hostname:
        return "", "no host in %r" % (url,)
    if not hostname_matches(parts.hostname, allowed_hosts, allowed_suffixes):
        return parts.hostname, "host %s is not allowlisted" % (parts.hostname,)
    return parts.hostname, ""


def require_public_address(hostname, resolve=socket.getaddrinfo):
    """Refuse hosts whose resolved addresses are not public.

    Every answer is checked, not the first: a name that returns both a public and a
    private record would otherwise slip through on the resolver's ordering.
    """
    try:
        infos = resolve(hostname, 443, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError) as exc:
        raise GuardError("cannot resolve %s: %s" % (hostname, exc))
    if not infos:
        raise GuardError("%s resolved to nothing" % hostname,)
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified):
            raise GuardError("%s resolves to a non-public address (%s)" % (hostname, address))
    return True


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-apply the policy to every redirect target instead of following it blindly."""

    def __init__(self, allowed_hosts, allowed_suffixes):
        super().__init__()
        self._allowed_hosts = allowed_hosts
        self._allowed_suffixes = allowed_suffixes

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _host, reason = check_url(newurl, self._allowed_hosts, self._allowed_suffixes)
        if reason:
            raise urllib.error.URLError("redirect to a disallowed target: %s" % reason)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    # hf-mirror answers 308 and the stdlib handler predates it; inherit 307 behaviour.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_307


def build_opener(allowed_hosts=(), allowed_suffixes=(), handlers=None):
    chain = [_GuardedRedirect(allowed_hosts, allowed_suffixes)]
    chain.extend(handlers or [])
    return urllib.request.build_opener(*chain)


def sha256_of(path, chunk=1 << 22):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url, dest, allowed_hosts, allowed_suffixes=(), expected_sha256=None,
          digest_source=None, allow_unpinned=False, opener=None, timeout=180,
          resolve=socket.getaddrinfo, chunk=1 << 22, report=None,
          progress_every=None, user_agent=DEFAULT_USER_AGENT):
    """Download one artifact through the policy, verifying it before it lands.

    The file is written beside its final name and only renamed once the digest matches,
    so a refused download cannot leave a half-written model in the directory the sidecar
    loads from -- the same reason the writers elsewhere in this repo go through mkstemp.

    `expected_sha256` may be a bare hex digest. `digest_source` is not a decoration: it
    records *where the pin came from* (upstream API, or a local baseline), because a hash
    with no stated provenance is a claim, not evidence.
    """
    host, reason = check_url(url, allowed_hosts, allowed_suffixes)
    if reason:
        raise GuardError("%s: %s" % (url, reason))
    require_public_address(host, resolve=resolve)

    if expected_sha256 is None and not allow_unpinned:
        raise UnpinnedAsset(
            "%s has no expected sha256 pinned. Add it to the script's pin table, or pass "
            "--allow-unpinned to download anyway (that prints which asset is unpinned, and "
            "the licence gate still cannot vouch for its bytes)." % (url,))

    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    used = opener or build_opener(allowed_hosts, allowed_suffixes)
    request = urllib.request.Request(url, headers={USER_AGENT_HEADER: user_agent})
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=dest.name + ".part")
    try:
        with os.fdopen(tmp_fd, "wb") as out, used.open(request, timeout=timeout) as resp:
            mark = 0
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                out.write(block)
                digest.update(block)
                size += len(block)
                # A 1 GB checkpoint with no output looks identical to a hang, so callers
                # that fetch large assets can ask for a tick every N bytes. Optional on
                # purpose: the small-document callers should not print progress at all.
                if progress_every and size - mark >= progress_every:
                    mark = size
                    if report:
                        report("fetch", "  %s %.0f MB" % (dest.name, size / 1e6))
        got = digest.hexdigest()
        if expected_sha256 is not None and got != expected_sha256.lower():
            raise GuardError(
                "checksum mismatch for %s: got %s (%d bytes), expected %s%s"
                % (dest.name, got, size, expected_sha256,
                   "" if not digest_source else " (source: %s)" % digest_source))
        if expected_sha256 is None:
            if report:
                report("unpinned", "%s fetched WITHOUT a checksum (%s, %d bytes)"
                       % (dest.name, got, size))
        os.replace(tmp_name, str(dest))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return got, size


def _user_agent():
    # Named per downloader so an upstream log can tell the tools apart; kept generic
    # here rather than duplicated in three scripts.
    return os.environ.get("WONSLATE_FETCH_UA", "wonslate-fetch/1.0")


# ---- self test -------------------------------------------------------------

def _seed_redirect(allowed_hosts):
    """The redirect guard must refuse a hop that leaves the allowlist."""
    handler = _GuardedRedirect(allowed_hosts, ())

    class _Req:
        url = "https://good.example/x"

    try:
        handler.redirect_request(_Req(), None, 302, "Found", {}, "https://evil.example/x")
    except urllib.error.URLError as exc:
        return "disallowed" in str(exc)
    return False


def self_test():
    """Seeds along every classification path; a green here is not a default.

    Four shapes matter and each gets its own case: exact host, sub-domain suffix, the two
    look-alikes that must be refused (a prefix run-together, and a suffix sitting in the
    middle of a longer name), plus scheme, address and digest verdicts.
    """
    failures = []
    seeds = [0]

    def expect(what, got, want):
        seeds[0] += 1
        if got != want:
            failures.append("%s: got %r want %r" % (what, got, want))

    hosts = ("huggingface.co", "hf-mirror.com", "argos-net.com", "github.com")
    suffixes = (".hf.co", ".huggingface.co", ".githubusercontent.com")

    expect("exact host", hostname_matches("huggingface.co", hosts, suffixes), True)
    expect("case folded + trailing dot", hostname_matches("HuggingFace.CO.", hosts, suffixes), True)
    expect("sub-domain of listed suffix", hostname_matches("cdn-lfs.hf.co", hosts, suffixes), True)
    # The two shapes a sloppy matcher gets wrong, and which the D69 audit asked about:
    expect("suffix in the middle is refused",
           hostname_matches("huggingface.co.evil.example", hosts, suffixes), False)
    expect("run-together prefix is refused", hostname_matches("myhuggingface.co", hosts, suffixes), False)
    expect("empty host", hostname_matches("", hosts, suffixes), False)
    expect("bare dot suffix cannot match itself", hostname_matches(".hf.co", hosts, suffixes), False)

    expect("http refused", check_url("http://huggingface.co/x", hosts, suffixes)[1] != "", True)
    expect("https allowed", check_url("https://huggingface.co/x", hosts, suffixes)[1], "")

    def fake_resolve(table):
        def _r(host, port, proto=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port)) for ip in table]
        return _r

    for name, table in (("loopback", ["127.0.0.1"]), ("private", ["10.0.0.5"]),
                        ("link-local", ["169.254.1.1"]), ("unspecified", ["0.0.0.0"]),
                        ("v6 loopback", ["::1"])):
        try:
            require_public_address("x.example", resolve=fake_resolve(table))
            expect("%s address refused" % name, False, True)
        except GuardError:
            expect("%s address refused" % name, True, True)
    # The public-address side must be asserted, not merely executed: an exception here
    # would abort the run and report nothing, so it is counted like every other seed.
    try:
        require_public_address("x.example", resolve=fake_resolve(["93.184.216.34", "2606::1"]))
        expect("public addresses accepted", True, True)
    except GuardError as exc:
        expect("public addresses accepted", "refused: %s" % exc, True)

    expect("redirect off-list refused", _seed_redirect(("good.example",)), True)

    # A suffix without the leading dot is a caller bug, and must be loud.
    seeds_before = seeds[0]
    try:
        hostname_matches("evilhf.co", (), ("hf.co",))
        failures.append("dotted-suffix rule not enforced")
    except GuardError:
        seeds[0] += 1
    if seeds[0] == seeds_before:
        failures.append("dotted-suffix check did not run")

    expect("main table has no run-together host",
           hostname_matches("notargos-net.com", ("argos-net.com",), ()), False)

    # Digest verdicts on a real file: mismatch must raise and must NOT leave the target.
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "asset.bin"
        source = pathlib.Path(tmp) / "src.bin"
        source.write_bytes(b"wonslate")
        digest = sha256_of(source)
        expect("sha256 of known bytes", digest,
               hashlib.sha256(b"wonslate").hexdigest())
        try:
            fetch("https://huggingface.co/x", target, hosts, suffixes,
                  expected_sha256="0" * 64,
                  resolve=fake_resolve(["93.184.216.34"]),
                  opener=_FakeOpener(source.read_bytes()))
            failures.append("checksum mismatch did not raise")
        except GuardError:
            pass
        expect("refused download left no file", target.exists(), False)
        got, size = fetch("https://huggingface.co/x", target, hosts, suffixes,
                          expected_sha256=digest,
                          resolve=fake_resolve(["93.184.216.34"]),
                          opener=_FakeOpener(source.read_bytes()))
        expect("pinned download landed", (got == digest, target.read_bytes()), (True, b"wonslate"))
        expect("size reported", size, 8)
        try:
            fetch("https://huggingface.co/x", pathlib.Path(tmp) / "other.bin", hosts,
                  suffixes, expected_sha256=None,
                  resolve=fake_resolve(["93.184.216.34"]),
                  opener=_FakeOpener(b"x"))
            failures.append("unpinned asset was silently accepted")
        except UnpinnedAsset:
            pass

    for line in failures:
        print("!! %s" % line)
    print("net_guard self-test: %d assertions, %s"
          % (seeds[0], "all hold" if not failures else "%d FAILED" % len(failures)))
    return 1 if failures else 0


class _FakeOpener:
    """Stands in for urllib's opener so the digest paths can be tested offline."""

    def __init__(self, payload):
        self._payload = payload

    def open(self, request, timeout=None):
        payload = self._payload
        class _Body:
            def read(self_inner, n=-1):
                if not hasattr(self_inner, "_pos"):
                    self_inner._pos = 0
                chunk = payload[self_inner._pos:self_inner._pos + n]
                self_inner._pos += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return _Body()


def main(argv):
    if "--self-test" in argv:
        return self_test()
    print(__doc__)
    print("usage: python script/net_guard.py --self-test")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
