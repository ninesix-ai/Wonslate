# 隐私架构说明：`privacy=true` 时源文本的完整去向

> 面向审计方与采购方的技术说明。本文只讲**代码里实际存在的判据**与**可自行复跑的验证方式**，
> 不含口头承诺。每一条陈述都给出源码位置；文末列出这份保证**不覆盖**什么。
> 用户视角的操作说明见 [user-guide.md](user-guide.md)，集成契约见 [sdk.md](sdk.md)。

## 1. 这份文档回答的问题

一次带 `privacy=true` 的翻译请求，**它的源文本会不会到达这台机器以外的地方？**

答案是：不会，且该结论由两道独立判据把守，任何一道不满足即拒绝使用该引擎，并把拒绝原因写进响应。
这个"不会"覆盖到网络地址一层，而不是只覆盖到"引擎名字看起来是本地的"——这两者的区别正是 2026-10-14
修复的缺陷 D68（见 §8）。

## 2. 保证的准确表述

| 项 | 内容 |
|---|---|
| **保证** | `privacy=true` 的请求，其源文本与译文不会被发送到本机以外的网络地址；也不会调用任何需要外部服务才工作的引擎 |
| **不保证** | 不留存。隐私模式仍在**本机磁盘**写入翻译记忆与术语表（§5）。隐私模式管的是"不出设备"，不是"不落盘" |
| **不保证** | 非隐私请求。默认 `privacy=false`；此时把 sidecar 指向局域网或另一台机器是支持的功能（§6） |
| **不保证** | 兼容入口 `tt_translate`。它内部固定 `privacy=false`，因此不提供上述保证（§6） |

## 3. 判据：两条轴，缺一不可

引擎 id 白名单单独不足以构成保证，因为白名单里两个引擎的服务地址来自环境变量。所以实际运行的判据是：

```
允许该引擎运行  ⇔  引擎 id ∈ 已知本地名单  ∧  该 id 本次解析出的地址属于本机回环
```

| 轴 | 判据 | 源码位置 |
|---|---|---|
| id 轴 | 只认 `demo`、`argos`、`madlad` 三个 id，其余一律视为非本地（fail-closed） | `translator-engine/src/engine/mod.rs` `is_local_engine` |
| 地址轴 | 引擎实例自己回答"我能拨的地址是否只有回环"，取自**实际解析结果**而非出厂常量 | 同文件 `is_privacy_safe`、`Translator::reaches_only_loopback`；`engine/sidecar.rs`、`engine/ollama.rs` 各有一份 override |

被认定的回环形状（`engine/mod.rs` `url_host_is_loopback`，刻意狭窄且 fail-closed）：

- 接受：`127.0.0.0/8` 的点分四段写法、IPv6 `::1`、主机名 `localhost`（大小写不敏感）
- 拒绝：其余一切，包括某些实现栈可能恰好本机可达的形状 —— `127.1`、`0.0.0.0`、
  IPv4-mapped `::ffff:127.0.0.1`、`127.0.0.1.evil.com` 这类"看起来像 127/8 的主机名"、
  缺 scheme 或无法解析的地址
- 判错的代价不对称，因此选择保守：误判为"本地"会把用户的文本送到别的机器；误判为"非本地"
  只是少用一档引擎，而且响应会明说（§4 最后一条）

## 4. 一次隐私请求能到达的位置（全部网络出口清单）

引擎层只有三个会发起网络调用，前两个是本机 sidecar，第三个是可选的 AI 精译档：

| 出口 | 默认地址 | `privacy=true` 时的处置 |
|---|---|---|
| Argos sidecar（实时档） | `http://127.0.0.1:11435` | 地址为回环则正常使用；被 `ARGOS_URL` 指向非回环则拒绝使用该引擎 |
| MADLAD sidecar（全语言档） | `http://127.0.0.1:11436` | 同上，判据由 `MADLAD_URL` 的实际解析结果决定 |
| Ollama（AI 精译升级） | `http://127.0.0.1:11434` | **无论地址为何都不调用**：它按 id 就不在本地名单内，且隐私模式禁止任何升级 |

被拒绝时的行为不是静默降级：请求仍会完成，由内置词典引擎 `demo` 作答，`engine` 字段为 `demo`，
`message` 里点名被拒绝的那个地址。例如把 `WONSLATE_MADLAD_URL` 指到 `http://192.168.1.20:11436`
再发一次隐私请求，响应会包含

```
privacy mode: engine 'madlad' dials http://192.168.1.20:11436, which is not this machine's loopback; clamped to local 'demo'
```

### 4.1 一条与文本无关的旁路说明（先写在这里，免得抓包时看起来矛盾）

桌面 UI 另有一条到 sidecar 的探测通道（`Wonslate.UI/Sidecar/SidecarManager.cs`，全仓唯一一处
`HttpClient`）：`GET /health`、`GET /readyz`，以及 `POST /warmup`——后者的请求体按源码固定为空
JSON `{}`（预热不需要参数），不携带任何用户句子。该通道读同一个
`WONSLATE_ARGOS_URL` / `WONSLATE_MADLAD_URL`，并且**不受 `privacy` 标志影响**：若运维把地址指到
另一台机器，UI 启动时会向那台机器发这三个探测请求（不含文本）。因此本文的"零连接"仅指
§4 表中的文本通道，不包含这三个探测。

### 4.2 没有遥测

除上述引擎通道与探测通道外，代码里没有任何遥测、崩溃上报、版本检查或统计上报：全仓搜索
`telemetry` / `analytics` / `crash.?report` / `check.?update` / `WebClient` 在产品源码中无命中，
仅有的字样出现在 `script/eval-data/` 与 `docs/evidence/` 的评测语料里（那是被翻译的句子，不是代码）。

## 5. 本机留存：隐私模式不关闭写入

| 位置 | 内容 | 何时写 |
|---|---|---|
| `<数据目录>/data/translator_tm.json` | 源文本、译文、语言对、置信度、命中次数 | 请求 `use_tm=true`（默认）且结果过质量门槛时；**与隐私模式无关** |
| `<数据目录>/data/glossary.json` | 术语行 | 术语表被增删改时 |
| `<数据目录>/config/settings.json` | 质量/成本偏好 | 用户在设置页保存时 |

数据目录的解析见 `translator-engine/src/config.rs` `data_dir_for`（三层，优先级从高到低）：

1. `LT_DATA_DIR` / `WONSLATE_DATA_DIR` 显式指定（旧前缀优先）；
2. 各系统默认：Windows `%LOCALAPPDATA%\Wonslate`；Linux `$XDG_DATA_HOME/wonslate`，未设时
   `$HOME/.local/share/wonslate`；macOS `$HOME/Library/Application Support/Wonslate`；
3. 以上均不可用时回退到当前目录下的 `./lt-data`（保留旧名以便找到既有数据）。

清空方法是关闭应用后删除上述 `data/` 下的两个文件（见 [user-guide.md](user-guide.md) §10）。需要
"这一次不留痕"的调用方应显式传 `use_tm: false`。

## 6. 不在这份保证范围内的入口与场景

逐条列出，避免读者把 §1 的结论推广到这些地方：

1. **`tt_translate`（兼容旧接口）**：内部固定 `mode=full`、`privacy=false`、`use_tm=false`
   （`translator-engine/src/pipeline.rs` `translate_simple`）。它不提供隐私保证，需要保证的调用方
   必须使用 `tt_translate_full` 并显式置 `privacy: true`。
2. **未带 `privacy` 或 `privacy=false` 的一切请求**：包括桌面界面的默认路径。界面把隐私开关绑定到
   `MainViewModel.PrivacySensitive`，勾选后该值随每次请求送出；未勾选时按 §3 之外的正常路由工作。
3. **非隐私地把 sidecar 放到别的机器**：`WONSLATE_ARGOS_URL` / `WONSLATE_MADLAD_URL` 仍然是可用
   配置项，指向局域网或容器地址时照常工作。地址判据只作用于 `privacy=true`。
4. **sidecar 服务端自身的监听地址**：`sidecar/ct2_sidecar.py` 的 `--host` 默认 `127.0.0.1`。这是
   *服务端暴露面*，与本文的*客户端拨号判据*是两件事——如果有人用 `--host 0.0.0.0` 启动服务，
   那是该进程对外可接受请求，不属于本文覆盖的范围。
5. **模型下载**：`script/fetch_argos_models.py` 等脚本在安装期从公网拉取模型权重，属于显式执行的
   运维动作，不传输任何用户文本。
6. **磁盘与进程内存**：本文不对存储加密、内存残留、页面交换作出任何声明。
7. **UI 的健在／就绪探测**：见 §4.1，它不受 `privacy` 约束。本文保证的是"用户文本不出本机"，
   不是"开启隐私模式后本机不会向已配置的 sidecar 地址发出任何包"。

## 7. 如何自行验证

判据的存在与"不过度触发"都需要能被第三方复算，而不是靠阅读本文。

```bash
# 判据与两条端到端断言（Rust）
cargo test --lib --test privacy_loopback_e2e

# 隐私模式不会升级到 AI（FFI 契约层，含真实 sidecar 调用）
python tests/test_phase1_ffi.py

# 完整权威门禁（Rust + Python + .NET，逐层报数）
python script/build.py --test
```

关键的四个用例，各自钉住一件事：

| 用例 | 钉住的事实 |
|---|---|
| `tests/privacy_loopback_e2e.rs` · `privacy_mode_never_sends_source_text_to_a_non_loopback_address` | **不靠读代码**：在被测主机的**非回环地址**上起一个真实监听，分别走"默认路由"与"显式 `engine_id`"两条路径，断言零连接、断言原文没有出现在该地址、并断言响应 `message` 里带着被拒的那个地址 |
| `tests/privacy_loopback_e2e.rs` · `a_loopback_sidecar_still_answers_under_privacy_mode` | 反向不失控：地址是 `127.0.0.1` 时，隐私模式仍然真的使用 MADLAD 作答（否则"只认回环"会被误实现成"隐私模式只剩词典引擎"） |
| `engine::tests::only_the_loopback_shapes_count_as_on_device` | 地址判据的接受/拒绝表，逐形状断言（含 `127.0.0.1.evil.com`、`127.1`、`0.0.0.0`、IPv4-mapped、缺 scheme） |
| `engine::tests::privacy_safety_needs_both_axes_and_never_asks_the_host` | 两轴各自生效，且断言只落在不依赖宿主环境的答案上；依赖 `ARGOS_URL` 的组合刻意不在此断言，改由上面两条 e2e 在钉住的环境上覆盖 |

另外三处既有防线：`router.rs` 的 `privacy_stays_an_invariant_whatever_the_table_says`（配表想把
隐私流量导向云端时不成立）、`pipeline_e2e.rs` 的
`privacy_mode_clamps_non_local_explicit_engine_to_local`（显式 `engine_id="ollama"` 也拨不出一次）、
`test_phase1_ffi.py` 的 `test_privacy_never_escalates`（跨 FFI 层看结果里不会出现 AI 引擎）。

## 8. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-10-14 | 首次成文。**同日修复缺陷 D68**：隐私判据原先只看引擎 id 白名单，而白名单内 `argos` / `madlad` 的地址来自 `ARGOS_URL` / `MADLAD_URL`（与当初把 `ollama` 排除出白名单是同一个理由），且出厂路由表把 `madlad` 放在精译档的本地槽位——于是一次默认的隐私请求会把原文拨到该变量所指的非回环地址，而响应对此保持沉默。修复为 §3 的双轴判据 + 拒绝时在 `message` 点名地址，并补 §7 前两条端到端用例（其中一条专门防止判据过度触发）。 |
