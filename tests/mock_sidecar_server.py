#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""极简 mock sidecar 翻译服务（仅 Python 标准库，零第三方依赖）。

用途：在【没有真实 Argos/MADLAD 模型】时，演示并手测 Rust sidecar 引擎
（engine/sidecar.rs）→ 本机 HTTP → 返回 的完整链路。它不做真翻译，
只是把输入按目标语言做一个可辨识的回显，便于断言链路打通。

启动：
    python tests/mock_sidecar_server.py --port 11435

然后让 Rust 引擎指向它（环境变量约定见 engine/sidecar.rs）：
    $env:LT_ARGOS_URL   = "http://127.0.0.1:11435"   # PowerShell
    powershell -File build.ps1 -Test                 # 或跑 GUI/FFI

契约（与 engine/sidecar.rs 对齐）：
    POST /translate   body {"text","source","target"[,"glossary":[{src,tgt}]]}
    200 -> {"text": "<标记后的译文>"}
    GET  /health      -> {"status":"ok"}
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


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
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/translate":
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        text = req.get("text", "")
        target = req.get("target", "?")
        has_gloss = "glossary" in req and req["glossary"]
        # 可辨识的伪译文，便于肉眼/断言确认链路走通且 glossary 已透传
        pseudo = f"[{target}] {text}" + (" +gloss" if has_gloss else "")
        self._send(200, {"text": pseudo})

    def log_message(self, *_):  # 静音访问日志
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11435)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"mock sidecar listening on http://{args.host}:{args.port}/translate")
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
