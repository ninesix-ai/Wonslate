# 硬件配置要求（模型 × 档位 × 硬件门槛）

> 适用于 Wonslate v0.2.x。所有"本机实测"数据来自开发参考机（配置见 §5）；其余为按模型体积推算的估算值，标注为"估算"。
> 模型文件统一放在数据目录 `<data_dir>/models/` 下（Windows 默认 `%LOCALAPPDATA%\Wonslate`，可用 `WONSLATE_DATA_DIR` 覆盖），不占用仓库空间。

## 1. 总览：每个组件需要什么

| 组件 | 模型 / 文件 | 磁盘 | 内存（进程实际占用） | 显存 | 硬件底线 | 实测速度（参考机） |
|---|---|---|---|---|---|---|
| L2 实时引擎 Argos | 每语言对一个包，如 `en_zh` 82 MB、`zh_en` 83 MB | 每对约 85 MB | 每对约 0.2 GB（进程 229 MB 实测） | 不需要 | 任意 x64 CPU | 句级 <0.1 s |
| L2 全语言引擎 MADLAD-400-3B int8 | 单个 CT2 检查点 `model.bin` 2.95 GB | 约 3.0 GB | 约 3.0 GB（进程 3040 MB 实测） | 不需要（刻意走 CPU） | 4 核 CPU + 8 GB 内存 | 句级 0.7–2 s |
| L2 备选 MADLAD-400-7B int8（估算） | 单个 CT2 检查点 | 约 8.3 GB | 约 8–10 GB | 不需要 | 8 核 CPU + 16 GB 内存 | 约为 3B 的 1/3 速度 |
| L3 质量升级 qwen3:8b Q4（Ollama） | `ollama pull qwen3:8b` | 5.2 GB | offload 部分 2–4 GB | 显存 ≥6 GB 可全进 GPU；4 GB 显存自动部分 offload | 6 GB 显存或 16 GB 内存 | 句级 2–8 s（取决于 offload 比例） |
| 语音识别 SenseVoice（int8） | 本机模型目录 | <0.5 GB | 约 1 GB | 可选 | 任意 4 核 CPU | 实时 |
| 语音合成 Kokoro | 本机模型目录 | <0.5 GB | 约 1 GB | 可选 | 任意 4 核 CPU | 实时 |

> 为什么 MADLAD 刻意不用 GPU：CT2 的 CUDA 路径需要额外安装 cuDNN，而 6 GB 显存的笔记本同时要跑 Ollama；3B int8 在 8 核现代 CPU 上已经是亚秒到秒级，GPU 收益不抵部署复杂度。两块业务天然分流：**MADLAD 占 CPU，Ollama 占 GPU**，互不争抢。

## 2. 三档典型配置

### A. 最低可用（文本翻译，离线）
- 任意 4 核 x64 CPU / 8 GB 内存 / 集显即可
- 只装 Argos 包：能实时翻译 en↔zh；其他语言对靠 L3（若不装 Ollama 则不可用）
- 体验：实时模式流畅；Full 模式离线时回落 Argos 质量

### B. 推荐配置（全语言离线，即本文档参考机档位）
- 8 核 CPU / 16 GB 内存 / 独显可选
- 装 MADLAD-400-3B：UI 里 11 种语言任意互译（实际覆盖 450+ 语言码）
- 装 Ollama + qwen3:8b：Full 模式自动升级到 LLM 质量（离线可用）
- 体验：MADLAD 单句 0.7–2 s 适合"文本框翻译"；语音实时模式仍走 Argos（仅 en↔zh）

### C. 高质量档位（估算）
- 12 GB+ 显存（RTX 4070Ti/4080 级）或 32 GB 内存
- 选项 1：MADLAD-400-7B int8 换掉 3B（`--model-dir` 指到 7B 目录即可，下载脚本 `--dest` 同理），质量提升约一档，CPU 速度约降到 1/3，建议配 GPU
- 选项 2：更大的 LLM（qwen3:14b 需显存 10–12 GB；qwen3:32b 需 20–24 GB；显存不足时 Ollama 自动 offload 到内存，速度明显下降）
- 32 GB 内存的纯 CPU 机器可以跑 14B Q4，单句 10–30 s，只适合批量离线任务

## 3. 显存与内存检查方法

```powershell
# 显存总量/占用（Windows，需 NVIDIA 驱动）
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv

# 进程实际内存占用（找 sidecar/Ollama 进程）
Get-Process python,ollama* | Select-Object ProcessName, @{n='RAM_MB';e={[math]::Round($_.WorkingSet64/1MB)}}
```

注意：WMI 的 `Win32_VideoController.AdapterRAM` 对 >4 GB 显存会溢出报错值（本机 4050 6 GB 被报成 4 GB），以 `nvidia-smi` 为准。

## 4. 配置入口（环境变量）

| 变量 | 作用 | 示例 |
|---|---|---|
| `WONSLATE_DATA_DIR` | 模型与基准数据根目录（`LT_DATA_DIR` 旧拼写优先） | Windows `%LOCALAPPDATA%\Wonslate`；Linux `$XDG_DATA_HOME/wonslate` |
| `WONSLATE_FLORES_DIR` | FLORES-101 基准数据根目录（仅评测脚本读；`--flores-root` 又优先于它） | `<数据目录>/benchmarks/flores101` |
| `WONSLATE_ARGOS_EXE` / `WONSLATE_ARGOS_ARGS` | UI 启动时拉起 Argos sidecar 的命令；留空 = 只探测已运行的服务 | `python` / `-m sidecar.ct2_sidecar --backend ct2 --port 11435` |
| `WONSLATE_MADLAD_EXE` / `WONSLATE_MADLAD_ARGS` | 同上，MADLAD 插槽（端口 11436） | `python` / `-m sidecar.ct2_sidecar --backend madlad --port 11436` |
| `WONSLATE_ARGOS_URL` / `WONSLATE_MADLAD_URL` | sidecar 地址覆盖（默认 127.0.0.1:11435/11436） | — |
| `WONSLATE_OLLAMA_MODEL` | L3 升级模型（默认 `qwen3:8b`） | `qwen3:14b` |
| `WONSLATE_OLLAMA_URL` | Ollama 地址（默认 127.0.0.1:11434） | — |

> 端口约定与 Rust 引擎侧 `translator-engine/src/engine/sidecar.rs` 对齐：argos=11435，madlad=11436，改动一侧必须同步另一侧。

## 5. 模型获取

```bash
# Argos（en↔zh，默认对；--list 可看全部可下载对）
python script/fetch_argos_models.py

# MADLAD-400-3B（一个检查点覆盖全部语言对；国内网络自动走 hf-mirror）
python script/fetch_madlad_model.py

# Ollama LLM
ollama pull qwen3:8b
```

模型落盘位置（默认）：`<data_dir>/models/argos`、`<data_dir>/models/madlad`。换 7B：把 CT2 转换目录（含 `model.bin` + `spiece.model`）放到任意路径，启动 sidecar 时 `--model-dir` 指过去。

## 6. 开发参考机（本文档实测数据来源）

| 项 | 值 |
|---|---|
| CPU | AMD Ryzen 7 7840H（8 核 16 线程） |
| 内存 | 32 GB |
| GPU | NVIDIA RTX 4050 Laptop，6 GB 显存（nvidia-smi 实测 6141 MiB） |
| Python | 3.10.11（ctranslate2 4.8.2 + sentencepiece 0.1.99，均为锁定版本） |
| Ollama | 0.34.0 |

翻译质量与速度的三引擎对比实测见 [translation-benchmark.md](translation-benchmark.md)。
