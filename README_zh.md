# JetBrains MCP Servers Router

根据当前项目路径，自动将 MCP 工具调用路由到正确的 JetBrains IDE。

> **English README**: [README.md](README.md)

---

## 背景

JetBrains IDE（IntelliJ IDEA、PyCharm、RustRover 等）各自运行内置 MCP Server，但端口号并不是按 IDE 类型固定分配的，而是从候选端口中自动选择。同时开发多个项目时，每个项目可能在不同的 IDE 中打开，coding agent 需要知道应该使用哪个 MCP Server。

各 JetBrains IDE 暴露的 MCP tool 大部分相同，但不同 IDE 仍可能提供少量专属工具（例如 PyCharm 的 notebook 相关工具、RustRover 的检查工具）。没有路由器时，需要在每个 coding agent 的配置里为每个 IDE 单独配置一条 MCP 条目，并手动处理端口和工具列表变化。

## 解决方案

路由器提供**单一 stdio MCP 入口**。每次工具调用时：

1. 解析目标项目路径：`projectPath` 参数 → `JBMCP_DEFAULT_PROJECT` 环境变量 → 当前工作目录（CWD）
2. 查找文件缓存（`~/.jetbrains-mcp-router/cache.json`），找出该项目对应的 IDE URL
3. 缓存未命中时，**并发探测**候选端口（默认 64342–64351），对每个响应的 IDE 调用 `get_repositories` 并匹配项目路径
4. 将工具调用转发到正确 IDE 的 `/stream` 端点，同时注入解析后的 `projectPath`

路由器返回所有已连接 IDE 工具列表的**并集**，因此各 IDE 的专属工具（如 PyCharm 的 `runNotebookCell`、RustRover 的 `run_inspection_kts`）全部对 coding agent 可见。

路由器位于 coding agent 和 JetBrains IDE 之间，会使用自己的 HTTP client 转发到 IDE 的 `/stream`。如果直接调用 IDE 的 `/stream`，超时行为由调用方 MCP 客户端决定；通过本路由器调用时，还会受到路由器侧 HTTP read timeout 的影响，因此长耗时工具需要支持按次覆盖。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（免安装执行）
- JetBrains IDE 2025.2+，且已启用内置 MCP Server

## 启用 JetBrains MCP Server

在 IDE 中：**Settings → Tools → MCP Server → Enable MCP Server**

Server 启动后自动占用从 64342 开始的端口。

## 快速开始

无需安装，直接用 `uv` 运行路由器：

```sh
uv run /path/to/jetbrains-mcp-servers-router/router.py
```

首次运行时 `uv` 会自动安装依赖。

## 在 Coding Agent 中注册

使用 coding agent 内置的 `/mcp add` 命令（或等效操作），以 **stdio** 方式添加路由器。命令填写：

```
uv run /path/to/jetbrains-mcp-servers-router/router.py
```

具体的 `/mcp add` 语法请参考你所使用的 coding agent 文档。

如果开发过程中额外启动了新的 JetBrains IDE，或者某个 IDE 重启后 MCP 端口发生变化，可以在 coding agent 中执行 `/mcp reload`（或等效操作）刷新 MCP tool 列表。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `JBMCP_PORT_START` | `64342` | 探测起始端口 |
| `JBMCP_PORT_COUNT` | `10` | 连续探测的端口数量 |
| `JBMCP_HOST` | `127.0.0.1` | IDE 主机地址 |
| `JBMCP_CACHE` | `~/.jetbrains-mcp-router/cache.json` | 路由缓存文件路径 |
| `JBMCP_DEFAULT_PROJECT` | — | CWD 未知时的兜底项目路径 |
| `JBMCP_CONNECT_TIMEOUT` | `1.5` | HTTP 连接超时时间，单位秒 |
| `JBMCP_READ_TIMEOUT` | `60` | 默认 HTTP 读取超时时间，单位秒；设为 `0`、`none`、`off` 或 `disabled` 可禁用 |
| `JBMCP_WRITE_TIMEOUT` | `10` | HTTP 写入超时时间，单位秒 |
| `JBMCP_POOL_TIMEOUT` | `5` | HTTP 连接池超时时间，单位秒 |
| `JBMCP_TIMEOUT_GRACE` | `5` | 从 tool 的 `timeout` 参数推导 router timeout 时额外增加的秒数 |
| `JBMCP_DEBUG` | — | 设为 `1` 开启 debug 日志 |
| `JBMCP_LOG_FILE` | — | 可选日志文件路径（同时写入 stderr 和文件） |

## Router Timeout

路由器不会给 JetBrains tool schema 添加 router 私有参数。

当 JetBrains tool schema 本身包含数字类型的 `timeout` 参数，并且描述明确说明单位是毫秒时，路由器会从这个原生 tool 参数推导 HTTP read timeout：

```text
router_read_timeout_seconds = timeout_milliseconds / 1000 + JBMCP_TIMEOUT_GRACE
```

例如 `execute_run_configuration(timeout=1200000)` 会让 router read timeout 使用 `1200 + grace` 秒，同时原始 `timeout=1200000` 会原样转发给 JetBrains IDE tool。

如果未来 JetBrains 新增 tool，并且它的 `timeout` 参数单位或语义不清，路由器会写 warning 日志，并继续使用默认的 `JBMCP_READ_TIMEOUT`，不会用这个字段推导 read timeout。

## 路由机制

1. **首次调用某项目** — 并发探测所有候选端口，对每个响应的 IDE 调用 `get_repositories`，匹配归一化后的项目路径，结果写入缓存。
2. **后续调用** — 优先查缓存。若缓存中的 IDE 已不再包含该项目（如 IDE 重启后端口发生变化），则自动失效并重新发现。
3. **工具列表** — 返回所有响应 IDE 的工具并集，IDE 专属工具全部可用。

## 缓存说明

路由缓存默认存储在 `~/.jetbrains-mcp-router/cache.json`（可用 `JBMCP_CACHE` 覆盖）。格式为归一化项目路径 → IDE URL 的映射。每次使用时会校验缓存有效性，过期条目自动替换。

## 许可证

[Apache License 2.0](LICENSE)
