#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for script/net_guard.py and for the fact that the downloaders use it.

Two separate jobs, and the second one is the one that decays:

  * the policy's own verdicts (host shapes, scheme, resolved addresses, digests);
  * **wiring** -- that every fetch_*.py actually goes through the policy. A guard nobody
    calls is the shape of defect D65 ("a validation script that existed but was never run
    by the gate"), and D69 started from exactly that asymmetry: one downloader was hardened
    and two were bare urlopen calls, which nothing noticed for as long as anyone can say.

The wiring cases read the scripts as text on purpose. An import-based check would pass as
soon as the module is imported, whether or not the download path uses it.
"""
import hashlib
import json
import pathlib
import re
import socket
import sys
import tempfile
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import net_guard  # noqa: E402

HOSTS = ("huggingface.co", "hf-mirror.com", "argos-net.com", "github.com")
SUFFIXES = (".hf.co", ".huggingface.co", ".githubusercontent.com")


def fake_resolve(table):
    def _resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port)) for ip in table]
    return _resolve


class HostShapeTests(unittest.TestCase):
    """The exact set of name shapes, because a suffix rule is a boundary rule."""

    def test_listed_hosts_and_subdomains_are_accepted(self):
        for host in ("huggingface.co", "HF-MIRROR.COM", "huggingface.co.",
                     "cdn-lfs.hf.co", "us.aws.cdn.hf.co", "objects.githubusercontent.com"):
            self.assertTrue(net_guard.hostname_matches(host, HOSTS, SUFFIXES), host)

    def test_look_alikes_are_refused(self):
        # Each of these is what a matcher without a label boundary would let through.
        for host in ("huggingface.co.evil.example", "myhuggingface.co", "evilhf.co",
                     "not-argos-net.com", "githubusercontent.com", "", "."):
            self.assertFalse(net_guard.hostname_matches(host, HOSTS, SUFFIXES), host)

    def test_suffix_without_a_leading_dot_is_a_caller_bug_and_says_so(self):
        with self.assertRaises(net_guard.GuardError):
            net_guard.hostname_matches("evilhf.co", (), ("hf.co",))

    def test_scheme_is_part_of_the_verdict(self):
        self.assertNotEqual("", net_guard.check_url("http://huggingface.co/x", HOSTS, SUFFIXES)[1])
        self.assertEqual("", net_guard.check_url("https://huggingface.co/x", HOSTS, SUFFIXES)[1])
        self.assertTrue(net_guard.check_url("https://x.example/y", HOSTS, SUFFIXES)[1])


class ResolvedAddressTests(unittest.TestCase):
    """SSRF: an allowlisted name is not enough once it resolves inward."""

    def test_non_public_answers_are_refused(self):
        for label, table in (("loopback", ["127.0.0.1"]), ("private", ["10.0.0.5"]),
                             ("private-192", ["192.168.1.1"]), ("link-local", ["169.254.9.9"]),
                             ("unspecified", ["0.0.0.0"]), ("v6-loopback", ["::1"]),
                             ("multicast", ["224.0.0.1"]), ("reserved", ["240.0.0.1"])):
            with self.subTest(label):
                with self.assertRaises(net_guard.GuardError):
                    net_guard.require_public_address("x.example", resolve=fake_resolve(table))

    def test_every_answer_is_checked_not_just_the_first(self):
        # A name that returns one public and one private record must still be refused:
        # only the first answer passing is what makes this a race instead of a check.
        with self.assertRaises(net_guard.GuardError):
            net_guard.require_public_address(
                "x.example", resolve=fake_resolve(["93.184.216.34", "127.0.0.1"]))

    def test_public_answers_pass(self):
        net_guard.require_public_address("x.example",
                                         resolve=fake_resolve(["93.184.216.34", "2606:2800::"]))

    def test_resolution_failure_is_a_refusal_not_a_pass(self):
        def boom(host, port, proto=0):
            raise OSError("dns down")
        with self.assertRaises(net_guard.GuardError):
            net_guard.require_public_address("x.example", resolve=boom)


class RedirectTests(unittest.TestCase):
    def test_cross_host_redirect_is_refused_with_a_reason(self):
        handler = net_guard._GuardedRedirect(HOSTS, SUFFIXES)

        class Req:
            url = "https://huggingface.co/a"

        with self.assertRaises(urllib.error.URLError) as caught:
            handler.redirect_request(Req(), None, 302, "Found", {},
                                    "https://attacker.example/a")
        self.assertIn("disallowed", str(caught.exception))


class _StubResponse:
    def __init__(self, payload, chunk_seen=None):
        self._payload, self._pos = payload, 0
        self._chunk_seen = chunk_seen

    def read(self, n=-1):
        if self._pos >= len(self._payload):
            return b""
        piece = self._payload[self._pos:self._pos + (n if n and n > 0 else 4096)]
        self._pos += len(piece)
        if self._chunk_seen is not None:
            self._chunk_seen.append(len(piece))
        return piece

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubOpener:
    def __init__(self, payload):
        self.payload = payload
        self.opened = 0

    def open(self, request, timeout=None):
        self.opened += 1
        return _StubResponse(self.payload)


class FetchIntegrityTests(unittest.TestCase):
    """The digest is the point: what landed must be what the licence note describes."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.payload = b"wonslate-model-bytes"
        self.good = hashlib.sha256(self.payload).hexdigest()
        self.target = pathlib.Path(self.dir.name) / "model.bin"

    def _fetch(self, **kw):
        args = dict(expected_sha256=self.good,
                    resolve=fake_resolve(["93.184.216.34"]),
                    opener=_StubOpener(self.payload))
        args.update(kw)
        return net_guard.fetch("https://huggingface.co/x/model.bin", self.target,
                               HOSTS, SUFFIXES, **args)

    def test_matching_digest_lands_the_file(self):
        digest, size = self._fetch()
        self.assertEqual((digest, size), (self.good, len(self.payload)))
        self.assertEqual(self.target.read_bytes(), self.payload)

    def test_mismatch_raises_and_leaves_no_partial_file(self):
        with self.assertRaises(net_guard.GuardError):
            self._fetch(expected_sha256="0" * 64)
        self.assertFalse(self.target.exists())
        leftovers = [p.name for p in pathlib.Path(self.dir.name).iterdir()]
        self.assertEqual([], leftovers, "a .part file must not survive a refused download")

    def test_unpinned_is_a_failure_not_a_silent_pass(self):
        with self.assertRaises(net_guard.UnpinnedAsset):
            self._fetch(expected_sha256=None)

    def test_unpinned_can_be_allowed_but_says_so(self):
        seen = []
        digest, _size = self._fetch(expected_sha256=None, allow_unpinned=True,
                                    report=lambda tag, msg: seen.append((tag, msg)))
        self.assertTrue(any("WITHOUT a checksum" in m for _t, m in seen), seen)

    def test_progress_is_reported_for_large_assets(self):
        reported = []
        self._fetch(progress_every=4, report=lambda tag, msg: reported.append(msg))
        self.assertTrue(reported, "a caller asking for progress got none: the 1 GB fetch "
                                  "would look exactly like a hang")

    def test_digest_provenance_appears_in_the_mismatch_message(self):
        with self.assertRaises(net_guard.GuardError) as caught:
            self._fetch(expected_sha256="0" * 64, digest_source="upstream API 2026-10-14")
        self.assertIn("upstream API 2026-10-14", str(caught.exception))


class _Headers:
    """Just enough of `http.client.HTTPResponse.headers` for one lookup."""

    def __init__(self, table):
        self._table = dict(table)

    def get(self, name, default=None):
        return self._table.get(name, default)


class _ServerResponse:
    def __init__(self, body, status, headers, die_after=None):
        self._pos = 0
        # A dropped connection is not an empty body: the reader gets some bytes and then an
        # error, which is the shape that used to lose the whole download.
        self._pending = None
        if die_after is not None:
            body = body[:die_after]
            self._pending = urllib.error.URLError("connection reset")
        self._body = body
        self.status = status
        self.headers = _Headers(headers)

    def read(self, n=-1):
        if self._pending is not None and self._pos >= len(self._body):
            raise self._pending
        if self._pos >= len(self._body):
            return b""
        piece = self._body[self._pos:self._pos + (n if n and n > 0 else 4096)]
        self._pos += len(piece)
        return piece

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RangeServer:
    """A stand-in upstream that answers Range requests, and can be told to misbehave.

    Every knob here is a real server behaviour a resumed download has to survive: `cut_at`
    promises the full length and then sends less (a connection drop), `die_after` raises
    mid-stream, `ignore_range` is a server without resume support, `promise_length=False` is
    a chunked response, and `slice_total_delta` makes the total it states wrong.
    """

    def __init__(self, body, cut_at=None, die_after=None, ignore_range=False,
                 promise_length=True, slice_total_delta=0, etag='"gen-1"', omit_slice=False):
        self.body = body
        self.cut_at = cut_at
        self.die_after = die_after
        self.ignore_range = ignore_range
        self.promise_length = promise_length
        self.slice_total_delta = slice_total_delta
        # `omit_slice` is what huggingface.co and jsDelivr were measured doing on 2026-10-18:
        # answer a Range request with a bare 206 and never say which slice it is.
        self.omit_slice = omit_slice
        self.etag = etag
        self.ranges = []
        self.asked_headers = []
        self.opened = 0

    def open(self, request, timeout=None):
        self.opened += 1
        self.asked_headers.append(dict(request.headers or {}))
        wanted = (request.headers or {}).get("Range")
        start = 0
        if wanted and wanted.startswith("bytes=") and not self.ignore_range:
            start = int(wanted[len("bytes="):].split("-")[0])
        self.ranges.append(start)
        if start and start > len(self.body):
            raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable",
                                        _Headers({}), None)
        # What the server *says* it has, versus what it actually puts on the wire.
        promised = self.body[start:]
        wire = promised[:self.cut_at] if self.cut_at is not None else promised
        status = 206 if (start and not self.ignore_range) else 200
        headers = {"ETag": self.etag, "Accept-Ranges": "bytes"}
        if self.omit_slice:
            return _ServerResponse(wire, status, headers, die_after=self.die_after)
        if status == 206:
            headers["Content-Range"] = "bytes %d-%d/%d" % (
                start, start + len(promised) - 1,
                len(self.body) + self.slice_total_delta)
        if self.promise_length:
            headers["Content-Length"] = str(len(promised))
        return _ServerResponse(wire, status, headers, die_after=self.die_after)


class LengthIntegrityTests(unittest.TestCase):
    """Defect D80: the digest says *which* artifact, nothing used to say *all of it*."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.payload = b"wonslate-checkpoint-bytes-0123456789"   # 34 bytes
        self.good = hashlib.sha256(self.payload).hexdigest()
        self.url = "https://huggingface.co/x/model.bin"

    def _fetch(self, server, dest=None, **kw):
        args = dict(expected_sha256=self.good, resolve=fake_resolve(["93.184.216.34"]),
                    opener=server)
        args.update(kw)
        return net_guard.fetch(self.url, dest or self.root / "model.bin", HOSTS, SUFFIXES,
                               **args)

    def test_short_body_is_a_truncation_not_a_finished_file(self):
        server = RangeServer(self.payload, cut_at=6)
        with self.assertRaises(net_guard.TruncatedError) as caught:
            self._fetch(server)
        self.assertIn("6 of %d bytes" % len(self.payload), str(caught.exception))
        self.assertFalse((self.root / "model.bin").exists(),
                         "a truncated file must never be published under its real name")

    def test_truncated_bytes_are_kept_and_the_next_attempt_resumes(self):
        # The other half of D80: the old code deleted the partial, so a gigabyte asset cost
        # one full download per attempt. Resume is only worth anything if attempt two asks
        # for the tail and lands the exact bytes.
        first = RangeServer(self.payload, cut_at=6)
        with self.assertRaises(net_guard.TruncatedError):
            self._fetch(first)
        self.assertEqual([0], first.ranges, "a first attempt has nothing to resume from")
        partial = self.root / "model.bin.part"
        self.assertTrue(partial.is_file(), "the kept bytes are what a retry resumes from")
        self.assertEqual(self.payload[:6], partial.read_bytes())
        server = RangeServer(self.payload)
        digest, size = self._fetch(server)
        self.assertEqual([6], server.ranges, "attempt two must ask for the missing tail")
        self.assertEqual((digest, size), (self.good, len(self.payload)))
        self.assertEqual(self.payload, (self.root / "model.bin").read_bytes())

    def test_a_resumed_digest_covers_the_bytes_already_on_disk(self):
        # If the prefix were not folded into the hash, the digest of the tail alone would be
        # reported and this would fail -- the pin is the only thing standing between a wrong
        # prefix and a published file, so it has to be exercised from the resumed path.
        with self.assertRaises(net_guard.TruncatedError):
            self._fetch(RangeServer(self.payload, cut_at=10))
        digest, _size = self._fetch(RangeServer(self.payload))
        self.assertEqual(self.good, digest)

    def test_server_ignoring_the_range_restarts_instead_of_doubling(self):
        with self.assertRaises(net_guard.TruncatedError):
            self._fetch(RangeServer(self.payload, cut_at=8))
        server = RangeServer(self.payload, ignore_range=True)
        digest, size = self._fetch(server)
        self.assertEqual((digest, size), (self.good, len(self.payload)))
        self.assertEqual(self.payload, (self.root / "model.bin").read_bytes(),
                         "appending a whole-file answer to a partial gives a doubled prefix")

    def test_partial_from_another_asset_or_pin_is_never_appended(self):
        for meta in ({"url": "https://huggingface.co/x/OTHER.bin", "pin": self.good,
                      "size": 6},
                     {"url": self.url, "pin": "0" * 64, "size": 6}):
            self.root.mkdir(exist_ok=True)
            (self.root / "model.bin.part").write_bytes(self.payload[:6])
            (self.root / "model.bin.part.meta").write_text(json.dumps(meta), encoding="utf-8")
            seen = []
            digest, size = self._fetch(RangeServer(self.payload),
                                       report=lambda tag, msg: seen.append(msg))
            with self.subTest(meta=sorted(meta)):
                self.assertEqual((digest, size), (self.good, len(self.payload)))
                self.assertEqual(self.payload, (self.root / "model.bin").read_bytes())
                self.assertTrue(any("another asset or pin" in m for m in seen),
                                "refusing to splice generations must be said out loud: %s" % seen)

    def test_one_claim_per_partial_when_two_racers_exist(self):
        # `os.replace` is the lock: whoever moves the file owns it, and the loser must fall
        # back to its own bytes rather than append into someone else's transfer.
        (self.root / "model.bin.part").write_bytes(self.payload[:6])
        (self.root / "model.bin.part.meta").write_text(
            json.dumps({"url": self.url, "pin": self.good, "size": 6}), encoding="utf-8")
        first = net_guard._claim(self.root / "model.bin.part",
                                 self.root / "model.bin.part.meta",
                                 str(self.root / "mine1"), self.url, self.good, None)
        second = net_guard._claim(self.root / "model.bin.part",
                                  self.root / "model.bin.part.meta",
                                  str(self.root / "mine2"), self.url, self.good, None)
        self.assertEqual(6, first)
        self.assertEqual(0, second, "the partial was already claimed by another racer")

    def test_checksum_mismatch_throws_the_partial_away(self):
        # Bad bytes are not a resumable state: keeping them would mean the next attempt
        # appends to a stream we already know is wrong.
        (self.root / "model.bin.part").write_bytes(b"wrongwrong")
        (self.root / "model.bin.part.meta").write_text(
            json.dumps({"url": self.url, "pin": "0" * 64, "size": 10}), encoding="utf-8")
        with self.assertRaises(net_guard.GuardError):
            self._fetch(RangeServer(b"x" * 40, promise_length=True), expected_sha256="f" * 64)
        self.assertEqual([], [p.name for p in self.root.iterdir()],
                         "a refused download leaves neither bytes nor a claim")

    def test_connection_death_midstream_keeps_the_bytes(self):
        first = RangeServer(self.payload, die_after=12)
        with self.assertRaises(urllib.error.URLError):
            self._fetch(first)
        self.assertEqual([0], first.ranges)
        self.assertEqual(self.payload[:12], (self.root / "model.bin.part").read_bytes())
        server = RangeServer(self.payload)
        digest, size = self._fetch(server)
        self.assertEqual((digest, size), (self.good, len(self.payload)))
        self.assertEqual([12], server.ranges)

    def test_a_slice_promise_that_does_not_add_up_is_a_truncation(self):
        # Plant a partial, then let the server claim a *larger* total than the asset: the
        # resume must fail on length rather than publish a short file with a plausible hash.
        (self.root / "model.bin.part").write_bytes(self.payload[:4])
        (self.root / "model.bin.part.meta").write_text(
            json.dumps({"url": self.url, "pin": self.good, "size": 4}), encoding="utf-8")
        with self.assertRaises(net_guard.TruncatedError):
            self._fetch(RangeServer(self.payload, slice_total_delta=+7))

    def test_no_length_promised_is_reported_rather_than_assumed(self):
        # Chunked responses are legal, so this must not fail -- but "the server never said"
        # has to be visible, because it is the one case where a short body cannot be caught.
        seen = []
        digest, _size = self._fetch(RangeServer(self.payload, promise_length=False),
                                    report=lambda tag, msg: seen.append(msg))
        self.assertEqual(self.good, digest)
        self.assertTrue(any("no length promised" in m for m in seen), seen)

    def test_a_claim_without_bytes_is_swept(self):
        (self.root / "model.bin.part.meta").write_text(
            json.dumps({"url": self.url, "pin": self.good, "size": 99}), encoding="utf-8")
        self._fetch(RangeServer(self.payload))
        self.assertEqual(["model.bin"], [p.name for p in self.root.iterdir()],
                         "a sidecar with no partial is not a state anyone can resume from")

    def park(self, count, pin):
        """Leave the server itself out of it: a host that never states a length cannot report a
        truncation, so the only honest way to reach the resume branch is to plant the claim."""
        (self.root / "model.bin.part").write_bytes(self.payload[:count])
        (self.root / "model.bin.part.meta").write_text(
            json.dumps({"url": self.url, "pin": pin, "size": count}), encoding="utf-8")

    def test_a_bare_206_still_resumes_when_a_pin_can_prove_the_result(self):
        # Must not be refused: huggingface.co answers exactly this way, and madlad's 2.9 GB
        # checkpoint is the download where restarting costs the most.
        self.park(6, self.good)
        server = RangeServer(self.payload, omit_slice=True)
        digest, size = self._fetch(server)
        self.assertEqual([6], server.ranges)
        self.assertEqual((digest, size), (self.good, len(self.payload)))
        self.assertEqual(self.payload, (self.root / "model.bin").read_bytes())

    def test_a_bare_206_never_splices_an_unpinned_asset(self):
        # The refusal has to also throw the bytes away, or every later attempt inherits the
        # same unverifiable state and the asset can never be fetched again.
        self.park(6, "")
        with self.assertRaises(net_guard.GuardError) as caught:
            self._fetch(RangeServer(self.payload, omit_slice=True), expected_sha256=None,
                        allow_unpinned=True)
        self.assertIn("discarded", str(caught.exception))
        self.assertEqual([], [p.name for p in self.root.iterdir()],
                         "a refused resume must not leave its bytes behind")

    def test_the_slice_guard_does_not_fire_on_a_normal_200(self):
        # The contrast case: no resume involved at all, so nothing may be refused. A guard that
        # fires here would break every first download and get switched off within a day.
        fetch = self._fetch(RangeServer(self.payload, omit_slice=True))
        self.assertEqual((fetch[0], fetch[1]), (self.good, len(self.payload)))

    def test_a_truncation_is_retried_and_not_refused(self):
        """The downloaders keep a list of messages that mean 'stop asking'.

        A truncation must not match that list -- retrying is the right answer -- while a
        checksum mismatch still must. Read from the scripts rather than copied, because a
        second copy of the keyword list drifts (the D64 shape).
        """
        message = str(net_guard.TruncatedError(
            "truncated download of model.bin: 6 of 34 bytes arrived (the server promised "
            "Content-Length); the partial is kept for the next attempt"))
        checked = 0
        for name in ("fetch_argos_models.py", "fetch_madlad_model.py", "fetch_voice_models.py"):
            text = (ROOT / "script" / name).read_text(encoding="utf-8")
            refusals = re.findall(r'"([a-z ]+mismatch|[a-z ]*allowlisted|disallowed|not https'
                                  r"|non-public)", text)
            refusals = sorted(set(refusals))
            if not refusals:
                continue
            checked += 1
            for word in refusals:
                self.assertNotIn(word, message,
                                 "%s would treat a truncation as a permanent refusal" % name)
            self.assertIn("checksum mismatch", " ".join(refusals),
                          "%s stopped refusing bad digests" % name)
        self.assertTrue(checked, "no downloader had a refusal list to check")


def sent_header(table, name):
    """Case-insensitive lookup of a request header.

    `urllib.request.Request` stores header names capitalised, so `If-None-Match` arrives here as
    `If-none-match`. Matching on the exact spelling would make a test fail while the request was
    correct -- and header names are case-insensitive on the wire, so the assertion should be too.
    """
    for key, value in (table or {}).items():
        if key.lower() == name.lower():
            return value
    return None


class ConditionalRequestTests(unittest.TestCase):
    """GA-13's other half: ask "is this current?" instead of downloading to find out.

    A 304 arrives in two different shapes depending on the urllib version -- an `HTTPError`, or a
    response with an empty body -- and both are handled, because the second one falling through
    would read as a zero-length download. The must-not-fire case matters as much: a server that
    answers 304 when we never asked must not be reported as a confirmed match.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.payload = b"whole-checkpoint"
        self.good = hashlib.sha256(self.payload).hexdigest()
        self.url = "https://argos-net.com/p.pkg"

    def _fetch(self, opener, dest=None, **kw):
        args = dict(expected_sha256=self.good, resolve=fake_resolve(["93.184.216.34"]),
                    opener=opener)
        args.update(kw)
        return net_guard.fetch(self.url, dest or self.root / "p.pkg", HOSTS, SUFFIXES, **args)

    def test_a_304_answered_as_an_error_becomes_not_modified(self):
        seen = {}
        with self.assertRaises(net_guard.NotModified) as caught:
            self._fetch(StubConditional(status="raise", headers={"ETag": '"v2"'}), observed=seen,
                        if_none_match='"v1"')
        self.assertEqual('"v2"', caught.exception.validators["etag"])
        self.assertEqual('"v2"', seen["etag"], "the fresh validator must reach the caller")
        self.assertEqual([], list(self.root.iterdir()),
                         "a confirmed 304 leaves no half-written file behind")

    def test_a_304_answered_as_a_response_is_also_not_modified(self):
        self.assertRaises(net_guard.NotModified,
                          self._fetch, StubConditional(status="return", headers={"ETag": '"v2"'}),
                          if_none_match='"v1"')

    def test_a_304_we_never_asked_is_refused_not_trusted(self):
        # No `observed` question sent, yet the server answers 304: an empty body is not the asset.
        with self.assertRaises(net_guard.GuardError) as caught:
            self._fetch(StubConditional(status="return"))
        self.assertIn("did not ask", str(caught.exception))
        self.assertFalse((self.root / "p.pkg").exists())

    def test_the_conditional_headers_are_actually_sent(self):
        server = StubConditional(status="raise", headers={"ETag": '"v2"'})
        self.assertRaises(net_guard.NotModified,
                          self._fetch, server, if_none_match='"v1"',
                          if_modified_since="Wed, 01 Jan 2025 00:00:00 GMT")
        self.assertEqual('"v1"', sent_header(server.asked, "If-None-Match"))
        self.assertEqual("Wed, 01 Jan 2025 00:00:00 GMT",
                         sent_header(server.asked, "If-Modified-Since"))

    def test_a_plain_download_sends_no_conditional_headers(self):
        # The counterpart: nobody asked, so nothing may be sent. A stray conditional header would
        # make an unrelated 304 read as a confirmed match.
        server = StubConditional(status="raise", headers={"ETag": '"v2"'})
        with self.assertRaises(urllib.error.HTTPError):
            self._fetch(server)
        self.assertIsNone(sent_header(server.asked, "If-None-Match"))

    def test_a_normal_download_is_untouched_and_reports_its_validators(self):
        # The must-not-fire counterpart: without a conditional request the behaviour is exactly the
        # old one, and the server's validator is still collected for next time.
        seen = {}
        digest, size = self._fetch(RangeServer(self.payload), observed=seen)
        self.assertEqual((digest, size), (self.good, len(self.payload)))
        self.assertEqual('"gen-1"', seen["etag"])

    def test_no_conditional_header_while_a_partial_is_resuming(self):
        # Mixing `Range` with `If-None-Match` makes "this slice is current" and "the whole file is
        # current" indistinguishable, so the resume path never asks the conditional question.
        (self.root / "p.pkg.part").write_bytes(self.payload[:4])
        (self.root / "p.pkg.part.meta").write_text(
            json.dumps({"url": self.url, "pin": self.good, "size": 4}), encoding="utf-8")
        server = RangeServer(self.payload)
        digest, size = self._fetch(server, if_none_match='"v1"')
        self.assertEqual((digest, size), (self.good, len(self.payload)))
        self.assertEqual([4], server.ranges, "the resume still asks for the tail")
        self.assertIsNone(sent_header(server.asked_headers[0], "If-None-Match"),
                          "a conditional header rode along with a Range request")
        self.assertEqual(self.payload, (self.root / "p.pkg").read_bytes())


class _StubCondHeaders:
    def __init__(self, table):
        self._table = dict(table)

    def get(self, name, default=None):
        return self._table.get(name, default)


class StubConditional:
    """Answers a conditional request with 304, either by raising it or by returning it."""

    def __init__(self, status="raise", headers=None):
        self.status_shape = status
        self.headers = headers or {}
        self.asked = {}

    def open(self, request, timeout=None):
        self.asked = dict(request.headers or {})
        if self.status_shape == "raise":
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified",
                                        _StubCondHeaders(self.headers), None)
        return _ServerResponse(b"", 304, dict(self.headers))


class DownloaderWiringTests(unittest.TestCase):
    """Every fetch_*.py must go through the policy, and say nothing about the network by hand.

    This is the case that would have caught D69: the guard existed, one script used it, and
    the other two reached for urlopen directly. Asserting on source text is deliberate --
    importing a module proves nothing about which code path a download takes.
    """

    SCRIPTS = ("fetch_argos_models.py", "fetch_madlad_model.py", "fetch_voice_models.py")

    def test_every_downloader_routes_through_net_guard(self):
        for name in self.SCRIPTS:
            with self.subTest(name):
                text = (ROOT / "script" / name).read_text(encoding="utf-8")
                self.assertIn("net_guard.fetch(", text,
                              "%s does not call the shared policy" % name)

    def test_no_downloader_reaches_for_the_network_directly(self):
        for name in self.SCRIPTS:
            with self.subTest(name):
                text = (ROOT / "script" / name).read_text(encoding="utf-8")
                offenders = [ln.strip() for ln in text.split("\n")
                             if "urlopen(" in ln and not ln.strip().startswith("#")]
                self.assertEqual([], offenders,
                                 "%s dials the network itself: %s" % (name, offenders))

    def test_every_downloader_declares_its_pinned_digests(self):
        for name in self.SCRIPTS:
            with self.subTest(name):
                text = (ROOT / "script" / name).read_text(encoding="utf-8")
                self.assertIn("EXPECTED_SHA256 = {", text)
                self.assertIn("--allow-unpinned", text,
                              "%s has no escape hatch, so an unpinnable asset would be "
                              "an unexplained failure" % name)

    def test_a_pin_without_a_provenance_note_is_rejected(self):
        # Each value must be a (digest, source) pair, not a bare hash: a hash with no
        # stated origin is a claim about where bytes came from that nothing supports.
        for name in self.SCRIPTS:
            with self.subTest(name):
                sys.path.insert(0, str(ROOT / "script"))
                module = __import__(name[:-3])
                for leaf, pin in module.EXPECTED_SHA256.items():
                    self.assertEqual(2, len(pin), "%s/%s is not a (digest, source) pair"
                                     % (name, leaf))
                    self.assertTrue(len(pin[0]) == 64, "%s/%s is not a sha256" % (name, leaf))
                    self.assertRegex(pin[1], r"20\d\d-\d\d-\d\d",
                                     "%s/%s states no date or source" % (name, leaf))


class NameGuardGateTests(unittest.TestCase):
    """Run script/check_name_guards.py from inside the suite that names it.

    D65 was a validator that existed and was never executed. Registering the gate in CI's
    list is not enough: whoever runs this suite locally must run it too, or the only copy
    of the truth is a machine nobody looks at until it is red.
    """

    GATE = ROOT / "script" / "check_name_guards.py"

    def _run(self, *extra):
        import subprocess
        return subprocess.run([sys.executable, str(self.GATE)] + list(extra),
                              capture_output=True, text=True, cwd=str(ROOT))

    def test_the_gate_passes_on_this_repository(self):
        result = self._run()
        self.assertEqual(0, result.returncode,
                         "check_name_guards.py says:\n%s%s" % (result.stdout, result.stderr))

    def test_the_gate_self_test_fires_on_every_seeded_violation(self):
        result = self._run("--self-test")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        # Match "N/N" rather than the current count: hard-coding 8/8 would make the next
        # seed added fail this test for the wrong reason.
        self.assertRegex(result.stdout, r"(\d+)/(\1) seeded violations detected",
                         "self-test did not report every seed firing: %s" % result.stdout)

    def test_the_gate_actually_audited_something(self):
        # A gate that found no files to look at exits 0 like a clean one. Say what it saw.
        result = self._run()
        self.assertIn("downloaders audited", result.stdout, result.stdout + result.stderr)
        self.assertIn("0 violation(s)", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
