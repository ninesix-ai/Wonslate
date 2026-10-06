# Wonslate 文档索引

> 这里是 Wonslate（万邦译）仓库的文档入口。用户文档为中文；`evidence/` 目录存放基准测试的逐句原始记录。

## 用户文档

| 文档 | 内容 | 适合谁 |
|---|---|---|
| [user-guide.md](user-guide.md) | 用户使用说明（UI/UX）：界面各区块、翻译流程、模式与隐私、TM / 术语表管理、语音、设置、数据位置、常见问题 | 所有使用者 |
| [terminology.md](terminology.md) | 缩写与术语解释：TM、FFI、ASR/TTS/VAD、CT2、MADLAD、SAC 等全量对照 | 读到不认识的词时来查 |
| [sdk.md](sdk.md) | SDK 说明：C ABI FFI 契约（18 个导出函数、JSON 结构、错误码）、集成示例、sidecar HTTP 接口、版本兼容承诺 | 做程序化集成的开发者 |
| [mcp.md](mcp.md) | MCP 接入：把 Wonslate 注册为 AI 客户端（ZCode / Claude Desktop 等）的翻译工具，工具清单、数据互通、后端质量说明 | 想让 AI 助手调用本地翻译的人 |

## 参考与实测

| 文档 | 内容 |
|---|---|
| [hardware-requirements.md](hardware-requirements.md) | 模型 × 档位 × 硬件门槛、配置入口（环境变量）、模型获取 |
| [translation-benchmark.md](translation-benchmark.md) | Argos / MADLAD / Qwen3 三引擎质量与速度实测对比（原始数据见 [evidence/](evidence/)） |