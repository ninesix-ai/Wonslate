#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Derive the reverse-direction AV corpus and seed pack from the forward ones (S12 T2).

Why a script instead of a committed copy: `av-en-zh` is, by construction, the same sentence
pairs with the two columns swapped, and the same AV terms with the two sides swapped.
Hand-copying either would create a second thing to maintain and a first thing to drift --
the failure mode this repo keeps meeting, where an artefact quietly stops matching the
thing it came from.

Both halves matter for the measurement to mean anything:

  * the corpus gives the two arms rows to translate;
  * the seed pack is the *treatment*. Without pack rows for a direction the scoped arm
    injects nothing, the delta is 0 by construction, and the report would read
    "terminology does not help here" when the truth is "nothing was given to help".
    `run_av_baseline.py` refuses that in preflight rather than letting it pass as a result.

Computing and writing are separate so `--check` can verify without touching the tree --
a checker that rewrites what it is checking can only ever agree with itself.

Usage:
    python script/derive_av_direction.py            # write the derived artefacts
    python script/derive_av_direction.py --check    # verify they match; writes nothing
Exit: 0 clean, 1 drift or missing source.
"""
import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
EVAL_DATA = REPO / "script" / "eval-data"
PACKS = REPO / "docs" / "glossary-packs"
RANGES = REPO / "script" / "av_domain_ranges.json"

# direction to derive -> the direction it comes from
DERIVED = {"en-zh": "zh-en"}


def _rows(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def corpus_text(from_direction, to_direction):
    """The swapped columns, as (src_text, tgt_text, rows). Raises on a misaligned source."""
    prefix_from = "av-{}".format(from_direction)
    src_dir = EVAL_DATA / prefix_from
    forward_src = _rows(src_dir / (prefix_from + ".src"))
    forward_tgt = _rows(src_dir / (prefix_from + ".tgt"))
    if len(forward_src) != len(forward_tgt):
        raise SystemExit("{} has {} src vs {} tgt; refusing to derive a misaligned pair"
                         .format(prefix_from, len(forward_src), len(forward_tgt)))
    return ("".join(l + "\n" for l in forward_tgt),
            "".join(l + "\n" for l in forward_src),
            len(forward_tgt))


def pack_payload(from_direction, to_direction):
    """The reversed pack, as (path, payload, kept_rows, dropped_rows)."""
    src_path = PACKS / "av-{}.json".format(from_direction)
    record = json.loads(src_path.read_text(encoding="utf-8"))
    entries = record.get("entries") or []
    reversed_entries = [{"source_term": e.get("target_term"),
                         "target_term": e.get("source_term")}
                        for e in entries if e.get("source_term") and e.get("target_term")]
    if not reversed_entries:
        raise SystemExit("nothing to reverse in {}".format(src_path))
    payload = {
        "domain": record.get("domain", "av"),
        "source_lang": record.get("target_lang"),
        "target_lang": record.get("source_lang"),
        "version": record.get("version"),
        "license": record.get("license", "Apache-2.0"),
        "attribution": ("Mechanically reversed from av-{d}.json by "
                        "script/derive_av_direction.py: the same rows, columns swapped. "
                        "License inherits the source pack's Apache-2.0 / MIT provenance."
                        ).format(d=from_direction),
        "notes": ("Do not hand-edit: fix av-{d}.json and re-run the derivation. "
                  "glossary_import_pack stamps source=\"seed:<domain>\" and defaults "
                  "confidence to 1.0 on import.").format(d=from_direction),
        "entries": reversed_entries,
    }
    return (PACKS / "av-{}.json".format(to_direction), payload,
            len(reversed_entries), len(entries) - len(reversed_entries))


def _same_entries(path, payload) -> bool:
    """Compare entry content only: prose fields may legitimately differ by hand."""
    if not path.is_file():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("entries") == payload["entries"]
    except (ValueError, OSError):
        return False


def forward_groups(from_direction):
    """The forward direction's sub-domain groups, as loaded from the ranges file.

    A group is a span of corpus ROW numbers, so a derived direction may reuse them only because
    its corpus is the same rows in the same order -- which this very script guarantees when it
    writes the columns. Owning that copy here is what keeps it from drifting: an out-of-date list
    is a DRIFT that --check reports, instead of a label quietly describing different sentences
    (defect D62).
    """
    doc = json.loads(RANGES.read_text(encoding="utf-8"))
    groups = doc.get("ranges")
    if not isinstance(groups, dict):
        raise SystemExit("{}: 'ranges' must map direction -> list of groups".format(RANGES))
    if from_direction not in groups:
        raise SystemExit("{} declares no groups for the source direction {}".format(
            RANGES.name, from_direction))
    return groups[from_direction]


def _same_groups(to_direction, want) -> bool:
    try:
        doc = json.loads(RANGES.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return (doc.get("ranges") or {}).get(to_direction) == want


def _display(path):
    """A printable path that never fails: relative inside the repo, absolute anywhere else.

    `relative_to` raises ValueError off-repo. Both writers here print progress, and a formatter
    that crashes mid-run leaves the derivation half-done while reporting an error unrelated to
    the work -- the same defect `check()` had, found when a test drove the script against a
    temporary tree.
    """
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def check() -> int:
    """Report every derived artefact that is missing or out of date; write nothing."""
    bad = []
    for to_direction, from_direction in DERIVED.items():
        prefix_to = "av-{}".format(to_direction)
        src_text, tgt_text, _rows_count = corpus_text(from_direction, to_direction)
        for suffix, want in ((".src", src_text), (".tgt", tgt_text)):
            path = EVAL_DATA / prefix_to / (prefix_to + suffix)
            if not path.is_file() or path.read_text(encoding="utf-8") != want:
                bad.append(path)
        pack_path, payload, _kept, _dropped = pack_payload(from_direction, to_direction)
        if not _same_entries(pack_path, payload):
            bad.append(pack_path)
        want_groups = forward_groups(from_direction)
        if not _same_groups(to_direction, want_groups):
            bad.append(RANGES)
    for path in bad:
        # A moved or temporary ranges file still has to be reportable: relative_to raises
        # ValueError off-repo, which used to turn "found drift" into a crash inside the
        # reporter -- the worst place for a formatter to fail (a test hit exactly this).
        print("DRIFT: {} is missing or does not match the derivation".format(_display(path)),
              file=sys.stderr)
    if bad:
        return 1
    print("derived artefacts match their sources")
    return 0


def write() -> int:
    for to_direction, from_direction in DERIVED.items():
        prefix_to = "av-{}".format(to_direction)
        src_text, tgt_text, rows = corpus_text(from_direction, to_direction)
        out = EVAL_DATA / prefix_to
        out.mkdir(parents=True, exist_ok=True)
        (out / (prefix_to + ".src")).write_text(src_text, encoding="utf-8")
        (out / (prefix_to + ".tgt")).write_text(tgt_text, encoding="utf-8")
        pack_path, payload, kept, dropped = pack_payload(from_direction, to_direction)
        pack_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print("av-{}: {} rows, columns swapped from av-{}".format(to_direction, rows,
                                                                 from_direction))
        print("{}: {} term rows reversed ({} dropped for a missing side)".format(
            _display(pack_path), kept, dropped))
        want_groups = forward_groups(from_direction)
        if _same_groups(to_direction, want_groups):
            print("{}: groups for {} already match av-{}".format(
                RANGES.name, to_direction, from_direction))
        else:
            doc = json.loads(RANGES.read_text(encoding="utf-8"))
            doc["ranges"][to_direction] = json.loads(json.dumps(want_groups, ensure_ascii=False))
            RANGES.write_bytes((json.dumps(doc, ensure_ascii=False, indent=2)
                                + "\n").encode("utf-8"))
            print("{}: declared {} groups for {} (copy of av-{}, same row order)".format(
                RANGES.name, len(want_groups), to_direction, from_direction))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report missing or stale derived files; write nothing")
    args = parser.parse_args(argv)
    return check() if args.check else write()


if __name__ == "__main__":
    sys.exit(main())
