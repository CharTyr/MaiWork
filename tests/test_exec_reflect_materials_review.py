"""通用执行复盘：可信的实际用工具、复杂度、8条预算与真实验收材料。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import clock, lessons
from CharTyr_MaiWork.maiwork.agents import Agents
from CharTyr_MaiWork.maiwork.coordinator import Coordinator
from CharTyr_MaiWork.maiwork.specialists import Specialists
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolResult, Tools
from CharTyr_MaiWork.maiwork.workers import Workers, WorkerReport
from test_workers import FakeChatResult, ReplayModels, _tool_call

G, OTHER = '111', '222'
NOW = 1_790_000_000.0
MISSING = object()
ACTUAL = ['read_file', 'write_file', 'list_files']


class ReflectModels:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(text='{"pass":"没有新做法"}')

    def prompt(self):
        return self.calls[0]['messages'][0]['content']


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, 'now', lambda: NOW)
    store = Store(tmp_path / 'test.db')
    store.migrate()
    settings = SimpleNamespace(served_groups=(G, OTHER), model_list=(), is_served=lambda gid: str(gid) in (G, OTHER))
    agents = Agents(store, lambda: settings)
    agents._ensure_schema()
    yield store, agents
    store.close()


def finish(env, *, status='accepted', used=MISSING, gid=G, brief='要求-marker', updated=NOW, allowed=None):
    store, agents = env
    hid = agents.begin(gid, 'task', brief, tools=allowed or ACTUAL)
    agents.running(gid, hid)
    agents.returned(gid, hid, '交回-marker', ok=status != 'failed', error='失败-marker' if status == 'failed' else '')
    if status != 'failed':
        agents.review(gid, hid, status == 'accepted', '验收-marker', learn=False)
    row = agents.handoff(gid, hid)
    review = json.loads(row['review'] or '{}')
    if used is not MISSING:
        review['used_tools'] = used
    with store.tx() as conn:
        conn.execute('UPDATE agent_handoffs SET review=?,updated=? WHERE id=?', (json.dumps(review), updated, hid))
    return hid


def reflect(env, models=None, *, now=NOW):
    store, agents = env
    models = models or ReflectModels()
    asyncio.run(lessons._run_exec_kind(store, models, agents, G, now, scrub=None))
    return models


@pytest.mark.parametrize('used', [[], ['read_file'], ['read_file', 'write_file']])
def test_simple_accepted_does_not_call_model(env, used):
    finish(env, used=used, allowed=ACTUAL + ['run_command'])
    assert reflect(env).calls == []


@pytest.mark.parametrize('used', [MISSING, None, {}, ['a', 'b', 3], ['a', 'b', 'bad/tool'], ['a', 'a', 'a'], ['a', 'b', 'submit_result']])
def test_unknown_or_invalid_telemetry_is_not_complex(env, used):
    finish(env, used=used)
    assert reflect(env).calls == []


def test_three_actual_tools_reflect_and_include_bounded_materials(env):
    finish(env, used=ACTUAL, allowed=['unused_whitelist_tool'])
    models = reflect(env)
    assert len(models.calls) == 1
    text = models.prompt()
    assert all(name in text for name in ACTUAL)
    assert 'unused_whitelist_tool' not in text
    assert '要求-marker' in text and '交回-marker' in text and '验收-marker' in text


@pytest.mark.parametrize('status', ['rejected', 'failed'])
@pytest.mark.parametrize('used', [MISSING, [], ['read_file'], ['read_file', 'write_file']])
def test_failure_or_rejection_still_reflects_without_three_tools(env, status, used):
    finish(env, status=status, used=used)
    models = reflect(env)
    assert len(models.calls) == 1
    assert '要求-marker' in models.prompt() and '交回-marker' in models.prompt()
    if used is not MISSING:
        assert all(name in models.prompt() for name in used)


def test_eight_signal_budget_is_after_eligibility(env):
    for i in range(9):
        finish(env, status='rejected', used=[], brief=f'eligible-{i}', updated=NOW-i)
    text = reflect(env).prompt()
    assert sum(f'eligible-{i}' in text for i in range(9)) == 8
    assert 'eligible-8' not in text


def test_newer_simple_handoffs_do_not_hide_older_eligible(env):
    finish(env, status='rejected', brief='eligible-older', updated=NOW-100)
    for i in range(40):
        finish(env, used=['read_file'], brief=f'simple-new-{i}', updated=NOW-i)
    text = reflect(env).prompt()
    assert 'eligible-older' in text
    assert 'simple-new-' not in text


def test_group_and_since_isolation(env):
    store, _ = env
    last = NOW - lessons.REFLECT_MIN_GAP_S - 10
    finish(env, used=ACTUAL, gid=OTHER)
    finish(env, used=ACTUAL, updated=last-1)
    with store.tx() as conn:
        store.kv_set(conn, f'lessons.state.{G}.task', {'last_reflect': last})
    assert reflect(env).calls == []
    assert store.kv_get(f'lessons.state.{G}.task')['last_reflect'] == last


def test_no_new_signal_and_twenty_hour_gate_remain(env):
    store, _ = env
    assert reflect(env).calls == []
    finish(env, used=ACTUAL)
    with store.tx() as conn:
        store.kv_set(conn, f'lessons.state.{G}.task', {'last_reflect': NOW-1})
    assert reflect(env).calls == []


def test_model_failure_does_not_advance_progress(env):
    store, _ = env
    finish(env, used=ACTUAL)
    reflect(env, ReflectModels(RuntimeError('fake failure')))
    state = store.kv_get(f'lessons.state.{G}.task')
    assert not state.get('last_reflect')
    assert state.get('last_attempt') == NOW


def test_specialist_signal_budget_stays_thirty(env):
    store, agents = env
    for i in range(31):
        hid = agents.begin(G, 'news', f'news-signal-{i}')
        agents.running(G, hid)
        agents.returned(G, hid, '交回')
        agents.review(G, hid, False, '需要更好', learn=False)
    assert len(lessons._collect_handoff_signals(store, G, 'news', NOW-1)) == 30


def build_specialists(env, tool_calls):
    store, agents = env
    tools = Tools(store)
    executed = []
    async def handler(ctx, args):
        executed.append(args.get('tag', ''))
        return ToolResult(ok=True, output='fake')
    async def fail_handler(ctx, args):
        raise RuntimeError('handler really ran')
    async def submit(ctx, args):
        return ToolResult(ok=True, output='', data={'summary':'成果真实-marker','data':args.get('data'),'evidence':[]})
    for name in ACTUAL + ['unused_whitelist_tool', 'must_have_arg', 'fails_after_dispatch']:
        tools.register(Tool(name=name, description='fake', parameters={'type':'object','properties':{},'required':['required'] if name == 'must_have_arg' else []}, roles=frozenset({'worker'}), handler=fail_handler if name == 'fails_after_dispatch' else handler, timeout_s=5))
    tools.register(Tool(name='submit_result', description='fake', parameters={'type':'object','properties':{},'required':[]}, roles=frozenset({'worker'}), handler=submit, timeout_s=5))
    models = ReplayModels([FakeChatResult(tool_calls=tool_calls)])
    sp = Specialists(agents, Workers(models, tools), SimpleNamespace(list=lambda: []))
    return sp, executed


def test_real_producer_records_only_dispatched_names_not_whitelist_or_payload(env):
    calls = [_tool_call(name, {}) for name in ['read_file','read_file','must_have_arg','not_registered','not_allowed','fails_after_dispatch','write_file']]
    calls.append(_tool_call('submit_result', {'data':{'used_tools':ACTUAL + ['forged']}}))
    sp, _ = build_specialists(env, calls)
    report = asyncio.run(sp.run('task', '真实要求-marker', group_id=G, tools=ACTUAL+['unused_whitelist_tool','must_have_arg','fails_after_dispatch'], task_id='T-1'))
    assert report.ok
    sp.review(G, report, True, '真实验收-marker', learn=False)
    handoff = sp.agents.handoff(G, report.handoff_id)
    data = json.loads(handoff['review'])
    assert set(data['used_tools']) == {'read_file','write_file','fails_after_dispatch'}
    assert json.loads(handoff['data']) == {'used_tools': ACTUAL + ['forged']}
    text = reflect(env).prompt()
    assert '真实验收-marker' in text and '成果真实-marker' in text
    assert 'fails_after_dispatch' in text and 'unused_whitelist_tool' not in text and 'forged' not in text


def test_coordinator_preserves_actual_acceptance_feedback(env):
    store, agents = env
    hid = agents.begin(G, 'task', '要求')
    agents.running(G, hid)
    agents.returned(G, hid, '子agent自述')
    sp = Specialists(agents, None, None)
    coordinator = object.__new__(Coordinator)
    coordinator._specialists = sp
    report = WorkerReport(ok=True, handoff_id=hid, summary='子agent自述')
    coordinator._settle_job_specialist_handoffs(G, [report], accepted=True, why='真正的主模型验收意见')
    assert json.loads(agents.handoff(G, hid)['review'])['summary'] == '真正的主模型验收意见'


@pytest.mark.asyncio
async def test_unexpected_worker_failure_keeps_actual_used_tools(env):
    store, agents = env
    tools = Tools(store)
    async def handler(ctx, args):
        return ToolResult(ok=True, output='已读')
    tools.register(Tool('read_file', 'read', {'type':'object','properties':{}}, frozenset({'worker'}), handler))
    models = ReplayModels([FakeChatResult(tool_calls=[_tool_call('read_file', {})]), RuntimeError('执行回合意外失败')])
    worker = Workers(models, tools)
    sp = Specialists(agents, worker, SimpleNamespace(list=lambda: []))
    report = await sp.run('task', '先读文件再整理', group_id=G, tools=['read_file'])
    row = agents.handoff(G, report.handoff_id)
    assert not report.ok and row['status'] == 'failed'
    assert json.loads(row['review'])['used_tools'] == ['read_file']


def test_scan_budget_does_not_advance_reflection_progress(env, monkeypatch):
    monkeypatch.setattr(lessons, '_EXEC_SIGNAL_SCAN_MAX', 4)
    finish(env, status='failed', brief='窗口后面的合格材料', updated=NOW-100)
    for i in range(5):
        finish(env, used=[], updated=NOW-i)
    store, _ = env
    assert reflect(env).calls == []
    state = store.kv_get(f'lessons.state.{G}.task', {})
    assert not state.get('last_reflect')


def test_explicit_acceptance_requirements_are_material(env):
    _, agents = env
    hid = agents.begin(G, 'task', '任务要求-marker', criteria=['验收标准-marker'])
    agents.running(G, hid)
    agents.returned(G, hid, '成果-marker', used_tools=set(ACTUAL))
    agents.review(G, hid, True, '通过的真实意见-marker', learn=False)
    text = reflect(env).prompt()
    assert '验收标准-marker' in text
    assert '成果-marker' in text and '通过的真实意见-marker' in text


@pytest.mark.asyncio
async def test_many_actual_tools_keep_complexity_and_bounded_names(env):
    store, agents = env
    tools = Tools(store)
    names = [f'operation_{i}' for i in range(30)]
    async def handler(ctx, args):
        return ToolResult(ok=True, output='完成', data={'summary':'完成'})
    for name in [*names, 'submit_result']:
        tools.register(Tool(name, '内存测试操作', {'type':'object','properties':{}}, frozenset({'worker'}), handler))
    models = ReplayModels([FakeChatResult(tool_calls=[_tool_call(name, {}, f'call-{i}') for i,name in enumerate([*names,'submit_result'])])])
    sp = Specialists(agents, Workers(models, tools), SimpleNamespace(list=lambda: []))
    report = await sp.run('task', '很多种真实操作', group_id=G, tools=names)
    assert report.ok
    sp.review(G, report, True, '真实操作验收通过', learn=False)
    row = agents.handoff(G, report.handoff_id)
    used = json.loads(row['review'])['used_tools']
    assert len(used) == 24 and set(used) <= set(names)
    reflection = ReflectModels()
    await lessons._run_exec_kind(store, reflection, agents, G, NOW, scrub=None)
    assert len(reflection.calls) == 1
