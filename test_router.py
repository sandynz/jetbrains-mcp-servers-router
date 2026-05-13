#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "mcp>=1.0"]
# ///
"""Unit tests for jetbrains-mcp-servers-router router.py internal logic."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import router


def _fake_path(name: str) -> str:
    """Return a platform-appropriate fake project path for test fixtures."""
    if sys.platform == "win32":
        return rf"C:\path\to\{name}"
    return f"/path/to/{name}"

P = "\u2705"
F = "\u274c"

def make_json_resp(result, status=200, session_id="", url="http://test"):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
    hdrs = {"content-type": "application/json"}
    if session_id:
        hdrs["mcp-session-id"] = session_id
    r = httpx.Response(status, content=body, headers=hdrs)
    r.request = httpx.Request("POST", url)
    return r

def make_404(url="http://test"):
    body = json.dumps({"error": {"code": -32000, "message": "Session expired"}}).encode()
    r = httpx.Response(404, content=body, headers={"content-type": "application/json"})
    r.request = httpx.Request("POST", url)
    return r

def make_notif_resp(url="http://test"):
    r = httpx.Response(202, content=b"")
    r.request = httpx.Request("POST", url)
    return r

def reset():
    router._route_cache.clear()
    router._session_ids.clear()
    router._req_id = 0

def make_mock(seq):
    """seq: list of httpx.Response or Exception instances, returned/raised in order."""
    box = [0]
    async def _side(*a, **kw):
        i = box[0]
        box[0] += 1
        item = seq[i]
        if isinstance(item, BaseException):
            raise item
        return item
    m = AsyncMock()
    m.post.side_effect = _side
    return m, box

def req(port=9999):
    return httpx.Request("POST", f"http://127.0.0.1:{port}/stream")

# Init sequence helper: [initialize-resp, notif-resp]
def init_seq(session_id="s1"):
    return [make_json_resp({}, session_id=session_id), make_notif_resp()]


# ── T1: _extract_sse ─────────────────────────────────────────────────────────
def t1_extract_sse():
    print("T1: _extract_sse")
    sse = 'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\ndata: [DONE]\n\n'
    res = router._extract_sse(sse)
    assert len(res) == 1 and res[0]["result"]["ok"] is True
    print(f"  {P} parses data lines, skips [DONE]")
    assert router._extract_sse("event: ping\n\n") == []
    print(f"  {P} ignores event-only blocks")
    multi = 'data: {"id":1,"result":{"a":1}}\n\ndata: {"id":2,"result":{"b":2}}\n\n'
    res2 = router._extract_sse(multi)
    assert len(res2) == 2
    print(f"  {P} parses multiple data blocks\n")


# ── T2: _norm normalizes paths ───────────────────────────────────────────────
def t2_norm():
    print("T2: _norm normalizes paths")
    proj = _fake_path("my_project")
    a = router._norm(proj)
    assert a == router._norm(proj), "same input must produce same output"
    print(f"  {P} consistent: same path normalizes identically")
    if sys.platform == "win32":
        assert router._norm(proj.upper()) == a, "Windows: casefold should match"
        print(f"  {P} Windows: mixed-case path compares equal (casefold)")
    else:
        print(f"  {P} non-Windows: no casefold applied")
    print()


# ── T3: cache save / load ─────────────────────────────────────────────────────
def t3_cache():
    print("T3: cache save/load")
    with tempfile.TemporaryDirectory() as tmp:
        old = router._CACHE_PATH
        router._CACHE_PATH = Path(tmp) / "cache.json"
        try:
            reset()
            key = router._norm(_fake_path("test_proj"))
            router._route_cache[key] = "http://127.0.0.1:64342/stream"
            router._save_cache()
            router._route_cache.clear()
            router._load_cache()
            assert router._route_cache.get(key) == "http://127.0.0.1:64342/stream"
            print(f"  {P} saved and reloaded correctly\n")
        finally:
            router._CACHE_PATH = old
            reset()


# ── T4: _post RemoteProtocolError → retry once, succeed ──────────────────────
async def t4_stale_retry():
    print("T4: _post RemoteProtocolError -> retry once")
    reset()
    url = "http://127.0.0.1:9994/stream"
    seq = [
        *init_seq("s1"),                                              # first _initialize
        httpx.RemoteProtocolError("stale", request=req(9994)),        # actual call -> stale
        *init_seq("s2"),                                              # re-_initialize
        make_json_resp({"tools": []}),                                # retry succeeds
    ]
    mock, box = make_mock(seq)
    router._http = mock

    result = await router._post(url, "tools/list", {})
    assert result == {"tools": []}, f"unexpected: {result}"
    assert router._session_ids.get(url) == "s2", "session should be s2"
    assert box[0] == 6, f"expected 6 HTTP calls, got {box[0]}"
    print(f"  {P} stale: reinit + retry -> success, session updated to s2")
    print(f"  {P} exactly 6 HTTP calls (init*2 + notif*2 + stale + retry)\n")


# ── T5: _post 404 → session reinit + retry ───────────────────────────────────
async def t5_404_retry():
    print("T5: _post 404 -> session reinit + retry")
    reset()
    url = "http://127.0.0.1:9993/stream"
    seq = [
        *init_seq("old"),
        make_404(),                                  # 404 on first actual call
        *init_seq("new"),
        make_json_resp({"tools": [{"name": "x"}]}),  # retry succeeds
    ]
    mock, box = make_mock(seq)
    router._http = mock

    result = await router._post(url, "tools/list", {})
    assert result.get("tools") == [{"name": "x"}]
    assert router._session_ids.get(url) == "new"
    print(f"  {P} 404: reinit + retry -> success")
    print(f"  {P} session updated to 'new'\n")


# ── T6: _post no infinite retry on persistent RemoteProtocolError ─────────────
async def t6_no_infinite_retry():
    print("T6: _post no infinite retry on persistent RemoteProtocolError")
    reset()
    url = "http://127.0.0.1:9992/stream"
    seq = [
        *init_seq("s1"),
        httpx.RemoteProtocolError("stale", request=req(9992)),  # stale -> retry
        *init_seq("s2"),
        httpx.RemoteProtocolError("still stale", request=req(9992)),  # stale again (_retry=False)
    ]
    mock, _ = make_mock(seq)
    router._http = mock

    try:
        await router._post(url, "tools/list", {})
        assert False, "should have raised"
    except ConnectionError as e:
        assert "protocol error" in str(e).lower(), str(e)
        print(f"  {P} second RemoteProtocolError -> ConnectionError (no infinite loop)\n")


# ── T7: dead port (ConnectError) is skipped in _project_paths_at ─────────────
async def t7_dead_port():
    print("T7: dead port -> _project_paths_at returns []")
    reset()
    url = "http://127.0.0.1:9991/stream"
    # _initialize ConnectError -> NOT added to _session_ids; actual call also ConnectError -> ConnectionError
    seq = [
        httpx.ConnectError("refused", request=req(9991)),   # _initialize fails
        httpx.ConnectError("refused", request=req(9991)),   # actual call -> ConnectionError
    ]
    mock, _ = make_mock(seq)
    router._http = mock

    paths = await router._project_paths_at(url)
    assert paths == [], f"expected [], got {paths}"
    assert url not in router._session_ids, "dead port should NOT be in _session_ids (allows re-init later)"
    print(f"  {P} ConnectError -> empty list, no exception raised")
    print(f"  {P} dead port NOT added to _session_ids (re-init allowed on next call)\n")


# ── T11: previously-dead port starts up -> re-init succeeds on next call ──────
async def t11_late_start_ide():
    print("T11: IDE starts after router -> re-init on next _project_paths_at call")
    reset()
    url = "http://127.0.0.1:9980/stream"
    project = _fake_path("project_a")

    # First call: _initialize fails (IDE not yet running), actual request also fails
    call_count = [0]
    ide_result = {"structuredContent": {"projects": [{"path": project}]}}

    async def smart_post(target_url, **kw):
        call_count[0] += 1
        n = call_count[0]
        r = httpx.Request("POST", target_url)
        if n <= 2:
            # Calls 1-2: both fail (IDE dead at startup)
            raise httpx.ConnectError("not yet", request=r)
        elif n == 3:
            # Call 3: _initialize (IDE now alive)
            return make_json_resp({}, session_id="late-s", url=target_url)
        elif n == 4:
            # Call 4: notifications/initialized
            return make_notif_resp(url=target_url)
        else:
            # Call 5: get_repositories
            return make_json_resp(ide_result, url=target_url)

    mock = AsyncMock()
    mock.post.side_effect = smart_post
    router._http = mock

    # First call: IDE dead -> returns []
    paths1 = await router._project_paths_at(url)
    assert paths1 == [], f"expected [], got {paths1}"
    assert url not in router._session_ids, "should not be in _session_ids after failed init"
    print(f"  {P} first call (IDE dead): returned []")

    # Second call: IDE now alive -> init succeeds, returns project
    paths2 = await router._project_paths_at(url)
    norm = router._norm(project)
    assert norm in paths2, f"{norm} not in {paths2}"
    assert router._session_ids.get(url) == "late-s"
    print(f"  {P} second call (IDE alive): re-init succeeded, project found")
    print(f"  {P} session ID 'late-s' correctly stored\n")


# ── T8: _project_paths_at parses structuredContent.projects ──────────────────
async def t8_structured_content():
    print("T8: _project_paths_at parses structuredContent.projects (isError=true case)")
    reset()
    url = "http://127.0.0.1:9990/stream"
    ide_result = {
        "isError": True,
        "content": [{"type": "text", "text": "Unable to determine..."}],
        "structuredContent": {"projects": [
            {"path": _fake_path("project_a")},
            {"path": _fake_path("project_b")},
        ]},
    }
    seq = [*init_seq(), make_json_resp(ide_result)]
    mock, _ = make_mock(seq)
    router._http = mock

    paths = await router._project_paths_at(url)
    assert router._norm(_fake_path("project_a")) in paths
    assert router._norm(_fake_path("project_b")) in paths
    print(f"  {P} extracted {len(paths)} paths, both present\n")


# ── T9: _route cache-hit validation success (no re-discovery) ─────────────────
async def t9_cache_hit():
    print("T9: _route cache hit -> validation passes -> no re-discovery")
    reset()
    url = "http://127.0.0.1:9989/stream"
    project = _fake_path("project_a")
    norm = router._norm(project)

    router._route_cache[norm] = url
    router._session_ids[url] = "s1"  # already initialized

    ide_result = {
        "structuredContent": {"projects": [{"path": project}]}
    }
    seq = [make_json_resp(ide_result)]  # only 1 call: get_repositories for validation
    mock, box = make_mock(seq)
    router._http = mock

    result_url = await router._route(project)
    assert result_url == url
    assert box[0] == 1, f"expected 1 HTTP call (validation only), got {box[0]}"
    print(f"  {P} returned cached URL without re-discovery\n")


# ── T10: _route cache stale -> re-discovers correct IDE ──────────────────────
async def t10_cache_stale():
    print("T10: _route stale cache (wrong port) -> re-discovers correct IDE")
    reset()
    stale_url  = "http://127.0.0.1:9988/stream"  # wrong IDE (doesn't have the project)
    correct_url = "http://127.0.0.1:9987/stream" # correct IDE
    project = _fake_path("project_a")
    norm = router._norm(project)

    router._route_cache[norm] = stale_url
    router._session_ids[stale_url] = "stale-s"

    # Cache validation: stale_url's get_repositories returns OTHER project
    stale_result = {"structuredContent": {"projects": [{"path": _fake_path("other_project")}]}}

    # _discover_ide probes _PORT_COUNT ports; we override them to use only 2 ports for speed
    old_start = router._PORT_START
    old_count = router._PORT_COUNT
    router._PORT_START = 9987
    router._PORT_COUNT = 2

    # Port 9987: correct IDE, has our project; port 9988: stale IDE, has other project
    call_counts = {"9987": 0, "9988": 0}
    async def smart_post(target_url, **kw):
        port = target_url.split(":")[2].split("/")[0]
        call_counts[port] = call_counts.get(port, 0) + 1
        if port == "9987":
            # _initialize + notif + get_repositories
            if call_counts["9987"] == 1:
                return make_json_resp({}, session_id="new-s")
            elif call_counts["9987"] == 2:
                return make_notif_resp()
            else:
                return make_json_resp({"structuredContent": {"projects": [{"path": project}]}})
        elif port == "9988":
            # Validation call (session already set)
            return make_json_resp(stale_result)

    mock_client = AsyncMock()
    mock_client.post.side_effect = smart_post
    router._http = mock_client

    try:
        result_url = await router._route(project)
        assert result_url == correct_url, f"expected {correct_url}, got {result_url}"
        assert norm not in router._route_cache or router._route_cache[norm] == correct_url
        print(f"  {P} stale cache detected, re-discovered correct IDE at {correct_url}\n")
    finally:
        router._PORT_START = old_start
        router._PORT_COUNT = old_count
        reset()


# ── T12: _route raises RuntimeError when no IDE owns the project ──────────────
async def t12_route_no_ide_found():
    print("T12: _route raises RuntimeError when no IDE found")
    reset()
    project = _fake_path("orphan_project")

    old_start = router._PORT_START
    old_count = router._PORT_COUNT
    router._PORT_START = 9970
    router._PORT_COUNT = 2

    async def all_dead(target_url, **kw):
        raise httpx.ConnectError("refused", request=httpx.Request("POST", target_url))

    mock = AsyncMock()
    mock.post.side_effect = all_dead
    router._http = mock

    try:
        try:
            await router._route(project)
            assert False, "should have raised RuntimeError"
        except RuntimeError as e:
            assert "No JetBrains IDE found" in str(e), str(e)
            print(f"  {P} RuntimeError raised: 'No JetBrains IDE found'\n")
    finally:
        router._PORT_START = old_start
        router._PORT_COUNT = old_count
        reset()


# ── Main ─────────────────────────────────────────────────────────────────────
async def run():
    t1_extract_sse()
    t2_norm()
    t3_cache()
    await t4_stale_retry()
    await t5_404_retry()
    await t6_no_infinite_retry()
    await t7_dead_port()
    await t8_structured_content()
    await t9_cache_hit()
    await t10_cache_stale()
    await t11_late_start_ide()
    await t12_route_no_ide_found()
    print("=" * 55)
    print("All 12 tests PASSED \u2705")

if __name__ == "__main__":
    asyncio.run(run())
