# FLORES-101 外部质量基准报告（2026-10-03 首发 / 2026-10-05 补全 22 方向）

> 定位：本报告是 Wonslate 翻译质量的**对外口径**（世界公认测试集 + 公认指标）；自建 108 句集（[translation-benchmark.md](translation-benchmark.md)）继续作为**回归基线**（引擎升级时防回退）。两套口径的分工见 REQ-B3。
> 数据：FLORES-101 devtest（Meta 等，CC-BY-SA 4.0），每语言 1012 句人工参考；每方向固定随机抽样 100 句（seed=42，各引擎输入完全相同）。原始证据（逐句假设译文 + 每样本 COMET 分）：[evidence/flores-benchmark-*.json](evidence/)——qwen 主表 18 方向 + qwen pt/it 补测 4 方向（`flores-benchmark-qwen.json` / `flores-benchmark-qwen-ptit.json`）、madlad 22 方向（`flores-benchmark-madlad.json`）、argos en↔zh（`flores-benchmark-argos.json`）。
> 指标：**COMET-22**（Unbabel/wmt22-comet-da，WMT 主流神经评估指标，与人工判断相关性最高）为主口径；chrF++/BLEU（sacrebleu，CJK 目标用字符级）为参照。引擎均按产品真实路径调用（sidecar /translate、Ollama /v1/chat/completions + engine/ollama.rs 同款提示词）。
> 2026-10-05 更新：基准扩样至 **11 语言 / 22 方向**（新增 en↔pt、en↔it），qwen 与 madlad 均已 22/22 全量跑通，0 翻译错误。

## TL;DR

| 引擎 | 方向 | 平均 chrF | **平均 COMET** | 平均延迟* | 状态 |
|---|---|---|---|---|---|
| qwen3:8b（L3 精译档） | 22/22 | 52.7 | **0.8817** | 3.58s | ✅ 完整 |
| madlad-3B（L2 全语言档） | 22/22 | 55.2 | **0.8847** | ~15s* | ✅ 完整 |
| argos（L2 实时档） | 2（en↔zh） | 40.5 | **0.8406** | 0.70s | ✅ 完整 |

\* 延迟在本机有其他重负载任务并行时测得，绝对值偏高约 2-3 倍，仅供相对参考；质量分数不受影响。

**结论一句话**：qwen3:8b 精译档在公认基准上全 22 方向平均 **COMET 0.882**（COMET-22 刻度上 0.85+ 属于高质量区间），支持"高质量多语言翻译"的产品宣称；en→ja 达 0.906、en→pt 达 0.898。

**一个意外发现**：22 方向同口径对比中，**madlad-3B 平均 COMET 0.8847，反而比 qwen 精译档高 0.003**（14/22 方向 madlad 胜出）。差距在抽样噪声范围内（±0.005-0.01），但这意味着 madlad 作为纯 CPU/NPU 可跑的全语言兜底档，质量并不比 8B LLM 精译档差——**路由策略中"罕见语言对才用 madlad"的保守设定可以放宽**，详见下文"档位选型建议"。

## qwen3:8b 逐方向成绩（100 句/方向，22 方向）

| 方向 | chrF | BLEU | **COMET** | 方向 | chrF | BLEU | **COMET** |
|---|---|---|---|---|---|---|---|
| en→zh | 37.1 | 42.7 | 0.8880 | zh→en | 57.4 | 28.1 | 0.8783 |
| en→ja | 36.2 | 41.8 | **0.9059** | ja→en | 53.5 | 23.3 | 0.8772 |
| en→ko | 31.0 | 36.3 | 0.8841 | ko→en | 55.6 | 26.9 | 0.8811 |
| en→fr | 65.6 | 44.2 | 0.8833 | fr→en | 66.4 | 43.0 | **0.8974** |
| en→de | 57.8 | 31.1 | 0.8681 | de→en | 65.3 | 39.7 | 0.8923 |
| en→es | 52.8 | 24.2 | 0.8678 | es→en | 58.2 | 27.0 | 0.8773 |
| en→ru | 50.4 | 25.9 | 0.8787 | ru→en | 59.9 | 32.6 | 0.8689 |
| en→ar | 44.5 | 17.0 | 0.8432 | ar→en | 63.3 | 37.5 | 0.8778 |
| en→pt | 66.7 | 43.4 | 0.8975 | pt→en | 69.1 | 44.8 | 0.8957 |
| en→it | 53.7 | 27.5 | 0.8854 | it→en | 60.4 | 31.5 | 0.8809 |
| zh→ja | 29.2 | 32.9 | 0.8955 | ja→zh | 25.2 | 29.3 | 0.8723 |

观察：

- **全部 22 方向 COMET ≥ 0.84**，最低是 en→ar（0.8432）——与 108 句基准的发现一致（形态丰富语言方向最难）；
- **chrF 与 COMET 严重背离**：en→ko chrF 只有 31.0 但 COMET 0.884、zh→ja chrF 29.2 但 COMET 0.896——字符级指标对译法灵活的语义等价翻译系统性低估，这正是对外口径必须用 COMET 的原因；
- zh→ja / ja→zh 直接互译（不经英语中转）COMET 0.896 / 0.872，验证了多语检查点的直译价值；
- **pt/it 补测（2026-10-05）**：en↔pt 0.8975 / 0.8957 是全表第二高的一对；en↔it 0.8854 / 0.8809 属中游。en→it 的 0.8854 是修复 LLM 提示词语言代码歧义（`"en to it"` 中的 `it` 被模型理解为英语代词，导致输出罗马尼亚语）之后重测的结果，修复前为 0.7745——**该缺陷同时是质量缺陷和 REQ-B2「不静默降级」的典型案例**，语言名映射修复见 `engine/ollama.rs::language_prompt`。

## argos 实时档（en↔zh）

| 方向 | chrF | BLEU | COMET | 延迟 |
|---|---|---|---|---|
| en→zh | 28.8 | 33.3 | **0.8402** | 0.48s |
| zh→en | 52.3 | 23.7 | **0.8410** | 0.92s |

argos 的 COMET 0.84 显著好于其 chrF 排名给人的印象——它作为实时档"够用即可"的定位在公认指标下得到支撑；精译档（qwen）比它平均高 ~4 个 COMET 点。

## madlad-3B 全语言档（100 句/方向，22 方向）

| 方向 | chrF | BLEU | **COMET** | 方向 | chrF | BLEU | **COMET** |
|---|---|---|---|---|---|---|---|
| en→zh | 35.8 | 41.4 | 0.8674 | zh→en | 56.5 | 28.1 | 0.8721 |
| en→ja | 34.4 | 38.9 | 0.8994 | ja→en | 55.3 | 25.9 | 0.8766 |
| en→ko | 38.8 | 44.0 | 0.8904 | ko→en | 57.6 | 30.2 | 0.8918 |
| en→fr | 71.0 | 53.1 | 0.8914 | fr→en | 67.7 | 44.8 | 0.8986 |
| en→de | 64.9 | 42.2 | 0.8900 | de→en | 68.2 | 42.8 | **0.8996** |
| en→es | 54.2 | 25.9 | 0.8669 | es→en | 59.7 | 30.4 | 0.8798 |
| en→ru | 56.5 | 33.0 | 0.8915 | ru→en | 60.7 | 35.2 | 0.8682 |
| en→ar | 52.9 | 23.9 | 0.8667 | ar→en | 64.6 | 39.1 | 0.8818 |
| en→pt | 70.0 | 48.7 | **0.9049** | pt→en | 70.5 | 48.0 | 0.8994 |
| en→it | 56.6 | 30.2 | 0.8834 | it→en | 60.4 | 31.4 | 0.8827 |
| zh→ja | 30.3 | 34.3 | 0.8870 | ja→zh | 27.0 | 30.7 | 0.8734 |

**22/22 方向 0 错误**，2200 句全部成功。

### madlad vs qwen 同口径对比

| 指标 | madlad-3B | qwen3:8b | 差值 |
|---|---|---|---|
| 平均 COMET（22 方向） | **0.8847** | 0.8817 | **+0.0030** |
| 平均 chrF（22 方向） | **55.2** | 52.7 | **+2.5** |
| 最低方向 COMET | 0.8667 (en→ar) | 0.8432 (en→ar) | +0.0235 |
| 方向胜负 | **14 胜 / 8 负** | 8 胜 / 14 负 | — |

madlad 胜出的方向集中在**形态丰富语言**（en→ar +0.024、en→de +0.022、en→ru +0.013、ko→en +0.011）；qwen 胜出的方向集中在**CJK 目标**（en→zh +0.021、zh→ja +0.009、ja→zh +0.001）和 en→ja（+0.007）。

这个分布有明确解释：madlad-3B 是**纯多语对照**模型（无英语中转），在同语族/近语族和非英语母语者视角上更有优势；qwen3:8b 走"英语思维→目标语言"的 LLM 路径，在 CJK 这类训练数据极充足的语言上更稳。**两者是互补而非替代关系**。

### 档位选型建议（可落地）

- **实时档**保持 argos 不变（0.8406，速度 0.7s 档位优势无可替代）；
- **罕见语言对 / 无 GPU 场景**：madlad 单独即可交付 0.88 的质量，不必强制升级到 LLM——这直接支持 `config.rs` 里 `full` 档 `local_engine=madlad, upgrade_engine=ollama` 的**可升级**设计（当前 madlad 是默认、ollama 是可选升级，方向正确）；
- **CJK 主场景**（zh/ja/ko 互译）保留 qwen 精译为默认，因其在 4 个 CJK 方向上稳定领先 0.007-0.021；
- ⚠️ **不建议**据此下调 LLM 档的路由优先级：0.003 的均差落在抽样噪声内（±0.005-0.01），且 madlad 侧延迟为本机并发污染下的粗测值，**吞吐/首字延迟的账还没算**。做决策前需在空载下补测延迟与内存。

## 方法与复现

```bash
pip install -r sidecar/requirements.txt && pip install sacrebleu unbabel-comet "setuptools<81"
# GPU 版 torch（COMET 加速）：pip install torch --index-url https://download.pytorch.org/whl/cu121
# 数据（一次性）：FLORES-101 devtest 解压到 <数据目录>/benchmarks/flores101
#   数据目录由 WONSLATE_DATA_DIR 控制（默认值按操作系统落在用户数据目录，见 docs/hardware-requirements.md §4）；
#   数据想放在别处就用 --flores-root 或环境变量 WONSLATE_FLORES_DIR 指过去。
#   本项仓库使用的镜像源：modelscope download --dataset OmniData/FLORES-101（HF 直连不可达，facebook/flores 为 gated 仓库）
# COMET 权重（一次性）：score_comet.py 默认经 HuggingFace hub 自动拉 Unbabel/wmt22-comet-da（2.3GB，权重不入库）；
#   已有本地权重时用 --ckpt <权重目录>/checkpoints/model.ckpt 指过去（hparams.yaml 需在同一目录）

python script/bench_flores.py --engines madlad --n 100     # 引擎按对拆进程并行更快
python script/bench_flores.py --engines qwen  --n 100
python script/bench_flores.py --engines argos --n 100 --pairs en-zh,zh-en

# 长耗时任务必备：--resume 断点续采（每采完一句原子落盘，中断后重跑跳过已完成样本）
#   仅当证据文件的 sample_ids 与本次抽样完全一致时才会续采，否则拒绝复用（防脏数据）
python script/bench_flores.py --engines madlad --n 100 --resume \
    --out docs/evidence/flores-benchmark-madlad.json

# COMET 评分（写入回证据 JSON；--cpu 可强制 CPU）
python script/score_comet.py docs/evidence/flores-benchmark-qwen.json
# 已有本地权重时：
python script/score_comet.py --ckpt <权重目录>/checkpoints/model.ckpt \
    docs/evidence/flores-benchmark-qwen.json
```

**本机（6GB 显存）上的执行纪律**：ollama 推理与 COMET 评分**必须串行**。两者并发时 COMET 会因显存争抢跑数小时零产出，ollama 侧同时抛 `TimeoutError`。COMET 权重路径建议固定为本地目录，避免每次联网校验。

抽样确定性：`seed=42` 对 1012 句做固定排序抽样，样本 ID 存于证据 JSON 的 `sample_ids`——任何人可用同一命令复现完全相同的输入。数据目录不写单台开发机的盘符路径：默认落在用户数据目录的 `benchmarks/flores101`，由 `tests/test_source_lint.py` 守这条规则（开源仓不得出现某台机器的路径）。

## 局限性

- **100 句/方向**（devtest 共 1012 句）：语料级 COMET 的抽样波动约 ±0.005-0.01，方向间差距小于 0.01 的不宜过度解读；
- 单参考（devtest 每句一个参考译文）——COMET 受此影响小于 chrF/BLEU；
- **延迟数字受本机并发负载污染**（测量期间另有下载/解压任务占满 CPU），引用延迟请以专门的空载测量为准；
- FLORES 句域为新闻/网络文本（Wikinews/Wikipedia），与产品主打场景（涉密文档/批量本地化）有分布差异；
- Flores-101 与 Flores-200 在本报告 11 种语言上同源（200 为 101 的扩展），使用 101 版 devtest 不影响结论方向；许可证 CC-BY-SA 4.0 仅用于评测，不进入产品分发物。
