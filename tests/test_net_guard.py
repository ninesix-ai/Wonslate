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
import pathlib
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
