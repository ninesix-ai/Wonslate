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
├─ LocalTranslator/            # .NET 10 WPF 客户端
│  ├─ Interop/EngineNative.cs  # P/Invoke 绑定
│  ├─ Routing/Router.cs        # 静态路由（隐私 -> 覆盖 -> 质量）
│  └─ ViewModels/MainViewModel.cs
├─ build.bat                   # Windows 一键入口（转发到 script/build.py）
├─ build.sh                    # Linux/macOS 一键入口
└─ script/build.py             # 跨平台构建/测试核心（Python）
```

## 构建

依赖：Rust 1.98+、.NET SDK 10.0+。构建将产出原生引擎库（`translator_engine.dll` 等，已在 `.gitignore` 中排除、由本地/CI 构建生成，不入库）。

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
