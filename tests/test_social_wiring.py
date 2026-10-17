"""Cross-layer social wiring regressions; no external CLI/network calls."""
from types import SimpleNamespace

import pytest

from maiwork import social_platforms as sp
from maiwork.feeds import Feeds
from maiwork.store import Store
from maiwork.tools import Tools, ToolContext
from maiwork.tools_builtin import register_builtin
from maiwork.skills_tools import register_skill_tools
from maiwork.onboarding import _checks


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / 'test.sqlite3')
    db.migrate()
    yield db
    db.close()


@pytest.mark.asyncio
async def test_social_tool_flattens_platform_mapping_and_handles_iso_date(store, monkeypatch):
    async def search(*args, **kwargs):
        kwargs['errors']['twitter'] = '登录态已失效'
        return {'bilibili': [{'title': 'video', 'url': 'https://bilibili.com/video/1', 'published': '2026-10-16T00:00:00Z', 'snippet': 'candidate'}]}
    monkeypatch.setattr(sp, 'search_all', search)
    tools = Tools(store)
    register_builtin(tools, search=None, profiles=None, store=store)
    result = await tools.call('social_search', {'query': 'query'}, ToolContext('g', role='worker'))
    assert result.ok, result.error
    assert result.data[0]['platform'] == 'bilibili'
    assert 'video' in result.output and 'twitter' in result.output
    assert 'fetched' not in result.data[0]


@pytest.mark.asyncio
async def test_social_tool_safe_failure_and_input_validation(store, monkeypatch):
    calls = []
    async def search(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('auth_token=secret-value')
    monkeypatch.setattr(sp, 'search_all', search)
    tools = Tools(store)
    register_builtin(tools, search=None, profiles=None, store=store)
    ctx = ToolContext('g', role='worker')
    result = await tools.call('social_search', {'query': 'query'}, ctx)
    assert not result.ok and 'secret-value' not in result.error
    for args in ({'query': 'x' * 201}, {'query': 'a', 'platforms': ['unknown']}, {'query': 'a', 'platforms': [1]}):
        result = await tools.call('social_search', args, ctx)
        assert not result.ok
    assert len(calls) == 1


class FakeSkills:
    def list(self, role):
        return [{'name': name, 'description': name} for name in ('social-login-helper', 'worker-only', 'main-only') if role in self.roles(name)]
    def roles(self, name):
        return {'social-login-helper': ('main',), 'worker-only': ('worker',), 'main-only': ('main',)}.get(name)
    def read(self, name):
        return 'helper instructions'


@pytest.mark.asyncio
async def test_admin_skill_access_is_narrow_and_group_skills_inaccessible(store):
    tools = Tools(store)
    register_skill_tools(tools, FakeSkills())
    group_calls = []
    def group_skills(*args, **kwargs):
        group_calls.append(args)
        return [{'name': 'private', 'description': 'private'}]
    tools._agents = SimpleNamespace(skills=group_skills)
    ctx = ToolContext('g', role='admin')
    result = await tools.call('list_skills', {}, ctx)
    assert result.ok
    assert [i['name'] for i in result.data] == ['social-login-helper']
    assert (await tools.call('read_skill', {'name': 'social-login-helper'}, ctx)).ok
    for name in ('worker-only', 'main-only', '本群/private'):
        assert not (await tools.call('read_skill', {'name': name}, ctx)).ok
    assert not group_calls


@pytest.mark.asyncio
async def test_social_floor_bounded_isolated_and_counted(store, monkeypatch):
    calls = []
    monkeypatch.setattr(sp, 'list_platforms', lambda db: [{'id': 'bilibili', 'enabled': True, 'installed': True, 'needs_cookie': False}])
    async def search(db, query, **kwargs):
        calls.append(query)
        if query == 'q0':
            raise RuntimeError('private-cookie')
        return {'bilibili': [{'title': query, 'url': f'https://bilibili.com/{query}', 'snippet': 'candidate'}]}
    monkeypatch.setattr(sp, 'search_all', search)
    feeds = object.__new__(Feeds)
    feeds._store, feeds._search = store, None
    funnel = {}
    added = await feeds._floor_searches('g', [{'query': f'q{i}'} for i in range(10)], [], funnel)
    assert 1 < len(calls) <= 3
    assert len(added) == len(calls) - 1
    assert funnel['queries'] == len(calls)
    assert funnel['providers']['social:bilibili'] == len(added)
    assert sum(i['cands'] for i in funnel['per_focus']) == len(added)
    assert all('fetched' not in i and 'quote' not in i for i in added)


@pytest.mark.asyncio
async def test_social_only_can_pass_search_gate(store, monkeypatch):
    feeds = object.__new__(Feeds)
    feeds._store = store
    feeds._search = SimpleNamespace(available=lambda: False, status=lambda: ('', 'no web'))
    monkeypatch.setattr(sp, 'list_platforms', lambda db: [{'enabled': True, 'installed': True, 'needs_cookie': False}])
    await feeds._ensure_search()


def test_onboarding_empty_platform_registry_is_not_ready(store, monkeypatch):
    monkeypatch.setattr(sp, 'list_platforms', lambda db: [{'enabled': False}])
    svc = SimpleNamespace(store=store, get_settings=lambda: None)
    assert not _checks(svc)['social']


@pytest.mark.asyncio
async def test_social_tool_empty_platforms_and_delayed_partial_failure(store, monkeypatch):
    import asyncio
    async def search(*args, **kwargs):
        assert kwargs['platforms'] is None
        await asyncio.sleep(0.01)
        kwargs['errors']['reddit'] = 'raw-private-cookie'
        return {'youtube': [{'title': 'kept', 'url': 'https://www.youtube.com/watch?v=sample'}]}
    monkeypatch.setattr(sp, 'search_all', search)
    tools = Tools(store)
    register_builtin(tools, search=None, profiles=None, store=store)
    assert tools._tools['social_search'].timeout_s > sp.SEARCH_TIMEOUT_S
    result = await tools.call('social_search', {'query': 'query', 'platforms': []}, ToolContext('g', role='worker'))
    assert result.ok and result.data[0]['title'] == 'kept'
    assert 'reddit' in result.output and 'raw-private-cookie' not in result.output


@pytest.mark.asyncio
async def test_worker_injects_broad_search_for_enabled_twitter(store):
    sp.enable_platform(store, 'twitter')
    calls = []
    async def broad(query, *, site, limit):
        calls.append(site)
        return [{'title': 'web candidate', 'url': 'https://x.com/a/status/1979000000000000000', 'provider': 'engine'}]
    tools = Tools(store)
    register_builtin(tools, search=SimpleNamespace(search_broad=broad), profiles=None, store=store)
    result = await tools.call('social_search', {'query': 'topic'}, ToolContext('g', role='worker'))
    assert result.ok, result.error
    assert sorted(calls) == ['twitter.com', 'x.com']
    assert result.data[0]['provider'] == 'social:twitter-web:engine'
    assert 'fetched' not in result.data[0]


@pytest.mark.asyncio
async def test_feeds_web_only_twitter_ready_and_provider_preserved(store):
    sp.enable_platform(store, 'twitter')
    async def broad(query, *, site, limit):
        return [{'title': 'web candidate', 'url': 'https://x.com/a/status/1979000000000000000', 'provider': 'engine'}]
    feeds = object.__new__(Feeds)
    feeds._store = store
    feeds._search = SimpleNamespace(search_broad=broad, available=lambda: True)
    assert feeds._social_search_ready()
    funnel = {}
    added = await feeds._floor_searches('g', [{'query': 'topic'}], [], funnel)
    assert added[0]['provider'] == 'social:twitter-web:engine'
    assert funnel['providers']['social:twitter-web:engine'] == 1
    assert 'fetched' not in added[0] and 'quote' not in added[0]


@pytest.mark.asyncio
async def test_social_tool_respects_shared_personal_search_budget(store, monkeypatch):
    from maiwork import verify_budget
    marks = []
    def consume(mark):
        marks.append(mark)
        return False, 'search budget exhausted'
    monkeypatch.setattr(verify_budget, 'consume_search', consume)
    async def search(*args, **kwargs):
        raise AssertionError('budget must block the actual search')
    monkeypatch.setattr(sp, 'search_all', search)
    tools = Tools(store)
    register_builtin(tools, search=None, profiles=None, store=store)
    result = await tools.call('social_search', {'query': 'query'}, ToolContext('g', task_id='personal-collect:test', role='worker'))
    assert not result.ok and result.error == 'search budget exhausted'
    assert marks == ['personal-collect:test']
