"""Physical MCP request limits include initialization, reconnection and retries."""
import asyncio
import json

import httpx
import pytest

from CharTyr_MaiWork.maiwork import mcp_client as mc
from CharTyr_MaiWork.maiwork.search import _call_retry_5xx


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['session', '5xx'])
async def test_physical_http_budget_includes_hidden_protocol_requests(monkeypatch, mode):
    hits = []
    async def handler(request):
        payload = json.loads(request.content)
        hits.append(payload['method'])
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        if payload['method'] == 'tools/call':
            return httpx.Response(404 if mode == 'session' else 503)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload['id'], 'result': {}})
    monkeypatch.setattr('CharTyr_MaiWork.maiwork.search._MCP_RETRY_DELAY_S', 0)
    client = mc.McpSessionClient('https://mcp.example/mcp', '', transport=httpx.MockTransport(handler))
    try:
        with mc.use_request_budget(mc.RequestBudget(requests=4, concurrency=2, timeout_s=1)):
            with pytest.raises((mc.MCPError, mc.RequestBudgetExceeded)):
                await _call_retry_5xx(client, 'search', {})
        assert len(hits) == 4
        assert hits[:3] == ['initialize', 'notifications/initialized', 'tools/call']
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_shared_concurrency_deadline_and_queued_cancellation():
    active = peak = hits = 0
    stopped = []
    async def handler(request):
        nonlocal active, peak, hits
        active += 1
        hits += 1
        peak = max(peak, active)
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            stopped.append(True)
    clients = [mc.McpSessionClient('https://mcp.example/mcp', '', transport=httpx.MockTransport(handler)) for _ in range(8)]
    budget = mc.RequestBudget(requests=4, concurrency=2, timeout_s=0.05)
    try:
        with mc.use_request_budget(budget):
            results = await asyncio.gather(*(c.call_tool('search', {}) for c in clients), return_exceptions=True)
        assert peak == hits == len(stopped) == 2
        assert active == 0 and all(isinstance(r, Exception) for r in results)
        assert budget.used == 2
    finally:
        await asyncio.gather(*(c.aclose() for c in clients))


@pytest.mark.asyncio
async def test_parent_cancellation_during_notification_reinitializes_session():
    methods = []
    notifying, stopped = asyncio.Event(), asyncio.Event()
    notifications = 0

    async def handler(request):
        nonlocal notifications
        payload = json.loads(request.content)
        methods.append(payload['method'])
        if payload['method'] == 'notifications/initialized':
            notifications += 1
            if notifications == 1:
                notifying.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return httpx.Response(202)
        return httpx.Response(200, headers={'mcp-session-id': 'fixture-session'},
                              json={'jsonrpc': '2.0', 'id': payload['id'], 'result': {}})

    client = mc.McpSessionClient('https://mcp.example/mcp', '', transport=httpx.MockTransport(handler))
    task = None
    try:
        with mc.use_request_budget(mc.RequestBudget(timeout_s=1)):
            task = asyncio.create_task(client.call_tool('search', {}))
            await asyncio.wait_for(notifying.wait(), 0.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped.is_set() and not client._init_lock.locked()
        assert client._session_id is None and not client._ready
        assert methods == ['initialize', 'notifications/initialized']
        with mc.use_request_budget(mc.RequestBudget(timeout_s=1)):
            assert await client.call_tool('search', {}) == {}
        assert methods == ['initialize', 'notifications/initialized',
                           'initialize', 'notifications/initialized', 'tools/call']
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['initialize', 'notifications/initialized'])
@pytest.mark.parametrize('stop', ['deadline', 'cancel'])
async def test_shared_initialization_waiter_deadline_and_cancellation(stage, stop):
    methods = []
    entered, release, waiter_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(request):
        payload = json.loads(request.content)
        methods.append(payload['method'])
        if payload['method'] == stage:
            entered.set()
            await release.wait()
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        return httpx.Response(200, headers={'mcp-session-id': 'fixture-session'},
                              json={'jsonrpc': '2.0', 'id': payload['id'], 'result': {}})

    client = mc.McpSessionClient('https://mcp.example/mcp', '', transport=httpx.MockTransport(handler))
    owner = asyncio.create_task(client.call_tool('search', {}))
    waiter = None
    budget = mc.RequestBudget(timeout_s=0.03 if stop == 'deadline' else 1)

    async def queued():
        with mc.use_request_budget(budget):
            waiter_started.set()
            return await client.call_tool('search', {})

    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        before = list(methods)
        waiter = asyncio.create_task(queued())
        await waiter_started.wait()
        if stop == 'deadline':
            with pytest.raises(mc.RequestBudgetExceeded, match='总时限'):
                await asyncio.wait_for(waiter, 0.3)
        else:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert methods == before and budget.used == 0
        assert client._init_lock.locked()
        release.set()
        assert await asyncio.wait_for(owner, 0.5) == {}
        assert methods == ['initialize', 'notifications/initialized', 'tools/call']
        assert not client._init_lock.locked()
        # Stopped waiters must never wake up and send later.
        assert await client.call_tool('search', {}) == {}
        assert methods[-2:] == ['tools/call', 'tools/call']
    finally:
        release.set()
        for task in (owner, waiter):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (owner, waiter) if t is not None), return_exceptions=True)
        await client.aclose()
