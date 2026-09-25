#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""
Phase 1 FFI 冒烟测试（跨语言集成）

验证 translator_engine.dll 的动态库边界与 v2.0 全功能流水线。
与 translator-engine/tests/pipeline_e2e.rs 互补：
- Rust 集成测试：证明流水线内部逻辑正确
- 本文件：证明 .NET/Python 通过 C ABI 调用能拿到一致的 JSON 契约

用法：
    # 1) 先构建 release DLL
    powershell -File build.ps1
    # 2) 跑本脚本
    python tests/test_phase1_ffi.py

依赖：仅标准库（ctypes / json / pathlib / sys / time）
所有输出走 stderr（Rust eprintln! 也走 stderr，PowerShell 重定向稳定）
"""

import ctypes
import json
import pathlib
import sys
import time


def _p(*args, **kw):
    """统一打印到 stderr。"""
    kw.setdefault("file", sys.stderr)
    print(*args, **kw)


# ---------- 加载 DLL ----------

def _find_dll() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    root = here.parent                              # local-translator/
    candidates = [
        root / "translator-engine" / "target" / "release" / "translator_engine.dll",
        root / "translator-engine" / "target" / "release" / "libtranslator_engine.so",
        root / "translator-engine" / "target" / "release" / "libtranslator_engine.dylib",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "translator_engine 动态库不存在。先运行：\n"
        "    powershell -File build.ps1\n"
        f"查找路径：\n  " + "\n  ".join(str(c) for c in candidates)
    )


class Engine:
    """封装 ctypes 调用，字符串返回自动释放。"""

    def __init__(self, dll_path: pathlib.Path):
        self.lib = ctypes.cdll.LoadLibrary(str(dll_path))
        # 所有 ptr 返回值统一声明为 c_void_p，避免 ctypes 自动 bytes 化后指针丢失
        self.lib.tt_version.argtypes = []
        self.lib.tt_version.restype = ctypes.c_void_p
        self.lib.tt_translate.argtypes = [ctypes.c_char_p] * 3
        self.lib.tt_translate.restype = ctypes.c_void_p
        self.lib.tt_free_string.argtypes = [ctypes.c_void_p]
        self.lib.tt_free_string.restype = None
        self.lib.tt_init.argtypes = [ctypes.c_char_p]
        self.lib.tt_init.restype = ctypes.c_void_p
        self.lib.tt_shutdown.argtypes = []
        self.lib.tt_shutdown.restype = None
        self.lib.tt_translate_full.argtypes = [ctypes.c_char_p]
        self.lib.tt_translate_full.restype = ctypes.c_void_p
        self.lib.tt_tm_lookup.argtypes = [ctypes.c_char_p] * 3
        self.lib.tt_tm_lookup.restype = ctypes.c_void_p
        self.lib.tt_tm_put.argtypes = [ctypes.c_char_p]
        self.lib.tt_tm_put.restype = ctypes.c_void_p
        self.lib.tt_engines.argtypes = []
        self.lib.tt_engines.restype = ctypes.c_void_p
        self.lib.tt_health.argtypes = []
        self.lib.tt_health.restype = ctypes.c_void_p

    def _take(self, ptr) -> str:
        """从 c_void_p 读出字符串并 free。ptr 为空返回空串。"""
        if not ptr:
            return ""
        try:
            return ctypes.string_at(ptr).decode("utf-8", errors="replace")
        finally:
            self.lib.tt_free_string(ptr)

    def version(self) -> str:
        return self._take(self.lib.tt_version())

    def init(self, config_json: str = "{}") -> dict:
        return json.loads(self._take(self.lib.tt_init(config_json.encode("utf-8"))) or "{}")

    def shutdown(self):
        self.lib.tt_shutdown()

    def translate_full(self, req: dict) -> dict:
        return json.loads(self._take(
            self.lib.tt_translate_full(json.dumps(req).encode("utf-8"))
        ) or "{}")

    def translate_legacy(self, engine_id: str, text: str, lang_pair: str) -> dict:
        return json.loads(self._take(
            self.lib.tt_translate(engine_id.encode(), text.encode(), lang_pair.encode())
        ) or "{}")

    def tm_lookup(self, text: str, src: str, tgt: str):
        raw = self._take(self.lib.tt_tm_lookup(
            text.encode("utf-8"), src.encode("utf-8"), tgt.encode("utf-8")
        ))
        return json.loads(raw) if raw and raw != "null" else None

    def tm_put(self, entry: dict) -> dict:
        return json.loads(self._take(
            self.lib.tt_tm_put(json.dumps(entry).encode("utf-8"))
        ) or "{}")

    def engines(self) -> list:
        return json.loads(self._take(self.lib.tt_engines()) or "[]")

    def health(self) -> dict:
        return json.loads(self._take(self.lib.tt_health()) or "{}")


# ---------- 断言辅助 ----------

class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, name: str):
        self.passed += 1
        _p(f"  [PASS] {name}")

    def fail(self, name: str, msg: str):
        self.failed += 1
        _p(f"  [FAIL] {name}: {msg}")

    def skip(self, name: str, reason: str):
        self.skipped += 1
        _p(f"  [SKIP] {name}: {reason}")

    def summary(self) -> int:
        total = self.passed + self.failed + self.skipped
        _p("")
        _p(f"  通过 {self.passed} / 失败 {self.failed} / 跳过 {self.skipped} / 总计 {total}")
        return 0 if self.failed == 0 else 1


# ---------- 测试用例 ----------

def test_version_and_init(eng: Engine, ck: Checker):
    v = eng.version()
    if v and v[0].isdigit():
        ck.ok(f"tt_version 返回版本号: {v}")
    else:
        ck.fail("tt_version", f"bad version string: {v!r}")

    r = eng.init("{}")
    if r.get("ok"):
        ck.ok("tt_init 成功")
    else:
        ck.fail("tt_init", f"returned: {r}")


def test_engines_and_health(eng: Engine, ck: Checker):
    engines = eng.engines()
    ids = [e.get("id") for e in engines]
    if "demo" in ids and "ollama" in ids:
        ck.ok(f"tt_engines 包含 demo + ollama：{ids}")
    else:
        ck.fail("tt_engines", f"missing expected ids, got: {ids}")

    h = eng.health()
    if h.get("status") == "ok" and "tm_entries" in h:
        ck.ok(f"tt_health: tm_entries={h['tm_entries']}")
    else:
        ck.fail("tt_health", f"unexpected: {h}")


def test_legacy_translate_still_works(eng: Engine, ck: Checker):
    """P0 稳定契约向后兼容：tt_translate 仍然可用"""
    r = eng.translate_legacy("demo", "hello", "en-zh")
    if r.get("ok") and "你好" in (r.get("output") or ""):
        ck.ok("tt_translate(demo, hello, en-zh) 返回 你好")
    else:
        ck.fail("legacy tt_translate", f"got: {r}")


def test_translate_full_and_tm_hit(eng: Engine, ck: Checker):
    """核心闭环：手动 tm_put 一条，再 translate_full 同原文应命中 TM"""
    unique = f"人工智能流水线验证 {int(time.time() * 1000)}"
    target = "PIPELINE_TM_HIT_MARK"

    # 直接通过 FFI 塞一条 TM 历史
    put = eng.tm_put({
        "source_text": unique,
        "source_lang": "zh",
        "target_text": target,
        "target_lang": "en",
        "engine": "manual",
        "quality": 0.95,
        "hit_count": 1,
        "domain": "",
    })
    if not put.get("ok"):
        ck.fail("tt_tm_put", f"put={put}")
        return

    req = {
        "input": unique,
        "source_lang": "zh",
        "target_lang": "en",
        "mode": "full",
        "privacy": False,
        "use_tm": True,
    }
    r = eng.translate_full(req)
    if r.get("source") == "tm_hit" and r.get("output") == target:
        ck.ok(f"translate_full 命中 TM (latency={r.get('latency_ms')}ms)")
    else:
        ck.fail("TM 命中", f"expected tm_hit/{target}, got {r}")


def test_privacy_never_escalates(eng: Engine, ck: Checker):
    """隐私模式：无论多低置信度都不能出现 ai_upgraded / ollama 引擎"""
    req = {
        "input": "这是一段机密内容验证隐私模式",
        "source_lang": "zh",
        "target_lang": "en",
        "mode": "full",
        "privacy": True,
        "use_tm": False,
    }
    r = eng.translate_full(req)
    src = r.get("source", "")
    eng_id = r.get("engine", "")
    # 允许 Ok 结果或 Err(NoResult)，只要不是 AI 升级
    if src == "ai_upgraded" or "ollama" in eng_id:
        ck.fail("PRIVACY 保护", f"违规: source={src} engine={eng_id}")
    else:
        ck.ok(f"隐私模式未升级到 AI (source={src or r.get('error', 'err')})")


def test_realtime_never_escalates(eng: Engine, ck: Checker):
    """实时档：即便置信度低也不应升级到 AI（保证延迟）"""
    req = {
        "input": f"实时档验证 {int(time.time() * 1000)}",
        "source_lang": "zh",
        "target_lang": "en",
        "mode": "realtime",
        "privacy": False,
        "use_tm": False,
    }
    r = eng.translate_full(req)
    if r.get("source") == "ai_upgraded":
        ck.fail("实时档延迟约束", f"realtime 竟然升级到 AI: {r}")
    else:
        ck.ok(f"实时档未升级 AI (source={r.get('source', r.get('error', 'err'))})")


def test_engine_id_override(eng: Engine, ck: Checker):
    """Agent 透传：显式 engine_id 应绕过路由决策"""
    req = {
        "engine_id": "demo",
        "input": "hello",
        "source_lang": "en",
        "target_lang": "zh",
        "mode": "full",
        "use_tm": False,
    }
    r = eng.translate_full(req)
    if r.get("engine") == "demo":
        ck.ok("engine_id=demo 透传生效")
    else:
        ck.fail("engine_id 透传", f"got engine={r.get('engine')}")


def test_bad_input_handled(eng: Engine, ck: Checker):
    """非法 JSON 输入应返回错误结构而不是崩溃"""
    ptr = eng.lib.tt_translate_full(b"this is not json")
    raw = eng._take(ptr)
    try:
        r = json.loads(raw)
    except Exception as e:
        ck.fail("非法 JSON 输入处理", f"无法解析: {e}; raw={raw[:100]}")
        return
    if r.get("ok") is False and r.get("error"):
        ck.ok(f"非法输入返回结构化错误: error={r['error']}")
    else:
        ck.fail("非法输入应报错", f"got {r}")


# ---------- 主流程 ----------

def main() -> int:
    try:
        dll = _find_dll()
    except FileNotFoundError as e:
        _p(f"[ABORT] {e}")
        return 2

    _p(f"加载 DLL: {dll}")
    eng = Engine(dll)
    ck = Checker()

    try:
        _p("")
        _p("== 基础接口 ==")
        test_version_and_init(eng, ck)
        test_engines_and_health(eng, ck)

        _p("")
        _p("== 向后兼容（P0 稳定契约）==")
        test_legacy_translate_still_works(eng, ck)

        _p("")
        _p("== v2.0 全功能流水线 ==")
        test_translate_full_and_tm_hit(eng, ck)
        test_privacy_never_escalates(eng, ck)
        test_realtime_never_escalates(eng, ck)
        test_engine_id_override(eng, ck)
        test_bad_input_handled(eng, ck)
    finally:
        eng.shutdown()
        _p("")
        _p("[tt_shutdown 已调用]")

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
