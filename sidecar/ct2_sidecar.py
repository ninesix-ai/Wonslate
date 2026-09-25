#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Wonslate 真 sidecar 翻译服务（本机 CT2 / Argos 后端）。

契约（与 Rust engine/sidecar.rs、.NET SidecarManager、tests/mock_sidecar_server.py 严格一致）：
    POST /translate   body {"text","source","target"[,"glossary":[{"src","tgt"}]]}  -> 200 {"text": "<译文>"}
    GET  /health      -> 200 {"status":"ok","backend":"<mock|ct2>"}

可插拔后端：
    --backend mock  无第三方依赖，回显伪译文；用于链路联调 / CI / 无模型环境。
    --backend ct2   真实 CTranslate2 推理（MADLAD-400 / Argos .argosmodel），需装依赖 + 下模型。

设计守则：
    - 缺依赖 / 缺模型时【明确抛 MissingDependency 并给安装指引】，不裸崩、不静默降级。
    - 服务只绑 127.0.0.1（隐私：翻译内容不出机器）。
    - 许可：依赖 ctranslate2(MIT)/sentencepiece(Apache)，模型 MADLAD-400(Apache-2.0)/Argos(MIT)——全宽松可商用。

真实 CT2 推理的环境准备（需你显式执行，脚本不会自动下载/安装）：
    python -m pip install ctranslate2 sentencepiece
    # MADLAD-400 CT2 int8 版（Apache-2.0，GB 级）示例（按需选一，含 tokenizer .model）：
    #   huggingface-cli download <madlad400-ct2-repo> --local-dir <model_dir>
    python -m sidecar.ct2_sidecar --backend ct2 --model-dir <model_dir> --port 11435

模块可被单测导入（serve 仅在 __main__ 阻塞启动）。
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ARGOS_PORT = 11435
DEFAULT_MADLAD_PORT = 11436


class MissingDependency(RuntimeError):
    """真后端所需第三方库或模型缺失时抛出（携带可执行的安装指引）。"""


# ── 后端抽象 ────────────────────────────────────────────────────────

class MockBackend:
    """无依赖回显后端：产出可辨识伪译文，用于联调链路与测试契约。"""

    name = "mock"

    def translate(self, text, source, target, glossary=None):
        out = "[{}] {}".format(target, text)
        if glossary:
            out += " +gloss"
        return out


class CT2Backend:
    """真实 CTranslate2 后端（MADLAD-400 / Argos）。缺依赖或模型即抛 MissingDependency。"""

    name = "ct2"

    def __init__(self, model_dir):
        if not model_dir:
            raise MissingDependency(
                "ct2 backend 需要 --model-dir 指向已下载的 CTranslate2 模型目录"
                "（MADLAD-400 ct2 / Argos .argosmodel 解包）。见本文件顶部安装/下载指引。")
        try:
            import ctranslate2          # noqa: F401
            import sentencepiece        # noqa: F401
        except ImportError as e:
            raise MissingDependency(
                "ct2 backend 缺依赖：{}。请先 `python -m pip install ctranslate2 sentencepiece`。".format(e))
        self._model_dir = model_dir
        # 真实加载留待接入具体 checkpoint（不同模型的 spiece/speed 配置不同），
        # 接入时在此 ctranslate2.Translator(model_dir) + sentencepiece 初始化并验证。
        raise MissingDependency(
            "ct2 backend 依赖与模型目录已就位，但具体 checkpoint 的加载/分词/语言码映射"
            "需按所选 MADLAD-400/Argos 模型实现并实测。")

    def translate(self, text, source, target, glossary=None):  # pragma: no cover - 需真模型
        raise NotImplementedError("CT2Backend.translate 待接入真实 checkpoint 后实现")


def make_backend(kind, model_dir=None):
    kind = (kind or "mock").lower()
    if kind == "mock":
        return MockBackend()
    if kind == "ct2":
        return CT2Backend(model_dir)
    raise ValueError("unknown backend: {!r}（可用：mock / ct2）".format(kind))


# ── HTTP 服务 ───────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "backend": self.server.backend.name})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/translate":
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return
        text = req.get("text")
        if not text or not str(text).strip():
            self._send(400, {"error": "missing 'text'"})
            return
        try:
            out = self.server.backend.translate(
                text, req.get("source", ""), req.get("target", ""), req.get("glossary"))
        except MissingDependency as e:
            self._send(503, {"error": "backend_unavailable", "message": str(e)})
            return
        self._send(200, {"text": out})

    def log_message(self, *_):  # 静音访问日志
        pass


def build_server(backend, host="127.0.0.1", port=0):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.backend = backend
    return srv


def serve_in_thread(backend, host="127.0.0.1", port=0):
    """在后台线程启动服务（供测试/嵌入），返回 (server, bound_port)。"""
    import threading
    srv = build_server(backend, host, port)
    bound = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, bound


def main():
    ap = argparse.ArgumentParser(description="Wonslate sidecar translation server")
    ap.add_argument("--backend", default="mock", choices=["mock", "ct2"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_ARGOS_PORT)
    ap.add_argument("--model-dir", default=None)
    args = ap.parse_args()

    backend = make_backend(args.backend, args.model_dir)
    srv = build_server(backend, args.host, args.port)
    print("sidecar[{}] listening on http://{}:{}/translate".format(backend.name, args.host, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
