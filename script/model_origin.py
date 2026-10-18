# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Record which version of which model actually landed on disk (defect GA-13's missing half).

The repo already knows how to check *bytes*: `net_guard` refuses a download whose sha256 does
not match the pin (D69). What nothing recorded is the version question -- "which release is this
directory holding, and when did we fetch it". Without that, an operator cannot tell whether an
upgrade happened, and `check_model_updates.py` has nothing to compare upstream against.

Two rules keep this from becoming a second source of truth:

  * The digest written here is the one `net_guard` just computed from the bytes on disk, not a
    copy of the pin. `tests/test_model_updates.py` (`ShippedBaselineTests`) then asserts the
    baseline's recorded digest agrees with the declared pin, or that the asset is on the
    unpinned list -- a comparison that is only possible because the two are separate records, and
    one that fails on purpose when somebody accepts a new version without re-verifying its pin.
  * Only facts go in: the asset name, the version string upstream used to describe it, the
    digest, the URL it came from, the UTC instant of the fetch. No machine path -- the same
    reason D70 hashes artefacts instead of writing filenames into evidence.

Versions are spelled differently by each upstream, and that is recorded rather than smoothed
over: Argos names a `package_version` per index entry, the sherpa-onnx assets carry their
version inside the file name, and MADLAD is fetched from a git revision whose contents move, so
its version is the revision plus the digest actually received.

One record *per asset*, in one file (defect D81, 2026-10-18). The first version wrote a single
object per directory, and `fetch_madlad_model.py` installs four assets into one directory -- so
the fourth write destroyed the first three, and the surviving record claimed the directory was
`config.json`. `read()` answered `ok` about that. A record whose key is the directory can only
be right when the directory holds one asset, which is an accident of two of the three layouts.

Format 2 keeps one file (it is the unit a person copies or deletes, and one file per asset
would leave stale orphans behind when an asset is renamed) and keys the entries:

    {"format": 2, "assets": {"model.bin": {...}, "config.json": {...}}}

Legacy format-1 files -- a bare object -- stay readable, because they exist on installed
machines; a reader that refused them would report "no record" for a directory that has one.
"""
import datetime
import hashlib
import json
import pathlib

ORIGIN_FILE = "wonslate-origin.json"
SCHEMA = ("asset", "version", "sha256", "url", "fetched_at", "fetcher")
#: Format 2 is the keyed map. Format 1 (a bare single-object file) is still read, never written.
FORMAT = 2
#: Server statements that let a later `--force` ask "is this still the same?" instead of pulling
#: gigabytes again. Optional: not every upstream publishes them, and `None` means "this asset has
#: no validator", which the conditional path must then treat as "we have to fetch it".
VALIDATOR_KEYS = ("etag", "last_modified")


class OriginError(ValueError):
    """The record on disk is not usable. Said loudly rather than repaired by guessing."""


def _as_map(payload):
    """Normalise either on-disk shape to (format, {asset: entry})."""
    if isinstance(payload, dict) and isinstance(payload.get("assets"), dict):
        return payload.get("format", 2), dict(payload["assets"])
    if isinstance(payload, dict) and "asset" in payload:
        # A format-1 file names its asset once, in the body. Trust that name over the key we
        # happened to look under, so a record is never silently filed somewhere else.
        name = payload.get("asset") or ""
        return 1, {name: payload}
    return None, {}


def record(dest_dir, *, asset, version, sha256, url, fetcher, kind=None, validators=None):
    """Write what landed for ONE asset, keeping the other assets' records in the same file.

    Read-modify-write rather than replace: that merge is the whole point of format 2, and the
    reason D81 happened is that these three callers write one file several times per run.

    `validators` carries what the server said (`etag` / `last_modified`) so a later run can ask a
    conditional question instead of downloading again. Storing them is only honest while something
    reads them back -- `net_guard.fetch` does, on the `--force` path.
    """
    payload = {
        "asset": asset,
        # Upstream's own spelling of the version. `None` is allowed and means "the upstream
        # does not name one" -- which check_model_updates then reports as UNKNOWN rather than
        # inventing a comparison.
        "version": version,
        "sha256": sha256,
        "url": url,
        "fetcher": fetcher,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if kind:
        payload["kind"] = kind
    # `validators=None` means "this write knows nothing about the server's answer", which is not
    # the same as `{}` ("the server published none"). Only the second one is allowed to clear what
    # is stored: a cache-hit write lands here with None, the bytes were just digest-verified, so
    # whatever the server last said about them still describes them. Dropping those validators
    # would silently cost the next `--force` its conditional question -- D80's lesson that a
    # cleanup path must not delete the state a later step reads, in a new costume.
    carry_over = validators is None
    given = dict(validators or {})
    payload.update({key: value for key, value in given.items() if value})
    dest = pathlib.Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    state, doc = read(dest_dir, asset=None, allow_ambiguous=True)
    if state == "incomplete":
        # Merging into a file we cannot parse would silently drop every sibling record, which is
        # the same loss D81 was -- so refuse and let the operator decide. The file is named in the
        # message because the fix is about that one file, not about the whole model directory.
        raise OriginError("%s is not readable (%s); refusing to overwrite it with a single asset. "
                          "Delete or repair that file, then fetch again."
                          % (dest / ORIGIN_FILE, doc))
    _used, entries = _as_map(doc)
    if carry_over:
        previous = entries.get(payload["asset"]) or {}
        for key in VALIDATOR_KEYS:
            if previous.get(key):
                payload.setdefault(key, previous[key])
    entries[payload["asset"]] = payload
    doc = {"format": FORMAT, "assets": entries}
    (dest / ORIGIN_FILE).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")      # LF on every platform, as for the committed baseline
    return payload


def write_validators(dest_dir, asset, validators):
    """Refresh only the server statements of one existing entry, and say if there is no entry.

    A 304 proves the bytes are unchanged; it does not produce a new digest or a new fetch, so
    the record's `fetched_at` must stay where it was. Rewriting the whole entry from the caller
    would have had it "fetched" again at a time nothing was fetched.
    """
    path = pathlib.Path(dest_dir) / ORIGIN_FILE
    if not path.is_file():
        return False
    state, doc = read(dest_dir, asset=None, allow_ambiguous=True)
    _used, entries = _as_map(doc)
    record_entry = entries.get(asset)
    if state != "ok" or record_entry is None:
        # Nothing to refresh: no file, an unreadable one, or a file that does not describe this
        # asset. Pretending otherwise would report a confirmed 304 for bytes nobody has a record of.
        return False
    for key in VALIDATOR_KEYS:
        # Only the keys this response actually spoke about are touched. A 304 carrying an `etag` but
        # no `Last-Modified` has not invalidated the old one -- it said the resource has not changed,
        # which makes every validator we already hold more true, not less. Dropping it here would
        # cost the conditional question one way to be answered and make the next fetch look like a
        # cache miss. (`record()` does clear on an empty dict, for the opposite reason: there the
        # bytes are new, so the old statements describe a different object.)
        value = (validators or {}).get(key)
        if value:
            record_entry[key] = value
    path.write_text(json.dumps({"format": FORMAT, "assets": entries}, indent=2,
                               ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return True


def assets(dest_dir):
    """{asset: entry} for every record in this directory's file; {} when there is none."""
    state, doc = read(dest_dir, asset=None, allow_ambiguous=True)
    _used, entries = _as_map(doc)
    return entries if state == "ok" else {}


def conditional(dest_dir, asset):
    """(validators, recorded_sha256) for asking "is this still current?", or ({}, None).

    Empty means *we cannot ask*, which is not the same as "ask and it will fail": the caller
    downloads in full. Reasons to come back empty -- no record, a record that does not name this
    asset (D81), a record with no server validator. Hugging Face publishes an `etag` for LFS
    objects and GitHub publishes one for release assets, so this is usually available where it
    matters, and it is never invented.
    """
    state, entry = read(dest_dir, asset=asset)
    if state != "ok":
        return {}, None
    validators = {key: entry[key] for key in VALIDATOR_KEYS if entry.get(key)}
    return validators, entry.get("sha256")


def matches_record(path, sha256):
    """Does the file on disk hash to what the record says? False means "do not trust a 304".

    A 304 confirms that the *server's* copy has not changed since we asked. It says nothing about
    whether our copy is the one we asked about -- so after a 304 the local bytes are checked against
    the recorded digest here, and a mismatch sends the caller back to fetching. Hashing a local
    gigabyte is seconds; downloading it again is minutes, which is why this order is worth it.
    """
    if not sha256:
        return False
    try:
        handle = open(path, "rb")
    except OSError:
        return False
    digest = hashlib.sha256()
    with handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest().lower() == str(sha256).lower()


def read(dest_dir, asset=None, allow_ambiguous=False):
    """(state, payload) for a directory, optionally for one named asset inside it.

    States are deliberately many, because they lead to different actions:

      ok          the entry (as `payload`) exists and has every SCHEMA field
      missing     no file here -- "run the fetch script"
      incomplete  a file we cannot trust (unparsable, or an entry missing required fields)
      absent      the file is fine but does not name that asset -- asking about `model.bin` and
                  getting `config.json` is the D81 failure, so this is its own answer
      ambiguous   no asset was named and the file holds several; picking one would be a guess,
                  unless `allow_ambiguous` says the caller wants the whole map

    `incomplete` is its own answer on purpose: a record missing its version is not "up to
    date", and treating a partial record as a usable one is how an upgrade check starts
    reporting green for a directory nobody verified (D57/D58's family again).
    """
    path = pathlib.Path(dest_dir) / ORIGIN_FILE
    if not path.is_file():
        return "missing", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return "incomplete", {"reason": "unparsable: %s" % exc}
    if not isinstance(payload, dict):
        return "incomplete", {"reason": "not an object"}
    used, mapped = _as_map(payload)
    if used is None:
        return "incomplete", {"reason": "neither a keyed map nor a single record"}
    if allow_ambiguous:
        return "ok", payload
    if asset is None:
        if len(mapped) > 1:
            return "ambiguous", {"assets": sorted(mapped),
                                 "reason": "this directory holds several assets; name one"}
        asset = next(iter(mapped), "")
    entry = mapped.get(asset)
    if entry is None:
        return "absent", {"asked": asset, "recorded": sorted(mapped)}
    absent = [key for key in SCHEMA if entry.get(key) in (None, "")]
    if "sha256" in absent:
        return "incomplete", {"reason": "no digest recorded", "absent": absent}
    return ("ok", entry) if not absent else ("incomplete",
                                             {"absent": absent, "payload": entry})
