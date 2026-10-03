# 评测语料（in-repo starter data）

本目录存放**随仓库分发**的评测语料。默认解析根 `<data_dir>/benchmarks/av-domain/`（见下）**不指向这里**——这里的文件是"你可以直接跑起来的最小样本"，而不是产品评测的唯一真相。真正的评测建议在 `<data_dir>/benchmarks/av-domain/` 下扩展至 300 对起步、500–1000 对完整（外层 `docs/tasks/S12-*.md` §D-S12.1）。

## 现有语料

| 目录 | 领域 | 语言对 | 句对数 | 用途 |
|---|---|---|---|---|
| `av-zh-en/` | AV / 多媒体 | zh → en | 300 | S12 harness 基线语料，12 类子领域分组（流水线、信号、缓冲/流式、编解码、字幕配音、模型训练、推理运行时、翻译技术、延迟与性能、多义词探针、UI/交互、项目术语），含 25 句多义词边界专测

## 布局与文件格式

每个语料目录遵循 sacrebleu 兼容布局：

```
<root>/
  av-zh-en/
    av-zh-en.src   # 源语言列，UTF-8，一行一段
    av-zh-en.tgt   # 目标语言参考，UTF-8，一行一段，与 .src 1:1 对齐
```

**注意**：`.src` 与 `.tgt` 都不允许有注释头（sacrebleu 会把每一行都当句对处理）。许可与来源信息在本 README 与 `attribution.md`（若需要）里承载。

## 许可

- **文件许可**：Apache-2.0（随主许可）
- **来源**：全部由本项目工程师手写，参考公开 Apache-2.0 / MIT 项目文档（sherpa-onnx、Coqui、WeNet、ESPnet、Mozilla Common Voice）的**术语**用法；**不**引用上游原文
- **红线**：不使用 CC-BY-SA（share-alike 污染主许可）、CC-BY-NC（禁商用）、AGPL / GPL（网络传染）
- **审核要求**：任何新增句对必须保持"技术描述性"而非"上游文档拷贝"；引用外部数据须在本目录加 `attribution.md` 逐条列出源

## 使用

**跑起步样本**（临时把根指到本目录）：

```bash
python script/bench_domain_av.py --list \
  --av-root script/eval-data
python script/bench_domain_av.py --engine argos --direction zh-en \
  --av-root script/eval-data
```

**跑扩展样本**（推荐的正式路径）：

1. 把本目录 `av-zh-en/` 复制到默认根：
   - Windows：`%LOCALAPPDATA%\Wonslate\benchmarks\av-domain\av-zh-en\`
   - Linux：`~/.local/share/wonslate/benchmarks/av-domain/av-zh-en/`
   - macOS：`~/Library/Application Support/Wonslate/benchmarks/av-domain/av-zh-en/`
2. 扩样至 300+ 对（外层任务卡 T4 挂账）；
3. `python script/bench_domain_av.py --engine argos --direction zh-en`
   —— 无需 `--av-root`，走 `WONSLATE_AV_DIR` > 默认根解析。

## 与 FLORES-101 的分工

| | 语料 | 域 | 许可 | 用途 |
|---|---|---|---|---|
| **FLORES-101** | 公开人工翻译集（Meta et al.） | 新闻/维基 | CC-BY-SA 4.0（仅评测，不入分发物） | 对外质量口径（REQ-B3） |
| **AV-domain**（本目录） | 本项目自撰 | AV / 多媒体技术 | Apache-2.0（可入仓） | S11 领域种子包的收益证明；对外仅作为补充哈尼斯 |

**两者不能相互替代**：FLORES 数字回答"通用翻译质量如何"；AV-domain 数字回答"我们加了 AV 术语包之后领域质量提升多少"。对外披露时按外层 `docs/requirements/REQ-B3-置信度门控与质量偏好.md` §质量口径 分别标注。

## 后续扩样（S12 T2/T4）

- **已完成（2026-10-03）**：起步 30 对 → 300 对（十二类子领域分组）；
- **仍挂账**：覆盖其他语对（zh↔ja、zh↔ko、en↔ja/ko/fr/de/es/ru/pt/it/ar，任务卡 T2）；引入 `--domain av` 对照（任务卡 T3）；进一步扩样到 500–1000 句（任务卡 T4）。

## 相关

- 脚本：`Wonslate_github/Wonslate/script/bench_domain_av.py`
- 任务卡：外层 `docs/tasks/S12-AV领域评测基线.md`
- 需求：外层 `docs/requirements/REQ-B3-置信度门控与质量偏好.md`、`REQ-E3-产品场景边界.md`
- 领域种子包（**术语**，非句对）：`Wonslate_github/Wonslate/docs/glossary-packs/av-zh-en.json`
