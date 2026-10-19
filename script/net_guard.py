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
  * an enterprise mirror (GA-13's private-mirror half) is a *name*, declared locally by the
    operator, never a host lifted out of downloaded content: it is allowed to resolve to a private
    address (that is its whole point) but only for the exact hosts named, it must still be https, and
    a host that appears in both the primary and the mirror table keeps the *primary* public-address
    rule (see `classify_host`). `apply_mirror` swaps only the base, so the path -- and therefore the
    pinned digest that decides "is this the artifact" -- is unchanged: a mirror that serves different
    bytes than the public upstream is caught exactly as a tampered download would be.

The resolver is injectable (`resolve=`) so the address check can be exercised without a
network: a guard whose only test needs the internet is a guard that does not get tested.

Integrity of *length* (defect D80, 2026-10-18). The digest answers "is this the artifact";
until D80 nothing answered "did all of it arrive". `resp.read()` returning b"" ended the
loop and the file was renamed into place, so a connection cut at 40 % of a 1 GB checkpoint
looked like a completed download. When a pin exists the checksum happens to catch it -- by
accident, and with a message about hashes rather than about bytes -- and under
`--allow-unpinned` (which `sense-voice` uses today) a short body landed as a success. The
length promised by `Content-Length` / `Content-Range` is now checked independently.

Resume. A refused download used to delete its partial unconditionally, so every retry of a
gigabyte-scale asset started from zero, and a killed process left an unclaimable random-named
`.part<random>` behind in the very directory the sidecar loads from. Bytes are now held under
one claimable name and handed to the next attempt, whichever process it belongs to, but only
when the metadata says those bytes are the same generation as the ones the server will send.
"""

import datetime
import hashlib
import ipaddress
import json
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
#: Suffix of the in-progress file. One deterministic name per destination, because that
#: name is what makes an interrupted download findable by the next attempt.
PART_SUFFIX = ".part"

#: What travels with the bytes in the sidecar. `size` is the byte count the file held when
#: the record was written, `pin` the digest we are trying to match, and the two validators are
#: the server's own statement about the generation -- a resumed run may only append to bytes
#: whose validator still agrees with the server's, or the file would splice two generations.
META_KEYS = ("url", "dest", "size", "etag", "last_modified", "accept_ranges", "pin")
# Each downloader passes its own string so an upstream log can tell the tools apart.
# Read from a parameter, never from the environment: a knob nobody declared is how D45
# and D36 happened, and a User-Agent is not worth an overridable variable.
DEFAULT_USER_AGENT = "wonslate-fetch/1.0"


class GuardError(RuntimeError):
    """Something the fetch policy refuses. Always carries a reason a human can act on."""


class UnpinnedAsset(GuardError):
    """No expected digest is recorded for this asset."""


class TruncatedError(GuardError):
    """The server promised more bytes than it sent.

    Kept distinct from a checksum mismatch because the two need opposite answers. A wrong
    digest says the artifact is not the one whose licence we registered -- retrying cannot
    help and the bytes are worthless. A short body says the connection ended early --
    retrying is exactly right and the bytes already on disk are worth keeping. Folding them
    together was defect D80: every retry of a gigabyte checkpoint started from zero, and a
    truncated download under `--allow-unpinned` landed as a success, because nothing ever
    compared `Content-Length` with what arrived.
    """


class NotModified(RuntimeError):
    """The server answered our conditional request with 304: the file we have is still current.

    Deliberately *not* a GuardError. Every downloader already catches GuardError to mean "this
    attempt failed"; a 304 is the opposite -- the bytes on disk were confirmed current without
    transferring them. Making it a distinct type means a caller that forgot to handle it crashes
    loudly instead of reporting a failure, which is the direction this repo keeps choosing.
    """

    def __init__(self, url, validators=None):
        RuntimeError.__init__(self, "%s is unchanged (the server returned 304)" % url)
        self.url = url
        #: A 304 may carry a fresh validator; keep it so the next question is asked correctly.
        self.validators = dict(validators or {})


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


def check_url(url, allowed_hosts=(), allowed_suffixes=(), mirror_hosts=(), mirror_suffixes=()):
    """Return (hostname, reason). reason is "" when the URL is acceptable.

    Two name axes, one scheme rule. A *primary* host is a public upstream on the fixed allowlist;
    a *mirror* host is an operator-declared enterprise mirror (GA-13's private-mirror half). Both
    must be https -- "internal" never buys a downgrade to cleartext -- and both are matched on
    label boundaries. What differs is the address check (see `fetch`): a primary host is still
    required to resolve public, a declared mirror is allowed to resolve private.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return parts.hostname or "", "scheme %r is not https" % (parts.scheme,)
    if not parts.hostname:
        return "", "no host in %r" % (url,)
    if hostname_matches(parts.hostname, allowed_hosts, allowed_suffixes):
        return parts.hostname, ""
    if hostname_matches(parts.hostname, mirror_hosts, mirror_suffixes):
        return parts.hostname, ""
    return parts.hostname, "host %s is not allowlisted" % (parts.hostname,)


def classify_host(hostname, allowed_hosts=(), allowed_suffixes=(), mirror_hosts=(),
                  mirror_suffixes=()):
    """'primary' | 'mirror' | None -- which axis accepted this host, and so what address rule applies.

    Primary is checked first on purpose: a host that appears in both tables keeps the *stricter*
    public-address rule (a mistyped or poisoned mirror entry can never quietly exempt a public
    host from SSRF screening). This is the asymmetry the whole mirror feature rests on.
    """
    if hostname_matches(hostname, allowed_hosts, allowed_suffixes):
        return "primary"
    if hostname_matches(hostname, mirror_hosts, mirror_suffixes):
        return "mirror"
    return None


def mirror_base_from(environ, names=("LT_MODEL_MIRROR", "WONSLATE_MODEL_MIRROR")):
    """The operator-declared mirror base, or "". Reads a passed-in mapping, never os.environ.

    The names live here so the three downloaders cannot drift to two spellings of the knob -- the
    D36/D45 lesson (a knob read under more than one name, resolved inconsistently per host). The
    prefix order is the repo's standing `LT_` > `WONSLATE_` chain.
    """
    for name in names:
        value = (environ.get(name) or "").strip().rstrip("/")
        if value:
            return value
    return ""


def mirror_hosts(mirror_base):
    """(host_set, suffixes) describing the mirror axis for one base URL.

    An exact host, no suffix: an enterprise mirror is a named server, not a domain you own every
    subdomain of, so the smallest authority that makes it work is the one granted.
    """
    host = urllib.parse.urlsplit(mirror_base).hostname
    return (frozenset({host.lower()}), ()) if host else (frozenset(), ())


def apply_mirror(url, mirror_base):
    """Rewrite `url` to fetch from `mirror_base`, keeping the original path.

    The mirror is a base-swap, not a host-relaxation: the operator decides *where*, the path -- and
    therefore the pinned digest -- still decides *what*. A path-preserving swap is the convention
    mirrors follow, so an artifact at `/<repo>/resolve/<rev>/<file>` upstream sits at the same path
    under the mirror. Raises when the base is not a usable https URL, rather than silently falling
    back to the public origin: a misconfigured mirror must be loud.
    """
    if not mirror_base:
        return url
    base = urllib.parse.urlsplit(mirror_base)
    if base.scheme != "https" or not base.hostname:
        raise GuardError("mirror base %r is not an https URL with a host" % mirror_base)
    original = urllib.parse.urlsplit(url)
    prefix = base.path.rstrip("/")
    tail = original.path.lstrip("/")
    path = "%s/%s" % (prefix, tail) if tail else prefix
    return urllib.parse.urlunsplit(("https", base.netloc, path, original.query, original.fragment))


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

    def __init__(self, allowed_hosts, allowed_suffixes, mirror_hosts=(), mirror_suffixes=()):
        super().__init__()
        self._allowed_hosts = allowed_hosts
        self._allowed_suffixes = allowed_suffixes
        self._mirror_hosts = mirror_hosts
        self._mirror_suffixes = mirror_suffixes

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _host, reason = check_url(newurl, self._allowed_hosts, self._allowed_suffixes,
                                  self._mirror_hosts, self._mirror_suffixes)
        if reason:
            raise urllib.error.URLError("redirect to a disallowed target: %s" % reason)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    # hf-mirror answers 308 and the stdlib handler predates it; inherit 307 behaviour.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_307


def build_opener(allowed_hosts=(), allowed_suffixes=(), mirror_hosts=(), mirror_suffixes=(),
                 handlers=None):
    chain = [_GuardedRedirect(allowed_hosts, allowed_suffixes, mirror_hosts, mirror_suffixes)]
    chain.extend(handlers or [])
    return urllib.request.build_opener(*chain)


def sha256_of(path, chunk=1 << 22):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _validators_of(source):
    """The two server statements that make a conditional request possible, as a dict.

    `source` is either a response or an `HTTPError`; both expose headers, and a 304 arrives as
    one or the other depending on the urllib version, so both are read the same way.
    """
    out = {}
    headers = getattr(source, "headers", None)
    if headers is None:
        headers = getattr(source, "msg", None)
    for key, name in (("etag", "ETag"), ("last_modified", "Last-Modified")):
        value = _header(source, name)
        if value is None and hasattr(headers, "get"):
            value = headers.get(name)
        if value:
            out[key] = value.strip()
    return out


def _header(resp, name):
    """One response header, or None. Stubs without a header object mean "not stated"."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except AttributeError:
        return None


def _expected_total(resp, status, already):
    """How many bytes the finished file must hold, or None when the server never said.

    A 206 answers for a *slice*, so its `Content-Range` suffix is the only thing that names
    the whole; a 200's `Content-Length` is the whole file. Returning None is not a pass: the
    caller prints that no length was promised, because "we cannot tell whether it was
    complete" and "it was complete" must not read alike (the D57/D58 family).

    Real upstreams were measured on 2026-10-18 and two of them matter here: huggingface.co
    sends neither Content-Length nor Content-Range even on a 206, and jsDelivr chunk-encodes
    the index. So None is the common answer for those hosts, and the digest is what protects
    them -- which is why an unpinned asset is refused above rather than assembled blind.
    """
    if status == 206:
        span = _header(resp, "Content-Range") or ""
        # `bytes 1000-1999/85320` -- the number after the slash is the total.
        _dash, _slash, total = span.rpartition("/")
        try:
            return int(total.strip())
        except ValueError:
            return None
    length = _header(resp, "Content-Length")
    try:
        return already + int(str(length).strip())
    except (TypeError, ValueError):
        return None


def _read_claim(meta_path):
    try:
        with open(meta_path, encoding="utf-8") as handle:
            claim = json.load(handle)
    except (OSError, ValueError):
        return None
    return claim if isinstance(claim, dict) else None


def _claim(partial, meta_path, private, url, expected_sha256, report):
    """Move interrupted bytes onto our private name; return how much of them is usable.

    The rename *is* the claim: exactly one of two racers can move the file, so the loser
    finds nothing to resume and downloads on its own instead of appending into the middle of
    someone else's transfer. That is the whole reason this does not need a pid table or an
    mtime heuristic -- and why a fixed temporary name shared by concurrent downloaders (the
    shape this repository has already been bitten by) cannot happen here.

    Resuming is refused, and the bytes dropped rather than trusted, unless the sidecar names
    the same URL and the same pin. Appending today's stream to yesterday's partial of a
    *different* artifact is how you end up with a file that hashes to neither of them.
    """
    if not os.path.exists(partial):
        # A sidecar with no bytes is not resumable, and leaving it would say "someone is
        # working on this file" to whoever meets it next.
        if os.path.exists(meta_path):
            os.unlink(str(meta_path))
        return 0
    claim = _read_claim(meta_path) if os.path.exists(meta_path) else None
    try:
        os.replace(partial, private)
    except OSError:
        return 0
    if os.path.exists(meta_path):
        os.unlink(str(meta_path))
    if claim is None:
        return 0
    if claim.get("url") != url or claim.get("pin") != (expected_sha256 or ""):
        if report:
            report("fetch", "%s: %.0f MB on disk belongs to another asset or pin, restarting"
                   % (os.path.basename(private), os.path.getsize(private) / 1e6))
        _discard(private)
        return 0
    have = os.path.getsize(private)
    if claim.get("size") != have:
        # The file changed between the hand-off and now, which is exactly the situation the
        # sidecar exists to rule out, so the honest answer is "we do not trust these bytes".
        if report:
            report("fetch", "%s: partial changed size (%s then, %d now), restarting"
                   % (os.path.basename(private), claim.get("size"), have))
        _discard(private)
        return 0
    return have


def _discard(path):
    """Remove a working file if it is still there. Safe to call twice, and after a rename."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _hand_off(private, partial, meta_path, url, expected_sha256, have, report):
    """Leave the bytes where the next attempt can claim them.

    Writing the sidecar *before* publishing the name matters: a reader that finds `.part`
    without a claim gets "untrustworthy", never "resumable with wrong metadata".
    """
    try:
        with open(meta_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"url": url, "pin": expected_sha256 or "", "size": have,
                       "written_at": datetime.datetime.now(
                           datetime.timezone.utc).isoformat()},
                      handle, sort_keys=True)
    except OSError as exc:
        if report:
            report("fetch", "%s: cannot record the partial (%s), dropping %d bytes"
                   % (os.path.basename(private), exc, have))
        _discard(private)
        _discard(str(meta_path))
        return
    try:
        os.replace(private, partial)
    except OSError as exc:
        if report:
            report("fetch", "%s: cannot park the partial (%s), dropping %d bytes"
                   % (os.path.basename(private), exc, have))
        _discard(private)
        _discard(str(meta_path))
        return
    if report:
        report("fetch", "  %s: kept %d bytes for the next attempt (%s)"
               % (os.path.basename(str(partial)), have, PART_SUFFIX))


def _hash_prefix(path, digest, chunk):
    """Fold the bytes already on disk into the digest, so the final hash covers the whole file."""
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)


def fetch(url, dest, allowed_hosts, allowed_suffixes=(), expected_sha256=None,
          digest_source=None, allow_unpinned=False, opener=None, timeout=180,
          resolve=socket.getaddrinfo, chunk=1 << 22, report=None,
          progress_every=None, user_agent=DEFAULT_USER_AGENT,
          if_none_match=None, if_modified_since=None, observed=None,
          mirror_hosts=(), mirror_suffixes=()):
    """Download one artifact through the policy, verifying it before it lands.

    The file is written beside its final name and only renamed once both the *length* and
    the digest check out, so a refused download cannot leave a half-written model in the
    directory the sidecar loads from.

    `expected_sha256` may be a bare hex digest. `digest_source` is not a decoration: it
    records *where the pin came from* (upstream API, or a local baseline), because a hash
    with no stated provenance is a claim, not evidence.

    An interrupted transfer parks its bytes at `<name>.part` next to a claim naming the URL
    and the pin, and the next attempt -- same process or not -- resumes from there with a
    `Range` request. `TruncatedError` and transport errors keep those bytes; a policy
    refusal or a checksum mismatch throws them away.

    `if_none_match` / `if_modified_since` ask the conditional question (GA-13's other half):
    pass what the last fetch recorded and a `NotModified` comes back instead of the bytes, so
    `--force` on an unchanged 2.9 GB checkpoint costs two headers. Two rules on it. It is sent
    only when nothing is being resumed -- mixing `Range` with `If-None-Match` makes "the slice is
    current" and "the whole file is current" indistinguishable -- and a server that ignores the
    condition simply answers 200 with the file, which is a normal download, not a failure.

    `observed`, if given, is filled with the validators this response returned so the caller can
    store them; without that the information arrives and is thrown away.
    """
    host, reason = check_url(url, allowed_hosts, allowed_suffixes, mirror_hosts, mirror_suffixes)
    if reason:
        raise GuardError("%s: %s" % (url, reason))
    # The SSRF screen belongs to the primary axis only. A mirror is exempt from "must resolve
    # public" because an enterprise mirror is *expected* to be internal -- but only for the exact
    # hosts the operator named, and a host that is both primary and mirror keeps the public rule
    # (classify_host checks primary first). This is why `mirror_hosts` cannot be "any host": the
    # exemption is per-name, so it never widens what an untrusted index/endpoint can point us at.
    if classify_host(host, allowed_hosts, allowed_suffixes, mirror_hosts, mirror_suffixes) == "primary":
        require_public_address(host, resolve=resolve)

    if expected_sha256 is None and not allow_unpinned:
        raise UnpinnedAsset(
            "%s has no expected sha256 pinned. Add it to the script's pin table, or pass "
            "--allow-unpinned to download anyway (that prints which asset is unpinned, and "
            "the licence gate still cannot vouch for its bytes)." % (url,))

    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.parent / (dest.name + PART_SUFFIX)
    meta_path = pathlib.Path(str(partial) + ".meta")
    used = opener or build_opener(allowed_hosts, allowed_suffixes, mirror_hosts, mirror_suffixes)
    # The unique name is ours before anything is written, and the claimed bytes are moved
    # onto it; `os.replace` may land on it because it exists and is empty.
    fd, private = tempfile.mkstemp(dir=str(dest.parent), prefix=dest.name + PART_SUFFIX + ".")
    os.close(fd)
    start = _claim(partial, meta_path, private, url, expected_sha256, report)

    digest = hashlib.sha256()
    written = start
    if start:
        _hash_prefix(private, digest, chunk)
        if report:
            report("fetch", "%s resuming at %d bytes" % (dest.name, start))
    headers = {USER_AGENT_HEADER: user_agent}
    if start:
        headers["Range"] = "bytes=%d-" % start
    conditional = bool(if_none_match or if_modified_since) and not start
    if conditional:
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since
    request = urllib.request.Request(url, headers=headers)

    landed = False
    resumable = False
    try:
        try:
            resp_ctx = used.open(request, timeout=timeout)
        except urllib.error.HTTPError as early:
            # urllib raises for any non-2xx, so a 304 usually arrives here. Only answer "nothing
            # changed" when we actually asked that question: a stray 304 from a server we did not
            # ask would otherwise be reported as a confirmed match (D74's shape, one layer down).
            if early.code == 304 and conditional:
                got = _validators_of(early)
                if observed is not None:
                    observed.update(got)
                raise NotModified(url, got)
            raise
        with resp_ctx as resp:
            status = getattr(resp, "status", None) or 200
            if observed is not None:
                observed.update(_validators_of(resp))
            if status == 304:
                # The other shape: some versions hand the 304 back as a response. Treated the same,
                # and it must not fall through -- the body is empty, which would otherwise read as
                # a zero-length success when no digest was pinned.
                if not conditional:
                    raise GuardError("%s answered 304 to a request that did not ask a conditional "
                                     "question; refusing to treat an empty body as the artifact"
                                     % dest.name)
                raise NotModified(url, _validators_of(resp))
            span = _header(resp, "Content-Range")
            total = _expected_total(resp, status, start)
            if start and status == 206 and not span and expected_sha256 is None:
                # Measured, not imagined: huggingface.co and jsDelivr both answer a ranged
                # request with a bare 206 and nothing naming the slice. With a pin the digest
                # can still prove the assembled file, so appending is safe there; without one
                # (the `--allow-unpinned` path) we would be trusting a server that just refused
                # to say what it sent. Drop the bytes and let the retry fetch the whole thing.
                raise GuardError(
                    "cannot resume %s: the server answered 206 without a Content-Range and no "
                    "digest is pinned to prove the assembled file; the partial was discarded, "
                    "the next attempt fetches it whole" % dest.name)
            mode = "ab" if (start and status == 206) else "wb"
            if start and mode == "wb":
                # The server ignored our Range and is sending the whole file. Appending
                # would duplicate the prefix we already have, so restart from zero -- and
                # say so, because the operator watching a 1 GB download restart deserves to
                # know the upstream does not support resuming.
                if report:
                    report("fetch", "%s: server answered %d to a Range request, restarting "
                                    "from zero (no resume support here)" % (dest.name, status))
                digest = hashlib.sha256()
                written = 0
                total = _expected_total(resp, status, 0)
            with open(private, mode) as out:
                mark = 0
                while True:
                    block = resp.read(chunk)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    written += len(block)
                    # A 1 GB checkpoint with no output looks identical to a hang, so callers
                    # that fetch large assets can ask for a tick every N bytes. Optional on
                    # purpose: the small-document callers should not print progress at all.
                    if progress_every and written - mark >= progress_every:
                        mark = written
                        if report:
                            report("fetch", "  %s %.0f MB" % (dest.name, written / 1e6))
        # Length before digest: a short body is the more actionable complaint, and it names
        # the byte counts rather than two hashes nobody can read.
        if total is not None and written != total:
            resumable = True
            raise TruncatedError(
                "truncated download of %s: %d of %d bytes arrived (the server promised %s); "
                "the partial is kept for the next attempt" % (dest.name, written, total,
                                                               "Content-Length" if status == 200
                                                               else "Content-Range"))
        if total is None and report:
            report("fetch", "%s: no length promised, completeness rests on the digest alone"
                   % dest.name)
        got = digest.hexdigest()
        if expected_sha256 is not None and got != expected_sha256.lower():
            raise GuardError(
                "checksum mismatch for %s: got %s (%d bytes), expected %s%s"
                % (dest.name, got, written, expected_sha256,
                   "" if not digest_source else " (source: %s)" % digest_source))
        if expected_sha256 is None:
            if report:
                report("unpinned", "%s fetched WITHOUT a checksum (%s, %d bytes)"
                       % (dest.name, got, written))
        os.replace(private, str(dest))
        landed = True
    except (urllib.error.URLError, OSError):
        # A connection that died mid-stream leaves good bytes behind: keep them. This is the
        # half of D80 that made a 4 GB checkpoint cost four full attempts instead of one.
        resumable = True
        raise
    finally:
        # if/elif, not two independent ifs. The sidecar `_hand_off` writes *is* the claim the
        # next attempt reads, so the cleanup that follows a refused download must not run on
        # the hand-off path -- it used to delete the claim it had just created, which made
        # resume dead on arrival while every single-attempt test still passed.
        if resumable and written:
            _hand_off(private, partial, meta_path, url, expected_sha256, written, report)
        elif not landed:
            _discard(private)
            _discard(str(meta_path))
    return got, written


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

    Host shapes (exact, sub-domain, the two look-alikes that must be refused), scheme,
    resolved address, digest verdicts, and since D80 the *length* verdicts: a body shorter
    than promised must fail as a transport problem, its bytes must survive for the next
    attempt, and a partial belonging to another asset must never be appended to.
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

    # ---- length integrity and resume (defect D80) ------------------------------------
    # The digests above cannot catch this class of failure at all: a short body whose hash
    # is never checked lands as a success. Each seed needs its own directory, because the
    # partial is deliberately left behind and would leak into the next case.
    body = b"wonslate-checkpoint-bytes"
    whole = hashlib.sha256(body).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "cut.bin"
        try:
            fetch("https://huggingface.co/x", target, hosts, suffixes, expected_sha256=whole,
                  resolve=fake_resolve(["93.184.216.34"]),
                  opener=_FakeOpener(body, cut_at=5))
            failures.append("a 5-of-23-byte body was accepted")
        except TruncatedError:
            pass
        except GuardError as exc:
            failures.append("truncation raised %s, not TruncatedError: %s"
                            % (type(exc).__name__, exc))
        expect("truncation left no published file", target.exists(), False)
        expect("truncation kept the bytes for a retry",
               (pathlib.Path(tmp) / "cut.bin.part").read_bytes(), body[:5])

    with tempfile.TemporaryDirectory() as tmp:
        # Refusing to splice is the safety half of resume: same name, different asset or a
        # re-pinned digest means those bytes belong to another artifact.
        (pathlib.Path(tmp) / "splice.bin.part").write_bytes(b"stale")
        (pathlib.Path(tmp) / "splice.bin.part.meta").write_text(
            json.dumps({"url": "https://huggingface.co/x", "pin": "f" * 64, "size": 5}),
            encoding="utf-8")
        got, size = fetch("https://huggingface.co/x", pathlib.Path(tmp) / "splice.bin", hosts,
                          suffixes, expected_sha256=whole,
                          resolve=fake_resolve(["93.184.216.34"]),
                          opener=_FakeOpener(body))
        expect("a foreign partial was not appended to", (got, size), (whole, len(body)))
        expect("and the landed file is the whole body",
               (pathlib.Path(tmp) / "splice.bin").read_bytes(), body)

    with tempfile.TemporaryDirectory() as tmp:
        # The resume path itself: attempt one drops, attempt two must send a Range, take the
        # 206 slice, and still hash the whole file.
        target = pathlib.Path(tmp) / "resume.bin"
        first = _FakeOpener(body, cut_at=9)
        try:
            fetch("https://huggingface.co/x", target, hosts, suffixes, expected_sha256=whole,
                  resolve=fake_resolve(["93.184.216.34"]), opener=first)
        except TruncatedError:
            pass
        else:
            failures.append("the resume seed's first attempt did not truncate")
        second = _FakeOpener(body)
        got, size = fetch("https://huggingface.co/x", target, hosts, suffixes,
                          expected_sha256=whole,
                          resolve=fake_resolve(["93.184.216.34"]), opener=second)
        expect("resumed download asked for the tail", [9], second.ranges_seen)
        expect("resumed download completed", target.read_bytes(), body)
        expect("and reported the whole length", (got, size), (whole, len(body)))

    with tempfile.TemporaryDirectory() as tmp:
        # A server that ignores Range sends the whole file: appending it to the bytes we hold
        # would duplicate the prefix, so the fetch has to restart instead.
        target = pathlib.Path(tmp) / "doubled.bin"
        try:
            fetch("https://huggingface.co/x", target, hosts, suffixes, expected_sha256=whole,
                  resolve=fake_resolve(["93.184.216.34"]),
                  opener=_FakeOpener(body, cut_at=4))
        except TruncatedError:
            pass
        fetch("https://huggingface.co/x", target, hosts, suffixes, expected_sha256=whole,
              resolve=fake_resolve(["93.184.216.34"]),
              opener=_FakeOpener(body, ignore_range=True))
        expect("a Range-ignoring server did not double the prefix",
               target.read_bytes(), body)

    with tempfile.TemporaryDirectory() as tmp:
        # The whole-file size on a resumed transfer comes from `Content-Range`, not from the
        # slice length: 9 kept + 14 sent is a *short* file if the asset is really 30 bytes.
        target = pathlib.Path(tmp) / "lying.bin"
        try:
            fetch("https://huggingface.co/x", target, hosts, suffixes, expected_sha256=whole,
                  resolve=fake_resolve(["93.184.216.34"]), opener=_FakeOpener(body, cut_at=9))
        except TruncatedError:
            pass
        try:
            fetch("https://huggingface.co/x", target, hosts, suffixes, expected_sha256=whole,
                  resolve=fake_resolve(["93.184.216.34"]),
                  opener=_FakeOpener(body, total_delta=7))
            failures.append("a 206 whose Content-Range total does not add up was accepted")
        except TruncatedError:
            pass
        expect("a slice that does not add up left nothing published", target.exists(), False)

    with tempfile.TemporaryDirectory() as tmp:
        # A claim with no bytes behind it is not a state anyone can resume from, and letting
        # it linger would tell the next reader that a download is in progress.
        stale = pathlib.Path(tmp) / "sweep.bin.part.meta"
        stale.write_text(json.dumps({"url": "https://huggingface.co/x", "pin": "", "size": 4}),
                         encoding="utf-8")
        fetch("https://huggingface.co/x", pathlib.Path(tmp) / "sweep.bin", hosts, suffixes,
              expected_sha256=whole, resolve=fake_resolve(["93.184.216.34"]),
              opener=_FakeOpener(body))
        expect("claim without bytes was swept", stale.exists(), False)

    with tempfile.TemporaryDirectory() as tmp:
        # The shape two of the three real upstreams answer with (measured 2026-10-18): a 206
        # that never names its slice. With a pin the digest can still prove the result, so the
        # resume must go through; without one it must refuse *and* discard, so that the retry
        # after it is not poisoned by the same bytes.
        #
        # The partial is planted rather than produced by a first attempt: a server that gives
        # no Content-Length cannot report a truncation, so there is no way to reach this state
        # through that same server. The claim file is what the next attempt reads, and writing
        # it by hand is the honest way to get there.
        def park(name, pin):
            here = pathlib.Path(tmp) / name
            here.mkdir()
            (here / "asset.bin.part").write_bytes(body[:6])
            (here / "asset.bin.part.meta").write_text(
                json.dumps({"url": "https://huggingface.co/x", "pin": pin, "size": 6}),
                encoding="utf-8")
            return here / "asset.bin"

        pinned = park("pinned", whole)
        got, size = fetch("https://huggingface.co/x", pinned, hosts, suffixes,
                          expected_sha256=whole, resolve=fake_resolve(["93.184.216.34"]),
                          opener=_FakeOpener(body, omit_slice=True))
        expect("a bare 206 still resumes when a pin can prove it", (got, size),
               (whole, len(body)))
        expect("and the assembled file is the whole asset", pinned.read_bytes(), body)

        loose = park("unpinned", "")
        try:
            fetch("https://huggingface.co/x", loose, hosts, suffixes, expected_sha256=None,
                  allow_unpinned=True, resolve=fake_resolve(["93.184.216.34"]),
                  opener=_FakeOpener(body, omit_slice=True))
            failures.append("an unpinned asset was spliced onto a slice nobody described")
        except GuardError as exc:
            expect("the refusal says it discarded the bytes", "discarded" in str(exc), True)
        expect("and nothing was left behind to poison the next attempt",
               [p.name for p in loose.parent.iterdir()], [])

    with tempfile.TemporaryDirectory() as tmp:
        # The conditional question (GA-13's other half). Both shapes a 304 arrives in are seeded,
        # because only one of them is what this interpreter does -- the other is what a different
        # urllib version does, and covering only the local one would let the fall-through read as a
        # zero-length download on another machine.
        for shape in ("raise", "return"):
            here = pathlib.Path(tmp) / ("cond-%s.bin" % shape)
            server = _FakeOpener(body, respond_304=shape, headers={"ETag": '"v2"'})
            seen = {}
            try:
                fetch("https://huggingface.co/x", here, hosts, suffixes,
                      expected_sha256=whole, resolve=fake_resolve(["93.184.216.34"]),
                      opener=server, if_none_match='"v1"', observed=seen)
                failures.append("a 304 delivered as %s was not reported as NotModified" % shape)
            except NotModified:
                pass
            expect("the 304's own validator reaches the caller (%s)" % shape,
                   seen.get("etag"), '"v2"')
            expect("a confirmed 304 publishes no file (%s)" % shape, here.exists(), False)
            expect("the conditional header was actually sent (%s)" % shape,
                   any("if-none-match" in {k.lower() for k in asked} for asked in server.asked),
                   True)

        # The opposite case, which must not be believed: a 304 from a server nobody asked.
        unasked = pathlib.Path(tmp) / "unasked.bin"
        try:
            fetch("https://huggingface.co/x", unasked, hosts, suffixes, expected_sha256=whole,
                  resolve=fake_resolve(["93.184.216.34"]),
                  opener=_FakeOpener(body, respond_304="return"))
            failures.append("an unsolicited 304 was accepted as a match")
        except GuardError as exc:
            expect("the unsolicited 304 says why", "did not ask" in str(exc), True)

        # And the two questions stay separate: while a partial is being resumed the conditional
        # header must not go out, because "this slice is current" and "the whole file is current"
        # would arrive indistinguishable and the answer would be acted on as the wrong one.
        mixed = pathlib.Path(tmp) / "mixed.bin"
        (mixed.parent / "mixed.bin.part").write_bytes(body[:5])
        (mixed.parent / "mixed.bin.part.meta").write_text(
            json.dumps({"url": "https://huggingface.co/x", "pin": whole, "size": 5}),
            encoding="utf-8")
        resume_server = _FakeOpener(body)
        fetch("https://huggingface.co/x", mixed, hosts, suffixes, expected_sha256=whole,
              resolve=fake_resolve(["93.184.216.34"]), opener=resume_server,
              if_none_match='"v1"')
        expect("a resume asked for the tail", resume_server.ranges_seen, [5])
        expect("a resume sent no conditional header",
               any("if-none-match" in {k.lower() for k in asked} for asked in resume_server.asked),
               False)
        expect("and the resumed file is complete", mixed.read_bytes(), body)

    # ---- mirror axis (GA-13's private-mirror half) ---------------------------------------
    # Seeded here too, not only in the unittest suite: CI and the pre-commit hook run --self-test,
    # and D81's lesson is that a new judgement with no seed here reads as "covered" while the exact
    # consumer that gates it never saw it.
    with tempfile.TemporaryDirectory() as tmp:
        body = b"mirrored"
        digest = hashlib.sha256(body).hexdigest()
        mgot, _ = fetch("https://mirror.corp/x.bin", pathlib.Path(tmp) / "m.bin", hosts, suffixes,
                        expected_sha256=digest, resolve=fake_resolve(["10.0.0.7"]),
                        opener=_FakeOpener(body), mirror_hosts=frozenset({"mirror.corp"}))
        expect("a declared private mirror is accepted", mgot, digest)
        try:
            fetch("https://huggingface.co/x.bin", pathlib.Path(tmp) / "p.bin", hosts, suffixes,
                  expected_sha256=digest, resolve=fake_resolve(["10.0.0.7"]),
                  opener=_FakeOpener(body), mirror_hosts=frozenset({"mirror.corp"}))
            failures.append("a primary host resolving private was accepted with mirror tables present")
        except GuardError:
            expect("primary keeps the public screen beside a mirror", True, True)
        # A host that lands in BOTH tables must keep the stricter (public) rule -- this is the seed
        # that catches `classify_host` reordering to check mirror first, which the disjoint case
        # above cannot see.
        try:
            fetch("https://huggingface.co/x.bin", pathlib.Path(tmp) / "pm.bin", hosts, suffixes,
                  expected_sha256=digest, resolve=fake_resolve(["10.0.0.7"]),
                  opener=_FakeOpener(body), mirror_hosts=frozenset({"huggingface.co"}))
            failures.append("a both-listed host escaped the public screen via the mirror table")
        except GuardError:
            expect("a host in both tables keeps the public screen", True, True)
        expect("apply_mirror swaps the base and keeps the path",
               apply_mirror("https://huggingface.co/a/m.bin", "https://mirror.corp/c"),
               "https://mirror.corp/c/a/m.bin")
        try:
            apply_mirror("https://huggingface.co/a.bin", "http://mirror.corp")
            failures.append("apply_mirror accepted a cleartext mirror base")
        except GuardError:
            expect("apply_mirror refuses a non-https base", True, True)

    for line in failures:
        print("!! %s" % line)
    print("net_guard self-test: %d assertions, %s"
          % (seeds[0], "all hold" if not failures else "%d FAILED" % len(failures)))
    return 1 if failures else 0


class _FakeOpener:
    """Stands in for urllib's opener so the digest and length paths can be tested offline.

    It answers a `Range` request with a 206 and a `Content-Range`, because a resume seed that
    got a whole-file answer would only ever exercise the restart path. `cut_at` then sends
    fewer bytes than the length it promised, which is what a connection drop looks like to a
    reader, `ignore_range` reproduces a server with no resume support, and `total_delta` lets
    the 206 state a whole-file size that does not add up -- the only way to prove the total is
    actually read from `Content-Range` rather than inferred from the slice length.
    """

    def __init__(self, payload, headers=None, cut_at=None, ignore_range=False,
                 total_delta=0, omit_slice=False, respond_304=None):
        self._payload = payload
        self._headers = headers or {}
        self._cut_at = cut_at
        self._ignore_range = ignore_range
        self._total_delta = total_delta
        # `omit_slice` is the huggingface.co / jsDelivr shape measured on 2026-10-18: a 206 that
        # never states which slice it is. Everything about resume has to degrade to the digest when
        # that happens, so the seed has to be able to say it.
        self._omit_slice = omit_slice
        # A 304 reaches us either as an HTTPError or as an empty response, depending on the urllib
        # path, so `respond_304` lets a seed produce either shape explicitly.
        self._respond_304 = respond_304
        self.ranges_seen = []
        self.asked = []

    def open(self, request, timeout=None):
        sent = dict(getattr(request, "headers", None) or {})
        self.asked.append(sent)
        if self._respond_304:
            if self._respond_304 == "raise":
                raise urllib.error.HTTPError("https://huggingface.co/x", 304, "Not Modified",
                                            _Headers(dict(self._headers)), None)
            return _FakeResponse(b"", 304, dict(self._headers))
        wanted = sent.get("Range")
        start = 0
        if wanted and wanted.startswith("bytes=") and not self._ignore_range:
            start = int(wanted[len("bytes="):].split("-")[0])
        self.ranges_seen.append(start)
        promised = self._payload[start:]
        wire = promised[:self._cut_at] if self._cut_at is not None else promised
        status = 206 if (start and not self._ignore_range) else 200
        headers = dict(self._headers)
        # The promise is about the whole asset; the wire carries less. That gap is the defect.
        if not self._omit_slice:
            headers.setdefault("Content-Length", str(len(promised)))
        if status == 206 and not self._omit_slice:
            headers.setdefault("Content-Range",
                               "bytes %d-%d/%d" % (start, start + len(promised) - 1,
                                                   len(self._payload) + self._total_delta))
        return _FakeResponse(wire, status, headers)


class _FakeResponse:
    """The answer `_FakeOpener` hands back: a status, headers, and a readable body."""

    def __init__(self, body, status, headers):
        self._body = body
        self._pos = 0
        self.status = status
        self.headers = _Headers(headers)

    def read(self, n=-1):
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Headers:
    """Just enough of `http.client.HTTPResponse.headers` for one lookup."""

    def __init__(self, table):
        self._table = dict(table)

    def get(self, name, default=None):
        return self._table.get(name, default)


def main(argv):
    if "--self-test" in argv:
        return self_test()
    print(__doc__)
    print("usage: python script/net_guard.py --self-test")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
