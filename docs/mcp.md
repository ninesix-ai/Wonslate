# MCP 接入（AI 客户端调用 Wonslate 翻译）

> 适用版本：engine v0.1.0+（`translator-engine/src/bin/wonslate-mcp.rs`）

Wonslate 内置一个 **MCP（Model Context Protocol）stdio 服务端**：`wonslate-mcp.exe`。
任何支持 MCP 的 AI 客户端（ZCode、Claude Desktop、Cursor 等）注册它之后，AI 就能直接
调用本机的 Wonslate 引擎做翻译——走的是与桌面应用完全相同的路由、翻译记忆（TM）和术语表。

## 构建与位置

```bash
python script/build.py        # 或单独构建：cd translator-engine && cargo build --release
```

产物在 `translator-engine/target/release/wonslate-mcp.exe`（Linux/macOS 为 `wonslate-mcp`），
与引擎动态库同目录。它是独立的 stdio 程序，不加载 DLL，也不占用 GUI。

## 注册到 MCP 客户端

通用 stdio 形态（Claude Desktop 的 `claude_desktop_config.json`、多数客户端同型）：

```json
{
  "mcpServers": {
    "wonslate": {
      "command": "<仓库检出路径>/translator-engine/target/release/wonslate-mcp.exe"
    }
  }
}
```

把 `<仓库检出路径>` 换成本机上的绝对路径（JSON 内 Windows 也可写正斜杠）；Linux / macOS 下产物名为 `wonslate-mcp`，无 `.exe` 后缀。

服务端协议版本支持 `2024-11-05` / `2025-03-26` / `2025-06-18`；日志全部走 stderr，
stdout 只承载协议消息。可用环境变量与引擎一致（`WONSLATE_DATA_DIR`、
`WONSLATE_QUALITY_PREFERENCE` 等，见 [hardware-requirements.md](hardware-requirements.md)）。

## 提供的工具

| 工具 | 入参 | 说明 |
|---|---|---|
| `translate` | `input`、`target_lang`（必填）；`source_lang`、`engine_id`、`mode`、`domain`、`privacy`、`use_tm`（可选） | 全管线翻译（TM + 路由 + 蒸馏）。返回 JSON 中带 `engine`（实际作答引擎）、`source`（`tm_hit`/`local`/`ai_upgraded`/`fallback`）、`confidence`、`latency_ms` |
| `list_engines` | 无 | 列出本二进制注册的引擎（`demo`/`ollama`/`argos`/`madlad`）。列出 ≠ 在线，实际作答者看 translate 结果的 `engine` 字段 |
| `tm_lookup` | `text`、`source_lang`、`target_lang` | TM 精确查询，永不触发翻译；命中返回条目，未命中返回 `{"hit": false}` |
| `glossary_list` | `source_lang`、`target_lang` | 列出某语言对的术语表（翻译时会强制应用这些译法） |
| `health` | 无 | 版本、TM 条目数、已注册引擎 |

## 与桌面应用的数据互通

MCP 服务端和 WPF 桌面应用读写同一个 `<data_dir>`（默认
`%LOCALAPPDATA%\Wonslate`）。因此在桌面应用里维护的术语表和 TM 条目，
AI 客户端翻译时立即生效；反之 AI 会话里的翻译也会沉淀进 TM（可在 UI 的
TM 管理器中审查、标记）。

## 质量与后端说明

- **demo**：内置 zh/en 小词表，永远可用，仅词级替换——离线兜底，不是真翻译；
- **argos / madlad**：需要本机 sidecar 在线（端口 11435 / 11436），真 NMT 推理，
  模型获取见 [hardware-requirements.md](hardware-requirements.md)；
- **ollama**：本机 Ollama + LLM，质量最高，用于 full 档精译升级；
- `privacy: true` 会禁止任何 AI 升级，只用本地引擎；
- 全部后端不可达时请求仍会成功返回（demo 兜底），调用方应检查返回里的
  `engine` / `source` 字段判断质量档位。

工具执行失败（如空输入）以 `isError: true` 的结果返回；未知工具 / 未知方法
返回 JSON-RPC 错误（-32602 / -32601）。

## 测试

- 单元测试（协议层）：`cargo test --bin wonslate-mcp`
- 子进程 e2e（真实 stdio 管道，覆盖帧协议、通知静默、坏行恢复、离线翻译）：
  `cargo test --test mcp_stdio_e2e`
