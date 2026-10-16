# -*- coding: utf-8 -*-
"""Proving that the sidecar answering on a port is the one this process started (D73).

The shape that keeps recurring: reserve a port (or simply use the shipped default 11435 /
11436), spawn a service, then poll `/health` and read "someone answered" as "my service is
up". When an older instance is still listening on that port, the new child cannot bind it and
dies -- while the poll is answered by the survivor. Everything then works, against a service
started with *different arguments* than the ones just requested, and the log says `ready`.

Two separate observations distinguish the cases, and both are needed:

  * `pid` from `/health` (added for D72) -- an exact answer, when the service publishes it.
  * whether our own child is still alive -- a weaker answer that needs nothing from the
    service, and the only one available against an older sidecar. A dead child plus a healthy
    port is already proof that someone else is answering.

`classify` is pure so the rule itself can be tested against a matrix instead of against a
live checkpoint. Callers keep their own retry policy (a test may re-draw a port; a batch
runner must fail loudly), which is why this module decides *what the evidence means* and not
what to do about it.
"""

import time


#: The answering process is demonstrably ours.
OURS = "ours"
#: The answering process named a pid that is not ours.
FOREIGN = "foreign"
#: Something answered, but nothing identifies it, and our child is still running.
UNPROVEN = "unproven"
#: Our child exited, yet the port answered: the responder cannot be it.
EXITED = "exited"
#: Nothing has answered yet -- not a verdict about identity, just no data.
SILENT = "silent"


def classify(proc_pid, proc_exited, answerer_pid, answered=True):
    """(label, detail) for one poll of one service we started.

    `proc_pid`: pid of the process we launched (None if we launched none, i.e. the caller is
    deliberately reusing an external service and has nothing to prove).
    `proc_exited`: that process's exit code, or None while it is still running.
    `answerer_pid`: the `pid` field of its `/health`, or None when absent/unparsable.
    `answered`: whether anything replied at all. This is a separate argument on purpose, and
    the first draft did not have it: it squeezed "nobody answered" and "answered without
    naming itself" into one None, which made the wait loop classify silence as an unproven
    identity and return immediately -- two opposite facts sharing one value, the mistake this
    repo has already written down three times (D57/D58/D70).
    """
    if not answered:
        return SILENT, "nothing answered the health probe yet"
    if proc_pid is None:
        # Not our claim to make: the operator started this endpoint on purpose.
        return OURS, "no process was launched by us; an external service is taken as asked for"
    if answerer_pid is not None and answerer_pid == proc_pid:
        return OURS, "answered by pid %d, the process we started" % proc_pid
    if proc_exited is not None:
        # Checked before the "unknown responder" branch on purpose. If our own child is gone,
        # a healthy port is evidence about *someone else* even when that someone will not say
        # who -- and this is the only branch that works against a sidecar older than D72.
        return EXITED, ("our process (pid %d) exited with code %s while the port answered%s"
                        % (proc_pid, proc_exited,
                           " by pid %d" % answerer_pid if answerer_pid is not None else
                           " without identifying itself"))
    if answerer_pid is not None:
        return FOREIGN, ("port answered by pid %d, not our process (pid %d)"
                         % (answerer_pid, proc_pid))
    return UNPROVEN, ("port answered without a pid (sidecar older than D72?), our process pid %d"
                      " still running" % proc_pid)


class SidecarIdentityError(RuntimeError):
    """The port is answering, but not for us -- or not by us. A different failure from "the
    service never came up", and it has to be said apart: the operator's next step is to stop
    whatever is holding that port, not to wait longer."""


def health_answer(url, timeout=2.0, opener=None):
    """`(answered, pid)` for one `/health` call: did anything reply, and did it say who it is.

    The two are kept apart because they demand opposite next steps -- waiting is right for the
    first, and identifying the responder is right for the second. `answered` is False on a
    refused connection, a non-200, or a body that is not JSON; `pid` is None whenever the
    answer does not publish one (an older sidecar included), never a fabricated 0.
    """
    import json

    if opener is None:
        from urllib.request import urlopen
        opener = urlopen
    try:
        with opener(url.rstrip("/") + "/health", timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return False, None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:                                       # noqa: BLE001 - any silence is no data
        return False, None
    if not isinstance(payload, dict):
        return False, None
    pid = payload.get("pid")
    known = int(pid) if isinstance(pid, (int, str)) and str(pid).lstrip("-").isdigit() else None
    return True, known


def answerer_pid(url, timeout=2.0, opener=None):
    """Just the responder's pid, for a caller that already knows the endpoint is up."""
    return health_answer(url, timeout, opener)[1]


def wait_until_ours(url, proc, timeout, allow_unproven=True, interval=1.0,
                    sleep=time.sleep, probe=health_answer):
    """Wait for a service we launched to answer as itself. Returns (label, detail).

    Raises `SidecarIdentityError` when the evidence says the responder is not ours
    (`FOREIGN`, or `EXITED` while the port answers). `UNPROVEN` is the caller's decision:
    a release-grade runner may accept it against a pre-D72 sidecar (refusing would take a
    working deployment down for a version gap the operator cannot fix by restarting), while a
    measurement must not -- hence the flag rather than a policy baked in here.
    """
    end = time.monotonic() + timeout
    while True:
        pid = proc.pid if proc is not None else None
        exited = proc.poll() if proc is not None else None
        answered, their_pid = probe(url)
        label, detail = classify(pid, exited, their_pid, answered=answered)
        if label in (OURS, UNPROVEN):
            if label == UNPROVEN and not allow_unproven:
                raise SidecarIdentityError("%s: %s" % (url, detail))
            return label, detail
        if label in (FOREIGN, EXITED):
            raise SidecarIdentityError("%s: %s" % (url, detail))
        if proc is not None and exited is not None:
            raise SidecarIdentityError("started sidecar exited with code %s before anything "
                                       "answered on %s" % (exited, url))
        if time.monotonic() >= end:
            raise SidecarIdentityError("nothing answered on %s within %ss (our process pid %s "
                                       "is still running)" % (url, timeout, pid))
        sleep(interval)
