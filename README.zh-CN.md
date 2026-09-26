# Wonslate（万邦译）

**中文** | [English](README.md)

> 本地离线、可商用的多引擎智能翻译引擎（**v0.0.1**）。

Wonslate 是一款**隐私优先**的本地翻译软件：数据不出设备，多引擎智能路由，并以内置翻译记忆（TM）+ AI 蒸馏，让重复翻译的成本持续降低。

## 架构

```
WPF UI (C#)  <-->  Router(规则路由)  <-->  EngineNative(FFI)  <-->  Rust translator_engine
                                                     │
                                              demo 引擎（内置词表）
                                              argos / madlad（本机 sidecar 插槽）
```

- **.NET 10**：业务逻辑、静态路由规则、WPF 界面
- **Rust (cdylib)**：引擎适配层，C ABI 导出 `tt_translate` / `tt_version` / `tt_free_string`
- 翻译结果以 JSON 回传；字符串由 Rust 分配、.NET 用后释放

## 目录

```
Wonslate/
├─ translator-engine/          # Rust crate（cdylib）：引擎/路由/TM/蒸馏核心
│  └─ src/
│     ├─ lib.rs                # C ABI 导出 + panic 兜底
│     └─ engine/               # Translator trait + 引擎注册表 + demo 引擎
├─ Wonslate.UI/                # .NET 10 WPF 客户端（产出 Wonslate.exe）
│  ├─ Interop/EngineNative.cs  # P/Invoke 绑定
│  ├─ Sidecar/                 # 本机 sidecar 进程管理
│  └─ ViewModels/MainViewModel.cs
├─ Wonslate.UI.Tests/          # .NET 客户端 xUnit 测试
├─ build.bat                   # Windows 一键入口（转发到 script/build.py）
├─ build.sh                    # Linux/macOS 一键入口
└─ script/build.py             # 跨平台构建/测试核心（Python）
```

## 构建

依赖：Rust 1.98+、.NET SDK 10.0+（WPF 客户端仅 Windows 可构建）。跨平台构建/测试入口与 FFI 冒烟脚本使用 Python 3。

```bash
python script/build.py            # 构建：Rust 引擎（release）+ Windows 上的 WPF 应用
python script/build.py --test     # 构建并跑全部测试层（Rust / .NET / Python FFI / sidecar e2e）
python script/build.py --unit     # 构建并只跑 Rust + .NET 测试
python script/build.py --ffi      # 构建并只跑 Python FFI 冒烟
python script/build.py --run      # 构建后启动 Wonslate.exe（Windows）
```

`build.bat`（Windows）与 `build.sh`（Linux/macOS）是各平台薄入口，把所有参数转发给 `script/build.py`。产物位于 `translator-engine/target/release/`（原生引擎库，如 `translator_engine.dll`）与 `Wonslate.UI/bin/Release/net10.0-windows/`（`Wonslate.exe`）；均由本地或 CI 构建生成，不入库。

## 快速体验（demo 引擎）

内置 `demo` 引擎带一张小型中英词表，**无需下载任何模型**即可跑通 `WPF → FFI → Rust` 全链路。启动程序、保持隐私模式开启，用下面几条输入核对——每条输出都是对已发布二进制的实测结果（`mode=realtime`、`privacy=true`、`engine=demo`），不是期望值：

| 输入 | 语言对 | 输出 |
|---|---|---|
| `hello` | en → zh | `你好` |
| `hello there` | en → zh | `你好` |
| `good morning` | en → zh | `早上好` |
| `good night` | en → zh | `晚安` |
| `i love the world` | en → zh | `i love 这个 世界` |
| `你好` | zh → en | `hello` |
| `你好世界` | zh → en | `hello world` |

`i love the world` 是刻意保留的例子：词表未收录的词按原样透传，因此 demo 引擎展示的是词级替换而非真正翻译。其余已注册引擎各有前置条件：`argos` 与 `madlad` 通过本机 sidecar 通信（`sidecar/ct2_sidecar.py` 目前只提供 mock 后端，接入真实 CTranslate2/Argos 权重尚未完成），`ollama` 需要本机运行 Ollama 并已拉取模型。

## 安全

Wonslate 隐私优先：翻译数据不出设备。如果你认为发现了安全问题，请通过私密渠道报告——见 [SECURITY.md](SECURITY.md)，**不要**开公开 Issue。

## 许可与版权

Copyright (c) 2026 ninesix-ai studio

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

坚持 Apache-2.0 / MIT 技术栈以保证可商用。完整协议文本见仓库根目录 `LICENSE` 文件。
