# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""check_model_updates.py -- ask each upstream which version it publishes (GA-13's other half).

`net_guard` answers "are these the bytes we declared" (D69). This answers the version question:
"has upstream released something newer than what is installed, and how would we know". Nothing in
the repo did that before, so an upgrade could only be noticed by a human who happened to re-run a
fetch with `--force`.

Three upstreams, three different notions of "version", each asked in its own terms -- smoothing
them into one string would be the same error as quoting one coverage number for three layers:

  argos    the index publishes `package_version` per entry (the field exists today and was
           simply never recorded). Newer = the index names a different version for the pair held.
  voice    the sherpa-onnx assets hang off long-lived release tags, so the version lives in the
           *asset name* (`kokoro-int8-multi-lang-v1_1`, `...-2024-07-17`). Newer = the release
           lists a different file for the same component.
  madlad   fetched from a git revision whose contents can move. Newer = the revision now serves
           bytes other than the digest we hold.

Every URL and directory rule is imported from the script that already uses it, not restated:
the fetch scripts are where those constants live, and a second copy here would eventually disagree.

Exit codes, because a tool that cannot tell must not look like a tool that found nothing:

  0  at least one asset was compared and no upstream published something newer than it
  2  at least one upstream publishes something newer
  1  at least one probe failed, OR nothing could be compared at all -- either way this run
     says nothing about updates and must not be read as "up to date" (defect D74)

D74 was found while wiring the scheduled check, not by reading this file: a fresh machine (and
GitHub Actions) has no `wonslate-origin.json` anywhere, so every row came back `unrecorded`,
the exit code was 0, and the output said "no newer upstream version found". A timer built on
that would report all-clear forever. Two things followed: the verdict had to require that at
least one comparison actually happened, and the check needed a reference that does not live on
one machine -- `--baseline`, which compares upstream against what this repository says we ship.

Probes take an `opener`, so they are testable with canned answers. This script deliberately does
not run in the gate: a check that needs the network to pass goes red on every sandboxed runner and
then gets switched off, which is D28's and D65's lesson. `tests/test_model_updates.py` covers the
classification logic offline and asserts that the three entry points keep using it.
"""
import argparse
import datetime
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "script"))

import model_origin                                            # noqa: E402
from sidecar.ct2_sidecar import (default_model_dir,            # noqa: E402  (one data-dir rule)
                                 default_madlad_model_dir)
import fetch_argos_models                                      # noqa: E402  (index URL, pairs)
import fetch_voice_models                                      # noqa: E402  (release root, components)
import fetch_madlad_model                                      # noqa: E402  (HF repo)

CURRENT, NEWER, UNRECORDED, FAILED = "current", "newer", "unrecorded", "failed"
INCOMPARABLE = "incomparable"

#: The two states in which a comparison really happened. Anything else says nothing, and the
#: difference is the whole content of D74.
COMPARABLE = (CURRENT, NEWER)

#: Where the repository records the newest upstream state it has seen. It lives in git rather
#: than in a data directory precisely so that a scheduled run on a machine with nothing
#: installed can still answer the question.
BASELINE_FILE = pathlib.Path(__file__).resolve().parent / "model_upstream_baseline.json"

USER_AGENT = "wonslate-check-updates/1.0"


def _json(url, opener, timeout=30, token=None):
    """One read-only upstream lookup. Anything that goes wrong raises: the caller says FAILED.

    `token` is only ever sent to api.github.com, by the probe that needs it. jsDelivr and the
    public Hugging Face endpoint are answered anonymously, and `tests/test_model_updates.py`
    asserts that no Authorization header leaves for them -- a bearer token that follows a redirect
    to a CDN is a leaked token.
    """
    headers = {"User-Agent": USER_AGENT}
    if token and urllib.parse.urlsplit(url).netloc == "api.github.com":
        headers["Authorization"] = "Bearer %s" % token
    req = urllib.request.Request(url, headers=headers)
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _why(exc):
    """A reason a reader can act on. `HTTPError` alone hides 403 (rate limited) from 404 (gone)."""
    code = getattr(exc, "code", None)
    return "HTTP %s" % code if code else type(exc).__name__


def _row(asset, state, local, upstream, detail, basis=None):
    row = {"asset": asset, "state": state, "local": local, "upstream": upstream,
           "detail": detail}
    if basis:
        # What the comparison rested on: `version`, `digest` or `name`. `--record-baseline` needs
        # it, because storing a file name into the `sha256` field would poison the reference with
        # something that can never match again.
        row["basis"] = basis
    return row


def _start(dest_dir, record=None):
    """The reference to compare against: given directly (baseline mode) or read from disk.

    One comparison path, two sources of reference. A second copy of the per-family comparison
    for baseline mode is how two answers to the same question start disagreeing (D64's shape).
    """
    if record is not None:
        return record, None
    state, payload = model_origin.read(dest_dir)
    if state == "missing":
        return None, _row(None, UNRECORDED, None, None,
                          "no origin record here; run the fetch script to create one")
    if state == "incomplete":
        return None, _row(None, FAILED, None, None,
                          "origin record is unusable: %s" % payload)
    return payload, None


def check_argos(dest_dir, opener=urllib.request.urlopen, timeout=30, record=None, token=None):
    """Argos versions come from the index's own `package_version` field.

    Takes `token` to match the other probes' signature and ignores it: the index is a public
    CDN object, and sending a credential there would be a leak, not a capability.
    """
    record, early = _start(dest_dir, record)
    if early:
        return dict(early, checked="argos")
    held = record.get("version")
    if not held:
        # D74's second face: without this guard the code below fell through to CURRENT and said
        # "index agrees" about an entry that had nothing to agree with. "We do not know what we
        # ship" is not the same finding as "we ship the newest".
        return dict(_row(record.get("asset"), INCOMPARABLE, None, None,
                         "the reference names no version to compare against"), checked="argos")
    url = fetch_argos_models.INDEX_URLS[0]
    try:
        entries = _json(url, opener, timeout)          # no token: a public CDN, by design
    except Exception as exc:                            # noqa: BLE001 - reported as FAILED
        return dict(_row(record.get("asset"), FAILED, held, None,
                         "index lookup failed: %s" % _why(exc)), checked="argos")
    by_code = {"{}_{}".format(entry.get("from_code"), entry.get("to_code")):
               entry.get("package_version") for entry in entries}
    # The record names a pair (`en_zh`), so the comparison is per pair and not "the newest thing
    # in the index": several pairs sit on different package versions. The pair key is built the
    # same way load_index() builds it -- the index's own `code` field is a package slug
    # (`translate-en_zh-1_9`), not a pair code, and matching on it would find nothing and then
    # report a perfectly good installation as missing from the index.
    upstream = by_code.get(record.get("asset"))
    if upstream and held and upstream != held:
        return dict(_row(record.get("asset"), NEWER, held, upstream,
                         "the index now publishes this version"), checked="argos")
    if upstream is None:
        return dict(_row(record.get("asset"), FAILED, held, None,
                         "the pair is not in the index anymore"), checked="argos")
    return dict(_row(record.get("asset"), CURRENT, held, upstream, "index agrees"),
                checked="argos")


def github_project(release_url):
    """`owner/repo` out of a release-download URL, so the API root is derived, never restated.

    D75: this probe used to append `/releases/latest` to `https://github.com/<owner>/<repo>`,
    which is a human web page. It returned HTML, `json.loads` threw, and the row said FAILED --
    yet nothing noticed, because on the machine that first ran it the voice asset was not
    installed and the code never reached the network. An unexercised URL is an unverified URL.
    """
    parts = urllib.parse.urlsplit(release_url)
    if parts.netloc != "github.com":
        raise ValueError("expected a github release URL, got %r" % release_url)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 3 or segments[2] != "releases":
        raise ValueError("not a github releases URL: %r" % release_url)
    return "/".join(segments[:2])


#: How many leading name segments two assets must share to be the same component. The first
#: draft used two, which is worthless here: every asset in that repository starts `sherpa-onnx-`,
#: so sense-voice got "compared" against an unrelated sherpa archive and the answer would have
#: been "there is a newer version" every single day. Three segments is the shortest prefix that
#: still identifies a model family, and an exact name always wins.
REQUIRED_OVERLAP = 3


def kin_asset(held, names):
    """The release asset that is the same component as `held`, or None when the list has none.

    Versions in that repository live inside the file name (`…-v1_1`, `…-2024-07-17`), so "is
    there a newer one" means "is there another name with the same model family".
    """
    if held in names:
        return held
    want = held.split("-")
    need = min(REQUIRED_OVERLAP, len(want))
    best, best_shared = None, 0
    for name in names:
        if name.endswith((".md5", ".sha256", ".sha512")):
            continue                       # checksum sidecars are not alternative releases
        parts = name.split("-")
        shared = 0
        while shared < min(len(want), len(parts)) and want[shared] == parts[shared]:
            shared += 1
        if shared >= need and shared > best_shared:
            best, best_shared = name, shared
    return best


def check_voice(dest_dir, opener=urllib.request.urlopen, timeout=30, record=None, token=None):
    """Compare the voice archive's digest under the release tag we actually download from.

    D76: the first draft asked `/releases/latest`. That was wrong in two ways at once, and only a
    real call showed it -- the latest release (a versioned app release, 289 assets) contains no
    model archives at all; ours live under the long-lived `tts-models` / `asr-models` tags, which
    hold 646 assets. It also compared file names, so `silero_vad.onnx`, whose name carries no
    version, could never show an upgrade, while GitHub publishes a `digest` for such assets. The
    digest is the same kind of evidence D69 pins, so it is what this probe now uses.
    """
    record, early = _start(dest_dir, record)
    if early:
        return dict(early, checked="voice")
    held = record.get("asset")
    tag = record.get("kind")
    if not held:
        return dict(_row(None, INCOMPARABLE, None, None,
                         "the reference names no asset to look for in the release"),
                    checked="voice")
    if not tag:
        # Without the tag there is nothing to ask: `latest` is not it, and guessing would produce
        # a confident wrong answer instead of an honest gap.
        return dict(_row(held, INCOMPARABLE, held, None,
                         "the reference records no release tag, so there is nothing to query"),
                    checked="voice")
    # The project comes from the release-download URL the fetch script already uses, so no second
    # copy of the URL exists here to drift out of step with it.
    try:
        url = "https://api.github.com/repos/%s/releases/tags/%s" % (github_project(
            fetch_voice_models.RELEASE), tag)
    except ValueError as exc:
        return dict(_row(held, FAILED, held, None, "cannot derive the release API: %s" % exc),
                    checked="voice")
    try:
        release = _json(url, opener, timeout, token=token)
    except Exception as exc:                            # noqa: BLE001 - reported as FAILED
        # GitHub answers 403 `rate limit exceeded` to anonymous callers sharing an IP, which is
        # what a plain `HTTPError` hid. The scheduled run gets a token from the runner; a human
        # without one has to be able to tell "ask again later, or set GITHUB_TOKEN" from "this
        # repository moved".
        return dict(_row(held, FAILED, held, None,
                         "release lookup failed: %s%s" % (
                             _why(exc),
                             " (anonymous rate limit; set GITHUB_TOKEN)" if "403" in _why(exc)
                             else "")), checked="voice")
    assets = [a for a in release.get("assets", []) if isinstance(a, dict) and a.get("name")]
    names = [a["name"] for a in assets]
    listed = {a["name"]: a for a in assets}
    asset = listed.get(held)
    if asset is None:
        # Our exact file is gone from that tag. Usually that means they published `...-v2_0` and
        # dropped the old name, which is a finding worth announcing -- but the direction is not
        # inferred from a name, so the row says which entry is listed instead.
        other = kin_asset(held, names)
        return dict(_row(held, NEWER if other else FAILED, held, other,
                         "the tag no longer lists this file%s"
                         % ("; same-family entry: %s" % other if other else ""),
                         basis="name" if other else None), checked="voice")
    digest = (asset.get("digest") or "").split(":", 1)[-1] if asset.get("digest") else None
    if not digest:
        # Upstream publishes `digest: null` for some large archives (sense-voice is one). Saying
        # "still listed" is all this can prove; a silently replaced archive would be invisible,
        # and that limitation belongs in the output rather than in a false CURRENT.
        return dict(_row(held, INCOMPARABLE, held, None,
                         "upstream publishes no digest for this asset, so only its name is "
                         "checkable and it is still listed"), checked="voice")
    ours = record.get("sha256")
    if not ours:
        return dict(_row(held, INCOMPARABLE, None, None,
                         "the reference carries no digest to compare against"), checked="voice")
    if digest != ours:
        # `upstream` keeps the full digest for the same reason madlad does: `--record-baseline`
        # stores it and compares it again tomorrow, and a 12-character prefix would never match a
        # 64-character answer. Only `local` and the printed table are shortened.
        return dict(_row(held, NEWER, ours[:12], digest,
                         "the archive under that name was replaced"), checked="voice")
    return dict(_row(held, CURRENT, ours[:12], digest,
                     "release still serves these bytes"), checked="voice")


def check_madlad(dest_dir, opener=urllib.request.urlopen, timeout=30, record=None, token=None):
    """MADLAD has no version scheme, so the signal is bytes under a moving revision.

    `token` is accepted for the same reason as argos and unused: this is a public HF endpoint.
    """
    record, early = _start(dest_dir, record)
    if early:
        return dict(early, checked="madlad")
    held = record.get("sha256")
    if not held:
        # Same shape as argos: bytes are the only comparable thing here, and with none the old
        # code reported "revision still serves these bytes" about bytes it never had.
        return dict(_row(record.get("asset"), INCOMPARABLE, None, None,
                         "the reference carries no digest to compare bytes against"),
                    checked="madlad")
    api = ("https://huggingface.co/api/models/%s/tree/%s"
           % (fetch_madlad_model.REPO, record.get("kind") or "main"))
    try:
        tree = _json(api, opener, timeout)              # no token: public HF API, by design
    except Exception as exc:                            # noqa: BLE001 - reported as FAILED
        return dict(_row("model.bin", FAILED, (held or "")[:12], None,
                         "revision lookup failed: %s" % _why(exc)), checked="madlad")
    listed = {item.get("path"): item for item in tree if isinstance(item, dict)}
    entry = listed.get("model.bin") or {}
    lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else {}
    # D75: the field is `lfs.oid`. Reading `sha256` here looked plausible -- Hugging Face calls
    # it an oid -- and made every healthy install read as "upstream publishes no comparable
    # digest". It was never caught because this probe had not been run against the network yet.
    # Nothing is substituted for it: a non-LFS file's `oid` is a git sha1, which is not a sha256
    # and must not be compared against one.
    upstream = lfs.get("oid")
    if not upstream:
        return dict(_row("model.bin", FAILED, (held or "")[:12], None,
                         "upstream publishes no comparable digest for this revision"),
                    checked="madlad")
    if held and upstream != held:
        return dict(_row("model.bin", NEWER, held, upstream,
                         "the revision now serves different bytes"), checked="madlad")
    # Full digests travel in the row, not the 12-character display form: `--record-baseline`
    # stores this value and later compares it against upstream again, and a truncated digest
    # would never match a full one -- the alert would fire daily forever.
    return dict(_row("model.bin", CURRENT, held, upstream,
                     "revision still serves these bytes"), checked="madlad")


PROBES = {"argos": check_argos, "voice": check_voice, "madlad": check_madlad}


def default_targets(dest=None):
    """Where each asset's origin record lives -- derived from the fetch scripts' own rules.

    Argos keeps one directory per unpacked package and voice one per component, so a single
    "the argos directory" would look one level too high and answer "unrecorded" for a machine
    that has two installed packages. Each package directory becomes its own target; when none
    carries a record yet, one entry is still emitted so the tool reports the gap instead of
    printing nothing at all and looking like a clean run.
    """
    if dest:
        return [("recorded", check_argos, pathlib.Path(dest))]
    models = default_model_dir()
    entries = []
    for probe, root, label in ((check_argos, models, "argos"),
                              (check_voice, models.parent, "voice")):
        found = [sub for sub in sorted(root.glob("*/"))
                 if (sub / model_origin.ORIGIN_FILE).is_file()] if root.is_dir() else []
        if found:
            entries += [("%s:%s" % (label, sub.name), probe, sub) for sub in found]
        else:
            # Nothing recorded here yet: still ask, so the row says "unrecorded" rather than
            # disappearing from the report.
            entries.append((label, probe, root if label == "argos"
                            else root / fetch_voice_models.COMPONENTS["tts"][0]))
    entries.append(("madlad", check_madlad, default_madlad_model_dir()))
    return entries


def run(targets, opener=urllib.request.urlopen, token=None):
    return [dict(probe(dest, opener=opener, token=token), checked=name)
            for name, probe, dest in targets]


def verdict(states, skipped=()):
    """One place decides what the collection of rows means, so the exit code cannot drift.

    D74 lived in this function's first draft: `unrecorded` rows were not `failed`, so a run that
    compared nothing returned 0 and printed "no newer upstream version found". Silence about
    updates and an all-clear are different findings and must not share an exit code, so 0 now
    requires that at least one comparison actually happened.
    """
    if FAILED in states:
        return 1, "UNKNOWN -- a probe failed, so this run says nothing about updates"
    unverified = sorted(set(skipped)) or sorted(set(states) - set(COMPARABLE))
    if not (set(states) & set(COMPARABLE)):
        return 1, ('UNKNOWN -- nothing was compared (%s), which is not the same finding as '
                   '"no newer version"' % ", ".join(unverified))
    # The number stays, qualified on the same line: hiding a partial comparison behind a bare 0
    # is the failure this repo already wrote down for noise floors (D70's "keep the figure, put
    # the qualifier next to it").
    caveat = " (not compared: %s)" % ", ".join(unverified) if unverified else ""
    if NEWER in states:
        return 2, "at least one upstream publishes something newer" + caveat
    return 0, "no newer upstream version found" + caveat


def read_baseline(path=None):
    """(state, entries) for the git-tracked baseline: ok / missing / invalid -- never guessed.

    `missing` and `invalid` stay distinct from an empty result on purpose: a timer that could not
    read its own reference must not fall through to "up to date" (D74 again, one layer down).
    """
    path = pathlib.Path(path) if path else BASELINE_FILE
    if not path.is_file():
        return "missing", {"reason": "no baseline file at %s" % path.name}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return "invalid", {"reason": "unparsable: %s" % exc}
    entries = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(entries, dict) or not entries:
        return "invalid", {"reason": "the baseline names no assets"}
    return "ok", entries


def baseline_rows(entries, opener=urllib.request.urlopen, token=None):
    """Ask upstream about each baseline entry. Touches no disk state, so it runs anywhere.

    Each entry is handed to the family's own probe as the reference, which is what keeps one
    comparison path serving both an installed machine and a bare runner.
    """
    rows = []
    for key, entry in sorted(entries.items()):
        family, _, label = key.partition(":")
        probe = PROBES.get(family)
        if probe is None:
            rows.append(dict(_row(key, FAILED, None, None,
                                  "no probe is registered for asset family '%s'" % family),
                             checked=key))
            continue
        record = dict(entry)
        record.setdefault("asset", label or key)
        rows.append(dict(probe(None, opener=opener, record=record, token=token), checked=key))
    return rows


def to_baseline_entry(key, row, previous=None):
    """What to store for one row, or None when the row answered nothing.

    A failed or uncompared row is never written: recording "we could not look" as the reference
    would freeze a blind spot into the thing future runs compare against -- D34/D35's family.
    The `basis` on the row decides which field is written, because writing a file name into the
    `sha256` slot would create a reference that can never match anything again.
    """
    if row["state"] not in COMPARABLE or not row.get("upstream"):
        return None
    family, _, label = key.partition(":")
    entry = dict(previous or {})
    if family == "argos":
        entry.update({"asset": row.get("asset") or label, "version": row["upstream"]})
    elif family == "voice":
        if row.get("basis") == "name":      # they renamed the archive; the name is the finding
            entry.update({"asset": row["upstream"], "version": row["upstream"]})
        else:                               # a replaced archive under the same name
            entry.update({"asset": row.get("asset") or label, "sha256": row["upstream"]})
    elif family == "madlad":
        entry.update({"asset": row.get("asset") or label or "model.bin",
                      "sha256": row["upstream"]})
    else:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry.setdefault("first_seen_at", now)
    entry["observed_at"] = now
    return entry


def write_baseline(entries, updates_by_key, path=None):
    """Merge recorded answers into the baseline file. Returns how many entries changed.

    Only entries whose probe answered are touched; everything else keeps its previous value and
    timestamp, so a partial outage cannot silently rewrite the whole file.
    """
    path = pathlib.Path(path) if path else BASELINE_FILE
    merged = {key: dict(entry) for key, entry in entries.items()}
    changed = 0
    for row_key, row in updates_by_key.items():
        if row_key not in merged:
            continue                       # never invent a baseline line from a stray row
        entry = to_baseline_entry(row_key, row, merged.get(row_key))
        if entry and entry != merged[row_key]:
            merged[row_key] = entry
            changed += 1
    payload = {"note": ("The newest upstream state this repository has seen, so the question "
                        "'has an upstream moved' can be answered on a machine that has no model "
                        "installed. `sha256`/`version` in an entry that carries `observed_at` are "
                        "upstream's answers, NOT our declared pins -- those live in each fetch "
                        "script's EXPECTED_SHA256 (D69), and the two are deliberately separate "
                        "records. An entry without `observed_at` was seeded from a pin and must "
                        "name where in `seeded_from`; treat it as unconfirmed. Advanced only by an "
                        "explicit `--record-baseline`, never by the scheduled check, or the alert "
                        "would stop firing after its first hit."),
               "assets": dict(sorted(merged.items()))}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
                    encoding="utf-8", newline="\n")
    # `newline="\n"` is not decoration: without it Python translates every line ending to CRLF on
    # Windows, and this file is committed -- so each `--record-baseline` on a developer machine
    # would leave a whole-file diff for `.gitattributes` to normalise back (the line-ending trap
    # this repository has hit repeatedly, this time inside the code rather than in an editor).
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Report model assets whose upstream published "
                                             "a newer version (0 current, 2 newer, 1 cannot tell)")
    ap.add_argument("--dest", default=None, help="check one directory instead of the data dir")
    ap.add_argument("--only", default="argos,voice,madlad",
                    help="comma-separated subset of argos,voice,madlad")
    ap.add_argument("--baseline", action="store_true",
                    help="compare upstream against this repository's baseline instead of against "
                         "whatever happens to be installed here; this is the mode a scheduled "
                         "check must use, because a runner has no models")
    ap.add_argument("--baseline-file", default=None,
                    help="override the baseline path (tests; defaults to the file in script/)")
    ap.add_argument("--record-baseline", action="store_true",
                    help="with --baseline, write what upstream answered into the baseline file. "
                         "Never automatic: advancing the baseline is exactly what stops the alert "
                         "from repeating, so only a human deciding to accept a new version may do "
                         "it")
    ap.add_argument("--json", default=None,
                    help="also write the rows and the verdict to this path, for the alert step "
                         "that must decide without parsing a table")
    args = ap.parse_args(argv)

    wanted = [k.strip() for k in args.only.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in PROBES]
    if unknown:
        raise SystemExit("unknown asset(s): %s (choose from %s)"
                         % (", ".join(unknown), ", ".join(sorted(PROBES))))
    if args.record_baseline and not args.baseline:
        raise SystemExit("--record-baseline needs --baseline: the file records what upstream "
                         "answers, so filling it from one machine's install would turn the "
                         "repository's reference into a snapshot of somebody's disk")
    if args.baseline and args.dest:
        raise SystemExit("--baseline compares against the repository's reference, so --dest "
                         "cannot be used with it")

    entries = {}
    # Read once, here, and said out loud below: a host-exported GITHUB_TOKEN changes what GitHub
    # answers (anonymous callers on a shared IP get 403), and D45's lesson is that such a reader
    # must not be invisible to whoever is trying to reproduce a result.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
    if args.baseline:
        state, payload = read_baseline(args.baseline_file)
        if state != "ok":
            # Refusing rather than answering 0 is the point: a timer that cannot read its own
            # reference has not checked anything (D74).
            print("verdict: UNKNOWN -- baseline %s" % payload["reason"])
            return 1
        entries = {key: entry for key, entry in payload.items()
                   if key.split(":", 1)[0] in wanted}
        if not entries:
            print("verdict: UNKNOWN -- the baseline holds no asset matching --only %s"
                  % args.only)
            return 1
        results = baseline_rows(entries, token=token)
    elif args.dest:
        if len(wanted) != 1:
            # One directory holds one asset, so guessing among several --only values would print a
            # verdict nobody asked for. Refuse and say why instead.
            raise SystemExit("--dest takes exactly one --only asset, got: %s" % ", ".join(wanted))
        targets = [(wanted[0], PROBES[wanted[0]], pathlib.Path(args.dest))]
        results = run(targets, token=token)
    else:
        targets = [(name, probe, dirpath) for name, probe, dirpath in default_targets()
                   if name.split(":")[0] in wanted]
        results = run(targets, token=token)

    if not results:
        print("verdict: nothing to check -- no asset matched --only")
        return 1
    for row in results:
        print("%-22s %-11s local=%-13s upstream=%-13s %s" % (
            row["checked"], row["state"], str(row["local"] or "-")[:13],
            str(row["upstream"] or "-")[:13], row["detail"]), flush=True)
    skipped = [row["checked"] for row in results if row["state"] not in COMPARABLE]
    code, message = verdict({row["state"] for row in results}, skipped)
    print("verdict: %s" % message)
    if token:
        print("note: sent the host's GITHUB_TOKEN to api.github.com only (argos/madlad probes "
              "never receive it)")

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"verdict": {"code": code, "message": message},
                                   "rows": results}, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8", newline="\n")

    if args.record_baseline:
        changed = write_baseline(entries, {row["checked"]: row for row in results},
                                 path=args.baseline_file)
        print("baseline: %d of %d entr%s updated" % (
            changed, len(entries), "y" if len(entries) == 1 else "ies"))
    return code


if __name__ == "__main__":
    sys.exit(main())
