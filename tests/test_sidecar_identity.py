# -*- coding: utf-8 -*-
"""D73: a port answering `/health` is not proof that *our* sidecar is answering.

The rule lives in `script/sidecar_identity.py` as a pure function so it can be exercised
against a matrix rather than against a 2.9 GB checkpoint -- and so four call sites cannot each
invent a slightly different version of "close enough". Every rejection below is paired with a
case that must NOT be rejected, because a guard that fires on the healthy path gets disabled.
"""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import sidecar_identity as si          # noqa: E402


class FakeProc:
    """Stand-in for subprocess.Popen: only `pid` and `poll()` are read here."""

    def __init__(self, pid=4242, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class ClassifyMatrixTests(unittest.TestCase):
    def test_matching_pid_is_ours(self):
        label, detail = si.classify(4242, None, 4242)
        self.assertEqual(si.OURS, label)
        self.assertIn("4242", detail)

    def test_a_different_answering_pid_is_foreign(self):
        label, _detail = si.classify(4242, None, 999)
        self.assertEqual(si.FOREIGN, label)

    def test_our_dead_child_wins_the_verdict_over_a_foreign_pid(self):
        # Ordering matters for what the operator does next: if the process we started is gone,
        # "someone else owns the port" is the consequence, not the diagnosis. Reporting FOREIGN
        # here would send the reader to compare pids instead of at their own exit code.
        label, detail = si.classify(4242, 1, 999)
        self.assertEqual(si.EXITED, label)
        self.assertIn("code 1", detail)
        self.assertIn("999", detail)

    def test_no_pid_plus_a_dead_child_is_still_caught(self):
        # The only branch that works against a sidecar predating D72: it needs nothing from
        # the service except that something answered while our own child is gone.
        label, _detail = si.classify(4242, 0, None)
        self.assertEqual(si.EXITED, label)

    def test_no_pid_with_a_live_child_is_unproven_not_ours(self):
        label, _detail = si.classify(4242, None, None)
        self.assertEqual(si.UNPROVEN, label)

    def test_reusing_an_external_service_is_a_valid_choice_not_a_failure(self):
        # `ExePath == ""` means the operator started this endpoint on purpose. Treating that
        # as "foreign" would break the documented external-sidecar mode (REQ-A4 / sdk.md).
        label, detail = si.classify(None, None, 999)
        self.assertEqual(si.OURS, label)
        self.assertIn("external", detail)

    def test_labels_are_distinct_strings(self):
        # A table of codes where two meanings share a value cannot be switched on later.
        self.assertEqual({"ours", "foreign", "unproven", "exited", "silent"},
                         {si.OURS, si.FOREIGN, si.UNPROVEN, si.EXITED, si.SILENT})

    def test_silence_is_its_own_answer_even_when_a_pid_is_known(self):
        # The opposite fact from "answered without a pid", and the reason `answered` had to
        # become its own argument: nothing replied, so there is no identity claim either way.
        label, _ = si.classify(4242, None, None, answered=False)
        self.assertEqual(si.SILENT, label)

    def test_nothing_answered_is_not_read_as_ours_even_for_external_mode(self):
        # proc_pid None means "we did not launch one"; it must not swallow "nobody is there".
        label, _ = si.classify(None, None, None, answered=False)
        self.assertEqual(si.SILENT, label)


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, ms):
        self.now += ms


class WaitTests(unittest.TestCase):
    def wait(self, proc, answers, allow_unproven=True, timeout=10.0):
        """Drive `wait_until_ours` with a scripted probe and a fake clock (no sockets).

        Each entry is the pair `(answered, pid)` that one poll of `/health` would give.
        """
        clock = _FakeClock()
        seq = list(answers)

        def probe(url):
            return seq.pop(0) if seq else answers[-1]

        real_monotonic = si.time.monotonic
        si.time.monotonic = clock.monotonic
        try:
            return si.wait_until_ours("http://127.0.0.1:1", proc, timeout,
                                      allow_unproven=allow_unproven,
                                      sleep=clock.sleep, probe=probe)
        finally:
            si.time.monotonic = real_monotonic

    def test_waits_until_our_pid_answers(self):
        label, _ = self.wait(FakeProc(), [(False, None), (False, None), (True, 4242)])
        self.assertEqual(si.OURS, label)

    def test_foreign_answer_raises_and_names_both_sides(self):
        with self.assertRaises(si.SidecarIdentityError) as ctx:
            self.wait(FakeProc(pid=4242), [(True, 999)])
        message = str(ctx.exception)
        self.assertIn("999", message)
        self.assertIn("4242", message)

    def test_unproven_is_refused_when_the_caller_declines_it(self):
        # What a measurement asks for: an older sidecar that will not say who it is must not
        # be silently accepted as "mine".
        with self.assertRaises(si.SidecarIdentityError):
            self.wait(FakeProc(), [(True, None)], allow_unproven=False)

    def test_unproven_is_accepted_when_the_caller_asks_for_that(self):
        # The paired non-firing case: a release runner against a pre-D72 sidecar must still
        # work, or this guard would have taken a healthy deployment down over a version gap.
        label, _detail = self.wait(FakeProc(), [(True, None)], allow_unproven=True)
        self.assertEqual(si.UNPROVEN, label)

    def test_silence_until_timeout_is_a_loud_failure(self):
        # Regression on the API itself: while "no answer" and "answered without a pid" shared
        # one None, this loop returned "unproven" on its first turn and could never time out.
        with self.assertRaises(si.SidecarIdentityError) as ctx:
            self.wait(FakeProc(), [(False, None)] * 3, timeout=1.0)
        self.assertIn("within", str(ctx.exception))

    def test_child_that_exited_before_any_answer_is_reported_as_such(self):
        with self.assertRaises(si.SidecarIdentityError) as ctx:
            self.wait(FakeProc(exit_code=3), [(False, None)] * 3, timeout=1.0)
        self.assertIn("code 3", str(ctx.exception))

    def test_dead_child_while_the_port_answers_is_not_reported_as_silence(self):
        # The production shape of D73: our child could not bind the port and left, an older
        # instance is answering it. Must raise, whatever that responder says about itself.
        for answers in ([(True, 999)], [(True, None)]):
            with self.subTest(answers):
                with self.assertRaises(si.SidecarIdentityError) as ctx:
                    self.wait(FakeProc(pid=4242, exit_code=0), answers)
                self.assertIn("exited", str(ctx.exception))


class AnswererParsingTests(unittest.TestCase):
    class Resp:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def opener(self, body, status=200):
        resp = self.Resp(body)
        resp.status = status

        def _open(url, timeout=None):
            return resp

        return _open

    def test_reads_the_published_pid(self):
        answered, got = si.health_answer("http://h", opener=self.opener(json.dumps({"pid": 77}).encode()))
        self.assertEqual((True, 77), (answered, got))

    def test_older_sidecar_without_the_field_still_counts_as_answered(self):
        # "up but anonymous" is UNPROVEN downstream, not silence: a caller waiting for a
        # service must stop waiting and go ask who is there.
        self.assertEqual((True, None), si.health_answer("http://h", opener=self.opener(b"{}")))

    def test_unparsable_body_is_no_answer_and_does_not_raise(self):
        # The probe runs inside a wait loop: throwing here would replace "identity unproven"
        # with a crash the caller cannot classify.
        self.assertEqual((False, None), si.health_answer("http://h", opener=self.opener(b"not json")))

    def test_json_that_is_not_an_object_is_no_answer(self):
        self.assertEqual((False, None), si.health_answer("http://h", opener=self.opener(b"[1,2]")))

    def test_non_200_answer_is_no_data(self):
        self.assertEqual((False, None),
                         si.health_answer("http://h", opener=self.opener(b'{"pid":77}', status=503)))

    def test_a_non_numeric_pid_is_answered_but_unknown(self):
        self.assertEqual((True, None), si.health_answer("http://h", opener=self.opener(b'{"pid":"abc"}')))

    def test_answerer_pid_helper_agrees_with_the_pair(self):
        self.assertEqual(77, si.answerer_pid("http://h", opener=self.opener(b'{"pid":77}')))
        self.assertIsNone(si.answerer_pid("http://h", opener=self.opener(b"{}")))


class WiringTests(unittest.TestCase):
    """Every place that starts a sidecar must go through the one rule (the D71 lesson)."""

    PRODUCTS = ("script/run_av_baseline.py", "script/bench_tm.py", "tests/test_sidecar_e2e.py",
                "tests/test_sidecar_busy_e2e.py")

    def test_starters_reference_the_shared_rule(self):
        for name in self.PRODUCTS:
            with self.subTest(name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertTrue("sidecar_identity" in text or "wait_until_ours" in text,
                                "%s starts a sidecar without proving who answers" % name)

    def test_the_line_rule_has_one_known_blind_spot_and_a_cover_for_it(self):
        """A call through a helper does not name the endpoint, so the line rule cannot see it.

        Recorded rather than papered over: teaching the line rule every helper name would make
        it a list that has to be updated in step with each rename, and a stale list fails
        silently. What actually covers that shape is the file-level rule above -- a file that
        launches a sidecar must reference `sidecar_identity` at all, and it cannot dodge that by
        hiding the URL behind a function.
        """
        self.assertEqual([], self.scan('        if sidecar_healthy(self.url):\n'))
        # and the cover really fires on such a file
        text = (ROOT / "script" / "bench_tm.py").read_text(encoding="utf-8")
        self.assertIn("sidecar_identity", text)

    def test_no_bare_health_poll_survives_where_a_child_is_started(self):
        # The defect shape in one line: start a child, then accept any 200 from /health.
        offenders = []
        for name in self.PRODUCTS:
            text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
            offenders += ["%s:%d %s" % (name, number, line)
                          for number, line in self.scan(text)]
        self.assertEqual([], offenders,
                         "a raw /health poll next to a started child: %s" % offenders)

    #: Words that mean the line is actually reaching the endpoint, as opposed to writing about it.
    FETCHERS = ("urlopen", "urllib", "_probe_url", "sidecar_healthy", "_health(", "Request(")

    @classmethod
    def scan(cls, text):
        """Lines that poll `/health` as if an answer proved ownership.

        Only lines that really reach the endpoint count: the first draft matched any line
        mentioning `/health` and reported five docstrings and one human-readable hint, which is
        the familiar failure of a heuristic predicate -- a rule that cries wolf gets switched off
        within a week. A line taking a side either routes through `sidecar_identity` or declares
        itself `lint-allow: liveness-only`; anything else is the D73 shape.
        """
        hits = []
        for number, line in enumerate(text.split("\n"), 1):
            stripped = line.strip()
            if ("/health" in stripped and not stripped.startswith("#")
                    and any(word in stripped for word in cls.FETCHERS)
                    and "sidecar_identity" not in stripped and "liveness-only" not in stripped):
                hits.append((number, stripped[:60]))
        return hits

    def test_the_guard_fires_on_the_shape_it_is_for(self):
        # Seed one of each, because a rule that never reports is indistinguishable from a rule
        # that cannot report -- and one that reports prose is indistinguishable from noise.
        seeded = [
            ('    if _probe_url(f"http://127.0.0.1:{port}/health", timeout=1.0):', 1),
            ('        with urllib.request.urlopen(base + "/health") as r:', 1),
            ("        if sidecar_healthy(url):", 0),          # known blind spot, see below
            ('    return _health(url).get("status") == "ok"  # polls /health', 1),
            ('        return _probe_url("http://127.0.0.1:1/health")  # lint-allow: liveness-only', 0),
            ('        with urllib.request.urlopen(base + "/health") as r:  # liveness-only', 0),
            ("    label, detail = classify(proc.pid, proc.poll(), their_pid)", 0),
            ("    from sidecar_identity import health_answer", 0),
            ('    """Samples `/health` while the engine call is in flight."""', 0),
            ('    reason = "gated service never answered /health"', 0),
            ('    print("reuse their /health endpoints if already running")', 0),
            ('    # a comment mentioning /health via urlopen', 0),
        ]
        for line, expected in seeded:
            with self.subTest(line=line[:44]):
                self.assertEqual(expected, len(self.scan(line + "\n")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
