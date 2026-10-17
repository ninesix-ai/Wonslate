# -*- coding: utf-8 -*-
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

  0  nothing newer is published (or the directory simply has no origin record yet)
  2  at least one upstream publishes something newer
  1  at least one probe failed -- no network, no permission, unusable record

Probes take an `opener`, so they are testable with canned answers. This script deliberately does
not run in the gate: a check that needs the network to pass goes red on every sandboxed runner and
then gets switched off, which is D28's and D65's lesson. `tests/test_model_updates.py` covers the
classification logic offline and asserts that the three entry points keep using it.
"""
import argparse
import json
import pathlib
import sys
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

USER_AGENT = "wonslate-check-updates/1.0"


def _json(url, opener, timeout=30):
    """One read-only upstream lookup. Anything that goes wrong raises: the caller says FAILED."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _row(asset, state, local, upstream, detail):
    return {"asset": asset, "state": state, "local": local, "upstream": upstream,
            "detail": detail}


def _start(dest_dir):
    """Read the record once and turn its three possible states into an early row or the payload."""
    state, payload = model_origin.read(dest_dir)
    if state == "missing":
        return None, _row(None, UNRECORDED, None, None,
                          "no origin record here; run the fetch script to create one")
    if state == "incomplete":
        return None, _row(None, FAILED, None, None,
                          "origin record is unusable: %s" % payload)
    return payload, None


def check_argos(dest_dir, opener=urllib.request.urlopen, timeout=30):
    """Argos versions come from the index's own `package_version` field."""
    record, early = _start(dest_dir)
    if early:
        return dict(early, checked="argos")
    url = fetch_argos_models.INDEX_URLS[0]
    try:
        entries = _json(url, opener, timeout)
    except Exception as exc:                                  # noqa: BLE001 - reported as FAILED
        return dict(_row(record.get("asset"), FAILED, record.get("version"), None,
                         "index lookup failed: %s" % type(exc).__name__), checked="argos")
    by_code = {"{}_{}".format(entry.get("from_code"), entry.get("to_code")):
               entry.get("package_version") for entry in entries}
    held = record.get("version")
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


def check_voice(dest_dir, opener=urllib.request.urlopen, timeout=30):
    """The voice assets carry their version in the file name, under long-lived release tags."""
    record, early = _start(dest_dir)
    if early:
        return dict(early, checked="voice")
    held = record.get("asset")
    # The API root is derived from the release-download URL the fetch script already uses, so
    # this keeps pointing at the same project even after someone edits that constant -- and it
    # avoids a second copy of the URL here to drift out of step with it.
    project = fetch_voice_models.RELEASE.split("/releases/")[0]
    try:
        release = _json(project + "/releases/latest", opener, timeout)
    except Exception as exc:                                  # noqa: BLE001 - reported as FAILED
        return dict(_row(held, FAILED, held, None,
                         "release lookup failed: %s" % type(exc).__name__), checked="voice")
    names = [a.get("name") for a in release.get("assets", []) if isinstance(a, dict)]
    stems = (held or "").replace(".tar.bz2", "").split("-")[:2]
    kin = [n for n in names if n and n.split("-")[:2] == stems]
    upstream = kin[0] if kin else None
    if upstream and upstream != held:
        return dict(_row(held, NEWER, held, upstream, "the release lists another file"),
                    checked="voice")
    if upstream is None:
        return dict(_row(held, FAILED, held, None,
                         "the latest release lists no asset for this component"), checked="voice")
    return dict(_row(held, CURRENT, held, upstream, "release still lists this asset"),
                checked="voice")


def check_madlad(dest_dir, opener=urllib.request.urlopen, timeout=30):
    """MADLAD has no version scheme, so the signal is bytes under a moving revision."""
    record, early = _start(dest_dir)
    if early:
        return dict(early, checked="madlad")
    held = record.get("sha256")
    api = ("https://huggingface.co/api/models/%s/tree/%s"
           % (fetch_madlad_model.REPO, record.get("kind") or "main"))
    try:
        tree = _json(api, opener, timeout)
    except Exception as exc:                                  # noqa: BLE001 - reported as FAILED
        return dict(_row("model.bin", FAILED, (held or "")[:12], None,
                         "revision lookup failed: %s" % type(exc).__name__), checked="madlad")
    listed = {item.get("path"): item for item in tree if isinstance(item, dict)}
    entry = listed.get("model.bin") or {}
    lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else {}
    upstream = lfs.get("sha256") or entry.get("sha256")
    if not upstream:
        return dict(_row("model.bin", FAILED, (held or "")[:12], None,
                         "upstream publishes no comparable digest for this revision"),
                    checked="madlad")
    if held and upstream != held:
        return dict(_row("model.bin", NEWER, held[:12], upstream[:12],
                         "the revision now serves different bytes"), checked="madlad")
    return dict(_row("model.bin", CURRENT, (held or "")[:12], upstream[:12],
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


def run(targets, opener=urllib.request.urlopen):
    return [dict(probe(dest, opener=opener), checked=name) for name, probe, dest in targets]


def verdict(states):
    """One place decides what the collection of rows means, so the exit code cannot drift."""
    if FAILED in states:
        return 1, "UNKNOWN -- a probe failed, so this run says nothing about updates"
    if NEWER in states:
        return 2, "at least one upstream publishes something newer"
    return 0, "no newer upstream version found"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Report model assets whose upstream published "
                                             "a newer version (0 current, 2 newer, 1 cannot tell)")
    ap.add_argument("--dest", default=None, help="check one directory instead of the data dir")
    ap.add_argument("--only", default="argos,voice,madlad",
                    help="comma-separated subset of argos,voice,madlad")
    args = ap.parse_args(argv)

    wanted = [k.strip() for k in args.only.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in PROBES]
    if unknown:
        raise SystemExit("unknown asset(s): %s (choose from %s)"
                         % (", ".join(unknown), ", ".join(sorted(PROBES))))

    if args.dest:
        if len(wanted) != 1:
            # One directory holds one asset, so guessing among several --only values would print a
            # verdict nobody asked for. Refuse and say why instead.
            raise SystemExit("--dest takes exactly one --only asset, got: %s" % ", ".join(wanted))
        targets = [(wanted[0], PROBES[wanted[0]], pathlib.Path(args.dest))]
    else:
        targets = [(name, probe, dirpath) for name, probe, dirpath in default_targets()
                   if name.split(":")[0] in wanted]

    results = run(targets)
    if not results:
        print("verdict: nothing to check -- no asset matched --only")
        return 1
    for row in results:
        print("%-22s %-11s local=%-13s upstream=%-13s %s" % (
            row["checked"], row["state"], str(row["local"] or "-")[:13],
            str(row["upstream"] or "-")[:13], row["detail"]), flush=True)
    code, message = verdict({row["state"] for row in results})
    print("verdict: %s" % message)
    return code


if __name__ == "__main__":
    sys.exit(main())
