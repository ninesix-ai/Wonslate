# 翻译质量对比基准报告（2026-10-02）

> **口径分工（2026-10-03 起）**：本报告（自建 108 句集）是**回归基线**——引擎升级后防回退的内部参照；**对外宣称一律使用 FLORES-101 + COMET 公认基准**（[flores-benchmark.md](flores-benchmark.md)，qwen3:8b 全 18 方向平均 COMET 0.8798）。两套口径不可互相替代。
>
> 引擎实测于开发参考机（Ryzen 7 7840H / 32 GB / RTX 4050 6 GB），硬件档位说明见 [hardware-requirements.md](hardware-requirements.md)。逐句原始记录（每个假设译文 + 每次延迟）在 [evidence/translation-benchmark.json](evidence/translation-benchmark.json)。

## 结论（TL;DR）

| 引擎 | 覆盖方向 | 平均 chrF | 平均 BLEU | 平均单句延迟 | 定位 |
|---|---|---|---|---|---|
| argos（Marian 包 ×2） | 2（仅 en↔zh） | 49.5 | 37.0 | **0.08 s** | 实时语音链路的速度档 |
| **madlad（MADLAD-400-3B int8）** | **18（全部测试对）** | **66.2** | **52.7** | 3.10 s | 离线全语言的主力档 |
| **qwen:qwen3:8b（Ollama LLM）** | **18（全部测试对）** | **66.3** | **52.8** | **1.68 s** | 质量升级档（L3） |

三个要点：

1. **Wonslate 的多语言能力已经落地**：MADLAD 一个检查点让 UI 的 11 种语言任意互译（模型本身覆盖 450+ 语言码），不再只有 en↔zh。
2. **madlad 与 qwen3:8b 总分打平（66.2 vs 66.3），但强项互补**——欧洲语言对 madlad 占优，CJK 相关方向 qwen 占优（见 §3）。Full 模式"本地保底 + LLM 升级"的双层结构因此有真实收益，而非摆设。
3. **argos 依旧不可替代的是延迟**：0.08 s 对 2-3 s，实时语音模式只能用它；质量上 en→zh 与 madlad 差距比预想小（38.0 vs 42.0）。

## 1. 测试集与方法

- **测试集**：`script/bench_translation_pairs.json`，9 个语言对 × 双向 × 6 句 = 每引擎 108 条。语言对：en↔zh、en↔ja、en↔ko、en↔fr、en↔de、en↔es、en↔ru、en↔ar、zh↔ja（最后一对验证多语模型"任意两语直译"，无需过英语中转）。
- **句子设计**：日常/科技/商务/文化/出行（含数字）/观点六类，8-20 词，语义明确无歧义。
- **参考译文**：专家级人工风格译文，为本基准专门撰写（英文原句为自创内容，避免测试集污染）。
- **评分**：[sacrebleu](https://github.com/mjpost/sacrebleu) 语料级 **chrF**（CJK 目标用纯字符级，其余用 chrF++ 字符+词级）与 **BLEU**（CJK 目标用 char 分词器）。chrF 对中文/日文更稳，BLEU 作交叉参照。
- **调用方式**：三引擎均按产品真实路径走 HTTP——argos/madlad 走 sidecar `/translate`（端口 11435/11436），qwen 走 Ollama `/v1/chat/completions`，提示词与 `translator-engine/src/engine/ollama.rs` 逐字一致（温度 0.3、关闭思考）。复现：`python script/bench_translation.py`。
- **延迟口径**：单请求端到端 wall time（含 HTTP），串行发送。

## 2. 完整逐方向成绩

chrF / BLEU（`-` = 引擎无该方向模型，显式报错拒绝，**不会静默给出错误翻译**）：

| 方向 | argos | madlad | qwen3:8b | 最快 |
|---|---|---|---|---|
| en→zh | 38.0 / 44.1 | 42.0 / 47.7 | **47.1 / 53.5** | argos 0.08s |
| zh→en | 60.9 / 30.0 | **72.2 / 50.7** | 69.1 / 46.0 | argos |
| en→ja | - | 40.4 / 46.3 | **45.0 / 49.3** | qwen 1.96s |
| ja→en | - | 64.3 / 31.1 | **73.9 / 52.3** | qwen |
| en→ko | - | 41.7 / 47.9 | **38.3** chrF / **47.9** BLEU | qwen 2.29s |
| ko→en | - | 66.5 / 41.8 | **69.1 / 39.8** | qwen |
| en→fr | - | **79.2 / 63.9** | 68.8 / 52.7 | qwen 1.83s |
| fr→en | - | **78.7 / 60.1** | 78.3 / 60.4 | qwen |
| en→de | - | **77.7 / 53.3** | 74.5 / 51.2 | qwen 2.01s |
| de→en | - | **83.3 / 70.9** | 80.5 / 68.6 | qwen |
| en→es | - | **86.0 / 71.9** | 76.0 / 56.5 | qwen 1.90s |
| es→en | - | 80.8 / 63.6 | **83.5 / 69.6** | qwen |
| en→ru | - | **70.9 / 45.3** | 67.7 / 38.4 | qwen 2.16s |
| ru→en | - | 80.9 / 63.1 | **82.2 / 67.3** | qwen |
| en→ar | - | **59.5 / 28.1** | 55.3 / 22.3 | qwen 1.95s |
| ar→en | - | **83.9 / 67.9** | 83.2 / 69.6 | qwen |
| zh→ja | - | 32.0 / 37.9 | **46.4 / 52.4** | qwen 1.76s |
| ja→zh | - | 52.2 / 57.6 | **55.0 / 59.2** | qwen |

## 3. 关键发现

**引擎强项互补，不是简单的"LLM 全赢"：**

- **madlad 赢的方向**集中在罗曼/日耳曼语系向：en→es（86.0 vs 76.0，差距最大）、en→fr（79.2 vs 68.8）、en→de、en→ru。MADLAD-400 的训练配比在欧洲语言对上极其密集。
- **qwen3:8b 赢的方向**集中在 CJK 相关：en→zh、ja↔en、zh↔ja（zh→ja 46.4 vs 32.0，差距第二大）、ko→en。LLM 对东亚语言的语序与敬语处理明显更好。
- **两个共同弱项**：进 CJK 和阿拉伯语方向（en→ko 38-42、en→ja 40-45、en→ar 55-59）。3B/8B 量级模型对目标语形态丰富语言（阿语词形、日语敬体）的一致性有限；换 7B MADLAD 或更大 LLM 预计改善（见硬件文档 §2C）。

**按方向选引擎是真实可行的优化**：路由表可配置（`WONSLATE_MADLAD_URL` / 路由 JSON），例如对 ja→en 直接走 qwen、跳过 madlad 保底，能省一半延迟。

**argos 的 chrF 偏低有一部分是标点机制**：argos 中文输出用半角标点（`云计算…硬件.`），参考译文用全角（`，。`），字符级指标逐字扣分。语义质量差距没有 38.0 vs 47.1 看起来那么大，但全角标点确实是产品级中文输出的硬要求，这是 argos 作为实时档的真实代价之一。

## 4. 定性样例（同一句子的三引擎输出）

**en→zh**（"Cloud computing lets small companies rent powerful servers instead of buying their own hardware."）

| 引擎 | 输出 |
|---|---|
| 参考 | 云计算让小公司可以租用强大的服务器，而不必自己购买硬件。 |
| argos | 云计算让小公司租用强大的服务器，而不是购买自己的硬件.（半角句号） |
| madlad | 云计算让小型公司可以租用功能强大的服务器，而不是购买自己的硬件。 |
| qwen3:8b | 云计算使小型企业能够租用强大的服务器，而不是购买自己的硬件。 |

**zh→ja**（"这款应用支持离线翻译，在没有网络的地方也能用。"——验证非英语中转的直译）

| 引擎 | 输出 |
|---|---|
| 参考 | このアプリはオフライン翻訳に対応しているので、ネットがない場所でも使えます。 |
| madlad | このアプリケーションはオフライン翻訳をサポートしており、ネットワーク接続がない場所でも使える。 |
| qwen3:8b | このアプリはオフライン翻訳をサポートしており、ネットワークがない場所でも使用できます。 |

两句三引擎都达意；差异在文体与搭配，不在正确性。

## 5. 局限性（读数字之前）

- **每方向仅 6 句**：语料级 chrF 在这个规模下波动约 ±2-4 分，方向间差距小于 5 分的结论不要过度解读。
- **单参考译文**：chrF/BLEU 都是对照单一参考计算的；译文天然多样，分数系统性偏低，**跨引擎相对比较有效，绝对值不代表"正确率"**。
- **参考译文由同一位作者撰写**（本基准集随仓库提交，可复查），存在作者风格偏置。
- **延迟为单请求串行**：不含并发争抢；qwen 的 1.68 s 依赖 RTX 4050 的 GPU offload，纯 CPU 机器上 qwen 会显著变慢（见硬件文档 §2C）。

## 6. 复现

```bash
pip install -r sidecar/requirements.txt && pip install sacrebleu
python script/fetch_argos_models.py            # argos 基线（可选）
python script/fetch_madlad_model.py            # ~3 GB
ollama pull qwen3:8b                           # 可选

# 三个终端分别启动（或让 Wonslate UI 拉起）：
python -m sidecar.ct2_sidecar --backend ct2 --port 11435
python -m sidecar.ct2_sidecar --backend madlad --port 11436
ollama serve

python script/bench_translation.py             # 全部引擎；--engines madlad,qwen 可选子集
```
