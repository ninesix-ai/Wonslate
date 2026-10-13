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
    for path in bad:
        print("DRIFT: {} is missing or does not match the derivation".format(
            path.relative_to(REPO)), file=sys.stderr)
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
            pack_path.relative_to(REPO), kept, dropped))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report missing or stale derived files; write nothing")
    args = parser.parse_args(argv)
    return check() if args.check else write()


if __name__ == "__main__":
    sys.exit(main())
