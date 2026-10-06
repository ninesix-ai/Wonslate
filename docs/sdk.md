# SDK 说明（C ABI FFI 集成）

> 本文是 Wonslate（万邦译）的对外集成契约说明，主体为 `translator_engine` 动态库的 18 个 C ABI 导出函数。
> 缩写解释见 [terminology.md](terminology.md)；面向最终用户的操作见 [user-guide.md](user-guide.md)。

## 1. 定位与现状

| 集成形态 | 状态 | 说明 |
|---|---|---|
| **C ABI FFI（`tt_*`）** | ✅ 已落地（本文主体） | **唯一契约真相源**；C / C# / Python 等语言均可直接绑定 |
| sidecar 本机 HTTP | ✅ 已落地 | `POST /translate`、`GET /health`（存活）、`GET /readyz`（就绪）、`POST /warmup`（显式预热）、`GET /languages`（能力枚举），仅绑定 `127.0.0.1`（内部契约，见 §8） |
| 官方 .NET / Python SDK 包 | ⏳ 规划中（未发布） | 发布前请按本文直接绑定 FFI |
| CLI / REST API / MCP Server / 批量接口 | ⏳ 规划中（未实现） | 当前批处理请在调用方循环；见 [terminology.md](terminology.md) |

设计原则：所有对外形态（界面 / SDK / 未来的 CLI、REST、MCP）最终都调用同一 Rust 核心 `tt_translate_full`——**隐私模式、不静默降级、置信度门控等不变量与图形界面完全一致**；绑定层不复制业务逻辑。

## 2. 快速开始

先构建出动态库：`python script/build.py`，产物在 `translator-engine/target/release/`（Windows：`translator_engine.dll`）。

调用三步：

1. `tt_init("{}")` —— 进程启动时调用一次（打开 TM、加载配置、启动蒸馏线程）；
2. `tt_translate_full(request_json)` —— 翻译（推荐入口）；
3. `tt_shutdown()` —— 退出前调用（把未写完的 TM / 术语表落盘）。

### 2.1 Python（ctypes，仅标准库）

```python
import ctypes, json

lib = ctypes.cdll.LoadLibrary("translator_engine.dll")

# 关键：指针返回值必须声明为 c_void_p；否则 ctypes 会把它变成 bytes，指针随即丢失
lib.tt_version.argtypes, lib.tt_version.restype = [], ctypes.c_void_p
lib.tt_init.argtypes, lib.tt_init.restype = [ctypes.c_char_p], ctypes.c_void_p
lib.tt_translate_full.argtypes, lib.tt_translate_full.restype = [ctypes.c_char_p], ctypes.c_void_p
lib.tt_free_string.argtypes, lib.tt_free_string.restype = [ctypes.c_void_p], None
lib.tt_shutdown.argtypes, lib.tt_shutdown.restype = [], None

def take(ptr) -> str:
    """读取 Rust 返回的 UTF-8 字符串并释放；空指针返回空串。"""
    if not ptr:
        return ""
    try:
        return ctypes.string_at(ptr).decode("utf-8")
    finally:
        lib.tt_free_string(ptr)          # 必须释放；漏掉就是每次调用都泄漏

print("engine:", take(lib.tt_version()))
print("init:", take(lib.tt_init(b"{}")))

req = {"input": "你好世界", "source_lang": "zh", "target_lang": "en",
       "mode": "full", "privacy": False, "use_tm": True}
print(take(lib.tt_translate_full(json.dumps(req).encode("utf-8"))))

lib.tt_shutdown()                        # 退出前落盘
```

### 2.2 C# / .NET（源生成 `LibraryImport`）

```csharp
using System.Runtime.InteropServices;   // LibraryImport 需要 `partial` 方法与源生成器
using System.Text.Json;

internal static partial class Engine
{
    private const string Dll = "translator_engine.dll";

    [LibraryImport(Dll, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_init(string configJson);

    [LibraryImport(Dll, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_translate_full(string requestJson);

    [LibraryImport(Dll)]
    private static partial void tt_free_string(IntPtr ptr);

    [LibraryImport(Dll)]
    private static partial void tt_shutdown();

    private static string Take(IntPtr p)
    {
        try { return p == IntPtr.Zero ? "" : Marshal.PtrToStringUTF8(p) ?? ""; }
        finally { if (p != IntPtr.Zero) tt_free_string(p); }
    }

    public static void Demo()
    {
        Console.WriteLine(Take(tt_init("{}")));

        var req = JsonSerializer.Serialize(new
        {
            input = "你好世界", source_lang = "zh", target_lang = "en", use_tm = true
        });
        Console.WriteLine(Take(tt_translate_full(req)));

        tt_shutdown();
    }
}
```

### 2.3 C（动态加载）

```c
/* Windows 示例；Linux / macOS 换 dlopen / dlsym 即可 */
#include <stdio.h>
#include <windows.h>

typedef char *(*tt_init_t)(const char *);
typedef char *(*tt_translate_full_t)(const char *);
typedef void (*tt_free_string_t)(char *);
typedef void (*tt_shutdown_t)(void);

int main(void) {
    HMODULE lib = LoadLibraryA("translator_engine.dll");
    if (!lib) { puts("load failed"); return 1; }

    tt_init_t            init      = (tt_init_t)GetProcAddress(lib, "tt_init");
    tt_translate_full_t  translate = (tt_translate_full_t)GetProcAddress(lib, "tt_translate_full");
    tt_free_string_t     free_str  = (tt_free_string_t)GetProcAddress(lib, "tt_free_string");
    tt_shutdown_t        shutdown  = (tt_shutdown_t)GetProcAddress(lib, "tt_shutdown");

    char *ack = init("{}");
    printf("init: %s\n", ack);
    free_str(ack);

    char *resp = translate("{\"input\":\"你好世界\",\"source_lang\":\"zh\",\"target_lang\":\"en\"}");
    printf("%s\n", resp);      /* UTF-8 输出；Windows 控制台建议先执行 chcp 65001 */
    free_str(resp);

    shutdown();
    return 0;
}
```

## 3. 通用约定

- **UTF-8**：所有字符串参数与返回值均为 UTF-8，不使用本地编码（Windows 上尤须注意）。
- **内存所有权**：所有返回的 `char*` 由 Rust 分配，调用方**必须**用 `tt_free_string` 释放；返回 `null` 表示无法分配。
- **异常安全**：每个导出函数内部都有 `catch_unwind` 兜底，panic 也会返回 JSON（`"error":"PANIC"`），不会跨 FFI 抛异常。
- **初始化**：`tt_init` 每进程调用一次即可；配置在启动时读取一次，**修改配置需重启进程**。`tt_init` 的 `config_json` 是最高优先级配置层（§7）。
- **线程安全**：核心内部使用进程级单例（TM 存储、配置、蒸馏队列），调用方无需自行加锁；但不要在翻译进行中并发调用 `tt_shutdown`。

## 4. 函数参考（18 个导出）

### 4.1 生命周期与元信息

| 函数 | 原型 | 返回 | 说明 |
|---|---|---|---|
| `tt_version` | `char* tt_version(void)` | 版本字符串 | 如 `"0.1.0"`；用于运行时兼容性自检 |
| `tt_init` | `char* tt_init(const char* config_json)` | Ack JSON | 传 `"{}"` 用默认配置；不认识的键会出现在 `ignored_config_keys`（不静默忽略） |
| `tt_config_json` | `char* tt_config_json(void)` | 配置 JSON | **实际生效**的路由 + 来源文件 + 被环境变量钉住的键名（§7） |
| `tt_health` | `char* tt_health(void)` | JSON | `{"status":"ok","version":…,"tm_entries":N,"engines":[…]}` |
| `tt_engines` | `char* tt_engines(void)` | JSON 数组 | `[{"id":"demo","name":"演示引擎（内置词表，兜底用）"},…]` |
| `tt_shutdown` | `void tt_shutdown(void)` | — | 退出前调用，落盘未写完的 TM / 术语表 |
| `tt_free_string` | `void tt_free_string(char* ptr)` | — | 释放任意由本库返回的字符串；空指针安全 |

`tt_init` 返回示例：

```json
{"ok":true,"version":"0.1.0"}
```

当 `config_json` 含不受支持的键时：

```json
{"ok":true,"version":"0.1.0","ignored_config_keys":["foo"]}
```

`tt_config_json` 返回示例（节选）：

```json
{
  "routing": {
    "common_pairs": ["zh","en","ja","ko","fr","de","es","ru","pt","it","ar"],
    "realtime": {"local_engine":"argos","upgrade_engine":"","upgrade_threshold":0.0,
                 "upgrade_policy":"low_confidence"},
    "full":     {"local_engine":"madlad","upgrade_engine":"ollama","upgrade_threshold":0.85,
                 "upgrade_policy":"always","tm_quality_floor":0.0},
    "rare_pair":{"local_engine":"madlad","upgrade_engine":"ollama","upgrade_threshold":0.8,
                 "upgrade_policy":"low_confidence"}
  },
  "env_pinned": [],
  "routes_file":   "…\\Wonslate\\config\\routes.json",
  "settings_file": "…\\Wonslate\\config\\settings.json"
}
```

### 4.2 翻译

| 函数 | 原型 | 说明 |
|---|---|---|
| `tt_translate_full` | `char* tt_translate_full(const char* request_json)` | **推荐入口**：TM + 路由 + 蒸馏 + 置信度的完整流水线（§5.1 / §5.2） |
| `tt_translate` | `char* tt_translate(engine_id, input, lang_pair)` | 兼容旧接口：`lang_pair` 以 `-` 分隔（如 `"en-zh"`）。内部 `mode=full`、`privacy=false`、`use_tm=false`（**不读写 TM**）；`engine_id` 非空 = 锁定该引擎且不升级；未知 id 显式回落 `demo` 并在 `message` 说明 |

### 4.3 TM（翻译记忆库）

| 函数 | 原型 | 说明 |
|---|---|---|
| `tt_tm_lookup` | `char* tt_tm_lookup(text, source_lang, target_lang)` | 只查历史、绝不触发翻译：命中返回 **TmEntry JSON**，未命中返回字符串 `"null"` |
| `tt_tm_put` | `char* tt_tm_put(const char* entry_json)` | 写入一条记忆对（§5.3），返回 Ack |
| `tt_tm_flag_bad` | `char* tt_tm_flag_bad(text, source_lang, target_lang)` | 软删除（标坏）：不再命中、不再出现在列表，记录仍在磁盘；未知条目为无害空操作 |
| `tt_tm_list` | `char* tt_tm_list(source_lang, target_lang, uint32_t limit)` | 返回 JSON 数组（按命中次数降序）；`limit` 会被夹到 1–1000 |

### 4.4 术语表

| 函数 | 原型 | 说明 |
|---|---|---|
| `tt_glossary_list` | `char* tt_glossary_list(source_lang, target_lang)` | 返回 JSON 数组（最多 500 条，按置信度降序） |
| `tt_glossary_upsert` | `char* tt_glossary_upsert(const char* entry_json)` | 新增 / 更新（§5.4）；`confidence` 以调用方为准，`source` 被强制标记为 `manual` |
| `tt_glossary_delete` | `char* tt_glossary_delete(source_term, source_lang, target_lang)` | 按键删除，返回 Ack |
| `tt_glossary_list_with_domain` | `char* tt_glossary_list_with_domain(source_lang, target_lang, domain, uint32_t limit)` | S11：按领域取术语行；`domain` 为空串等同只取泛域行（`tt_glossary_list` 即其 limit=500 的便捷形态） |
| `tt_glossary_import_pack` | `char* tt_glossary_import_pack(const char* pack_json)` | 导入领域术语包，返回 `{"ok":true,"imported":N}`；该路径**不经过**蒸馏的源语言守卫，故错标行仍能入库，由 `tests/test_glossary_hygiene.py` 兜 |

## 5. JSON 数据契约

字段命名统一为 `snake_case`；**新增字段属于兼容变更**，旧解析应容忍未知字段。

### 5.1 TranslateRequest（`tt_translate_full`）

所有字段可选，缺省值如下：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `input` | string | `""` | 待翻译文本；为空时返回 `NO_RESULT` |
| `source_lang` | string | `"auto"` | 空串等同 `"auto"`；当前自动检测只区分 zh / en |
| `target_lang` | string | `""` | 目标语言代码，必填 |
| `mode` | string | `"full"` | `"realtime"` 或 `"full"`；其它值一律按 `full` |
| `privacy` | bool | `false` | `true` 时强制本地（禁止 AI 升级，非本地引擎被钳制） |
| `use_tm` | bool | `true` | 是否使用 TM（读 + 写） |
| `engine_id` | string | `""` | 显式引擎 id（§6）：非空 = 锁定不升级 |
| `domain` | string | `""` | 领域标签，用于术语表匹配（界面当前不设置） |

### 5.2 TranslateResponse

| 字段 | 说明 |
|---|---|
| `ok` | 成功与否 |
| `engine` | 实际产出引擎名（如 `argos` / `madlad` / `ollama-qwen` / `demo` / `tm`） |
| `source_lang` / `target_lang` | 实际使用的语言代码（`auto` 会被解析为具体值） |
| `input` | 原样回显输入 |
| `output` | 译文；失败时可能为 `null` |
| `source` | 结果来源枚举（跨语言稳定契约）：`tm_hit` / `local` / `ai_upgraded` / `fallback` |
| `latency_ms` | 端到端耗时（毫秒） |
| `confidence` | 置信度 0–1（AI 结果固定 0.95） |
| `error` / `message` | 仅出错/有说明时出现；**回落原因、缓存被拒、隐私钳制等都写在 `message` 里**，建议透出给用户 |

成功示例：

```json
{"ok":true,"engine":"madlad","source_lang":"en","target_lang":"zh",
 "input":"hello","output":"你好","source":"local",
 "latency_ms":312,"confidence":0.84}
```

失败示例（`err_response` 统一形状）：

```json
{"ok":false,"engine":"unknown","source_lang":"de","target_lang":"ar",
 "input":"…","output":null,"source":"fallback",
 "latency_ms":0,"confidence":0.0,
 "error":"NO_RESULT","message":"NO_RESULT: no engine could translate de->ar; …"}
```

### 5.3 TmEntry

```json
{"source_text":"你好世界","source_lang":"zh","target_text":"hello world","target_lang":"en",
 "engine":"manual","quality":1.0,"hit_count":3,"domain":""}
```

### 5.4 GlossaryEntry

```json
{"source_term":"人工智能","source_lang":"zh","target_term":"Artificial Intelligence","target_lang":"en",
 "confidence":1.0,"frequency":1,"domain":"","source":"manual"}
```

`source` 为来源标记：`distill`（机器抽取）/ `manual`（用户编辑）/ `tm`（由记忆库转化）；空值为旧记录。

### 5.5 Ack 与错误码

写操作成功：`{"ok":true}`。错误对象：`{"ok":false,"error":"<码>","message":"…"}`（翻译接口的错误还会带 §5.2 的完整字段）。

| 错误码 | 含义 |
|---|---|
| `INVALID_INPUT` | 参数/JSON 非法（含非 UTF-8） |
| `UNKNOWN_ENGINE` | 请求的引擎未注册（路由层通常已回落并在 `message` 说明） |
| `ENGINE_FAILED` | 引擎执行失败（如 sidecar 报错），`message` 含引擎名与原因 |
| `NO_RESULT` | 没有任何引擎能产出结果（语言对缺模型且无可用 AI 升级） |
| `TM_ERROR` | TM / 术语表存储层错误（I/O 等） |
| `CONFIG_ERROR` | 配置错误 |
| `PANIC` | 兜底捕获的 panic（保证不会跨 FFI 抛异常） |

## 6. 引擎与路由行为

**`engine_id` 语义**：留空 → 按路由自动决策；显式给出 → 锁定该引擎、不升级（尊重调用方选择）；未注册 id → 回落 `demo` 并在 `message` 说明「requested engine '…' unavailable, fell back to 'demo'」。

**已注册引擎**（`tt_engines` / `is_available` 一致）：

| id | 说明 |
|---|---|
| `demo` | 内置词表，兜底用；en↔zh 基础词 |
| `argos` | 实时档引擎（本机 Argos / CTranslate2 sidecar，端口 11435） |
| `madlad` | 全语言引擎（本机 MADLAD-400 sidecar，端口 11436；450+ 语言码互译） |
| `ollama` | AI 精译（本机 Ollama + Qwen3）；别名 `qwen`、`ollama-qwen` 指向同一实现 |

**默认路由表**（可被配置覆盖，§7）：

| 规则 | 本地引擎 | 升级引擎 | 升级策略 | 阈值 |
|---|---|---|---|---|
| 实时档 `realtime` | `argos` | 无（从不升级） | — | — |
| 精译档 `full` | `madlad` | `ollama` | `always`（可达就升级） | 0.85* |
| 冷门语言对 `rare_pair` | `madlad` | `ollama` | `low_confidence` | 0.80 |

\* `always` 时阈值不参与判断；改为 `low_confidence` 后生效。

**不变量**（对 SDK 调用与界面一视同仁）：

- 隐私模式 fail-closed：非本地引擎一律钳制为 `demo`，且钳制原因写入 `message`；
- 升级只发生在 `mode=full` 且非隐私时；升级前先探测 AI 引擎可达性，不可达则本地出结果并注明；
- TM 命中在路由之后按该规则的「缓存质量下限」过滤：低于下限且 AI 可达 → 重新翻译；低于下限但 AI 不可达 → 照常返回缓存并注明"质量偏好未兑现"（不静默）；
- 本地结果置信度 ≥ 0.70 才写入 TM；AI 结果一律以 `quality=0.95` 写入并触发蒸馏。

## 7. 嵌入式配置

**优先级（低 → 高）**：内置默认 < `routes.json`（部署默认） < `settings.json`（用户偏好） < 环境变量 < `tt_init` 的 `config_json`。

`tt_init` 可覆盖的键（仅路由表）：`common_pairs`、`realtime`、`full`、`rare_pair`；规则字段：`local_engine` / `upgrade_engine` / `upgrade_threshold` / `upgrade_policy`（`always` | `low_confidence`）/ `tm_quality_floor`。未识别键不会被静默忽略——会出现在 ack 的 `ignored_config_keys` 中。

```json
{
  "full": {
    "local_engine": "madlad",
    "upgrade_engine": "ollama",
    "upgrade_policy": "low_confidence",
    "upgrade_threshold": 0.85,
    "tm_quality_floor": 0.0
  }
}
```

常用环境变量（完整清单见 [hardware-requirements.md](hardware-requirements.md#4-配置入口环境变量)）：

| 类别 | 变量（`LT_*` 旧前缀同样有效且优先） |
|---|---|
| 数据目录 | `WONSLATE_DATA_DIR` |
| 配置来源 | `WONSLATE_ROUTES_FILE` / `WONSLATE_SETTINGS_FILE` |
| 质量偏好 | `WONSLATE_QUALITY_PREFERENCE`（`quality_first` / `cost_first`） |
| 精译档微调 | `WONSLATE_FULL_UPGRADE_POLICY` / `WONSLATE_FULL_UPGRADE_THRESHOLD` / `WONSLATE_FULL_TM_QUALITY_FLOOR` |
| Ollama | `WONSLATE_OLLAMA_URL` / `WONSLATE_OLLAMA_MODEL` / `WONSLATE_OLLAMA_TIMEOUT_MS`（裸名 `OLLAMA_URL` 等亦可） |
| 蒸馏阈值 | `WONSLATE_DISTILL_MIN_CONF` |

## 8. sidecar 本机 HTTP 接口（内部契约）

给 argos / madlad 两个引擎供能的 Python 服务，可独立启动：

```bash
python -m sidecar.ct2_sidecar --backend ct2   --port 11435   # Argos 包
python -m sidecar.ct2_sidecar --backend madlad --port 11436  # MADLAD-400
python -m sidecar.ct2_sidecar --backend mock  --port 11435   # 无依赖调试
```

| 接口 | 请求 | 响应 |
|---|---|---|
| `POST /translate` | `{"text":"…","source":"en","target":"zh"[,"glossary":[{"src":"…","tgt":"…"}]]}` | `200 {"text":"…"}`（发生区域码折叠时附带 `requested_target`/`resolved_target`，源侧同理）；`400 {"error":"missing 'text'"/"invalid json"}`；**`422 {"error":"unsupported_target","message":"…","supported":{…}}`**；`503 {"error":"backend_unavailable","message":"…"}` |
| `GET /health` | — | `200 {"status":"ok","backend":"mock\|ct2\|madlad"}` —— **只代表存活（端口在应答），不代表首句不会卡在冷加载** |
| `GET /readyz` | — | 就绪 `200 {"status":"ready","backend":…,"loaded":[…]}`；未就绪 `503 {"status":"warming","error":"not_ready","reason":"model_not_loaded"}`；后端无法自述 `501 {"error":"readiness_unknown"}` |
| `GET /languages` | 可选 `?target=xx` | `200 {"backend":…,"pairs":[…],"target_codes":[…],"complete":bool,"note":"…"}`；带 `?target=` 时附加 `"supported": true\|false\|null`（`null` = 后端无法判定，与「不支持」严格区分）；区域码经折叠而可用时为 `true` 并附 `resolved_target` |
| `POST /warmup` | 可选 `{"source":"en","target":"zh"}` | 就绪 `200 {"status":"ready","backend":…,"warmed":bool,"load_s":秒,"loaded":[…]}`；仍未就绪 `503 {"status":"warming","error":"not_ready"}`；后端不支持 `501 {"error":"warmup_unknown"}`；语言码不可用 `422 unsupported_target`（与 `/translate` 同一判定，区域码同样先折叠） |

两类失败**必须分开处理**：`422 unsupported_target` 是该引擎在这个语言码上**永久无解**，重试、重启、重载模型都不会改变，响应里的 `supported` 已给出可用集合，调用方应直接跳过该方向；`503 backend_unavailable` 才是可恢复故障（依赖或模型尚未就位），值得等待与重试。把两者混为 503 会让批量任务在冷门语言上白白付出「杀进程 → 等端口 → 重载」的整轮代价。

区域码**由引擎自己折叠**：`pt-BR`→`pt`、`zh-Hans-CN`→`zh`、`es-419`→`es`（取 BCP-47 主语言子标），因为产品侧语言码几乎都带区域后缀，而 MADLAD 词表与 Argos 包只认主码。折叠只朝向**后端确认能服务**的码：折不出可行目标的请求仍按原码交给引擎，422 文案由它（唯一知道自己覆盖面的组件）给出。发生过折叠时，响应会并列 `requested_target` 与 `resolved_target` 供审计；未折叠则不多这两个字段。`GET /languages?target=pt-BR` 同样回答 `supported: true` 并附 `resolved_target`，所以批量调用方**不必自己再写一份 `split('-')[0]`**。

就绪与存活是**两个信号**：MADLAD 的 3B 检查点与 Argos 的逐对 translator 都是首次翻译才加载，故 `/health` 的 200 只说明进程已监听；要避免首批句子撞上冷加载，请轮询 `/readyz` 至 `status:"ready"`。`/languages` 让调用方在**不消耗一次翻译**的前提下问清能力边界（`?target=` 探针直接查词表/磁盘包）。

`/readyz` 只说真话，不会把模型装进内存——**把它变成就绪的手段是 `POST /warmup`**。该端点按当前配置真实加载并回报代价（`warmed` 表示本次是否真的付了加载，`load_s` 仅在真加载时为非零）。**Argos 是按方向加载的**：不带 `source`/`target` 的 warmup 不会把磁盘上所有包都拉进内存（那比懒加载贵得多），而是回 `503 warming` 并在 `note` 里提示方、目标语言；若调用方把 200 当作“已暖好”，这个区分就是必需的。桌面客户端的用法：先 `GET /readyz` 探得 warming，再在后台 `POST /warmup`，完成后状态转 `ready`。

约束：**只绑定 `127.0.0.1`**（翻译文本不出设备）。该契约与 Rust `engine/sidecar.rs`、.NET `SidecarManager`、测试 mock 四方对齐；作为内部接口随实现演进，不承诺与 FFI 同级的兼容性——新集成请优先用 FFI。

## 9. 版本与兼容承诺

| 层级 | 承诺 |
|---|---|
| 冻结契约 | `tt_version` / `tt_translate` / `tt_free_string` 签名不再变更 |
| 扩展契约 | 其余 `tt_*` 按语义化版本演进：同大版本内**只增不改不删** |
| 响应结构 | JSON 字段向后兼容（新增字段不破坏旧解析）；`source` 等枚举字符串是跨语言契约，变更需大版本 |
| 运行时自检 | 用 `tt_version` / `tt_health` / `tt_config_json` 校验引擎版本与实际配置 |

## 10. Windows 注意事项

- 本地自行构建的 `translator_engine.dll` 可能被 **Smart App Control / 安全软件**拦截而加载失败；开发机处理方式见 [terminology.md](terminology.md) 的 SAC 条目，正式分发以签名产物为准。
- 所有字符串均为 UTF-8：C 示例在控制台直接打印中文前建议 `chcp 65001`；.NET 用 `Marshal.PtrToStringUTF8`。
- 动态库与调用方必须位数一致（x64 对 x64）。

## 相关文档

- [用户使用说明（UI/UX）](user-guide.md) · [缩写与术语解释](terminology.md)
- [硬件配置要求](hardware-requirements.md) · [翻译质量基准](translation-benchmark.md) · [文档索引](README.md)