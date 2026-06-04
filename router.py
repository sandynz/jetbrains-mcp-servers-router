#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.0",
#   "httpx>=0.27",
# ]
# ///
"""
JetBrains MCP Router — routes MCP tool calls to the correct JetBrains IDE.

Exposes a single MCP server over stdio. On each tool call it:
  1. Resolves the project path (from ``projectPath`` arg → env var → CWD)
  2. Looks up a file-based cache for the IDE URL that owns that project
  3. On cache miss, concurrently probes candidate ports and calls
     ``get_repositories`` on each responding IDE to find the right one
  4. Forwards the call to the correct IDE's ``/stream`` endpoint (MCP
     Streamable HTTP), injecting the resolved ``projectPath`` into the args

Environment variables
---------------------
JBMCP_PORT_START       First port to probe  (default: 64342)
JBMCP_PORT_COUNT       Number of ports      (default: 10)
JBMCP_HOST             IDE host             (default: 127.0.0.1)
JBMCP_CACHE            Cache file path      (default: ~/.jetbrains-mcp-router/cache.json)
JBMCP_DEFAULT_PROJECT  Fallback project when no projectPath in args and CWD unknown
JBMCP_CONNECT_TIMEOUT  HTTP connect timeout in seconds (default: 1.5)
JBMCP_READ_TIMEOUT     HTTP read timeout in seconds; 0/none disables it (default: 60)
JBMCP_WRITE_TIMEOUT    HTTP write timeout in seconds (default: 10)
JBMCP_POOL_TIMEOUT     HTTP pool timeout in seconds (default: 5)
JBMCP_TIMEOUT_GRACE    Extra seconds added when deriving router timeout from tool timeout
                         (default: 5)
JBMCP_DEBUG            Set to 1 to include DEBUG messages on stderr
JBMCP_LOG_FILE         Log file path (default: ~/.jetbrains-mcp-router/router.log);
                         set to empty string to disable file logging
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_PORT_START = int(os.environ.get("JBMCP_PORT_START", "64342"))
_PORT_COUNT = int(os.environ.get("JBMCP_PORT_COUNT", "10"))
_HOST = os.environ.get("JBMCP_HOST", "127.0.0.1")
_CACHE_PATH = Path(
    os.environ.get(
        "JBMCP_CACHE",
        str(Path.home() / ".jetbrains-mcp-router" / "cache.json"),
    )
)
_TIMEOUT_UNSET = object()


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0, got {raw!r}")
    return value


def _optional_float_env(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if raw.lower() in {"0", "none", "null", "off", "disabled"}:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number or 'none', got {raw!r}") from exc
    if value <= 0:
        return None
    return value


_CONNECT_TIMEOUT_SECONDS = _float_env("JBMCP_CONNECT_TIMEOUT", 1.5)
_READ_TIMEOUT_SECONDS = _optional_float_env("JBMCP_READ_TIMEOUT", 60.0)
_WRITE_TIMEOUT_SECONDS = _float_env("JBMCP_WRITE_TIMEOUT", 10.0)
_POOL_TIMEOUT_SECONDS = _float_env("JBMCP_POOL_TIMEOUT", 5.0)
_TIMEOUT_GRACE_SECONDS = _optional_float_env("JBMCP_TIMEOUT_GRACE", 5.0) or 0.0


def _stream_url(port: int) -> str:
    return f"http://{_HOST}:{port}/stream"


# ── Path normalisation ────────────────────────────────────────────────────────


def _norm(p: str) -> str:
    """Resolve and case-fold (Windows) a path for stable cache keys."""
    resolved = str(Path(p).resolve())
    return resolved.casefold() if sys.platform == "win32" else resolved


# ── Persistent route cache ────────────────────────────────────────────────────

_route_cache: dict[str, str] = {}  # normalised project path → IDE stream URL


def _load_cache() -> None:
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _route_cache.update(data)
                log.info("Loaded %d route(s) from %s", len(_route_cache), _CACHE_PATH)
    except Exception as exc:
        log.error("Cache load failed: %s", exc)


def _save_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(_route_cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.error("Cache save failed: %s", exc)


# ── HTTP client + MCP Streamable HTTP helpers ─────────────────────────────────

_http: httpx.AsyncClient  # initialised in _run()
_req_id = 0
_session_ids: dict[str, str] = {}  # IDE URL → Mcp-Session-Id (empty string = none)
_millisecond_timeout_tools: set[str] = set()
_ambiguous_timeout_tools: set[str] = set()


def _next_id() -> int:
    global _req_id
    _req_id += 1
    return _req_id


def _extract_sse(text: str) -> list[dict]:
    """Return all JSON-RPC objects from ``data:`` lines in an SSE response body."""
    out: list[dict] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                raw = line[6:].strip()
                if raw and raw != "[DONE]":
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, dict):
                            out.append(obj)
                    except json.JSONDecodeError:
                        pass
    return out


def _build_headers(url: str) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream"}
    sid = _session_ids.get(url, "")
    if sid:
        headers["Mcp-Session-Id"] = sid
    return headers


def _timeout(read_timeout: float | None | object = _TIMEOUT_UNSET) -> httpx.Timeout:
    resolved_read_timeout = (
        _READ_TIMEOUT_SECONDS if read_timeout is _TIMEOUT_UNSET else read_timeout
    )
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS,
        read=resolved_read_timeout,
        write=_WRITE_TIMEOUT_SECONDS,
        pool=_POOL_TIMEOUT_SECONDS,
    )


def _milliseconds_to_seconds(value: Any, name: str) -> float | None:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a number of milliseconds, got {value!r}")
    if value <= 0:
        return None
    return float(value) / 1000.0


def _has_millisecond_timeout(schema: dict[str, Any]) -> bool | None:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or "timeout" not in properties:
        return None
    timeout_schema = properties["timeout"]
    if not isinstance(timeout_schema, dict):
        return False
    timeout_type = timeout_schema.get("type")
    description = str(timeout_schema.get("description", "")).lower()
    has_numeric_type = timeout_type in {"integer", "number"}
    mentions_milliseconds = any(
        keyword in description for keyword in ("milliseconds", "millisecond", " ms")
    )
    return has_numeric_type and mentions_milliseconds


def _remember_tool_timeout_semantics(name: str, schema: dict[str, Any]) -> None:
    has_millisecond_timeout = _has_millisecond_timeout(schema)
    if has_millisecond_timeout is None:
        return
    if has_millisecond_timeout:
        if name not in _ambiguous_timeout_tools:
            _millisecond_timeout_tools.add(name)
        return
    _millisecond_timeout_tools.discard(name)
    if name not in _ambiguous_timeout_tools:
        log.warning(
            "Tool %s has a timeout argument with unclear semantics; "
            "router will not derive HTTP read timeout from it. schema=%s",
            name,
            json.dumps(schema.get("properties", {}).get("timeout"), ensure_ascii=False),
        )
    _ambiguous_timeout_tools.add(name)


def _resolve_tool_read_timeout(name: str, args: dict[str, Any]) -> float | None | object:
    if type(args.get("timeout")) not in (int, float):
        return _TIMEOUT_UNSET
    if name in _ambiguous_timeout_tools:
        log.warning(
            "Tool %s timeout argument is not known to be milliseconds; "
            "using default router HTTP read timeout",
            name,
        )
        return _TIMEOUT_UNSET
    if name not in _millisecond_timeout_tools:
        log.warning(
            "Tool %s provided timeout before its schema was classified; "
            "using default router HTTP read timeout",
            name,
        )
        return _TIMEOUT_UNSET
    tool_timeout = _milliseconds_to_seconds(args["timeout"], "timeout")
    if tool_timeout is None:
        return None
    return tool_timeout + _TIMEOUT_GRACE_SECONDS


def _tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if isinstance(schema, dict):
        return schema
    return {"type": "object", "properties": {}}


async def _initialize(url: str) -> None:
    """Run MCP initialize + initialized handshake; cache session ID if provided."""
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "jetbrains-mcp-router", "version": "0.1.0"},
        },
    }
    resp = await _http.post(
        url, json=payload, headers={"Accept": "application/json, text/event-stream"}
    )
    resp.raise_for_status()
    _session_ids[url] = resp.headers.get("Mcp-Session-Id", "")

    # Send initialized notification (fire-and-forget; response may be empty 202)
    try:
        await _http.post(
            url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers=_build_headers(url),
        )
    except Exception:
        pass


async def _post(
    url: str,
    method: str,
    params: dict,
    *,
    read_timeout: float | None | object = _TIMEOUT_UNSET,
    _retry: bool = True,
) -> dict:
    """POST a JSON-RPC request; auto-initialises session and retries once on stale."""
    if url not in _session_ids:
        try:
            await _initialize(url)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            # Port unreachable — expected during startup probing of dead ports.
            # Do NOT mark url as initialized — allows re-init on the next call
            # so IDEs that start up after the router can be discovered later.
            log.debug("initialize at %s unreachable: %s", url, exc)
        except Exception as exc:
            log.warning("initialize at %s failed unexpectedly: %s", url, exc)

    payload = {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params}
    try:
        resp = await _http.post(
            url, json=payload, headers=_build_headers(url), timeout=_timeout(read_timeout)
        )
    except httpx.RemoteProtocolError as exc:
        # Stale keepalive connection (IDE closed it while router was idle).
        # httpx removes the dead connection from its pool; reinitialise and retry once.
        if _retry:
            log.warning("Stale connection to %s (%s); reinitialising and retrying", url, exc)
            _session_ids.pop(url, None)
            return await _post(
                url, method, params, read_timeout=read_timeout, _retry=False
            )
        raise ConnectionError(f"IDE at {url} protocol error: {exc}") from exc
    except httpx.ConnectError as exc:
        raise ConnectionError(f"IDE at {url} unreachable: {exc}") from exc
    except httpx.TimeoutException as exc:
        log.warning("IDE at %s timed out while handling %s: %s", url, method, exc)
        raise TimeoutError(f"IDE at {url} timed out while handling {method}: {exc}") from exc

    if resp.status_code == 404 and _retry:
        # Session expired (IDE restarted); reinitialise and retry once
        log.warning("Session at %s expired (404); reinitialising", url)
        _session_ids.pop(url, None)
        return await _post(url, method, params, read_timeout=read_timeout, _retry=False)

    resp.raise_for_status()

    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        messages = _extract_sse(resp.text)
    else:
        raw = resp.json()
        messages = [raw] if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])

    for msg in messages:
        if "result" in msg:
            return msg["result"]
        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(err.get("message", str(err)))

    raise RuntimeError(f"No JSON-RPC result from {url}: {resp.text[:300]}")


# ── IDE discovery ─────────────────────────────────────────────────────────────


async def _project_paths_at(url: str) -> list[str]:
    """Return normalised project paths open in the IDE at ``url``, or [] on failure.

    When ``get_repositories`` is called without ``projectPath`` the IDE returns
    ``isError: true`` but still embeds the open-project list in ``structuredContent``,
    which is the authoritative source. The ``content[0].text`` fallback exists for
    future IDE versions that may change the response format.
    """
    try:
        result = await _post(url, "tools/call", {"name": "get_repositories", "arguments": {}})
        paths: list[str] = []

        # Primary: structuredContent.projects (reliable structured data)
        struct = result.get("structuredContent") or {}
        if "projects" in struct:
            for repo in struct["projects"]:
                p = repo.get("path") or ""
                if p:
                    paths.append(_norm(p))
            return paths

        # Fallback: parse content[0].text as JSON
        for item in result.get("content", []):
            if item.get("type") != "text":
                continue
            text: str = item["text"]
            try:
                data = json.loads(text)
                repos = data if isinstance(data, list) else [data]
                for repo in repos:
                    p = (
                        repo.get("path")
                        or repo.get("projectPath")
                        or repo.get("rootPath")
                        or ""
                    )
                    if p:
                        paths.append(_norm(p))
            except json.JSONDecodeError:
                pass
        return paths
    except (ConnectionError, TimeoutError) as exc:
        log.debug("get_repositories at %s unreachable: %s", url, exc)
        return []
    except Exception as exc:
        log.warning("get_repositories at %s failed unexpectedly: %s", url, exc)
        return []


async def _discover_ide(project_path: str) -> str | None:
    """Concurrently probe all candidate ports; return the URL that owns the project."""
    normalized = _norm(project_path)

    async def probe(port: int) -> tuple[str, bool]:
        url = _stream_url(port)
        paths = await _project_paths_at(url)
        return url, normalized in paths

    results = await asyncio.gather(
        *(probe(p) for p in range(_PORT_START, _PORT_START + _PORT_COUNT)),
        return_exceptions=True,
    )
    for item in results:
        if isinstance(item, BaseException):
            continue
        url, matched = item
        if matched:
            log.info("Discovered %s → %s", normalized, url)
            return url
    return None


async def _route(project_path: str) -> str:
    """Return the IDE URL for ``project_path``, using the cache when valid."""
    normalized = _norm(project_path)

    if normalized in _route_cache:
        url = _route_cache[normalized]
        # _project_paths_at never raises (it has full exception coverage); the call
        # either returns a list of paths or returns [] if the IDE is unreachable.
        current_paths = await _project_paths_at(url)
        if normalized in current_paths:
            return url
        log.warning(
            "IDE at %s no longer has project %s (port reassigned?); re-discovering",
            url, normalized,
        )
        del _route_cache[normalized]
        _session_ids.pop(url, None)
        _save_cache()

    url = await _discover_ide(normalized)
    if url is None:
        raise RuntimeError(
            f"No JetBrains IDE found for project: {normalized}\n"
            f"Scanned {_PORT_COUNT} port(s) starting at {_PORT_START}. "
            "Is the IDE running with its built-in MCP server enabled?"
        )
    _route_cache[normalized] = url
    _save_cache()
    return url


# ── MCP server ────────────────────────────────────────────────────────────────

server = Server("jetbrains-mcp-router")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """Return the union of tool lists from all responding IDEs.

    Different JetBrains IDEs expose IDE-specific tools (e.g. ``runNotebookCell``
    in PyCharm, ``run_inspection_kts`` in RustRover). Returning the union ensures
    the coding agent can see and invoke all available tools regardless of which
    IDE happens to respond first.

    All candidate ports are probed concurrently to avoid sequential timeouts on
    dead ports (each dead port takes ~2 s on Windows; sequential × 8 dead = 16+ s).
    """
    seen: dict[str, types.Tool] = {}  # name → Tool (first schema wins for duplicates)

    async def _fetch_from(port: int) -> list[dict]:
        url = _stream_url(port)
        try:
            result = await _post(url, "tools/list", {})
            tools = result.get("tools", [])
            log.info("Collected %d tools from port %d", len(tools), port)
            return tools
        except (ConnectionError, TimeoutError) as exc:
            log.debug("tools/list at port %d unreachable: %s", port, exc)
            return []
        except Exception as exc:
            log.warning("tools/list at port %d failed unexpectedly: %s", port, exc)
            return []

    all_results = await asyncio.gather(
        *(_fetch_from(p) for p in range(_PORT_START, _PORT_START + _PORT_COUNT)),
        return_exceptions=False,
    )
    for tools in all_results:
        for t in tools:
            name = t["name"]
            input_schema = _tool_schema(t.get("inputSchema"))
            _remember_tool_timeout_semantics(name, input_schema)
            if name not in seen:
                seen[name] = types.Tool(
                    name=name,
                    description=t.get("description", ""),
                    inputSchema=input_schema,
                )

    if not seen:
        raise RuntimeError(
            f"No JetBrains IDE found in ports {_PORT_START}–{_PORT_START + _PORT_COUNT - 1}. "
            "Start an IDE with MCP server enabled."
        )
    log.info("Returning %d tools (union of all responding IDEs)", len(seen))
    return list(seen.values())


@server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict[str, Any] | None,
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Route a tool call to the IDE that owns the resolved ``projectPath``."""
    args = dict(arguments or {})

    # Resolve projectPath: arg → env var → CWD; always re-inject normalised absolute path
    project_path: str = (
        args.get("projectPath")
        or os.environ.get("JBMCP_DEFAULT_PROJECT", "")
        or os.getcwd()
    )
    resolved = str(Path(project_path).resolve())
    args["projectPath"] = resolved
    read_timeout = _resolve_tool_read_timeout(name, args)

    url = await _route(resolved)
    log.info("tool=%s → %s (project: %s)", name, url, resolved)
    result = await _post(
        url, "tools/call", {"name": name, "arguments": args}, read_timeout=read_timeout
    )

    items: list[types.TextContent | types.ImageContent | types.EmbeddedResource] = []
    for item in result.get("content", []):
        t = item.get("type")
        if t == "text":
            items.append(types.TextContent(type="text", text=item["text"]))
        elif t == "image":
            items.append(
                types.ImageContent(
                    type="image",
                    data=item["data"],
                    mimeType=item.get("mimeType", "image/png"),
                )
            )
    if not items:
        # Fallback: wrap entire result as JSON text
        items.append(types.TextContent(type="text", text=json.dumps(result)))
    return items


# ── Entry point ───────────────────────────────────────────────────────────────


async def _run() -> None:
    global _http
    _load_cache()
    _http = httpx.AsyncClient(timeout=_timeout())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await _http.aclose()


def _make_log_handlers() -> list[logging.Handler]:
    stderr_level = logging.DEBUG if os.environ.get("JBMCP_DEBUG") else logging.WARNING
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(stderr_level)
    handlers: list[logging.Handler] = [stderr_handler]

    default_log_file = str(_CACHE_PATH.parent / "router.log")
    log_file = os.environ.get("JBMCP_LOG_FILE", default_log_file)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        handlers.append(file_handler)

    return handlers


def main() -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = _make_log_handlers()

    # Root logger accepts everything; per-handler levels do the filtering.
    logging.basicConfig(level=logging.DEBUG, format=fmt, handlers=handlers)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
