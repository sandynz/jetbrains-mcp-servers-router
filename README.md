# JetBrains MCP Servers Router

A single-file Python MCP server that routes tool calls to the correct JetBrains IDE based on the active project path.

> **Chinese README**: [README_zh.md](README_zh.md)

---

## Problem

JetBrains IDEs (IntelliJ IDEA, PyCharm, RustRover, …) each expose a built-in MCP server on a different port. When you work on multiple projects simultaneously — each open in a different IDE — you need a way to tell your coding agent which MCP server to use for the current project.

Without this router you need one MCP entry per IDE in every coding agent configuration file.

## Solution

The router exposes a **single stdio MCP endpoint**. On every tool call it:

1. Resolves the target project path (`projectPath` argument → `JBMCP_DEFAULT_PROJECT` env var → current working directory)
2. Looks up a file-based cache (`~/.jetbrains-mcp-router/cache.json`) for the IDE URL that owns that project
3. On a cache miss, **concurrently probes** candidate ports (default: 64342–64351), calls `get_repositories` on each responding IDE, and matches the project path
4. Forwards the call to the correct IDE's `/stream` endpoint, injecting the resolved `projectPath`

The **union** of all connected IDEs' tool lists is returned, so IDE-specific tools (e.g. `runNotebookCell` in PyCharm, `run_inspection_kts` in RustRover) are all visible to the coding agent.

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

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JBMCP_PORT_START` | `64342` | First port to probe |
| `JBMCP_PORT_COUNT` | `10` | Number of consecutive ports to scan |
| `JBMCP_HOST` | `127.0.0.1` | IDE host |
| `JBMCP_CACHE` | `~/.jetbrains-mcp-router/cache.json` | Route cache file path |
| `JBMCP_DEFAULT_PROJECT` | — | Fallback project path when CWD is unknown |
| `JBMCP_DEBUG` | — | Set to `1` to enable debug logging |
| `JBMCP_LOG_FILE` | — | Optional log file path (logs go to stderr **and** the file) |

## How Routing Works

1. **First call for a project** — the router probes all candidate ports concurrently, calls `get_repositories` on each responding IDE, and matches the normalized project path. The result is written to the cache.
2. **Subsequent calls** — the cache is checked first. If the cached IDE no longer has the project open (e.g. after a restart that changed the port), the entry is invalidated and rediscovery runs automatically.
3. **Tool list** — the union of all responding IDEs' tool lists is returned, making every IDE-specific tool available regardless of which IDE owns the current project.

## Cache

The route cache lives at `~/.jetbrains-mcp-router/cache.json` (override with `JBMCP_CACHE`). It maps normalized project paths to IDE URLs. Each cached entry is validated on use — stale entries are replaced automatically.

## License

[Apache License 2.0](LICENSE)
