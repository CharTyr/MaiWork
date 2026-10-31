import asyncio
import pytest
from test_profile import store, _make, _msg, GID, T0
from fakes import FakeHost


@pytest.mark.asyncio
async def test_r4_concurrent_refresh_never_leaks_notice(store, monkeypatch):
    p = _make(store, FakeHost(), ready=True)
    for gid, ready in [(GID, 0), ('other', T0-10)]:
        p.ensure_group(gid)
        with store.tx() as c:
            c.execute('UPDATE groups SET profile_ready_ts=?,pending_count=1,session_id=? WHERE group_id=?', (ready, gid, gid))
    monkeypatch.setattr(p, '_refresh_due', lambda *a: True)
    async def read(*args): return [_msg('m', T0)]
    async def notices(gid): return ['仅A可见公告']
    monkeypatch.setattr(p, '_read_window', read)
    monkeypatch.setattr(p, '_notices_for_first_profile', notices)
    entered, release = asyncio.Event(), asyncio.Event()
    original = p._refine_with_split
    async def gated(gid, msgs, now, **kwargs):
        if gid == GID:
            entered.set()
            await release.wait()
        return await original(gid, msgs, now, **kwargs)
    monkeypatch.setattr(p, '_refine_with_split', gated)
    prompts = {}
    async def ask(gid, messages):
        prompts[gid] = str(messages)
        from types import SimpleNamespace
        return SimpleNamespace(text='结果', finish_reason='stop')
    monkeypatch.setattr(p, '_ask_model', ask)
    monkeypatch.setattr(p, '_read_answer', lambda result: ([], [], []))
    a = asyncio.create_task(p._maybe_refresh(GID, T0, True))
    await entered.wait()
    try:
        await p._maybe_refresh('other', T0, True)
    finally:
        release.set()
        await a
    assert '仅A可见公告' not in prompts['other']
    assert '仅A可见公告' in prompts[GID]


def test_r4_notice_is_explicit_prompt_input(store):
    p = _make(store, FakeHost())
    prompt = str(p._build_prompt(GID, [], notices=['仅本轮公告']))
    assert '仅本轮公告' in prompt
    assert '仅本轮公告' not in str(p._build_prompt(GID, []))
