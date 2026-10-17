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
"""
import datetime
import json
import pathlib

ORIGIN_FILE = "wonslate-origin.json"
SCHEMA = ("asset", "version", "sha256", "url", "fetched_at", "fetcher")


class OriginError(ValueError):
    """The record on disk is not usable. Said loudly rather than repaired by guessing."""


def record(dest_dir, *, asset, version, sha256, url, fetcher, kind=None):
    """Write the origin file next to what was installed. Returns the payload written."""
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
    dest = pathlib.Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ORIGIN_FILE).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")      # LF on every platform, as for the committed baseline
    return payload


def read(dest_dir):
    """(state, payload) where state is one of ok / missing / incomplete.

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
    absent = [key for key in SCHEMA if payload.get(key) in (None, "")]
    if "sha256" in absent:
        return "incomplete", {"reason": "no digest recorded", "absent": absent}
    return ("ok", payload) if not absent else ("incomplete",
                                              {"absent": absent, "payload": payload})
