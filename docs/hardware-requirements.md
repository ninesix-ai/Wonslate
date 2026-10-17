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
| 完整性 | 每个文件比对**钉住的 sha256**，不匹配就删除临时文件并报错；**已缓存的文件同样复核**（缓存分支是正常运行的快路径，只比大小的话会接受一个同长度的替换件） |
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

## 6. 开发参考机（本文档实测数据来源）

| 项 | 值 |
|---|---|
| CPU | AMD Ryzen 7 7840H（8 核 16 线程） |
| 内存 | 32 GB |
| GPU | NVIDIA RTX 4050 Laptop，6 GB 显存（nvidia-smi 实测 6141 MiB） |
| Python | 3.10.11（ctranslate2 4.8.2 + sentencepiece 0.1.99，均为锁定版本） |
| Ollama | 0.34.0 |

翻译质量与速度的三引擎对比实测见 [translation-benchmark.md](translation-benchmark.md)。
