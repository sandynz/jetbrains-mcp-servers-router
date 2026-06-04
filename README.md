# JetBrains MCP Servers Router

A single-file Python MCP server that routes tool calls to the correct JetBrains IDE based on the active project path.

> **Chinese README**: [README_zh.md](README_zh.md)

---

## Problem

JetBrains IDEs (IntelliJ IDEA, PyCharm, RustRover, …) each expose a built-in MCP server, but the port is not fixed by IDE type. Each IDE instance automatically chooses from the candidate port range. When you work on multiple projects simultaneously, each project may be open in a different IDE, and your coding agent needs a way to know which MCP server owns the current project.

Most MCP tools exposed by JetBrains IDEs are shared, but individual IDEs may add a small number of IDE-specific tools (for example notebook tools in PyCharm or inspection tools in RustRover). Without this router you need one MCP entry per IDE in every coding agent configuration file, and you need to manage port and tool-list changes manually.

## Solution

The router exposes a **single stdio MCP endpoint**. On every tool call it:

1. Resolves the target project path (`projectPath` argument → `JBMCP_DEFAULT_PROJECT` env var → current working directory)
2. Looks up a file-based cache (`~/.jetbrains-mcp-router/cache.json`) for the IDE URL that owns that project
3. On a cache miss, **concurrently probes** candidate ports (default: 64342–64351), calls `get_repositories` on each responding IDE, and matches the project path
4. Forwards the call to the correct IDE's `/stream` endpoint, injecting the resolved `projectPath`

The **union** of all connected IDEs' tool lists is returned, so IDE-specific tools (e.g. `runNotebookCell` in PyCharm, `run_inspection_kts` in RustRover) are all visible to the coding agent.

The router uses its own HTTP client between the coding agent and the JetBrains IDE. Calling an IDE's `/stream` endpoint directly uses the caller's MCP timeout behavior. Calling through this router also needs a router-side HTTP read timeout, so long-running tools can override it per call.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — for zero-install execution
- JetBrains IDE 2025.2+ with the built-in MCP server enabled

## Enabling the JetBrains MCP Server

In your IDE: **Settings → Tools → MCP Server → Enable MCP Server**

The server starts on an automatically chosen port beginning at 64342.

## Quick Start

No installation needed. Run the router directly with `uv`:

```sh
uv run /path/to/jetbrains-mcp-servers-router/router.py
```

`uv` installs the required dependencies automatically on first run.

## Registering with Your Coding Agent

Use your coding agent's built-in `/mcp add` command (or equivalent) to register the router as a **stdio** MCP server. The command to provide is:

```
uv run /path/to/jetbrains-mcp-servers-router/router.py
```

Refer to your coding agent's documentation for the exact `/mcp add` syntax.

If you start another JetBrains IDE during development, or an IDE restart changes its MCP port, run `/mcp reload` (or the equivalent operation in your coding agent) to refresh the MCP tool list.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JBMCP_PORT_START` | `64342` | First port to probe |
| `JBMCP_PORT_COUNT` | `10` | Number of consecutive ports to scan |
| `JBMCP_HOST` | `127.0.0.1` | IDE host |
| `JBMCP_CACHE` | `~/.jetbrains-mcp-router/cache.json` | Route cache file path |
| `JBMCP_DEFAULT_PROJECT` | — | Fallback project path when CWD is unknown |
| `JBMCP_CONNECT_TIMEOUT` | `1.5` | HTTP connect timeout in seconds |
| `JBMCP_READ_TIMEOUT` | `60` | Default HTTP read timeout in seconds; set to `0`, `none`, `off`, or `disabled` to disable |
| `JBMCP_WRITE_TIMEOUT` | `10` | HTTP write timeout in seconds |
| `JBMCP_POOL_TIMEOUT` | `5` | HTTP connection-pool timeout in seconds |
| `JBMCP_TIMEOUT_GRACE` | `5` | Extra seconds added when deriving router timeout from a tool's `timeout` argument |
| `JBMCP_DEBUG` | — | Set to `1` to enable debug logging |
| `JBMCP_LOG_FILE` | — | Optional log file path (logs go to stderr **and** the file) |

## Router Timeout

The router does not add router-specific arguments to JetBrains tool schemas.

When a JetBrains tool schema has a numeric `timeout` argument whose description clearly says it is measured in milliseconds, the router derives its HTTP read timeout from that original tool argument:

```text
router_read_timeout_seconds = timeout_milliseconds / 1000 + JBMCP_TIMEOUT_GRACE
```

For example, `execute_run_configuration(timeout=1200000)` gives the router a read timeout of `1200 + grace` seconds while forwarding the original `timeout=1200000` unchanged to the JetBrains IDE tool.

If a future JetBrains tool adds a `timeout` argument with unclear units or semantics, the router logs a warning and keeps using the default `JBMCP_READ_TIMEOUT` instead of deriving a read timeout from it.

## How Routing Works

1. **First call for a project** — the router probes all candidate ports concurrently, calls `get_repositories` on each responding IDE, and matches the normalized project path. The result is written to the cache.
2. **Subsequent calls** — the cache is checked first. If the cached IDE no longer has the project open (e.g. after a restart that changed the port), the entry is invalidated and rediscovery runs automatically.
3. **Tool list** — the union of all responding IDEs' tool lists is returned, making every IDE-specific tool available regardless of which IDE owns the current project.

## Cache

The route cache lives at `~/.jetbrains-mcp-router/cache.json` (override with `JBMCP_CACHE`). It maps normalized project paths to IDE URLs. Each cached entry is validated on use — stale entries are replaced automatically.

## License

[Apache License 2.0](LICENSE)
