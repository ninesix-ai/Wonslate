# 缩写与术语解释

> 本文汇总 Wonslate 文档与界面中共用的缩写和术语。界面操作见 [user-guide.md](user-guide.md)；FFI 契约见 [sdk.md](sdk.md)。
> 分类：§0 命名说明（先读）· §1 产品与使用概念 · §2 引擎与模型 · §3 接口与工程 · §4 基准指标 · §5 许可

## 0. 命名说明（先读这条）

- 产品名 **Wonslate / 万邦译**；曾用名 **LocalTranslator**（改名后环境变量与工程目录均有过迁移）。
- 为兼容历史，文档与配置里会同时出现三类拼写，**不是三套产品**：
  - `LT_*`：旧前缀，仍被识别且**优先级最高**（向后兼容）；
  - `WONSLATE_*`：现行前缀，新集成请用这一套；
  - 裸名（如 `OLLAMA_URL`）：兼容第三方惯例的兜底拼写。
- 数据目录在无环境变量时回退到 `./lt-data`（保留旧名以便找到既有数据）。
- FFI 符号保留 `tt_*` 技术前缀（如 `tt_translate_full`）；`README` 中的"万邦译"即 Wonslate。

## 1. 产品与使用概念

| 术语 | 英文 / 全称 | 含义 |
|---|---|---|
| Wonslate / 万邦译 | — | 本产品：本地离线、可商用的多引擎智能翻译软件 |
| TM | Translation Memory | **翻译记忆库**：原文 → 译文的历史库。命中即零成本复用；条目带质量分与命中计数 |
| 术语表 | Glossary | 蒸馏或人工维护的"术语原文 → 术语译文"对照，翻译时只注入**源文确实出现**的那些（默认每次最多 20 条，上限为 `glossary_max_terms`） |
| 蒸馏 | Distillation | 从 AI 精译结果中异步抽取术语 / 示例、回灌本地引擎质量的流程 |
| Few-shot | 少样本示例 | 把相似历史译法作为参考样例喂给 AI 引擎，保持译法一致 |
| 置信度 | Confidence | 0–1 的译文质量估计：AI 结果固定 0.95；本地结果按"引擎基线 + 输出信号"估算 |
| 质量分 | Quality | TM 条目携带的评分：AI 写入 0.95、本地写入 = 当时置信度、手动写入 1.0 |
| 路由 | Routing | 每次翻译时按优先级链选择引擎的决策（TM → 隐私 → 覆盖 → 本地 → 置信度 → AI 升级） |
| 实时档 / 精译档 | Realtime / Full | 两个翻译档位。实时档延迟优先、从不升级 AI；精译档质量优先、可升级 AI |
| 兜底降级 | Fallback | 未能按预期路径完成时的显式回落；**必须带原因说明**，不允许静默 |
| 隐私模式 | Privacy mode | 强制本地执行的不可旁路约束：禁止 AI 升级，非本地引擎被钳制为 `demo` |
| 命中 / 未命中 | Hit / Miss | TM 是否直接复用了历史译文；命中时 `hit_count` 自增 |
| 缓存质量下限 | tm_quality_floor | 精译档允许**复用**的 TM 条目最低质量（默认 0.0；「质量优先」偏好会设为 0.95） |
| 升级策略 | upgrade_policy | `always`：AI 可达就升级；`low_confidence`：本地结果低于阈值才升级 |
| 质量偏好 | quality_preference | 设置页二选一：`quality_first`（质量优先）/ `cost_first`（省成本优先） |
| engine_id | — | 引擎标识：留空 = 按路由自动决策；显式给出 = 锁定该引擎且不升级 |
| 常用语言集 | common_pairs | 11 种常用语言代码（zh/en/ja/ko/fr/de/es/ru/pt/it/ar），不在其中的走"冷门语言对"规则 |
| 不静默降级 | No silent degradation | 任何回落 / 钳制都会写进响应的 `message` 字段，供调用方感知 |

## 2. 引擎与模型

| 术语 | 英文 / 全称 | 含义 |
|---|---|---|
| Argos | Argos Translate | MIT 许可的离线 NMT（Marian 系）包；一个语言对一个包，速度最快（en↔zh 实时档） |
| CT2 | CTranslate2 | 高效神经网络推理引擎；本项目用它跑 Argos 与 MADLAD（刻意走 CPU） |
| SentencePiece | — | 子词分词器，Argos / MADLAD 模型配套组件 |
| MADLAD | MADLAD-400 | Google 多语言模型（Apache-2.0）；**单个检查点覆盖 450+ 语言码**，本项目用 3B int8 |
| Ollama | — | 本机 LLM 运行时（默认 `127.0.0.1:11434`），精译档的 AI 升级引擎 |
| Qwen3 | — | 默认升级模型 `qwen3:8b` |
| LLM | Large Language Model | 大语言模型 |
| MT / NMT | Machine / Neural Machine Translation | 机器翻译 / 神经机器翻译 |
| 精译 | — | 本项目对"经 AI 引擎产出"的叫法（路由标签 `[AI 精译]`） |
| ASR | Automatic Speech Recognition | 语音识别（本产品用 SenseVoice） |
| TTS | Text-To-Speech | 语音合成（本产品用 Kokoro v1.1） |
| VAD | Voice Activity Detection | 语音活动检测（Silero VAD），先把音频切成语段 |
| SenseVoice / Kokoro | — | 语音识别 / 语音合成模型（随 `script/fetch_voice_models.py` 下载） |
| sherpa-onnx | — | 本机语音推理框架，串联 VAD/ASR/TTS |
| int8 / 量化 | Quantization | 8 位整数量化，显著缩小模型与内存占用，质量略降 |
| offload | — | 显存不足时把部分模型层交给内存 / CPU 执行（Ollama 自动处理，速度下降） |
| sidecar | 伴随进程 | 本机 Python HTTP 翻译服务；端口约定 argos=11435、madlad=11436，**仅绑定 127.0.0.1** |

## 3. 接口与工程

| 术语 | 英文 / 全称 | 含义 |
|---|---|---|
| FFI | Foreign Function Interface | 外部函数接口；本项目指 C ABI 导出的 16 个 `tt_*` 函数（见 [sdk.md](sdk.md)） |
| C ABI | C Application Binary Interface | C 调用约定；跨语言（C / .NET / Python）稳定契约的载体 |
| cdylib / DLL | — | Rust 动态库产物 `translator_engine.{dll,so,dylib}` |
| P/Invoke / LibraryImport | — | .NET 调用 C ABI 的机制（现有 WPF 客户端用 `LibraryImport`） |
| SDK | Software Development Kit | 面向集成方的编程接口封装；**当前 = 直接使用 FFI 契约**，官方 .NET / Python 包规划中 |
| API / REST / CLI | — | 规划中的命令行与 HTTP 入口；**当前版本未提供**（现状说明见 [sdk.md](sdk.md)） |
| MCP | Model Context Protocol | AI Agent 工具调用标准；**规划中、尚未实现**（实现后再补充专项文档） |
| stdio | Standard I/O | 标准输入输出流；MCP 的首选传输形态（子进程管道，无网络监听面） |
| JSON | JavaScript Object Notation | 数据交换格式；FFI 所有请求 / 响应均为 UTF-8 JSON 字符串 |
| UTF-8 | — | 全链路字符串编码约定（跨 FFI 边界不得使用本地编码） |
| LRU | Least Recently Used | 最近最少使用淘汰策略；TM 内存缓存默认容量 1000 条 |
| SHA256 | — | 设计文档中 TM 精确查找的哈希方案；**当前实现为精确文本匹配**（JSON 存储） |
| SQLite | — | 设计文档中的 TM 存储方案；**当前实现为 `data/*.json` 文件**，未采用 SQLite |
| WPF / XAML | Windows Presentation Foundation | Windows 桌面 UI 技术与标记语言（本产品桌面客户端） |
| .NET 10 | — | 桌面客户端运行时 |
| CI | Continuous Integration | 持续集成 |
| SAC | Smart App Control | Windows 智能应用控制；可能拦截**未签名的本地构建 DLL**，开发/自构建场景需知 |
| 数据目录 | data_dir | 默认 `%LOCALAPPDATA%\Wonslate`；用 `LT_DATA_DIR` / `WONSLATE_DATA_DIR` 覆盖 |
| routes.json | — | 部署方默认路由表（`<数据目录>/config/routes.json`） |
| settings.json | — | 用户在设置页保存的偏好（`<数据目录>/config/settings.json`） |
| `LT_*` / `WONSLATE_*` | — | 环境变量的旧 / 新前缀（优先级见 §0） |
| `tt_*` | — | FFI 符号前缀，如 `tt_translate_full`、`tt_free_string` |

## 4. 基准指标

| 术语 | 英文 / 全称 | 含义 |
|---|---|---|
| chrF / chrF++ | character n-gram F-score | 字符级（chrF++ 叠加词级）相似度指标；对中文 / 日文更稳定 |
| BLEU | BiLingual Evaluation Understudy | 经典机器翻译指标（n-gram 精确度），作交叉参照 |
| sacrebleu | — | 上述指标的标准化实现（基准报告用它评分） |
| wall time | — | 端到端挂钟耗时；基准报告的延迟口径（含 HTTP，串行发送） |

## 5. 许可

| 术语 | 含义 |
|---|---|
| Apache-2.0 / MIT | 宽松开源许可；本项目全栈红线，保证可商用 |
| CC-BY-NC | 仅限非商用；本项目的许可红线，相关模型仅可做原型评估 |
| AGPL | 网络传染性 copyleft；不可引入 |
| Open-Core | 商业模式：开源核心 + 增值（企业）能力收费 |

## 相关文档

- [用户使用说明（UI/UX）](user-guide.md)
- [SDK 说明](sdk.md)
- [硬件配置要求](hardware-requirements.md) · [翻译质量基准](translation-benchmark.md) · [文档索引](README.md)