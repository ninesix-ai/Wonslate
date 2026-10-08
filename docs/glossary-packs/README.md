# 领域种子术语包（Glossary Seed Packs）

本目录存放**领域限定的中英术语对照包**，用户按需导入到自己的本地术语表。文件本身不进入运行时数据目录，也不会自动生效——只有主动导入后才会写入 `glossary.json`。

## 现有包

| 文件 | domain | 语言对 | 版本 | 条目数 |
|---|---|---|---|---|
| `av-zh-en.json` | `av` | zh → en | 2026.10 | 266 |
| `software-ui-en-<lang>.json` ×10 | `software-ui` | en → de / fr / es / pt-BR / ru / it / pl / tr / ja / ko | 2026.10 | 14 each |

> `software-ui` 收录**产品中立**的桌面应用常用标签（About / Settings / Cancel / Keep on Top
> …）。每行取自一个 Apache-2.0 桌面应用已过本地化门禁的线上文案，不是机翻产物。
>
> 它的存在理由：短标签脱离语境时模型会翻错。实测同一引擎同一输入，只有 `domain` 不同：
> `About` → ja 无域得到「について」（残缺），带 `domain="software-ui"` 得到
> 「このアプリについて」；`Keep on Top` → de 分别为 "Immer oben" 与
> "Immer im Vordergrund"。
>
> 注意：域行只在请求携带该 `domain` 时才会被读到，而当前界面不传 `domain`，因此导入后
> 需调用方显式带域（或界面提供域选择器）才会生效。

## 文件格式契约

一个种子包必须是合法的 UTF-8 JSON 对象，字段与 `translator-engine/src/tm/store.rs::glossary_import_pack` 的期望严格对齐：

```json
{
  "domain":      "av",
  "source_lang": "zh",
  "target_lang": "en",
  "version":     "YYYY.MM",
  "license":     "Apache-2.0",
  "attribution": "…来源与许可说明…",
  "notes":       "…可选备注…",
  "entries": [
    {"source_term": "语音活动检测", "target_term": "voice activity detection"},
    {"source_term": "声道", "target_term": "audio channel"}
  ]
}
```

必填：`domain` / `source_lang` / `target_lang` / `entries`；每条 entry 必填 `source_term` 与 `target_term`。`confidence` 缺省视为 `1.0`（手动种子级别），`version` / `license` / `attribution` / `notes` 由导入方保留但不参与匹配。

## 装载路径（依赖 S11 GREEN）

导入 FFI `tt_glossary_import_pack` 与 UI "术语表"页签的**领域筛选 + 导入按钮**都在 **S11 GREEN** 中落地，patch 见外层仓 [`docs/tasks/S11-green.patch`](../../../../docs/tasks/S11-green.patch)。S11 未 apply 之前，本目录的数据文件对运行时不生效——不是缺陷，是**归属约束**：数据先入库、加载器随后接入。

S11 GREEN 落地后，两种装载方式：

1. **UI**：设置 → 术语表页签 → 选择 `av` → 点"导入领域包"；
2. **命令行**（若提供 `script/install_glossary_pack.py`，S11 后继任务）：
   ```
   python script/install_glossary_pack.py av
   ```

导入成功后：
- 每条 entry 的 `domain` 被写入 `"av"`、`source` 被写入 `"seed:av"`；
- UI 中把 `GlossaryDomainFilter` 切到 `av` 可见这 266 条 + 用户通用术语（并集，av 版覆盖同名通用版）；
- 请求体带 `domain: "av"` 时，这些条目会注入 Ollama 系统提示；不带或空 domain 时**不**进入上下文。

## 语义与冲突规则

- **通用条目 vs 特定域条目**：同一 `source_term` 可同时存在 `domain=""`（通用）和 `domain="av"`（特定）两行，主键含 domain 段；查询时 `req.domain="av"` 返回**通用 ∪ av**，同名冲突下 av 版胜出。
- **术语行的主键**：`term|source_lang|target_lang|domain`，术语段先做小写归一，所以 `HELLO` 与 `hello` 是同一条术语行；"语音" 与 "语音合成" 各自独立，主键本身不做包含判断。
- **注入时的命中判定**：一条术语只有出现在本次源文里（子串、大小写不敏感）才会进上下文；某 domain 一条都没命中时不注入任何术语，并在 `message` 里说明。主键匹配与注入匹配是两件事，对外口径见 `docs/sdk.md` 的 `domain` 行。
- **confidence 与是否顶掉旧行**：导入只在**严格更高**时替换同名同 domain 的既有行。条目省略 `confidence` 时实际入库是 **0.90**（读路径的默认值；导入器里"缺失即 1.0"的兜底取不到，登记为 D34），所以种子包无法覆盖同为 0.90 的蒸馏产出，且不会报错——想让它顶掉，请在包里显式写 `confidence`。
- **不覆盖用户锁定**：`user_locked=true` 的行，蒸馏与导入都不改动（读路径会如实恢复这个标记）。但当前**没有**界面开关或 FFI 字段能把某条术语设为锁定（登记为 D35），唯一可行的途径是在**引擎停止时**手工编辑用户数据目录里的 `glossary.json`（Windows：`%LOCALAPPDATA%\Wonslate\data\glossary.json`）——引擎运行期间改、或退出时写回，都会被内存索引覆盖掉。

## 许可红线

按 REQ-E1 全仓宽松许可约束：

- **允许**作为来源：Apache-2.0 / MIT / BSD / CC-0 / CC-BY-4.0 项目的公开技术文档；
- **禁止**：CC-BY-SA（share-alike 会污染主许可）、CC-BY-NC / ND（非商用或禁演绎）、AGPL / GPL（网络传染）；
- **允许**：项目工程师自行撰写（本包大部分条目属此类）；
- **每个包必须**在 `attribution` 字段里说清来源，让审查可复核。

## 加新领域包

1. 在 `docs/glossary-packs/` 新建 `<domain>-<src>-<tgt>.json`，遵循上文格式；
2. 保持 `entries` 按 source_term 字典序或按子领域分组排列（本包按子领域分组）；
3. 在本 README 的"现有包"表格追加一行；
4. 若领域名与 UI 需要联动（下拉选项、模板包列表），须同步 S11 后继任务里的 `GlossaryDomainFilter` 候选值清单。

## 相关文档

- 需求侧：外层 `docs/requirements/REQ-A3-术语表与蒸馏飞轮.md`、`REQ-E3-产品场景边界.md`（S6 音视频批量本地化）
- 任务侧：外层 `docs/tasks/S11-领域路由与术语包接线.md`（B-1）与本目录的 S11-green.patch 装载契约
- 术语解释（**非**种子包）：[../terminology.md](../terminology.md) 是产品自身文档用词的中英对照，不是可导入的领域包
