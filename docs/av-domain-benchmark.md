# AV 领域质量基准报告（骨架 · 待回填）

> 定位：本报告是 Wonslate **S11 领域种子包收益**的对外证据。与 [flores-benchmark.md](flores-benchmark.md)（通用域，18 方向 × 3 引擎）、[translation-benchmark.md](translation-benchmark.md)（自建 108 句回归基线）**分工不同**：本报告的样本与指标专为"AV / 多媒体技术域"设计，回答的是"加了 `--domain av` 术语注入之后质量提升多少"。
> 数据：`script/eval-data/av-zh-en/`（本项目自撰，Apache-2.0，300 对 zh↔en，十二类子领域分组，设计层需求见外层仓任务台账 `translation/docs/tasks/S12-AV领域评测基线.md`）。逐句证据落 [evidence/av-domain-*.json](evidence/)。
> 指标：**COMET-22** 为主口径（与 flores 报告一致），chrF++ / BLEU 为参照。sacrebleu CJK 目标用字符级约定。引擎均按产品真实路径调用（sidecar /translate 或 FFI `tt_translate_full` + `engine_id` 锁定），domain 通过请求体 `domain` 字段传递。
> 状态：⬜ **本报告为模板骨架，所有表格数字待实跑回填**。回填条件：S11 已 apply + 三 sidecar 与 ollama 均可应答 + 已完成两轮 300 句 bench（`--domain ""` 与 `--domain av`）。

## TL;DR（待回填）

| 引擎 | 方向对 | 未接线 COMET | 接线 COMET | Δ | 状态 |
|---|---|---|---|---|---|
| argos（L2 实时档） | zh↔en | _待填_ | _待填_ | _待填_ | ⬜ |
| madlad-3B（L2 全语言档） | zh↔en | _待填_ | _待填_ | _待填_ | ⬜ |
| qwen3:8b（L3 精译档） | zh↔en | _待填_ | _待填_ | _待填_ | ⬜ |

**结论一句话**（待回填）：AV 域 300 句基线 vs `--domain av` 接线后，_待填引擎_ 平均 COMET 从 _待填_ 提升到 _待填_（Δ _待填_），多义词边界子集 Δ 更大——支撑 S11 术语包对外可披露。

## 1. 未接线基线（`--domain ""`）

_本节 3 张表分别对应三引擎。所有数字待实跑回填。_

### 1.1 qwen3:8b（L3 精译档）

| 子领域 | 句对数 | chrF | BLEU | COMET | 延迟 |
|---|---|---|---|---|---|
| 流水线核心 | 60 | _待填_ | _待填_ | _待填_ | _待填_ |
| 信号与声码 | 25 | _待填_ | _待填_ | _待填_ | _待填_ |
| 缓冲与流式 | 25 | _待填_ | _待填_ | _待填_ | _待填_ |
| 编解码与容器 | 25 | _待填_ | _待填_ | _待填_ | _待填_ |
| 字幕与配音 | 25 | _待填_ | _待填_ | _待填_ | _待填_ |
| 模型与训练 | 25 | _待填_ | _待填_ | _待填_ | _待填_ |
| 推理运行时 | 25 | _待填_ | _待填_ | _待填_ | _待填_ |
| 翻译技术 | 60 | _待填_ | _待填_ | _待填_ | _待填_ |
| 延迟与性能 | 20 | _待填_ | _待填_ | _待填_ | _待填_ |
| **多义词边界** | 25 | _待填_ | _待填_ | _待填_ | _待填_ |
| UI 与项目术语 | 35 | _待填_ | _待填_ | _待填_ | _待填_ |
| **合计** | **300** | _待填_ | _待填_ | _待填_ | _待填_ |

_方向：zh→en。en→zh 的对应表同样落 `evidence/av-domain-ollama-qwen.json`，本节按同样格式列。_

### 1.2 madlad-3B（L2 全语言档）

_结构同 1.1；证据落 `evidence/av-domain-madlad.json`。待填。_

### 1.3 argos（L2 实时档）

_结构同 1.1；证据落 `evidence/av-domain-argos.json`。argos 只有 en↔zh 两包，样本量对齐时同样按 300 句跑。_

## 2. 接线后（`--domain av`）

_S11 落地后（`tt_glossary_import_pack` + 266 条 AV 种子包已导入本地库），三引擎在同一 300 句集上重跑一遍。三张表结构同 §1，每格记 `--domain av` 的结果。_

### 2.1 qwen3:8b（`--domain av`）

_待填。证据落 `evidence/av-domain-ollama-qwen-av.json`（与 §1.1 的 `-ollama-qwen.json` 分开保存，方便 diff）。_

### 2.2 madlad-3B（`--domain av`）

_待填。证据 `evidence/av-domain-madlad-av.json`。_

### 2.3 argos（`--domain av`）

_待填。argos 的 prompt 注入路径与 madlad/qwen 不同（Marian 不读术语表上下文），预期 Δ 最小甚至为 0——这本身就是"术语注入只在能感知它的引擎上生效"的实证，写进结论。_

## 3. 差值与解读（待回填）

| 引擎 | 未接线 COMET | 接线 COMET | Δ 全样 | Δ 多义词边界 | Δ 长句 |
|---|---|---|---|---|---|
| argos | _待填_ | _待填_ | _待填_ | _待填_ | _待填_ |
| madlad | _待填_ | _待填_ | _待填_ | _待填_ | _待填_ |
| qwen3:8b | _待填_ | _待填_ | _待填_ | _待填_ | _待填_ |

**观察结论**（回填时逐条落实，不预估）：

- **_待填_**：qwen3:8b 在多义词边界子集上的 Δ 是否显著大于全样平均？（预期方向：是，因为种子包正是为这类词准备的；但需要数据说话）
- **_待填_**：madlad 是否也有可测的 Δ，还是完全被自己的多语容量摊平？
- **_待填_**：argos 作为 Marian 系实时档不读术语上下文，Δ 应接近 0——如果实测不是这样，说明 argos 侧有别的机制在起作用，需要单独解释。
- **_待填_**：长句（20-30 token 段）上的 Δ 是否比短句（10 token 内）小？术语注入可能被长上下文里的其他信号冲淡。

## 4. 复现命令

**一键方式（推荐）**：先确保 sidecar 已拉模且 ollama 已启，然后双击 `run_av_baseline.bat`（Windows）或 `./run_av_baseline.sh`（Linux/macOS）。脚本会：体检环境 → 自动拉起 argos/madlad sidecar（已在则复用）→ `install_glossary_pack --domain av` → 三引擎各跑两轮 bench（baseline / scoped）→ COMET 打分 → **自动把数字回填本文件**。

可选粒度：

```bash
python script/run_av_baseline.py --preflight-only   # 只体检
python script/run_av_baseline.py --quick           # 前 20 句冒烟（跳 COMET）
python script/run_av_baseline.py --report-only     # 从现有 evidence 重出报告
python script/run_av_baseline.py --skip-comet      # 不跑 COMET（CPU 上很省）
python script/run_av_baseline.py --engines argos,madlad  # 子集引擎
```

手工方式（想逐步骤看）：

前提：

```bash
# 1) S11 GREEN 已 apply 且 cargo build --release 通过
cd translator-engine && cargo build --release && cd ..

# 2) 三个 sidecar 与 ollama 均已启动（详见 hardware-requirements.md）
python sidecar/ct2_sidecar.py --engine argos  --port 11435 &
python sidecar/ct2_sidecar.py --engine madlad --port 11436 &
ollama serve &
ollama pull qwen3:8b   # 或已存在则跳过

# 3) 起步语料 300 对已装到默认根
mkdir -p "$LOCALAPPDATA/Wonslate/benchmarks/av-domain"    # Windows；Linux/macOS 相应路径
cp -r script/eval-data/av-zh-en "$LOCALAPPDATA/Wonslate/benchmarks/av-domain/"

# 4) 导入 AV 种子包（266 条）
python script/install_glossary_pack.py --domain av
```

跑基线与对照：

```bash
# 未接线（domain 空）
for eng in argos madlad ollama-qwen; do
  for dir in zh-en en-zh; do
    python script/bench_domain_av.py --engine "$eng" --direction "$dir" --domain "" \
      > "docs/evidence/av-domain-${eng}.log" 2>&1
  done
done

# 接线（domain=av）
for eng in argos madlad ollama-qwen; do
  for dir in zh-en en-zh; do
    python script/bench_domain_av.py --engine "$eng" --direction "$dir" --domain av \
      > "docs/evidence/av-domain-${eng}-av.log" 2>&1
  done
done

# COMET 打分（每引擎各跑一次；两份对照各跑一次）
python script/score_comet.py --input docs/evidence/av-domain-qwen.json
python script/score_comet.py --input docs/evidence/av-domain-qwen-av.json
# ...其余同理
```

采样策略：本 harness 与 `bench_flores.py` 不同，跑**全部 300 句**（不 seed 抽样）——语料本身就是自撰固定集，抽样没有额外信号。

## 5. 局限与免责（写死，不回填）

- **域覆盖窄**：AV / 多媒体技术只是众多专业域之一；本报告数字**不外推**到医疗 / 法律 / 金融等域；
- **语料自撰**：300 对由本项目工程师手写，风格偏技术描述，与真实用户素材（会议记录、视频教程、播客、纪录片配音）在句长、口语度、噪声水平上仍有分布差；扩到 500-1000 对并覆盖更多素材型句子挂在 S12 T4；
- **单语言对**：本报告仅 zh↔en；ja/ko/fr/de/es/ru/pt/it/ar 上的 AV 域覆盖挂在 S12 T2，未跑之前**不对其他语对做数字宣称**；
- **术语注入的机制依赖**：madlad / qwen 通过 prompt 消费术语表；argos（Marian 系）不吃这个上下文，Δ 可能为 0——这是**引擎能力边界**，不是 S11 缺陷，写报告时必须明说以免误导；
- **COMET 参考不完美**：COMET-22 在通用域与人工判断相关性高，在窄域技术文本上相关性会下降（flores 报告已观察到 chrF 与 COMET 在 en↔ko / zh↔ja 上的背离）；本报告的 COMET 数字应作为**同引擎同批样本的相对 Δ**解读，不作绝对"好不好"的判断；
- **一次运行 vs 波动**：延迟类指标（如 argos 平均 xx 秒）受本机并发负载影响，质量类指标（COMET/chrF）理论上稳定但依赖模型端点稳定；跨日重跑出现偏差时以证据 JSON 为准；
- **对外披露口径**：本报告的绝对数字（例如"COMET 0.87"）**不单独抽出对外宣传**，必须与"AV 域自撰 300 句、zh↔en、未做过第三方审计"三个限定一起出现（外层 `docs/requirements/REQ-E2-商业与叙事红线.md` §披露约束）。

## 6. 相关

- 任务：外层仓任务台账 `translation/docs/tasks/S11-领域路由与术语包接线.md`、`translation/docs/tasks/S12-AV领域评测基线.md`（本仓 `.gitignore` 已排除嵌套 clone，不内链跨仓文件）
- 需求：`docs/requirements/REQ-B3-置信度门控与质量偏好.md`、`REQ-E2-商业与叙事红线.md`、`REQ-E3-产品场景边界.md`
- 姊妹报告：[flores-benchmark.md](flores-benchmark.md)（通用域外部基准）、[translation-benchmark.md](translation-benchmark.md)（108 句回归基线）
- 术语包：[glossary-packs/av-zh-en.json](glossary-packs/av-zh-en.json)（266 条）、装载器 `script/install_glossary_pack.py`
- 语料：`script/eval-data/av-zh-en/av-zh-en.{src,tgt}`（300 对）

---

## 变更记录

- 2026-10-03：建骨架（本报告当前所有数字均为 `_待填_` 占位，无一条实测）。骨架落库的意义：让 S11 apply 后**跑一次填一次**，结构与解读框架不再临场讨论。骨架不宣称任何数字，因此不违反 REQ-E2 披露纪律。
- 回填触发条件：见 §4 前提四条全部满足 + 完成 §4 六次跑批 + §5 局限段落保持不动（除非有实质变化）。
