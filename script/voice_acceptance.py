#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""voice_acceptance.py -- walk the M4 microphone acceptance and record what a human saw.

Why this exists. M4's capture code is unit-tested with an injected device, and a one-off probe
proved the NAudio path runs on this host (2 devices, 6338 bytes, non-zero peak) -- but neither can
answer the question the product actually needs: *does speaking into a real microphone produce the
right text, translation and audio?* That answer only comes from a person in front of the app. This
tool makes that person's job five minutes: it launches the app, asks one question per observable
step, and writes the answers into an evidence file that the ledger can cite.

Two rules make it trustworthy:

  * **No explicit "yes" means no pass.** An unanswered or skipped question is `incomplete`, never a
    silent yes (D74: "nothing was checked" must not read as "everything is fine"). Only every
    question answered `yes` exits 0.
  * **It records, it does not judge the product.** The questions are observables ("the ASR box shows
    what you said"), not the tool's opinion; a `no` is written verbatim next to the question so the
    next round can act on it. The tool never opens the microphone, the models or the engine itself.

It deliberately does **not** re-implement `VoiceModelLocator`'s resolution rules: the voice panel
already reports missing models, and a second copy in Python would be the repo's oldest failure shape
(two rules that agree until they do not -- D64). If the panel says models are missing, the checklist
tells the operator to run `fetch_voice_models.py` and stop.

Usage:
    python script/voice_acceptance.py                    # launch the app, ask the questions
    python script/voice_acceptance.py --no-launch        # ask only (app already open / CI dry runs)
    python script/voice_acceptance.py --answers a.json   # non-interactive (scripted answers)
    python script/voice_acceptance.py --out path.json    # default: docs/evidence/voice-m4-acceptance-<utc>.json
    python script/voice_acceptance.py --force            # overwrite an existing --out

Exit codes: 0 = every question answered yes; 1 = failed or incomplete; 2 = refused / cannot run.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_APP = ROOT / "Wonslate.UI" / "bin" / "Release" / "net10.0-windows" / "Wonslate.exe"
EVIDENCE_DIR = ROOT / "docs" / "evidence"
FORMAT = 1

#: One entry per observable. Ids are the keys an --answers file uses; the text is what the operator
#: is asked to look at, kept close to the UI's own wording so it can be checked at a glance.
QUESTIONS = (
    ("models_ready",
     "语音页签顶部显示“语音模型已就绪”（而不是列出缺失模型）"),
    ("record_started",
     "点“开始录音”后，状态行变为“正在录音…说完点‘停止并翻译’”"),
    ("record_stopped",
     "点“停止并翻译”后，状态行显示录音时长；若设备不是 16 kHz，还写明“… Hz → 16 kHz”"),
    ("asr_text",
     "“识别文本（ASR）”显示了你刚才说出的中文内容（意思一致即可，不必逐字）"),
    ("translation",
     "“译文”是目标语言，且意思与你说的一致"),
    ("playback",
     "点“播放译文”能听到目标语言的语音"),
    ("timings",
     "耗时行显示了 VAD / ASR / 翻译 / TTS / 首段可播 / 合计 六个数字"),
    ("no_unexpected_notes",
     "说明区没有意料之外的降级信息（有就如实答 no，并在备注里写清）"),
)
QUESTION_IDS = tuple(q[0] for q in QUESTIONS)
RESULTS = ("yes", "no", "skip")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _sha256(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path):
    """{name, sha256} for a build artefact, or None. Basename only -- never a machine path (D70)."""
    path = pathlib.Path(path)
    if not path.is_file():
        return None
    return {"name": path.name, "sha256": _sha256(path)}


def _normalise_answers(raw):
    """Turn the --answers mapping into {id: {'result', 'note'}}, or raise ValueError.

    Unknown ids are refused rather than ignored: a typo'd key would otherwise leave the real
    question unanswered while looking like it was answered (the "unanswered is not yes" rule).
    """
    if not isinstance(raw, dict):
        raise ValueError("answers must be a JSON object")
    mapping = raw.get("answers", raw)
    if not isinstance(mapping, dict):
        raise ValueError("'answers' must be an object")
    unknown = sorted(set(mapping) - set(QUESTION_IDS))
    if unknown:
        raise ValueError("unknown question id(s): %s" % ", ".join(unknown))
    out = {}
    for qid, value in mapping.items():
        if isinstance(value, str):
            result, note = value, ""
        elif isinstance(value, dict):
            result, note = value.get("result"), value.get("note", "")
        else:
            raise ValueError("%s: answer must be a string or {result, note}" % qid)
        if not isinstance(result, str) or result.strip().lower() not in RESULTS:
            raise ValueError("%s: result must be one of %s (got %r)"
                             % (qid, "/".join(RESULTS), result))
        out[qid] = {"result": result.strip().lower(), "note": note if isinstance(note, str) else ""}
    return out, (raw.get("notes") if isinstance(raw.get("notes"), str) else "")


def _ask_interactively():
    answers = {}
    for qid, text in QUESTIONS:
        while True:
            reply = input("  %s\n    [y]通过 / [n]失败 / [s]跳过，备注可选: " % text).strip().lower()
            if reply in ("y", "yes"):
                answers[qid] = {"result": "yes", "note": ""}
                break
            if reply in ("n", "no"):
                note = input("    备注（哪里不对）: ").strip()
                answers[qid] = {"result": "no", "note": note}
                break
            if reply in ("s", "skip"):
                note = input("    备注（为什么跳过）: ").strip()
                answers[qid] = {"result": "skip", "note": note}
                break
            print("    请先明确回答 y / n / s ——空回车不算通过。")
    return answers, ""


def _verdict(answers):
    """(verdict, failed_ids, skipped_ids, unanswered_ids) -- only all-yes is 'passed'."""
    failed = [q for q in QUESTION_IDS if answers.get(q, {}).get("result") == "no"]
    skipped = [q for q in QUESTION_IDS if answers.get(q, {}).get("result") == "skip"]
    unanswered = [q for q in QUESTION_IDS if q not in answers]
    if failed:
        return "failed", failed, skipped, unanswered
    if skipped or unanswered:
        return "incomplete", failed, skipped, unanswered
    return "passed", failed, skipped, unanswered


def accept(app, out, *, answers, notes="", launch=None, force=False, now=None):
    """Run one acceptance and write its evidence. Returns (verdict, exit_code).

    `app` must exist before anything is asked: accepting an unbuilt tree would produce evidence
    about nothing. `launch(app_path)` is injected so tests never start a GUI.
    """
    app = pathlib.Path(app)
    if not app.is_file():
        print("!! 找不到应用：%s\n   先运行 python script/build.py（或 build.bat），再试。"
              % app, file=sys.stderr)
        return "refused", 2
    out = pathlib.Path(out)
    if out.exists() and not force:
        print("!! %s 已存在；拒绝覆盖证据文件（确要覆盖请加 --force）" % out, file=sys.stderr)
        return "refused", 2

    if launch is not None:
        launch(str(app))
        print("已启动应用：%s\n请切到“语音（离线）”页签，按提示逐项确认。" % app.name)

    verdict, failed, skipped, unanswered = _verdict(answers)
    payload = {
        "format": FORMAT,
        "generated_at": (now or _now()).isoformat(),
        "app": _identity(app),
        "engine_lib": _identity(app.parent / "translator_engine.dll"),
        "questions": [{"id": qid, "text": text} for qid, text in QUESTIONS],
        "answers": {
            qid: {"text": dict(QUESTIONS)[qid],
                  "result": answers.get(qid, {}).get("result", "unanswered"),
                  "note": answers.get(qid, {}).get("note", "")}
            for qid, _ in QUESTIONS
        },
        "failed": failed,
        "skipped": skipped,
        "unanswered": unanswered,
        "notes": notes,
        "verdict": verdict,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")

    print("\n结果：%s" % verdict)
    if failed:
        print("  失败项：%s" % "、".join(failed))
    if skipped:
        print("  跳过项（不算通过）：%s" % "、".join(skipped))
    if unanswered:
        print("  未回答（不算通过）：%s" % "、".join(unanswered))
    print("  证据：%s" % out)
    return verdict, 0 if verdict == "passed" else 1


def _load_answers(path):
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read answers %s: %s" % (path, exc))
    return _normalise_answers(raw)


def main(argv=None, *, stdin_isatty=None):
    parser = argparse.ArgumentParser(
        description="Walk the M4 microphone acceptance and record the answers (never guesses pass).")
    parser.add_argument("--app", default=str(DEFAULT_APP),
                        help="built Wonslate.exe (default: %s)" % DEFAULT_APP)
    parser.add_argument("--out", default=None,
                        help="evidence path (default: docs/evidence/voice-m4-acceptance-<utc>.json)")
    parser.add_argument("--answers", default=None,
                        help="JSON file with scripted answers (non-interactive)")
    parser.add_argument("--no-launch", action="store_true", help="do not start the app")
    parser.add_argument("--force", action="store_true", help="overwrite an existing --out")
    args = parser.parse_args(argv)

    now = _now()
    out = pathlib.Path(args.out) if args.out else (
        EVIDENCE_DIR / ("voice-m4-acceptance-%s.json" % now.strftime("%Y%m%dT%H%M%SZ")))

    if args.answers:
        try:
            answers, notes = _load_answers(args.answers)
        except ValueError as exc:
            print("!! %s" % exc, file=sys.stderr)
            return 2
    else:
        interactive = (stdin_isatty or sys.stdin.isatty)()
        if not interactive:
            print("!! 没有交互终端，也没有 --answers；拒绝在没有人工回答的情况下出证据。",
                  file=sys.stderr)
            return 2
        answers, notes = _ask_interactively()

    launch = None if args.no_launch else (
        lambda path: subprocess.Popen([path], cwd=str(ROOT)))
    _verdict_text, code = accept(args.app, out, answers=answers, notes=notes,
                                 launch=launch, force=args.force, now=now)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
