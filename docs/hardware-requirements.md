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
| `WONSLATE_SIDECAR_TIMEOUT_MS` | 引擎侧对 sidecar **单次请求**的超时（默认 30000 ms；`LT_SIDECAR_TIMEOUT_MS` 旧拼写优先） | `5000` |
| `WONSLATE_SIDECAR_RETRY_MS` | 引擎侧为一个 `429 busy` **最多等多久**（默认 12000 ms；`LT_SIDECAR_RETRY_MS` 旧拼写优先）。只在服务带 `--decode-slots` 启动时才会花到，因此对现有部署是零代价；它只封顶等待，不封顶翻译，所以最坏耗时是它加上上面那个超时。**预算是按整步 `Retry-After` 花的**（一步 = 服务自己 p50 的上取整，负载越重越大）：小于一步的非零值不会睡任何一秒，行为等同于 `0` | `0` = 不等，被拒即降级；`20000` ≈ 在 p50 升至 7 s 时还能等两步 |
| `WONSLATE_OLLAMA_MODEL` | L3 升级模型（默认 `qwen3:8b`） | `qwen3:14b` |
| `WONSLATE_OLLAMA_URL` | Ollama 地址（默认 127.0.0.1:11434） | — |

> 端口约定与 Rust 引擎侧 `translator-engine/src/engine/sidecar.rs` 对齐：argos=11435，madlad=11436，改动一侧必须同步另一侧。
>
> 上表两个 `*_SIDECAR_*_MS` 是引擎侧（Rust）读数，不是 sidecar 进程的启动参数；`--decode-slots` / `--max-requests` / `--queue-wait-ms` 这类才是 sidecar 自己的（见 [sdk.md](sdk.md#81-长批次不用猜的重载与自退)）。
>
> `--queue-wait-ms` 有一条 **3600 s 上限**（超出会被夹到上限）：在这个旋钮上"更大 = 更宽容"并不成立——超过平台定时锁的表示范围后 CPython 不再等待而是抛 `OverflowError`，异常逃出 handler 后调用方看到的是**连接被断开**（登记为 D46，已于 2026-10-11 修）。配它的法则是 **W ≥ 排在该调用方前面的全部解码**（不是"一个解码"），且调用方自己的 HTTP 超时需要 ≥ `W + 单次解码`，否则服务还在排队而调用方已经先走。**队列深度可从 `GET /health` 的 `decodes_queued` 字段直接读出**（D47，2026-10-11 加入）：那是当时在 `acquire(timeout=W)` 里等待槽位的调用方人数，`--decode-slots` 与 `--queue-wait-ms` 任一未开启时恒为 `0`。
>
> **桌面端 `/translate` 的超时链（2026-10-11 审计，同日订正补全）**：一次桌面翻译从点击到拿到结果，**串行走四层超时**（从外到内），队列只作在最内层：
>
> | 层 | 变量 / 默认 | 是否可配 | 作用 |
> |---|---|---|---|
> | ① **.NET UI 逐次逻辑超时** | `MainViewModel.TranslateTimeout` 内置 **20 s**，超时**自动重试 1 次**（`TranslateRetries=1`） | 代码常量，**无环境变量开关** | 包住整个 `tt_translate_full` FFI 调用；超时时原生调用**仍在后台跑完、结果被丢弃**，而重试会**再发一次**（即第二次解码叠在第一个之后） |
> | ② .NET 探活 HTTP（`/health` `/readyz` `/warmup`） | 内置 **1500 ms** | 否 | 不包 `/translate`；因 `/health`/`/readyz` 不走解码闸门而**不受队列影响** |
> | ③ Rust→sidecar 单次读超时 | `*_SIDECAR_TIMEOUT_MS` 默认 **30000 ms**（+ 429 退避预算 12 s） | 是 | `tcp_roundtrip` 的 socket 读超时；**唯一能兜住服务端的队列与解码**的那一段 |
> | ④ sidecar 排队 | `--queue-wait-ms W`（0=立即拒） | 启动参数 | 只在 `--decode-slots` 开启时存在 |
>
> **两个直接后果**（均由代码读实，非推测）：
> - **① 比 ③ 更先生效**。上一版本本节列过“三段超时、只有 Rust 那一段兜得住队列”，**遗漏了最外层的 .NET 20 s**，现已补上。于是 `*_SIDECAR_TIMEOUT_MS ≥ W + 本句解码` 仍是必要条件、但对桌面**不够**：即使 ③ 盖住了 W，① 也会在 20 s 先撒手。**且 `TranslateTimeout` 目前不走环境变量**，开大队列的人无法靠配置抬高它（要改代码），所以 `--queue-wait-ms` 对**交互式桌面基本不适用**，它是给仓外批量调用方的。
> - **页面量级粘贴本就会撞 ①**：sidecar 对多行输入是**逐行串行解码、整份请求只占一个槽**（`_translate_lines`），所以多行粘贴的服务端耗时 ≈ 各行之和。一轮实测 madlad 110 词单条就 ~25 s（见 `docs/evidence/queue-wait-madlad.json`），已超 20 s。此时默认无闸门（`--decode-slots` 关），① 重试的第二次解码会与第一次未中止的后台解码**重叠**——正是 D30② 量过的“被弃解码拖慢仍被需要的请求”。
>
> 安全组合（批量调用方，含仓外）：**`min(客户端逐次超时) ≥ W + 本句解码`**，桌面的“客户端逐次超时”是①的 20 s 而非③的 30 s；③ 需 ≥ ① 才有意义。退避预算 `*_SIDECAR_RETRY_MS` **对排队完全不参与**（排队等待不回 `429`，handler 在信号量上睡着、一个字节都不写）。行为本身诚实：① 超时会在 UI 显式报“翻译超时（20s）”、从不假成成功（REQ-B2 未破）；但重试叠加解码是产品待定的取舍（是否抬高 20 s / 是否抑制会对未中止解码重叠的重试）。

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
