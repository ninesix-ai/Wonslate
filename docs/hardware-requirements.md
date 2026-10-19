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
| `WONSLATE_ARGOS_URL` / `WONSLATE_MADLAD_URL` | sidecar 地址覆盖（默认 127.0.0.1:11435/11436）。**隐私模式（`privacy=true`）下只接受回环地址**：指到非回环地址时该引擎不会被调用，而是钳制为内置 `demo` 并在 `message` 里点名被拒的地址（D68）；非隐私请求不受此限制，可照旧指向局域网／容器地址 | — |
| `WONSLATE_SIDECAR_TIMEOUT_MS` | 引擎侧对 sidecar **单次请求**的超时（默认 30000 ms；`LT_SIDECAR_TIMEOUT_MS` 旧拼写优先） | `5000` |
| `WONSLATE_SIDECAR_RETRY_MS` | 引擎侧为一个 `429 busy` **最多等多久**（默认 12000 ms；`LT_SIDECAR_RETRY_MS` 旧拼写优先）。只在服务带 `--decode-slots` 启动时才会花到，因此对现有部署是零代价；它只封顶等待，不封顶翻译，所以最坏耗时是它加上上面那个超时。**预算是按整步 `Retry-After` 花的**（一步 = 服务自己 p50 的上取整，负载越重越大）：小于一步的非零值不会睡任何一秒，行为等同于 `0` | `0` = 不等，被拒即降级；`20000` ≈ 在 p50 升至 7 s 时还能等两步 |
| `WONSLATE_OLLAMA_MODEL` | L3 升级模型（默认 `qwen3:8b`） | `qwen3:14b` |
| `WONSLATE_OLLAMA_URL` | Ollama 地址（默认 127.0.0.1:11434） | — |
| `WONSLATE_UI_TRANSLATE_TIMEOUT_MS` | **桌面 UI 侧**（.NET）一次翻译的逐次超时（默认 **45000 ms**；`LT_UI_TRANSLATE_TIMEOUT_MS` 旧拼写优先）。这是超时链**最外层**那一段（包住整个 `tt_translate_full` FFI 调用），不是引擎侧的 `*_SIDECAR_*_MS`。默认值取在 Rust 最坏耗时（`retry 12s + timeout 30s = 42s`）之上；超时后**不再自动重试**（重试会叠一个未中止的后台解码）。非正整数/脏值一律回落默认 | `60000`（给页面量级长粘贴留余量） |

> 端口约定与 Rust 引擎侧 `translator-engine/src/engine/sidecar.rs` 对齐：argos=11435，madlad=11436，改动一侧必须同步另一侧。
>
> **改了 `WONSLATE_*_ARGS` 却“看起来没生效”（缺陷 D73，已于 2026-10-16 修）**：桌面端与评测脚本都按上面的固定端口起 sidecar，再轮询 `GET /health` 判存活。上一次会话残留的实例（或你自己常驻的那个）会占着端口：新拉的进程 bind 不上而退出，健康检查却仍由旧实例应答。修前有三处都会把这读成“就绪”（.NET 的 `SidecarManager`、`script/run_av_baseline.py`、`script/bench_tm.py`），于是新加的 `--decode-slots`、`--max-requests` 或换了模型目录**全部不会生效**，而界面一切正常。现在启动方会比对 `/health` 报的 `pid` 与自己拉起的那个进程：对不上就**判失败**，并在诊断行写明“端口被另一个 sidecar 占用”或“本程序启动的 sidecar 已退出”；它**不会去杀那个应答者**（那可能是你有意常驻的服务）。处置：先找出占端口的进程 `Get-NetTCPConnection -State Listen -LocalPort 11436 | Select-Object OwningProcess`，或按 §4 把 URL 指到别的端口。
>
> 上表两个 `*_SIDECAR_*_MS` 是引擎侧（Rust）读数，不是 sidecar 进程的启动参数；`--decode-slots` / `--max-requests` / `--queue-wait-ms` 这类才是 sidecar 自己的（见 [sdk.md](sdk.md#81-长批次不用猜的重载与自退)）。
>
> `--queue-wait-ms` 有一条 **3600 s 上限**（超出会被夹到上限）：在这个旋钮上"更大 = 更宽容"并不成立——超过平台定时锁的表示范围后 CPython 不再等待而是抛 `OverflowError`，异常逃出 handler 后调用方看到的是**连接被断开**（登记为 D46，已于 2026-10-11 修）。配它的法则是 **W ≥ 排在该调用方前面的全部解码**（不是"一个解码"），且调用方自己的 HTTP 超时需要 ≥ `W + 单次解码`，否则服务还在排队而调用方已经先走。**队列深度可从 `GET /health` 的 `decodes_queued` 字段直接读出**（D47，2026-10-11 加入）：那是当时在 `acquire(timeout=W)` 里等待槽位的调用方人数，`--decode-slots` 与 `--queue-wait-ms` 任一未开启时恒为 `0`。
>
> **桌面端 `/translate` 的超时链（2026-10-11 审计 → 同日已改代码）**：一次桌面翻译从点击到拿到结果，**串行走四层超时**（从外到内）。最外层原本是固定 20 s 的代码常量，现已抬到 45 s 默认、可环境变量覆盖、且不再自动重试：
>
> | 层 | 变量 / 默认 | 是否可配 | 作用 |
> |---|---|---|---|
> | ① **.NET UI 逐次逻辑超时** | `MainViewModel.TranslateTimeout` 默认 **45 s**（`*_UI_TRANSLATE_TIMEOUT_MS`）；超时**不再自动重试**（`TranslateRetries` 默认 0） | **是**（env 可配） | 包住整个 `tt_translate_full` FFI 调用；超时时原生调用**仍在后台跑完、结果被丢弃**（不可中止） |
> | ② .NET 探活 HTTP（`/health` `/readyz` `/warmup`） | 内置 **1500 ms** | 否 | 不包 `/translate`；因 `/health`/`/readyz` 不走解码闸门而**不受队列影响** |
> | ③ Rust→sidecar 单次读超时 | `*_SIDECAR_TIMEOUT_MS` 默认 **30000 ms**（+ 429 退避预算 12 s） | 是 | `tcp_roundtrip` 的 socket 读超时；**唯一能兜住服务端的队列与解码**的那一段 |
> | ④ sidecar 排队 | `--queue-wait-ms W`（0=立即拒） | 启动参数 | 只在 `--decode-slots` 开启时存在 |
>
> **为何默认是 45 s 而不是随便抬大**：① 包住的是整个原生调用，而 Rust 侧一次 `/translate` 本身最坏可合法地花 `busy_retry 12s + timeout 30s = 42s`（正当等 `429` 退避时）。旧 20 s 会在引擎做对事的中途就撒手；45 s 恰好高于这个 42s 最坏值，使外层不再抢在内层前面。
>
> **为何关掉自动重试**：原生调用不可中断，重试会再发一个全新解码叠在未中止的后台解码上；默认无闸门（`--decode-slots` 关）时两者重叠、拖慢仍被需要的请求（D30② 量到 2.5x）。重试机制保留为显式可配（测试钉 `TranslateRetries=1`），不再是出厂放大器。
>
> **两个仍成立的链事实**（代码读实）：① `*_SIDECAR_TIMEOUT_MS ≥ W + 本句解码` 对桌面只是必要条件——外层①也得盖住（现已默认 45 s 且可调）；且多行粘贴在服务端**逐行串行、整份只占一槽**（`_translate_lines`），耗时≈各行之和，实测 madlad 110 词单条 ~25 s（`docs/evidence/queue-wait-madlad.json`），仍在 45 s 内可拿回，超长需分段或调大①。② 退避预算 `*_SIDECAR_RETRY_MS` **对排队完全不参与**（排队等待不回 `429`）。行为诚实：① 超时会显式报“翻译超时（45s）”、从不假成成功（REQ-B2 未破）。

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

### 5.1 来源与完整性策略（2026-10-15 起）

三个下载脚本（`fetch_argos_models.py` / `fetch_madlad_model.py` / `fetch_voice_models.py`）现在走**同一条**策略（`script/net_guard.py`）：

| 规则 | 内容 |
|---|---|
| 传输 | 只允许 `https`；**每一次重定向重新判定**（不是一次信任） |
| 主机 | 各脚本自带白名单：HuggingFace 及 `*.hf.co` CDN、`argos-net.com`、`github.com` 与 `*.githubusercontent.com`。后缀按**标签边界**匹配，所以 `huggingface.co.evil.example` 这类拼接不会被误放行 |
| 地址 | 连接前解析域名并拒绝私有／回环／链路本地／保留地址（这个工具可以用 `$HF_ENDPOINT` 指向镜像，因此需要防 SSRF） |
| 完整性 | 每个文件比对**钉住的 sha256**，不匹配就删除临时文件并报错；**已缓存的文件同样复核**（缓存分支是正常运行的快路径，只比大小的话会接受一个同长度的替换件）。自 2026-10-18（缺陷 D80）起**还单独核对长度**：服务器声明的 `Content-Length` / `Content-Range` 总量与实读字节数不等就是失败——哈希回答的是“是不是这一份”，它不回答“是否到齐”，而无钉值的资产（`--allow-unpinned`）此前一个被截断的下载会以“成功”落盘 |
| 无钉值 | **默认拒绝**并说明原因；`--allow-unpinned` 可显式放行，且会把“这个资产未钉值”打印出来 |

钉值的**出处**分两级，脚本里逐条标注（一个没有来源的哈希是主张，不是证据）：

- **上游权威**：MADLAD 的 `model.bin`、`spiece.model` 取 HuggingFace LFS 公布的 sha256，并已和本机现有副本**逐字节核对一致**；语音侧 `silero_vad.onnx`、`kokoro-int8-multi-lang-v1_1.tar.bz2` 取 GitHub release 的 `digest` 字段（前者同样与本机一致）。
- **本地基线**：Argos 的官方 index **完全不提供校验和**（2026-10-15 实测其字段只有 `argos_version`/`code`/`from_*`/`to_*`/`links`/`package_version`），所以这两个包的哈希是从在用副本算出来的——它们能防**今后的漂移与替换**，不声称能证明**当初字节的来源**。MADLAD 的两个非 LFS 小文件同理（上游只给 git blob 的 sha1，不是同一函数，无法对照）。
- **有意留空**：`sherpa-onnx-sense-voice-…tar.bz2`（约 1.0 GB）上游 `digest` 为空，本机也没有可作基线的副本，因此不填——取它需要 `--allow-unpinned`。写一个空值会读成“已校验”，恰好相反。

Argos 还有一层：包的真实下载地址来自那份 index 的 `links` 数组（旧代码取 `links[0]`，而实测 100 条中有 25 条是 `ipfs://`）。现在**先按白名单筛再取**，被跳过的条目会打印出来，不静默丢弃；因此一份被替换的 index 只能造成拒绝，不能造成投毒——被钉的包仍要命中自己的哈希。

> 对你意味着什么：若上游**重新打包**同名文件（Argos 官方升级包内容而文件名不变是最可能的一种），脚本会以 `REFUSED: checksum mismatch` 停下而不是静默收下新字节。这是有意的：请先核对新值的来源（上游发布页或 release 的 `digest`），再更新脚本顶部的 `EXPECTED_SHA256`，并在其 `source` 字段里写清日期与出处。`--allow-unpinned` 只用于临时取用，不该成为默认。

### 5.2 版本记录与“上游有没有新版”（2026-10-17 起）

上面的策略只回答“是不是那份字节”；本节回答另一半：“装的是哪一版、怎么知道上游发了新版”（GA-13）。

三个下载脚本成功装好后，会在**自己的目录里**留一份 `wonslate-origin.json`：哪个版本、字节哈希、来源 URL、何时取的。三家上游对“版本”的说法本来就不同，这里如实分开记，**不抹成一个字符串**：

> **2026-10-18 订正（缺陷 D81）**：上面那句“一个目录一份记录”在**目录里不止一个资产时是错的**。MADLAD 一个目录装四个文件（`model.bin`、`spiece.model`、`shared_vocabulary.json`、`config.json`），旧实现每个资产写一次同一个文件，于是**后一次写抹掉前一次**，留下的那条声称整个目录就是 `config.json`——而读取侧回答 `ok`。现在文件内部**按资产分栏**：
>
> ```json
> { "format": 2, "assets": { "model.bin": {…}, "config.json": {…} } }
> ```
>
> 读取必须**指名资产**；不指名而目录里有多个候选时回答“歧义”而不是挑一个，问了未记录的资产时回答“没有”而不是返隔壁那条。旧格式（一个光对象）仍读（已装机器上有），合并时升级为格式 2。每条还记服务器给的 `etag` / `last_modified`（它发了才有），用途见 §5.5。

| 资产 | 版本从哪来 | 记法 |
|---|---|---|
| Argos | 官方 index 的 `package_version`（这个字段一直都有，只是过去没人记） | 按语向分别记录，`en_zh` 与 `zh_en` 可以不同版本 |
| 语音（sherpa-onnx） | 比对按 release asset 的 `digest`（GitHub 为它们发布 `sha256:…`）；文件名只在“上游改名/换版”时作为次要信号 | `asset` 记文件名、`kind` 记它所在的长期 release tag（查询必须带 tag，见下） |
| MADLAD | 上游不发版本号；它从 `main` 取，而 `main` 的内容会动 | 记 `revision` + 实际哈希，比对按字节而不是按标签 |

> **2026-10-17 的一次订正（缺陷 D76）**：语音原先写成“按文件名里的版本比”。真打上游才发现两处都错：其一，模型归档在 `tts-models` / `asr-models` 这两个长期 tag 下（各六百余个 asset），而 `/releases/latest` 是**软件版本发布**，里面一个模型文件都没有——所以那个探针从未问对过对象；其二，`silero_vad.onnx` 这类文件名里根本没有版本，按名字比永远看不见上游重新打包。现在按 `digest` 比，与 D69 钉的是同一类证据。`sense-voice` 上游的 `digest` 为 `null`（它本就在 `UNPINNED_EXCEPTIONS` 上），只能比名字，于是如实报 `incomparable` 而不是“已最新”。

MADLAD 现在有版本旋钮：`--rev <分支|tag|commit sha>`，默认 `main`。**要可复现的装机就传 commit sha**，选了什么会写进 origin 记录。

查上游是否已发新版：

```bash
python script/check_model_updates.py                     # 看本机装了什么
python script/check_model_updates.py --baseline          # 对照仓里记录的基线（CI 用的就是这条）
python script/check_model_updates.py --only argos         # 只看 Argos
python script/check_model_updates.py --dest <目录> --only argos
python script/check_model_updates.py --baseline --record-baseline   # 人工接受新版时才推进基线
```

`--baseline` 是后加的，因为**定时任务那台机器上什么模型都没装**：它拿 `script/model_upstream_baseline.json`（本仓库“最后一次看到的上游状态”）作参照，所以任何机器、任何 runner 都能回答“上游动过没有”。基线里的 `sha256`/`version` 是**上游的回答**，不是我们的准入钉值（钉值在各 fetch 脚本的 `EXPECTED_SHA256` 里，D69），两份记录故意分开；没被 live 确认过的条目必须带 `seeded_from` 说明自己从哪抄来。

退出码是**三态**，因为“没查到”与“没有新版”是两件事：`0` **至少做成了一次比对**且无更新、`2` 上游有更新、`1` 至少一处没能查到，**或一处都没比对成功**。

> **最后半句是 2026-10-17 补的（缺陷 D74）**：本节的上一版写着“目录还没有记录时报 `unrecorded` 并保持 `0`——新机器就是新机器，不是错误”。那句话现在是错的，而且错得很值钱：一台什么都没装的机器（以及 GitHub Actions）跑出来全是 `unrecorded`，工具却报 `0` 并打印“没发现更新版本”。把它挂成定时器，得到的就是**每天准时报一切正常**。现在 `0` 要求至少真比过一次，否则 `1`；部分资产没比成时**数字照给、限定同行**（例：`no newer upstream version found (not compared: voice:asr)`）。同一次真跑还抖出 D75：Hugging Face 的 sha256 字段名是 `lfs.oid`（我写的是 `lfs.sha256`，于是健康的装机一律被报成“上游没发布可比摘要”），而 GitHub API 匿名请求会 403 限流——错误文本现在会点名这一点。

它需要访问上游，所以**不进门禁序列**；它的判读逻辑由 `tests/test_model_updates.py` 用注入的上游响应离线覆盖（载荷形状取自 2026-10-17 的真实响应，不是按代码的假设编的）。

### 5.3 上游发了新版怎么自动知道（2026-10-17 起）

上面那条命令仍是“你问它才答”。自动那半边是 `.github/workflows/model-updates.yml`：每天 03:17 UTC 跑一次 `--baseline`，发现上游比基线新就**开一条 issue**（同一个发现只开一次，发现变了就在原 issue 上追加评论——否则每天一条，第三天就有人把它关了）。

几条得说清的边界：

| 事项 | 实际行为 |
|---|---|
| 谁推进基线 | **只有人**。`--record-baseline` 需要显式跑；定时器不推进，否则提醒响一次就再也不响了 |
| 无凭据时的 GitHub | 匿名请求常被 403 限流。workflow 用 runner 自带的 `GITHUB_TOKEN`，且它**只发往 `api.github.com`**（argos/madlad 的探针拿不到它，由测试钉住） |
| 查不到时 | 不开 issue，作业**判失败**（把“没查到”发成告警是错的，把它保持静默更错） |
| 定时任务会自己停 | GitHub 会禁用默认分支 60 天未动的仓库的 schedule。手动拉一次：`gh workflow run model-updates.yml`（默认 `dry_run=true`，只决定不改动） |
| 进门禁吗 | 不进。它要联网；联网才能绿的检查会在沙箱 runner 上变红，然后被关掉（D28/D65 的老路） |

判据在 python 里（`script/report_model_updates.py` 的 `plan()`），不在 YAML 的 shell 里，所以它能被 `tests/test_report_model_updates.py` 离线测：幂等、marker 往返、“`gh` 失败不等于没发布过”、以及**YAML 里写的每个 flag 必须还在 argparse 里**（改名会让定时任务绿着什么都不查）。

### 5.4 下到一半断了怎么办（2026-10-18 起，缺陷 D80）

以前只有一条规则：**失败就删临时文件**。于是重试永远从 0 字节重新开始，2.9 GB 的 MADLAD 检查点碰上一次网络抖动就要重下全部；而被杀掉的进程留下的是一份随机命名的 `.part<random>`，之后没有任何一次运行会再认它。

现在按**失效原因**走两条路：

| 发生了什么 | 已到达的字节 | 为什么这样处理 |
|---|---|---|
| 连不上、中途断线、长度不足 | **保留**在 `同名.part`，旁边一份 `同名.part.meta` 记下它属于哪个 URL、哪个钉值、多少字节 | 这些字节没有错，只是还没到齐 |
| 哈希不匹配、主机不在白名单、无钉值 | **删除** | 错的字节不是“可续传的状态”，留着只会让下一次接着错 |

下一次尝试——同一进程的重试，或隔一天的重新运行——读那份 meta，**只在它写的 URL 与钉值都和本次一致时**才用 `Range` 从那个位置接着要，否则丢掉重来。同名不同物是真实存在的情况（上游重新打包，或 `EXPECTED_SHA256` 被改过），把两代字节拼起来会得到一个既不是新也不是旧的文件。

服务器把 `Range` 当耳边风（回 200 与整份文件）时**从头重写而不是追加**，否则前缀会被算两遍，而多算的前缀在哈希上表现为“字节不对”，看起来像上游换了内容。

三家上游到底怎么配合，是 2026-10-18 用**真实网络探出来的**，不是推的（下面这张表决定“续传能保证到什么程度”）：

| 上游 | 全量 GET 说什么 | 对它发 `Range: bytes=0-1` | 于是这次续传能验到什么 |
|---|---|---|---|
| GitHub（语音） | `Content-Length` ✓、`Accept-Ranges: bytes`、`ETag` | **206 + `bytes 0-1/643854`** | 长度与切片范围都能核，**最强** |
| HuggingFace（MADLAD） | **不发 `Content-Length`** | **206，但既不发 `Content-Range` 也不发 `Content-Length`** | 只能靠钉住的 sha256 证明完整 |
| jsDelivr（Argos index） | **不发 `Content-Length`**（chunked） | **206 但不发 `Content-Range`** | 同上；且是小文件，index 另有结构校验 |

后两行逼出一条额外规则：**一个 `206` 不说自己从哪开始切、而我们手上又没有钉值时，不拼接——丢弃字节并报错**，让下一次尝试拉整份。有钉值时允许拼接，因为最终那把哈希能证明拼出来的东西对不对；无钉值时拼接就是在信任一个拒绝说明自己发了什么的服务器。

两个进程同时拉同一个文件时，`.part` 用**原子重命名**认领：抢到的人续，没抢到的人自己下整份。浪费带宽，但不会出现两个写入者交织进同一个文件。

> 一个已知边界：`kill -9` 或断电会把字节留在 `同名.part.<pid>` 这个**只属于那个进程**的名字上，而后续运行只认领 `同名.part`，所以那一份不会被自动续用（也不会被误用成正式文件——正式名字只在长度与哈希都对上之后才出现）。处理办法就是删掉它。正确性不受影响，受影响的只是一次白下的带宽。
>
> 实测的端到端一次：真实拉取 GitHub 上的 `silero_vad.onnx`（643,854 字节），在第 200,000 字节处**人为切断连接**（真实的断线无法预约，注入点只在读取边界，服务器侧的 206、`Content-Range` 与字节全部是真的），随后重新运行 → 日志 `resuming at 200000 bytes`，最终 sha256 与 D69 的钉值逐字相同，目录里只剩正式文件。

### 5.5 `--force` 不再必然重下（2026-10-18 起，GA-13 那半边的后半）

以前 `--force` 的意思是“把字节全部再拿一遍”，对 2.9 GB 的检查点就是几分钟与几 GB 流量，只为了知道上游没动。现在三个脚本都先问一句：**上一次服务器怎么说的？**（存进 §5.2 那条记录里的 `etag` / `last_modified`）若服务器回 `304`，就是它确认了没变，**一个字节也不拉**。

三条得说清的边界：

| 事项 | 实际行为 |
|---|---|
| **304 不等于“我手上的文件是对的”** | 它只说上游那份没变。所以收到 304 后还要把**本地文件重算一遍哈希**、与记录里的值对账：对得上才保留；对不上就不信这个 304，**丢掉条件头重下一次** |
| **正在续传时不问这个问题** | `Range` 与 `If-None-Match` 同发会让“这一片没变”与“整个文件没变”不可区分，所以续传路径一律不问（§5.4） |
| **问不了就不假装问过** | 无记录、记录里没这个资产、或上游根本没发校验符 ⇒ 不发明一个，直接走全量。三家真实上游里 HuggingFace 与 jsDelivr **不发 `Last-Modified`**，GitHub 两样都发 |

实测（本日一次真网络往返）：拉 `silero_vad.onnx`（643,854 字节，sha256 与 D69 钉值一致）→ 服务器给 `etag: "0x8DDC0492FC5CD24"` 与 `Last-Modified` → 拿它们重请求 → **真实 304**，本地文件原封不动，本次零字节传输。（本机 Python 3.10 把 304 抛成 `HTTPError`；代码两种形状都接——因为不同 urllib 版本会“返一个空体响应”——但**只有抛形这条被真网络跑过**，另一种只靠桩验证。）

它解决了“企业集中分发”里的一半（**离线包**，§5.6）；另一半**私有镜像指向**（§5.7）同日也已做，两半齐备，GA-13 因此转 ✅。

### 5.6 离线包：把模型搬到不联网的机器上（2026-10-19 起，GA-13 剩下的那一格里先做的那件）

`script/offline_bundle.py` 回答的是“集中分发”里**离线包策略**这一半：一台能上网（或已经装好模型）的机器把资产打成一个包，内网／离线那台机器不需要拨出公网就能装。命令：

```
python script/offline_bundle.py pack --src <数据目录>/models --out 交付包.zip
python script/offline_bundle.py list 交付包.zip
python script/offline_bundle.py verify 交付包.zip
python script/offline_bundle.py install 交付包.zip --dest <数据目录>/models [--force]
```

`pack` 以 §5.2 那份 `wonslate-origin.json` 记录为线索找目录：一个目录**没有**这条记录，就**不进包**，不是猜测它是什么、也不是静默跳过（不声不响少装一个模型目录，与 D74“什么都没比却说一切正常”是同一形状）。本机上 `models/madlad` 那份 2.9 GB 检查点就还没有这条记录（它在这个特性之前就在盘上了），所以它不会进包——先在能联网的机器上跑一次 `fetch_madlad_model.py`（缓存命中路径也会补写记录），再打包即可。

它跟 §5.1 的下载校验共享同一套纪律，因为“从 U 盘／内网共享拷过来的一个文件”和“从公网下载的一段字节”要防的是同一件事：

| 事项 | 实际行为 |
|---|---|
| **清单里写了，不等于包里有** | `verify` 拿包里的字节**重算**每个文件的 sha256 与长度，逐项与清单对账：长度不符先报长度（“承诺 16 字节、实际 5 字节”），长度对了再报摘要不符。信任 `wonslate-bundle.json` 自己的声明就是 D69 的错重演 |
| **包里有，不等于清单写了** | 一个没登记在清单里的成员同样判失败——“多出来一个没人记录的文件”与“少一个文件”一样不能装 |
| **包自己完整，不代表它能用** | 内嵌的每条 `wonslate-origin.json` 都要用 §5.2 **同一个解析器**再过一遍，读不懂的整包拒绝——装出一个以后永远答不上“这是哪一版”的目录，等于没装 |
| **断在一半 ≠ 版本冲突** | `CorruptError`（包本身对不上清单，或包打不开）与 `ConflictError`（包是好的，只是目标机上已有一份不同的字节）是两类错。`--force` 只对后者有效；拿 `--force` 去硬装一个哈希对不上的包，正是 §5.1 明令禁止的事 |
| **路径不能借包跳出目标目录** | 成员名里只要有绝对路径形状（盘符冒号、前导斜杠）、空段、`.` 或 `..`，整包拒绝，且在**任何字节落盘之前**判——沿用 `fetch_madlad_model.py` 里 `final_path` 为单个下载文件定的同一条规则，而不是另发明一套 |
| **一次安装要么全成，要么一行不写** | 先验完整性、再查目标机现状、最后才落盘。中途发现有文件坏，不会因为“前面几个是好的”就先装上——半个模型目录比没有更糟 |

装完会在目标目录旁留一份 `wonslate-install.json`：包文件名（只有名字，**不写盘符**，理由同 §5.2 与 D70）、整包的 sha256、打包时刻、安装时刻（UTC 带偏移）、落了几个文件。它**不覆盖**包自带的 `wonslate-origin.json`——那份记的是“这批字节什么时候从上游取的”，这一份记的是“它什么时候到达这台机器的”，两件事分开，否则 §5.4 那种“缓存命中看起来像重新下载过”的误读会换个地方再犯一次。

实测（本日一次真往返，用本机已装的两个 argos 包，共 172 MB）：`pack` 打出 145 MB 的 zip → 把文件改名成另一个名字模拟“经内网共享转手” → `install` 到全新目录 → 逐文件哈希与源目录比对**完全一致**（18 个文件全部落盘）；再次 `install` 同一个包，`files_written=0`（已经是对的字节，不重写，也不是冲突）；把这个包截断一半，`verify` 报“不是可读的 zip”，拒绝装。测试侧另有 31 例覆盖上述每一条判据，且每条判据都配了一个“删掉它就转红”的变异样本（`.mimosa/scratch/mutate_offline_bundle.py`，10/10 全抓，含两处**我自己的测试写得不严、被变异跑出来才发现**的情形：一是“同长度替换”的样本其实长度并不相同，二是“路径穿越”的用例其实被兜底检查接住、没有真正考验到那条专项判据）。

它不改变一件已被 §5.7 补上的事：另一半“私有镜像指向”现也已做，两半齐备后 GA-13 转 ✅（见 §5.7）。

### 5.7 私有镜像指向：让下载脚本走企业内网源（2026-10-19 起，“企业集中分发”的另一半）

§5.6 解决“预先打包一份搬过去”，本节解决互补的另一半：“客户在自己内网维护一个源，随上游持续更新”。三个 `fetch_*.py` 现在都读一个运维在**本地环境**声明的镜像基址：

```
# 镜像是一个基址；下载时只把“去哪儿取”换成它，路径与钉值都不变
set WONSLATE_MODEL_MIRROR=https://mirror.corp.internal/wonslate
# （legacy 前缀 LT_MODEL_MIRROR 同样认，LT_ > WONSLATE_ 的既定链）
python script/fetch_argos_models.py       # 包与语音、MADLAD 三个脚本一视同仁
```

镜像是**本地声明的下载目的地**，不是从任何下载内容里读出来的 host——这个分别是整件事的安全地基，也是 D69“白名单不拿本次实际值重算就不算白名单”的另一面。三条判据（都落在 `net_guard` 里、都有正反例与变异自证）：

| 事项 | 实际行为 |
|---|---|
| **镜像可以解析到内网地址，但只限被点名的主机** | 主机的 SSRF 屏（“必须公网”）只对公共白名单轴强制；声明为镜像的那个主机允许私有 IP。未声明的主机（哪怕它恰好解析到内网）根本不进白名单——豁免是**按名**的，不放宽任何“由下载内容决定的 host” |
| **一个主机同时出现在两张表里，走更严的那条** | `classify_host` 先判公共轴：若某公共主机被误（或被投毒）也写进镜像表，它的地址仍按公共规则要求公网，镜像永远不会把公共主机“洗”成可内网 |
| **镜像仍必须 https** | “内网”不是降到明文的许可证；基址不是 https 就地拒绝，绝不静默退回公共地址 |
| **镜像换的是基址，不改路径与摘要** | `apply_mirror` 只把 `scheme://host` 换成镜像（保留原路径），所以钉值照旧把关：一个给不同字节的镜像，和被篡改的下载一样被拦下 |

`wonslate-origin.json` 里的 `url` 记的是**实际拨出去的镜像地址**，不是公共上游——“这批字节从哪来”必须反映本次的真实值（D69／D70），不能因走了内网就假装是从 HuggingFace 下的。

实测（本日真网络跑，用真实公共 https 主机当镜像基址）：`apply_mirror` 把 `https://argos-net.com/index.json` 组合成真实镜像 URL → 走镜像轴 `fetch` 真取回 34,612 字节、重算 sha256 与落盘一致 → `require_public_address("localhost")` 对**真实**回环地址确实拒绝（证明公共轴的 SSRF 屏不是只对着桩）。**诚实边界**：镜像“接受私有 IP”那一半只能靠 resolve 注入测（本机无内网 HTTPS 镜像，且 https-only 又排除了回环替身）——那一半要在客户的内网环境才见到真私有地址，本仓不声称已真跑过。测试侧：`tests/test_net_guard.py` 新增 15 例（镜像轴 13 + 接线 2）；`net_guard.py --self-test` 46→**51** 条断言；变异自证 5/5 套件全抓、其中 4 条同时被 `--self-test` 抓到（初版 self-test 漏抓 classify 顺序——种子用的是两张不相交的表，补了“同一主机既在公共又在镜像表”的种子才咬住）。


## 6. 开发参考机（本文档实测数据来源）

| 项 | 值 |
|---|---|
| CPU | AMD Ryzen 7 7840H（8 核 16 线程） |
| 内存 | 32 GB |
| GPU | NVIDIA RTX 4050 Laptop，6 GB 显存（nvidia-smi 实测 6141 MiB） |
| Python | 3.10.11（ctranslate2 4.8.2 + sentencepiece 0.1.99，均为锁定版本） |
| Ollama | 0.34.0 |

翻译质量与速度的三引擎对比实测见 [translation-benchmark.md](translation-benchmark.md)。

## 7. 离线整合包：应用与模型一起随包（2026-10-19 起，GA-4 的单文件与 REQ-C1 的模型随包）

§5.6 解决的是“把模型搬到不联网的机器”；本节解决另一半：“把应用也一起交出去”。`script/pack_installer.py` 把已构建的应用（可选先经 `publish` 发成单文件）与一个 §5.6 的离线模型包合并成一个交付 zip，目标机器拿一个包即可装。

| 事项 | 实际行为 |
|---|---|
| **清单对自己字节的声明不是证据** | `verify` 拿交付包里的字节**重算**每个成员的 sha256 与长度再与 `wonslate-assembly.json` 对账，与 §5.1／§5.6 同一味药（D69／D80） |
| **“包完整”不等于“包能用”** | 内嵌的模型包要再过一遍 §5.6 那个 `offline_bundle.verify`：哈希对得上但内嵌包自己答不上“是哪一版”的交付物，整包拒绝（D82） |
| **普通 `dotnet build` 不受影响** | 单文件那些开关（`PublishSingleFile` / `SelfContained` / `RuntimeIdentifier`）**只在 `dotnet publish` 且显式传 PublishSingleFile=true 时生效**；门禁与 CI 每 OS 跑的普通 build 不进入该条件，也不会去拉 win-x64 runtime pack |
| **发布不成不会假称发了** | `publish` 接缝在**非 Windows / 无 dotnet / dotnet 退出码非 0** 时退回普通 build 产物，并在清单里写明哪种前提没满足；一个把 `single_file` 当成 true 却并没真发出来的交付物，就是本仓一直在拆的“默认值拍胸脯”那一族（D57／D58／D74） |
| **路径与盘符同样设防** | 成员名含绝对路径／`..`／`.`／空段整包拒，在任何字节落盘**之前**判；沿用 `offline_bundle.safe_member_path` 同一个函数而不是再写一份（D64） |

它不新增一个第二类型的错：`offline_bundle` 分 `CorruptError`／`ConflictError` 两类是因为 `--force` 只答后者；交付物是一次写成、不是“装到已有目录上”，没有“保留更新那份”这个取舍，所以只有一个 `AssemblyError`（D82 第三条：“不要拷一个没后果的第二类型”）。

实测（本日两次真跑，产物只在本机临时目录、**不入库**）：

- **真发单文件**：`publish` 在 Windows 主机上跑 `dotnet publish -r win-x64 --self-contained -p:PublishSingleFile=true` → **真实产出单文件 `Wonslate.exe` 70.1 MB**（含 native 自解压）。这一步也抖出一个只由真跑才发现的缺陷：初版 csproj 注释里写了 XML 禁的 `--`，导致 `.csproj` 加载失败（`dotnet publish` 报 MSB4025）——单测从不加载 csproj，抓不到它。修后重跑才拿到上面的数。
- **真交付物往返**：拿本机已构建的 Release 产物（428 个 app 文件）+ 两个已装 argos 包打出的离线模型包 → `assemble` 成一个 306 MB 交付包 → `verify` 全清（含内嵌模型包跑 `offline_bundle.verify`）→ 改个名字模拟转手再验仍清 → 把包截断一半 → `verify` 拒。测试侧另有 23 例覆盖上述判据，每条都配了一个“删掉它就转红”的变异样本（10/10 全抓，无幸存者）。

签名那一半（公开信任链）仍为零，所以 GA-4 维持 🟡；模型随包这一半已可交。

