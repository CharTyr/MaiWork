"""管理员对话改设置（docs/02 §18）：MCP / 专岗 / 端点与模型库。

用户定的规矩：
- 复用网页接口背后的同一套校验和写入（settings_ops / extensions_web），不另写规则；
- 确认门：新增 MCP、MCP 放宽（改地址 / 加 main / 白名单放宽）、删自定义专岗、新增端点、
  改端点地址或协议、删端点、删模型库条目 → 先写小票不动手，管理员点头后才生效；
  收窄的 MCP 改动、测连接、重连、改专岗模型、建自定义专岗、加 / 改模型库条目 → 直接做；
- 密钥一律不经过对话：参数里带 headers / api_key 之类直接拒，并指路网页设置页；
  读回来的信息也不带密钥。

起的是真 MaiWorkApp（fixture 复用 test_tools_admin_real），只把「真连 MCP」换成假的。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from CharTyr_MaiWork.maiwork import extensions_web, settings_ops, tools_admin
from CharTyr_MaiWork.maiwork.admin_chat import AdminChat

from test_tools_admin_real import G1, _call, app  # noqa: F401  （fixture 复用）


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _bind(app) -> int:  # noqa: F811
    cid = int(AdminChat(app).create(G1)["id"])
    app.admin_pending.bind_chat(cid, 0)
    return cid


async def _ticket(app, name: str, args: dict) -> int:  # noqa: F811
    """调一次，断言它只写了小票（没动手），返回小票号。"""
    _bind(app)
    r = await _call(app, name, args)
    assert r.ok, f"{name} 应该写小票，实际报错：{r.error}"
    assert tools_admin.NEED_CONFIRM in r.output, f"{name} 应该先等确认，实际：{r.output}"
    pid = int(r.data["pending_id"])
    row = app.admin_pending._row(pid)
    assert row is not None and row["tool"] == name and row["status"] == "pending"
    return pid


async def _direct(app, name: str, args: dict):  # noqa: F811
    """调一次，断言它直接做了（没写小票）。"""
    _bind(app)
    before = len(app.admin_pending.pending())
    r = await _call(app, name, args)
    assert r.ok, f"{name} 应该直接做成，实际报错：{r.error}"
    assert tools_admin.NEED_CONFIRM not in (r.output or ""), f"{name} 不该等确认：{r.output}"
    assert len(app.admin_pending.pending()) == before, f"{name} 不该写小票"
    return r


async def _refused(app, name: str, args: dict, *words: str):  # noqa: F811
    """调一次，断言它被拒、没写小票、也没动手；错误里含给定的字。"""
    _bind(app)
    before = len(app.admin_pending.pending())
    r = await _call(app, name, args)
    assert not r.ok, f"{name} 应该被拒，实际：{r.output}"
    assert len(app.admin_pending.pending()) == before, f"{name} 被拒时不该写小票"
    for w in words:
        assert w in r.error, f"{name} 的错误里应有「{w}」：{r.error}"
    return r


@pytest.fixture
def fake_mcp(app, monkeypatch):  # noqa: F811
    """把「真连 MCP」换成记账的假货：reload_mcp / try_connect。"""
    log: dict[str, list] = {"reload": [], "connect": []}

    async def _reload(name: str):
        log["reload"].append(name)
        return {"ok": True, "tools": 3, "error": ""}

    async def _connect(url, headers=None, *, timeout_s=20, transport=None):
        log["connect"].append((url, dict(headers or {})))
        return {"ok": True, "tools": ["search", "fetch"], "error": ""}

    def _resolve(host: str) -> list[str]:
        """假 DNS：example.com 一族是公网，intranet.example.org 解析到内网，别的解析失败。"""
        if host == "intranet.example.org":
            return ["10.1.2.3"]
        if host.endswith("example.com"):
            return ["93.184.216.34"]
        raise OSError("解析不出来")

    from CharTyr_MaiWork.maiwork import tools_builtin

    monkeypatch.setattr(app, "reload_mcp", _reload)
    monkeypatch.setattr(extensions_web, "try_connect", _connect)
    monkeypatch.setattr(tools_builtin, "_default_resolver", _resolve)
    return log


def _seed_mcp(app, **over) -> None:  # noqa: F811
    body = {"name": "exa", "url": "https://mcp.example.com/mcp", "tools": ["search", "fetch"],
            "roles": ["worker", "main"], "enabled": True}
    body.update(over)
    extensions_web.create(app.store, app.get_settings(), body)


def _mcp(app, name: str = "exa") -> dict | None:  # noqa: F811
    return extensions_web.web_entry(app.store, name)


# ----------------------------------------------------------------------
# 一、MCP
# ----------------------------------------------------------------------


class TestMcpAdd:
    @pytest.mark.asyncio
    async def test_add_needs_confirm_then_creates(self, app, fake_mcp) -> None:  # noqa: F811
        args = {"name": "exa", "url": "https://mcp.example.com/mcp", "tools": ["search"], "roles": ["worker"]}
        pid = await _ticket(app, "mcp_manage", {"action": "add", **args})
        assert _mcp(app) is None, "没点头前不能落库"
        assert fake_mcp["reload"] == [], "没点头前不能连"
        r = await app.admin_pending.execute(pid, True)
        assert r.ok, r.error
        got = _mcp(app)
        assert got is not None and got["url"] == "https://mcp.example.com/mcp"
        assert got["tools"] == ["search"] and got["roles"] == ["worker"]
        assert fake_mcp["reload"] == ["exa"], "加完要立刻连一次（同网页）"
        # 要密钥的 MCP：指路网页自己填
        assert "设置 → 工具" in r.output

    @pytest.mark.asyncio
    async def test_add_same_validation_as_web(self, app, fake_mcp) -> None:  # noqa: F811
        """http 地址、坏名字：网页接口那套校验当场拒，不写票。"""
        await _refused(app, "mcp_manage", {"action": "add", "name": "exa", "url": "http://mcp.example.com/mcp"}, "https://")
        await _refused(app, "mcp_manage", {"action": "add", "name": "坏 名字", "url": "https://mcp.example.com/mcp"}, "名字不合法")
        _seed_mcp(app)
        await _refused(app, "mcp_manage", {"action": "add", "name": "exa", "url": "https://other.example.com/mcp"}, "重名")

    @pytest.mark.asyncio
    async def test_add_rejects_secrets(self, app, fake_mcp) -> None:  # noqa: F811
        for extra in ({"headers": {"Authorization": "Bearer sk-x"}}, {"api_key": "sk-x"}, {"token": "x"}):
            r = await _refused(
                app, "mcp_manage", {"action": "add", "name": "exa", "url": "https://mcp.example.com/mcp", **extra}, "密钥", "设置 → 工具",
            )
            assert "sk-x" not in r.error
        # 地址里夹查询串（很多服务把 key 放这里）也算密钥进了对话
        await _refused(app, "mcp_manage", {"action": "add", "name": "exa", "url": "https://mcp.example.com/mcp?apiKey=abc"}, "密钥")
        assert _mcp(app) is None


class TestMcpUpdate:
    @pytest.mark.asyncio
    async def test_url_change_needs_confirm(self, app, fake_mcp) -> None:  # noqa: F811
        _seed_mcp(app)
        pid = await _ticket(app, "mcp_manage", {"action": "update", "name": "exa", "url": "https://new.example.com/mcp"})
        assert _mcp(app)["url"] == "https://mcp.example.com/mcp", "没点头前地址不能变"
        r = await app.admin_pending.execute(pid, True)
        assert r.ok, r.error
        assert _mcp(app)["url"] == "https://new.example.com/mcp"
        assert fake_mcp["reload"] == ["exa"]

    @pytest.mark.asyncio
    async def test_add_main_role_needs_confirm(self, app, fake_mcp) -> None:  # noqa: F811
        _seed_mcp(app, roles=["worker"])
        pid = await _ticket(app, "mcp_manage", {"action": "update", "name": "exa", "roles": ["worker", "main"]})
        assert _mcp(app)["roles"] == ["worker"]
        assert (await app.admin_pending.execute(pid, True)).ok
        assert _mcp(app)["roles"] == ["main", "worker"]

    @pytest.mark.asyncio
    async def test_widen_whitelist_needs_confirm(self, app, fake_mcp) -> None:  # noqa: F811
        _seed_mcp(app)
        await _ticket(app, "mcp_manage", {"action": "update", "name": "exa", "tools": ["search", "fetch", "crawl"]})
        assert _mcp(app)["tools"] == ["search", "fetch"]
        # 从「有白名单」变成「不限」也是放宽
        pid = await _ticket(app, "mcp_manage", {"action": "update", "name": "exa", "tools": []})
        assert _mcp(app)["tools"] == ["search", "fetch"]
        assert (await app.admin_pending.execute(pid, True)).ok
        assert _mcp(app)["tools"] == []

    @pytest.mark.asyncio
    async def test_narrowing_is_direct(self, app, fake_mcp) -> None:  # noqa: F811
        _seed_mcp(app)
        await _direct(app, "mcp_manage", {"action": "update", "name": "exa", "tools": ["search"]})
        assert _mcp(app)["tools"] == ["search"]
        await _direct(app, "mcp_manage", {"action": "update", "name": "exa", "roles": ["worker"]})
        assert _mcp(app)["roles"] == ["worker"]
        await _direct(app, "mcp_manage", {"action": "update", "name": "exa", "enabled": False})
        assert _mcp(app)["enabled"] is False
        assert fake_mcp["reload"] == ["exa", "exa", "exa"], "改完立刻重连（同网页）"

    @pytest.mark.asyncio
    async def test_unlimited_to_list_is_narrowing(self, app, fake_mcp) -> None:  # noqa: F811
        _seed_mcp(app, tools=[])
        await _direct(app, "mcp_manage", {"action": "update", "name": "exa", "tools": ["search"]})
        assert _mcp(app)["tools"] == ["search"]

    @pytest.mark.asyncio
    async def test_update_rejects_secrets_and_bad(self, app, fake_mcp) -> None:  # noqa: F811
        _seed_mcp(app)
        await _refused(app, "mcp_manage", {"action": "update", "name": "exa", "headers": {"X-Key": "abc"}}, "密钥", "设置 → 工具")
        await _refused(app, "mcp_manage", {"action": "update", "name": "exa", "remove_headers": ["X-Key"]}, "密钥")
        await _refused(app, "mcp_manage", {"action": "update", "name": "exa", "url": "http://x.example.com"}, "https://")
        await _refused(app, "mcp_manage", {"action": "update", "name": "nope", "enabled": False}, "没有")
        assert fake_mcp["reload"] == []


@pytest.mark.asyncio
async def test_mcp_delete_clears_search_binding_like_web(app, fake_mcp) -> None:  # noqa: F811
    """网页删 MCP 会顺手清掉绑着它的搜索绑定；对话里删也一样（同一套收尾）。"""
    from CharTyr_MaiWork.maiwork import search_binding

    _seed_mcp(app)
    search_binding.set_binding(app.store, {"mcp": "exa", "tool": "search"})
    pid = await _ticket(app, "mcp_manage", {"action": "delete", "name": "exa"})
    assert (await app.admin_pending.execute(pid, True)).ok
    assert _mcp(app) is None
    assert search_binding.get_binding(app.store) is None


class TestMcpTestReload:
    @pytest.mark.asyncio
    async def test_test_by_url_is_direct_and_lists_tools(self, app, fake_mcp) -> None:  # noqa: F811
        r = await _direct(app, "mcp_manage", {"action": "test", "url": "https://try.example.com/mcp"})
        assert "search" in r.output and "fetch" in r.output
        assert fake_mcp["connect"] == [("https://try.example.com/mcp", {})], "试新地址不带任何已存的头"

    @pytest.mark.asyncio
    async def test_test_by_url_refuses_internal(self, app, fake_mcp) -> None:  # noqa: F811
        """只给地址时挡内网 / 本机（防群友诱导主模型去探内网）；和 fetch_page 同一套检查。"""
        for url in (
            "https://127.0.0.1/mcp", "https://10.0.0.8/mcp", "https://172.16.0.1/mcp",
            "https://192.168.1.2/mcp", "https://169.254.169.254/mcp", "https://[::1]/mcp",
            "https://[fc00::1]/mcp", "https://localhost/mcp", "https://intranet.example.org/mcp",
            "https://no-such-host.invalid/mcp",
        ):
            await _refused(app, "mcp_manage", {"action": "test", "url": url}, "内网")
        assert fake_mcp["connect"] == [], "内网地址一次都不能连"

    @pytest.mark.asyncio
    async def test_test_by_name_uses_stored(self, app, fake_mcp) -> None:  # noqa: F811
        extensions_web.create(app.store, app.get_settings(), {
            "name": "exa", "url": "https://mcp.example.com/mcp", "headers": {"X-Key": "secret-value-1"},
        })
        r = await _direct(app, "mcp_manage", {"action": "test", "name": "exa"})
        assert fake_mcp["connect"] == [("https://mcp.example.com/mcp", {"X-Key": "secret-value-1"})]
        assert "secret-value-1" not in r.output and "secret-value-1" not in str(r.data)
        # 已存的头不能被带去别的地址
        await _refused(app, "mcp_manage", {"action": "test", "name": "exa", "url": "https://evil.example.com/mcp"}, "地址")
        assert len(fake_mcp["connect"]) == 1

    @pytest.mark.asyncio
    async def test_test_rejects_secrets(self, app, fake_mcp) -> None:  # noqa: F811
        await _refused(app, "mcp_manage", {"action": "test", "url": "https://try.example.com/mcp", "headers": {"A": "b"}}, "密钥")
        assert fake_mcp["connect"] == []

    @pytest.mark.asyncio
    async def test_reload_is_direct(self, app, fake_mcp) -> None:  # noqa: F811
        _seed_mcp(app)
        r = await _direct(app, "mcp_manage", {"action": "reload", "name": "exa"})
        assert fake_mcp["reload"] == ["exa"] and "3" in r.output


# ----------------------------------------------------------------------
# 二、专岗
# ----------------------------------------------------------------------


class TestAgents:
    @pytest.mark.asyncio
    async def test_update_model_effort_enabled_direct(self, app) -> None:  # noqa: F811
        await _direct(app, "agent_manage", {"action": "update", "kind": "news", "model": "m1", "backup": "m2", "escalate": "m1"})
        p = app.agents.profile("news")
        assert (p["model"], p["backup"], p["escalate"]) == ("m1", "m2", "m1")
        await _direct(app, "agent_manage", {"action": "update", "kind": "idea", "enabled": False})
        assert app.agents.profile("idea")["enabled"] is False

    @pytest.mark.asyncio
    async def test_update_same_validation_as_web(self, app) -> None:  # noqa: F811
        """模型库里没有的 id、主模型岗的「做不动时换用」：和 PUT /api/agents/{kind} 同一句话。"""
        for kind, patch in (("news", {"model": "no-such"}), ("main", {"escalate": "m1"})):
            with pytest.raises(ValueError) as web_err:
                app.agents.update_profile(kind, dict(patch))
            await _refused(app, "agent_manage", {"action": "update", "kind": kind, **patch}, str(web_err.value))
        # 只开放模型三件套 + 做不动时换用 + 启停；别的字段（人设之类）去网页改
        await _refused(app, "agent_manage", {"action": "update", "kind": "news", "instructions": "x"}, "instructions")

    @pytest.mark.asyncio
    async def test_create_direct_delete_confirm(self, app) -> None:  # noqa: F811
        r = await _direct(app, "agent_manage", {"action": "create", "title": "翻译"})
        kind = r.data["kind"]
        assert app.agents.is_custom_kind(kind)
        pid = await _ticket(app, "agent_manage", {"action": "delete", "kind": kind})
        assert app.agents.is_custom_kind(kind), "没点头前不能删"
        assert (await app.admin_pending.execute(pid, True)).ok
        assert not app.agents.is_custom_kind(kind)

    @pytest.mark.asyncio
    async def test_delete_builtin_refused_without_ticket(self, app) -> None:  # noqa: F811
        await _refused(app, "agent_manage", {"action": "delete", "kind": "news"}, "内置")
        await _refused(app, "agent_manage", {"action": "delete", "kind": "c_zzzzzz"}, "没有")

    @pytest.mark.asyncio
    async def test_list_agents(self, app) -> None:  # noqa: F811
        r = await _direct(app, "agent_manage", {"action": "list", })
        assert "news" in r.output and "m2" in r.output


class TestAgentDetail:
    """agent_detail：只读看一个岗「能用哪些工具、记了哪些经验、会哪些做法」（docs/02 §18）。

    - 给 group_id：本群记忆 + 本群做法 + 规矩相关条目都能看到；
    - 不给：只有岗位身份和有效工具名单；
    - 非服务群 / 不存在的 kind：中文报错，不写小票。
    """

    async def _seed_group_stuff(self, app) -> None:  # noqa: F811
        # 本群记忆：news 岗在 G1 攒过一条经验、一个 task 做法；
        # news-本群做法 启动迁移时已建好（空正文，source=migrate），补上 description。
        app.agents.remember(G1, "news", "上次核对资讯时发现了假官网", refs=["https://a.example.com/x"])
        app.agents.skill_add(G1, "task", name="整理周报", description="怎么整理周报", body="正文")
        cur = app.agents.skills(G1, "news")
        if cur:
            app.agents.skill_update(G1, int(cur[0]["id"]), description="本群资讯怎么挑")

    @pytest.mark.asyncio
    async def test_with_group_sees_memory_and_skills(self, app) -> None:  # noqa: F811
        await self._seed_group_stuff(app)
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news", "group_id": G1})
        d = r.data
        assert d["kind"] == "news" and d["group_id"] == G1
        assert d["memory"]["count"] == 1 and "假官网" in d["memory"]["items"][0]["text"]
        kinds = {s["name"] for s in d["skills"]}
        assert "news-本群做法" in kinds and "整理周报" not in kinds, "只看本岗的做法"
        assert d["skills"][0]["locked"] is False and d["skills"][0]["status"] == "active"
        # 种一条点名这岗的规矩 + 一条「记住」行（remember 写进规矩的样子）
        app.agents.group_rules_set(
            G1, "这个群最近爱在周五晚上分享折腾成果。\n给 news 岗：少收手机营销稿\n记住要先查官网\n",
            updated_by="admin",
        )
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news", "group_id": G1})
        d = r.data
        titles = d["rules"]["recent"]
        assert d["rules"]["count"] == 2, titles
        assert any("news" in t for t in titles) and any("官网" in t for t in titles)
        assert "假官网" in r.output and "news-本群做法" in r.output

    @pytest.mark.asyncio
    async def test_without_group_identity_and_tools_only(self, app) -> None:  # noqa: F811
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news"})
        d = r.data
        assert d["kind"] == "news" and d["title"]
        assert "enabled" in d and "model" in d and "effort" in d and "backup" in d and "escalate" in d
        assert d["group_id"] == ""
        # 不给群号 → 没有本群三块
        for key in ("memory", "skills", "rules"):
            assert key not in d, f"不给群号不该有 {key}"
        # 有效工具名 = specialists.effective_tools(kind, profile.tools, profile)；
        # 名单上只可能有 web_search / fetch_page 这类调研工具 + submit_result 保底
        assert "submit_result" in d["tools"] and "web_search" in d["tools"]
        assert "run_command" not in d["tools"]

    @pytest.mark.asyncio
    async def test_tools_match_specialists_effective(self, app) -> None:  # noqa: F811
        """工具名单必须和 Specialists.effective_tools 算出来的一模一样（同一入口）。"""
        p = app.agents.profile("news")
        want = app.specialists.effective_tools("news", p.get("tools"), p)
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news"})
        assert r.data["tools"] == want

    @pytest.mark.asyncio
    async def test_task_kind_tools_from_profile(self, app) -> None:  # noqa: F811
        """task 岗（per_job）：data["tools"] 是全部子 agent 工具候选，不再误写成只剩 submit_result。"""
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "task"})
        assert r.data["describe"]["mode"] == "per_job"
        assert r.data["tools"] == r.data["describe"]["candidates"]
        assert len(r.data["tools"]) > 1
        assert "工具不固定" in r.output

    @pytest.mark.asyncio
    async def test_memory_truncated_and_capped(self, app) -> None:  # noqa: F811
        """条目 ≤10、每条 ≤80 字。"""
        long_text = "长" * 200
        for i in range(12):
            app.agents.remember(G1, "news", f"第{i}条{long_text}", source_id=f"s{i}")
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news", "group_id": G1})
        items = r.data["memory"]["items"]
        assert r.data["memory"]["count"] == 12
        assert len(items) <= 10
        assert all(len(it["text"]) <= 81 for it in items)  # 80 字 + 省略号

    @pytest.mark.asyncio
    async def test_skill_dates_and_locked(self, app) -> None:  # noqa: F811
        sid = int(app.agents.skills(G1, "news")[0]["id"])  # 启动迁移已建的 news-本群做法
        app.agents.skill_update(G1, sid, locked=True)
        app.agents.skill_touch_use(G1, "news", "news-本群做法")
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news", "group_id": G1})
        sk = next(s for s in r.data["skills"] if s["name"] == "news-本群做法")
        assert sk["locked"] is True and sk["uses"] == 1
        assert sk["last_used"], "用过要给日期"
        assert "-" in sk["last_used"], f"日期该是 YYYY-MM-DD 形状：{sk['last_used']}"

    @pytest.mark.asyncio
    async def test_archived_skill_not_listed(self, app) -> None:  # noqa: F811
        sid = int(app.agents.skills(G1, "news")[0]["id"])
        app.agents.skill_update(G1, sid, status="archived")
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news", "group_id": G1})
        assert "news-本群做法" not in {s["name"] for s in r.data["skills"]}

    @pytest.mark.asyncio
    async def test_no_group_no_rules_memory_skills(self, app) -> None:  # noqa: F811
        await self._seed_group_stuff(app)
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news"})
        for key in ("memory", "skills", "rules"):
            assert key not in r.data

    @pytest.mark.asyncio
    async def test_non_served_group_refused(self, app) -> None:  # noqa: F811
        await _refused(app, "agent_manage", {"action": "detail", "kind": "news", "group_id": "99999"}, "服务群")

    @pytest.mark.asyncio
    async def test_unknown_kind_refused(self, app) -> None:  # noqa: F811
        await _refused(app, "agent_manage", {"action": "detail", "kind": "nope"}, "岗位")
        await _refused(app, "agent_manage", {"action": "detail", }, "kind")

    @pytest.mark.asyncio
    async def test_extra_args_refused(self, app) -> None:  # noqa: F811
        await _refused(app, "agent_manage", {"action": "detail", "kind": "news", "headers": {"X": "y"}}, "密钥")

    @pytest.mark.asyncio
    async def test_no_ticket_written(self, app) -> None:  # noqa: F811
        """只读工具：不写小票。"""
        await self._seed_group_stuff(app)
        r = await _direct(app, "agent_manage", {"action": "detail", "kind": "news", "group_id": G1})
        assert tools_admin.NEED_CONFIRM not in r.output


# ----------------------------------------------------------------------
# 三、端点与模型库
# ----------------------------------------------------------------------


def _ep(app, eid: str):  # noqa: F811
    return next((e for e in app.get_settings().endpoints if e.id == eid), None)


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_new_endpoint_needs_confirm(self, app) -> None:  # noqa: F811
        args = {"id": "ds", "name": "DeepSeek", "protocol": "openai", "base_url": "https://api.deepseek.com/v1"}
        pid = await _ticket(app, "model_manage", {"action": "endpoint_save", **args})
        assert _ep(app, "ds") is None, "没点头前不能写配置"
        r = await app.admin_pending.execute(pid, True)
        assert r.ok, r.error
        ep = _ep(app, "ds")
        assert ep is not None and ep.base_url == "https://api.deepseek.com/v1"
        assert "设置 → 模型" in r.output, "新端点还没密钥：要指路网页自己填"

    @pytest.mark.asyncio
    async def test_base_url_change_needs_confirm_and_keeps_key(self, app) -> None:  # noqa: F811
        old_url = _ep(app, "default").base_url
        pid = await _ticket(app, "model_manage", {"action": "endpoint_save", "id": "default", "base_url": "https://new.example.com/v1"})
        assert _ep(app, "default").base_url == old_url
        # 已存的密钥会跟着去新地址：小票上要写明
        assert "已存的密钥" in app.admin_pending._row(pid)["summary"]
        assert (await app.admin_pending.execute(pid, True)).ok
        ep = _ep(app, "default")
        assert ep.base_url == "https://new.example.com/v1"
        assert ep.api_key == "test-key", "改地址不能把已存的密钥弄丢"

    @pytest.mark.asyncio
    async def test_protocol_change_needs_confirm(self, app) -> None:  # noqa: F811
        await _ticket(app, "model_manage", {"action": "endpoint_save", "id": "default", "protocol": "anthropic"})
        assert _ep(app, "default").protocol == "openai"

    @pytest.mark.asyncio
    async def test_other_fields_direct(self, app) -> None:  # noqa: F811
        await _direct(app, "model_manage", {"action": "endpoint_save", "id": "default", "name": "主端点", "retries": 5, "max_rpm": 30})
        ep = _ep(app, "default")
        assert (ep.name, ep.retries, ep.max_rpm) == ("主端点", 5, 30)
        assert ep.api_key == "test-key"

    @pytest.mark.asyncio
    async def test_endpoint_rejects_secrets_and_bad(self, app) -> None:  # noqa: F811
        for extra in ({"api_key": "sk-new"}, {"headers": {"X-Api-Key": "v"}}, {"key": "sk-new"}):
            r = await _refused(app, "model_manage", {"action": "endpoint_save", "id": "default", **extra}, "密钥", "设置 → 模型")
            assert "sk-new" not in r.error
        assert _ep(app, "default").api_key == "test-key"
        await _refused(app, "model_manage", {"action": "endpoint_save", "id": "x1", "base_url": "ftp://x"})
        await _refused(app, "model_manage", {"action": "endpoint_save", "id": "x2", "base_url": "https://u:p@x.example.com/v1"}, "密钥")

    @pytest.mark.asyncio
    async def test_delete_endpoint_confirm(self, app) -> None:  # noqa: F811
        await settings_ops.save_endpoint(app, "spare", {"base_url": "https://spare.example.com/v1"})
        assert _ep(app, "spare") is not None
        pid = await _ticket(app, "model_manage", {"action": "endpoint_delete", "id": "spare"})
        assert _ep(app, "spare") is not None
        assert (await app.admin_pending.execute(pid, True)).ok
        assert _ep(app, "spare") is None
        # 还有模型挂着：同网页一样当场拒，不写票
        await _refused(app, "model_manage", {"action": "endpoint_delete", "id": "default"}, "模型")


class TestModelList:
    @pytest.mark.asyncio
    async def test_add_and_edit_direct_delete_confirm(self, app) -> None:  # noqa: F811
        await _direct(app, "model_manage", {"action": "model_save", "id": "m3", "endpoint": "default", "model": "gpt-x", "name": "备用"})
        ids = [m.id for m in app.get_settings().model_list]
        assert "m3" in ids
        await _direct(app, "model_manage", {"action": "model_save", "id": "m3", "max_tokens": 4096})
        m3 = next(m for m in app.get_settings().model_list if m.id == "m3")
        assert m3.max_tokens == 4096 and m3.model == "gpt-x"
        pid = await _ticket(app, "model_manage", {"action": "model_delete", "id": "m3"})
        assert "m3" in [m.id for m in app.get_settings().model_list]
        assert (await app.admin_pending.execute(pid, True)).ok
        assert "m3" not in [m.id for m in app.get_settings().model_list]

    @pytest.mark.asyncio
    async def test_model_same_validation_as_web(self, app) -> None:  # noqa: F811
        await _refused(app, "model_manage", {"action": "model_save", "id": "m4", "endpoint": "no-such", "model": "x"})
        # 有专岗在用：同网页一句话，当场拒、不写票
        await _refused(app, "model_manage", {"action": "model_delete", "id": "m2"}, "专岗")
        await _refused(app, "model_manage", {"action": "model_save", "id": "m4", "endpoint": "default", "model": "x", "api_key": "k"}, "密钥")

    @pytest.mark.asyncio
    async def test_list_models_no_secrets(self, app) -> None:  # noqa: F811
        r = await _direct(app, "model_manage", {"action": "list", })
        assert "default" in r.output and "m1" in r.output
        assert "test-key" not in r.output and "test-key" not in str(r.data)
        assert "已填" in r.output  # 只说密钥填没填


# ----------------------------------------------------------------------
# 四、网页链接与说明文字
# ----------------------------------------------------------------------


def test_settings_link_uses_public_url() -> None:
    s = SimpleNamespace(console=SimpleNamespace(public_url="https://mw.example.com/"))
    assert tools_admin.settings_link(s, "models") == "https://mw.example.com/#/settings/models"
    assert tools_admin.settings_link(SimpleNamespace(console=SimpleNamespace(public_url="")), "models") == ""


@pytest.mark.asyncio
async def test_env_overview_mentions_new_abilities(app) -> None:  # noqa: F811
    r = await _call(app, "env_overview", {})
    assert r.ok, r.error
    assert "MCP" in r.output and "专岗" in r.output and "端点" in r.output
    for bad in ("mcp_manage", "model_manage", "agent_manage"):
        assert bad not in r.output


def test_admin_prompt_knows_new_tools(app) -> None:  # noqa: F811
    p = AdminChat(app)._system_prompt({"group_id": ""})
    for name in ("mcp_manage", "agent_manage", "model_manage"):
        assert name in p, name
    assert "不要在对话里发密钥" in p
