"""Admin chat upgrade contracts, exercised against real SQLite and tool wiring."""
import asyncio
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import compaction, skills_web
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat, ChatBusy
from CharTyr_MaiWork.maiwork.models import ModelError
from test_admin_chat import G1, G2, SECRET, _Svc, _Models, _res, _call


@pytest.fixture
def env(tmp_path):
    svc = _Svc(tmp_path)
    svc.admin_pending._store = svc.store
    chat = AdminChat(svc)
    yield svc, chat, chat.create(G1)['id']
    chat.cancel_all()
    svc.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('immediate', [False, True])
async def test_stop_cancels_and_idle_stop_adds_nothing(env, immediate):
    svc, chat, cid = env
    svc.models.gate = asyncio.Event()
    await chat.send(cid, 'hello')
    if not immediate:
        await asyncio.sleep(0)
    assert await chat.stop(cid) == {'ok': True, 'stopped': True, 'chat_id': cid}
    assert not chat.is_busy(cid)
    rows = chat.detail(cid)['messages']
    assert rows[-1]['content'] == '已停止本轮回复'
    assert rows[-1]['meta'] == {'kind': 'stopped'}
    assert await chat.stop(cid) == {'ok': True, 'stopped': False, 'chat_id': cid}
    assert chat.detail(cid)['messages'] == rows
    assert not chat._tasks_by_chat and not chat._stopped_chats


@pytest.mark.asyncio
async def test_cancel_all_cleanup_cannot_release_new_round(env):
    svc, chat, cid = env
    svc.models.gate = asyncio.Event()
    await chat.send(cid, 'old')
    await asyncio.sleep(0)
    chat.cancel_all()
    await chat.send(cid, 'new')
    await asyncio.sleep(0)
    assert chat.is_busy(cid)
    svc.models.gate.set()
    assert await chat.wait_idle(cid)


@pytest.mark.asyncio
async def test_stop_keeps_new_round_busy(env, monkeypatch):
    svc, chat, cid = env
    svc.models.gate = asyncio.Event()
    await chat.send(cid, 'old')
    await asyncio.sleep(0)
    original_gather = asyncio.gather
    async def gather_then_restart(*args, **kwargs):
        result = await original_gather(*args, **kwargs)
        await chat.send(cid, 'new')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, 'gather', gather_then_restart)
        await chat.stop(cid)
    assert chat.is_busy(cid)
    svc.models.gate.set()
    assert await chat.wait_idle(cid)


@pytest.mark.asyncio
async def test_stop_during_tool_replays_pairs(env, monkeypatch):
    svc, chat, cid = env
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(chat, '_run_call', blocked)
    svc.models.script = [_res(tool_calls=[_call('stub_read', {}, 'first'), _call('stub_read', {}, 'second')])]
    await chat.send(cid, 'tools')
    await asyncio.wait_for(entered.wait(), 2)
    await chat.stop(cid)
    await chat.send(cid, 'continue')
    assert await chat.wait_idle(cid)
    messages = svc.models.calls[-1][1]
    block = next(i for i, m in enumerate(messages) if m.get('tool_calls'))
    assert [m.get('tool_call_id') for m in messages[block+1:block+3]] == ['first', 'second']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['stopped', 'turn_error'])
async def test_retry_removes_only_trailing_failure_notes_without_duplicate_user(env, kind):
    svc, chat, cid = env
    chat._add_msg(cid, 'user', 'original')
    chat._add_msg(cid, 'assistant', 'partial')
    chat._add_msg(cid, 'system_note', 'failed', meta={'kind': kind})
    chat._add_msg(cid, 'system_note', 'failed again', meta={'kind': kind})
    await chat.retry(cid)
    assert await chat.wait_idle(cid)
    rows = chat.detail(cid)['messages']
    assert [m['content'] for m in rows if m['role'] == 'user'] == ['original']
    assert not any(m['meta'].get('kind') == kind for m in rows)
    assert rows[-1]['role'] == 'assistant'


@pytest.mark.asyncio
async def test_retry_rejects_busy_empty_and_pending(env):
    svc, chat, cid = env
    with pytest.raises(ValueError):
        await chat.retry(cid)
    chat._add_msg(cid, 'user', 'hello')
    chat._begin(cid)
    with pytest.raises(ChatBusy):
        await chat.retry(cid)
    chat._end(cid)
    with svc.store.tx() as conn:
        conn.execute("INSERT INTO admin_chat_pending(chat_id, tool, args, summary, status, created) VALUES (?, 'x', '{}', 'confirm', 'pending', 1)", (cid,))
    with pytest.raises(ValueError):
        await chat.retry(cid)


def test_explicit_empty_message_meta_is_json_encoded(env):
    svc, chat, cid = env
    mid = chat._add_msg(cid, 'system_note', 'note', meta={})
    assert svc.store.read().execute('SELECT meta FROM admin_chat_msgs WHERE id=?', (mid,)).fetchone()['meta'] == '{}'


def test_detail_budget_status(env):
    svc, chat, cid = env
    svc.models.settings = lambda: SimpleNamespace(main='active-main', ready=lambda: True)
    status = chat.detail(cid)['status']
    assert status['model'] == 'active-main' and status['ready'] is True
    for key in ('estimated_input_tokens', 'trigger_threshold', 'context_window', 'output_reserve', 'summary_covers'):
        assert type(status[key]) is int
    assert status['has_uncompacted'] is False
    chat._add_msg(cid, 'user', 'hello')
    assert chat.detail(cid)['status']['has_uncompacted'] is True


@pytest.mark.asyncio
async def test_compact_focus_masked_and_bounded(env, monkeypatch):
    svc, chat, cid = env
    chat._add_msg(cid, 'user', 'hello')
    seen = []
    async def summarize(*args, **kw):
        seen.append(kw['focus'])
        return SimpleNamespace(text='summary', coverage=None)
    monkeypatch.setattr(compaction, 'summarize_messages_ex', summarize)
    focus = SECRET + ' focus ' + 'x' * 200
    await chat.compact(cid, focus=focus)
    masked = chat._mask(focus).strip()[:120]
    assert masked in seen[0] and SECRET not in seen[0]


def seed_skill(svc, name='demo', body=None):
    return skills_web.create(svc.get_settings().data_dir, svc.store, {
        'name': name, 'description': 'useful', 'roles': ['worker'],
        'body': body or ('unique_skill_body ' + SECRET + 'z' * 35000),
    })


def test_available_skills_enabled_and_group_boundaries(env):
    svc, chat, cid = env
    seed_skill(svc)
    seed_skill(svc, 'disabled')
    skills_web.toggle(svc.get_settings().data_dir, svc.store, 'disabled', False)
    calls = []
    def skills(gid, kind, *, include_archived):
        calls.append((gid, kind, include_archived))
        return [{'id': 1, 'name': '经验', 'description': 'group', 'body': 'work'}]
    svc.agents = SimpleNamespace(skills=skills)
    items = chat.available_skills(G1)
    demo = next(s for s in items if s['name'] == 'demo')
    assert demo['scope'] == 'global' and demo['title'] == 'demo'
    assert all(k in demo for k in ('description', 'roles', 'source', 'allowed_tools'))
    assert not any(s['name'] == 'disabled' or s['name'].startswith('search-') for s in items)
    group = next(s for s in items if s['scope'] == 'group')
    assert group['name'] == '本群/经验' and group['raw_name'] == '经验' and group['group_id'] == G1
    assert calls == [(G1, 'task', False)]
    with pytest.raises(ValueError):
        chat.available_skills('unserved')


@pytest.mark.asyncio
async def test_skill_injection_latest_only_and_clean_storage(env):
    svc, chat, cid = env
    seed_skill(svc)
    await chat.send(cid, '/demo first')
    assert await chat.wait_idle(cid)
    first = chat.detail(cid)['messages'][0]
    assert first['content'] == 'first'
    assert first['meta']['skill'] == {'name': 'demo', 'scope': 'global', 'description': 'useful'}
    payload = svc.models.calls[-1][1][-1]['content']
    assert 'unique_skill_body' in payload and SECRET not in payload
    await chat.send(cid, 'next')
    assert await chat.wait_idle(cid)
    history = svc.models.calls[-1][1]
    assert '[曾选用技能：demo]' in history[1]['content']
    assert 'unique_skill_body' not in str(history)
    await chat.send(cid, '', skill='demo')
    assert await chat.wait_idle(cid)
    assert [m for m in chat.detail(cid)['messages'] if m['role'] == 'user'][-1]['content'] == '请按所选技能处理'


@pytest.mark.asyncio
async def test_group_skill_boundary_and_invalid_selection(env):
    svc, chat, cid = env
    svc.agents = SimpleNamespace(skills=lambda *a, **k: [{'name': '经验', 'description': 'd', 'body': 'group-secret ' + SECRET}])
    await chat.send(cid, 'work', skill='本群/经验')
    assert await chat.wait_idle(cid)
    payload = svc.models.calls[-1][1][-1]['content']
    assert '只读' in payload and '确认' in payload and 'task_manage' in payload and SECRET not in payload
    for target, skill in [(cid, 'missing'), (chat.create()['id'], '本群/经验')]:
        with pytest.raises(ValueError):
            await chat.send(target, 'work', skill=skill)


def test_refs_served_sorted_masked_and_limited(env):
    svc, chat, cid = env
    with svc.store.tx() as conn:
        conn.execute("INSERT INTO tasks(id, group_id, workspace, title, status, created, updated) VALUES ('T-1', ?, 'test', ?, 'queued', 1, 3)", (G1, 'task ' + SECRET))
        conn.execute("INSERT INTO goals(id, group_id, kind, title, state, created, updated) VALUES ('G-1', ?, 'agent', 'goal', 'active', 1, 4)", (G2,))
        conn.execute("INSERT INTO ideas(group_id, title, state, created, updated) VALUES (?, 'idea', 'new', 1, 5)", (G1,))
        conn.execute("INSERT INTO ideas(group_id, title, state, created, updated) VALUES ('unserved', 'hidden', 'new', 1, 6)")
    refs = chat.available_refs()
    assert [r['kind'] for r in refs] == ['idea', 'goal', 'task']
    assert refs[0]['id'] == f"构想#{refs[0]['raw_id']}"
    assert SECRET not in str(refs)
    assert len(chat.available_refs(G1, limit=1)) == 1
    assert all(r['group_id'] == G1 for r in chat.available_refs(G1))
    with pytest.raises(ValueError):
        chat.available_refs('unserved')


@pytest.mark.asyncio
@pytest.mark.parametrize('redacted', [False, True])
async def test_reasoning_and_tool_arguments_masked_bounded(env, redacted):
    svc, chat, cid = env
    result = _res('done', [_call('stub_read', {'query': SECRET + 'x' * 500})])
    result.reasoning = SECRET + 'thought' * 1000 if not redacted else ''
    result.reasoning_redacted = redacted
    svc.models.script = [result, _res('finished')]
    await chat.send(cid, 'think')
    assert await chat.wait_idle(cid)
    rows = chat.detail(cid)['messages']
    meta = next(m['meta'] for m in rows if m['role'] == 'assistant')
    assert meta['reasoning_redacted'] is redacted
    assert len(meta['reasoning']) <= 4000 and SECRET not in meta['reasoning']
    tool = next(m['meta'] for m in rows if m['role'] == 'tool')
    assert len(tool['args']) <= 200 and SECRET not in tool['args'] and tool['ok'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ModelError('broken'), RuntimeError('broken')])
async def test_model_errors_mark_retryable(env, error):
    svc, chat, cid = env
    svc.models.script = [error]
    await chat.send(cid, 'hello')
    assert await chat.wait_idle(cid)
    assert chat.detail(cid)['messages'][-1]['meta']['kind'] == 'turn_error'
