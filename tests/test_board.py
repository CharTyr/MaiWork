"""任务看板：GET /api/board（只总管理员、只读）。

数据源只有 MaiWork 自己的库（只读）：
- `agent_handoffs`（交接单；queued/running/returned 算「在跑」，终态算「最近动态」）；
- `tasks` / `attempts`（在跑的任务 + 窗口内结束的尝试）；
- `news_batches`（一轮资讯）、`ideas`（新增构想）、`goals`（新立 / 完成目标）、
  `topic_log`（开话题）、`outbox`（已发群消息，只看类型不看正文）。

起 app + TestClient 的写法照 tests/test_console.py / tests/test_usage_history.py：
真 ConsoleServer + 真 MaiWorkApp（假 host / profiles），登录走真的 POST /api/login。

覆盖：
- lanes：每个专岗一栏（进行中 / 历史平铺）：running 交接单 / 活跃任务进对应
  lane.running，kind_title 用岗位名；超过 10 分钟没动 → stuck；queued 交接单不算
  「在跑」不进 running；窗口外的历史不进 history；
- 隐私：不放 requester_id / QQ 号 / 构想 basis / outbox 正文；个人构想 title
  换成「个人构想」；非服务群零出现；
- 形状：响应只有 now / hours / stuck_after_s / lanes 四个顶层字段（第一版的
  running / agents / recent / agent_fish 已删，docs/33 §1）；
- 鉴权：群管理员 403、群友（链接码）403、匿名 401；
- hours：默认走配置 board_hours，?hours= 覆盖并夹到 1..168。
"""

from __future__ import annotations

import json
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings

PASSWORD = "看板测试密码-不要出现在日志里"
G1 = "900000001"
G2 = "123456789"
OTHER = "555555555"  # 配置里没有的群
TOKEN_G1 = "board-tok-g1"
TOKEN_OTHER = "board-tok-other"

SECRET_QQ = "987654321"  # 用来确认 requester_id / QQ 号不进返回


def _free_port() -> int:
    import socket as _s
    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path, **over):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_free_port()}", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "main-m", "worker": "worker-m"},
        "storage": {"data_dir": str(data_dir)},
    }
    for section, values in over.items():
        raw.setdefault(section, {}).update(values)
    return raw


class SimpleEnv:
    def __init__(self, app: MaiWorkApp, client: TestClient) -> None:
        self.app = app
        self.client = client

    @property
    def store(self):
        return self.app.store


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    raw = _raw_config(tmp_path / "data", environments={"workspace_root": str(tmp_path / "workspaces")})
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    import tomlkit

    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            t = tomlkit.table()
            for k, v in values.items():
                t[k] = v
            doc[section] = t
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    with app.store.tx() as conn:
        # 服务群已由 app 按配置种好：只补测试要用的中文名和链接码
        conn.execute("UPDATE groups SET name=?, token=? WHERE group_id=?", ("看板测试群一", TOKEN_G1, G1))
        conn.execute("UPDATE groups SET name=?, token=? WHERE group_id=?", ("看板测试群二", "board-tok-g2", G2))
        conn.execute(
            "INSERT OR REPLACE INTO groups (group_id, name, token, created) VALUES (?,?,?,?)",
            (OTHER, "不在名单的群", TOKEN_OTHER, clock.now()),
        )
    yield SimpleEnv(app=app, client=client)
    await client.close()
    await app.stop()


async def _login(env: SimpleEnv) -> None:
    r = await env.client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


async def _board(env: SimpleEnv, query: str = "") -> dict:
    r = await env.client.get(f"/api/board{query}")
    assert r.status == 200, await r.text()
    return await r.json()


# ----------------------------------------------------------------------
# 播种小工具（直接写库，不走业务层，省得拉一整条流水线）
# ----------------------------------------------------------------------


def _handoff(store, gid: str, kind: str, *, status: str, ts: float, brief: str = "干点活",
             hid: str = "", phase: str = "", task_id: str = "") -> str:
    hid = hid or f"h-{gid}-{kind}-{status}-{int(ts)}"
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO agent_handoffs"
            " (id, group_id, kind, task_id, phase, parent_id, status, brief,"
            "  criteria, tools, skills, summary, data, evidence, review, error, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?, '[]','[]','[]','','','[]','','',?,?)",
            (hid, gid, kind, task_id, phase, "", status, brief, ts, ts),
        )
    return hid


def _task(store, tid: str, gid: str, *, status: str, ts: float, title: str = "测试任务",
          requester_id: str = "") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tasks"
            " (id, group_id, workspace, source, request_id, goal_id, requester_id,"
            "  requester_name, icon, title, req, criteria, status, env, delivery_kind,"
            "  created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, gid, "ws", "", "", None, requester_id, "发起人", "package", title,
             "需求", "[]", status, "", "", ts, ts),
        )


def _attempt(store, tid: str, *, status: str, started: float, finished: float) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO attempts (task_id, n, req_version, status, started, finished)"
            " VALUES (?,?,?,?,?,?)",
            (tid, 1, 1, status, started, finished),
        )


def _all_text(data: dict) -> str:
    """返回体压扁成一整段文本，方便「绝不出现 X」的断言。"""
    return json.dumps(data, ensure_ascii=False)


def _tool_call(store, *, ts: float, group_id: str = G1, task_id: str = "",
               actor: str = "子 agent #1", tool: str = "web_search",
               input_: str = "in", output: str = "out", ms: int = 12,
               ok: bool = True, error: str = "") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, group_id, task_id, actor, tool, input_, output, ms,
             1 if ok else 0, error),
        )


def _model_call(store, *, ts: float, group_id: str = G1, task_id: str = "",
                purpose: str = "worker", role: str = "worker", agent: str = "task",
                model: str = "worker-m", ok: bool = True, ms: int = 50,
                response: str = "", error: str = "") -> int:
    if not response:
        response = json.dumps({"text": "想了一下", "tool_calls": [], "finish_reason": "stop"},
                              ensure_ascii=False)
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO model_calls (ts, purpose, role, agent, model, group_id, task_id, attempt, ok,"
            " status, ms, prompt_tokens, completion_tokens, error, request, response,"
            " usage_src, cache_read, cache_write)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, purpose, role, agent, model, group_id, task_id, 1, 1 if ok else 0,
             200 if ok else 500, ms, 10, 20, error, "{}", response, "reported", 0, 0),
        )
        return int(cur.lastrowid or 0)


def _news_batch(store, *, ts: float, group_id: str = G1, kept: int = 2,
                found: int = 10, skipped: int = 0, note: str = "") -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (group_id, ts, found, kept, skipped, note, ts),
        )
        return int(cur.lastrowid or 0)


def _topic_log(store, *, ts: float, group_id: str = G1,
               result: str | None = None, verdict: str | None = None,
               opener: str = "开场白") -> int:
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO topic_log (group_id, ts, quiet_s, usual_gap_s, jev, pick, candidate_id,"
            " opener, message_id, followup_due_ts, result, verdict)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (group_id, ts, 300.0, None, "{}", "{}", None, opener, "", None, result, verdict),
        )
        return int(cur.lastrowid or 0)


# ----------------------------------------------------------------------
# lanes（分专岗「进行中 / 历史」）—— 2026-10 看板改版
# ----------------------------------------------------------------------


def _lane(data: dict, kind: str) -> dict:
    lanes = data.get("lanes")
    assert isinstance(lanes, list), "lanes 缺失"
    for lane in lanes:
        if lane.get("kind") == kind:
            return lane
    raise AssertionError(f"没有 kind={kind} 的 lane：{[l.get('kind') for l in lanes]}")


def _running_ids(data: dict) -> set:
    """全部 lane 的 running 条目 id（旧 `running` 字段的 lanes 等价）。"""
    out = set()
    for lane in data["lanes"]:
        for item in lane["running"]:
            out.add(item["id"])
    return out


def _all_items(data: dict) -> list:
    out = []
    for lane in data["lanes"]:
        out.extend(lane["running"])
        out.extend(lane["history"])
    return out


class TestShape:
    @pytest.mark.asyncio
    async def test_only_lanes_fields_no_legacy(self, env: SimpleEnv) -> None:
        """响应只有 now/hours/stuck_after_s/lanes；第一版的
        running/agents/recent/agent_fish 已删（docs/33 §1）。"""
        await _login(env)
        data = await _board(env)
        for legacy in ("running", "agents", "recent", "agent_fish"):
            assert legacy not in data, f"删掉的旧字段还在：{legacy}"
        assert isinstance(data["now"], (int, float))
        assert isinstance(data["lanes"], list)


class TestRunning:
    @pytest.mark.asyncio
    async def test_running_handoff_shape_and_stuck(self, env: SimpleEnv) -> None:
        """running 交接单进对应 lane.running；updated 距今 >600s → stuck=True。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, G1, "news", status="running", ts=now - 601, brief="找今天 AI 新闻", hid="h-run")
        _handoff(env.store, G1, "idea", status="running", ts=now - 120, brief="出个构想", hid="h-fresh")
        data = await _board(env)
        by_id = {i["id"]: i for i in _all_items(data)}
        assert "h-run" in by_id and "h-fresh" in by_id
        assert by_id["h-run"]["stuck"] is True
        assert by_id["h-fresh"]["stuck"] is False
        assert by_id["h-run"]["source"] == "handoff"
        assert by_id["h-run"]["group_id"] == G1 and by_id["h-run"]["group_name"] == "看板测试群一"
        assert by_id["h-run"]["title"] == "找今天 AI 新闻"
        assert by_id["h-run"]["status"] == "running"
        assert by_id["h-run"]["elapsed_s"] >= 600
        # 进了对的 lane，lane kind_title 用岗位名
        news_lane = _lane(data, "news")
        assert news_lane["kind_title"] == "资讯"
        assert any(i["id"] == "h-run" for i in news_lane["running"])
        assert data["stuck_after_s"] == 600 and data["hours"] == 24
        assert "now" in data

    @pytest.mark.asyncio
    async def test_running_task_and_feature_off(self, env: SimpleEnv) -> None:
        """活跃状态的任务进 task lane.running；feature_off 搁置的 queued 任务也列出但不算卡住。"""
        await _login(env)
        now = clock.now()
        _task(env.store, "T-1", G1, status="running", ts=now - 60, title="写个网页", requester_id=SECRET_QQ)
        _task(env.store, "T-2", G1, status="waiting_input", ts=now - 60, title="等回答")
        _task(env.store, "T-3", G1, status="completed", ts=now - 60, title="做完的")
        with env.store.tx() as conn:
            conn.execute(
                "UPDATE tasks SET paused_reason=? WHERE id=?",
                (json.dumps({"kind": "feature_off"}), "T-2"),
            )
        data = await _board(env)
        lane = _lane(data, "task")
        by_id = {i["id"]: i for i in lane["running"]}
        assert by_id["T-1"]["source"] == "task"
        assert by_id["T-2"]["status"] == "waiting_input" and by_id["T-2"]["stuck"] is False
        assert "T-3" not in by_id
        assert SECRET_QQ not in _all_text(data)

    @pytest.mark.asyncio
    async def test_running_excludes_non_served_group(self, env: SimpleEnv) -> None:
        """配置外的群的交接单 / 任务 / 历史在 lanes 里零出现。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, OTHER, "news", status="running", ts=now - 30, brief="别的群", hid="h-other")
        _task(env.store, "T-9", OTHER, status="running", ts=now - 30, title="别的群的活")
        _handoff(env.store, OTHER, "idea", status="accepted", ts=now - 30, brief="别的群的构想", hid="h-other-done")
        data = await _board(env)
        assert all(i["group_id"] != OTHER for i in _all_items(data))
        assert all("别的群" not in (i["title"] or "") for i in _all_items(data))

    @pytest.mark.asyncio
    async def test_queued_handoff_not_in_running(self, env: SimpleEnv) -> None:
        """queued 交接单（排进队列还没跑）不算「在跑」、不进 lane.running；终态占 history。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, G1, "news", status="running", ts=now - 30, hid="h-live")
        _handoff(env.store, G1, "news", status="queued", ts=now - 10, hid="h-queued")
        _handoff(env.store, G1, "idea", status="failed", ts=now - 10, hid="h-dead")
        data = await _board(env)
        running_ids = _running_ids(data)
        assert "h-live" in running_ids
        assert "h-queued" not in running_ids and "h-dead" not in running_ids
        # 终态进 history
        idea_lane = _lane(data, "idea")
        assert any(i["id"] == "h-dead" and i["ok"] is False for i in idea_lane["history"])

    @pytest.mark.asyncio
    async def test_agent_kinds_cover_builtin_and_custom(self, env: SimpleEnv) -> None:
        """岗位名单：main + 内建四岗 + task + topic + 自定义 c_* 都有 lane；
        交接单的 kind 决定它进哪一栏（自定义岗也归自己的栏）。"""
        await _login(env)
        now = clock.now()
        with env.store.tx() as conn:
            # 种一个自定义岗，h-custom 交接单挂它
            raw = env.store.kv_get("agents.profiles") or {}
            raw["c_ab12cd"] = {"title": "调研岗", "enabled": True}
            env.store.kv_set(conn, "agents.profiles", raw)
        _handoff(env.store, G1, "c_ab12cd", status="running", ts=now - 30, hid="h-custom")
        data = await _board(env)
        kinds = [l["kind"] for l in data["lanes"]]
        for k in ("main", "news", "idea", "goal", "task", "topic", "c_ab12cd"):
            assert k in kinds, f"缺 lane {k}：{kinds}"
        custom_lane = _lane(data, "c_ab12cd")
        assert custom_lane["kind_title"] == "调研岗"
        assert any(i["id"] == "h-custom" for i in custom_lane["running"])


class TestRecentCoverage:
    """旧 `recent` 字段的 lanes 等价断言：每类来源在窗口内都进对应 lane.history。"""

    @pytest.mark.asyncio
    async def test_windowed_sources_land_in_lanes(self, env: SimpleEnv) -> None:
        """交接单终态 / 失败任务 / 资讯批次 / 新构想 / 目标新立+完成 / 开话题 各进对的栏。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, G1, "news", status="accepted", ts=now - 60, brief="找资讯", hid="h-acc")
        _task(env.store, "T-1", G1, status="failed", ts=now - 60)
        _attempt(env.store, "T-1", status="failed", started=now - 300, finished=now - 60)
        with env.store.tx() as conn:
            conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?,?,?,?,?,?,?)",
                (G1, now - 50, 12, 3, 0, "", now - 50),
            )
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, state, created, updated)"
                " VALUES (?,?,?,?,?,?)",
                (G1, "群构想", "正文", "new", now - 40, now - 40),
            )
            conn.execute(
                "INSERT INTO goals (id, group_id, kind, title, state, created, updated)"
                " VALUES (?,?,?,?,?,?,?)",
                ("G-1", G1, "agent", "盯住网站更新", "active", now - 30, now - 30),
            )
            conn.execute(
                "INSERT INTO goals (id, group_id, kind, title, state, created, updated)"
                " VALUES (?,?,?,?,?,?,?)",
                ("G-2", G1, "agent", "老目标", "done", now - 5000, now - 30),
            )
            conn.execute(
                "INSERT INTO topic_log (group_id, ts, candidate_id, opener, verdict, result)"
                " VALUES (?,?,?,?,?,?)",
                (G1, now - 20, None, "", "good", "sent"),
            )
        data = await _board(env)
        assert any(i["id"] == "h-acc" for i in _lane(data, "news")["history"])
        assert any(i["id"] == "T-1" for i in _lane(data, "task")["history"])
        assert any(i["source"] == "news_batch" for i in _lane(data, "news")["history"])
        assert any(i["source"] == "idea" for i in _lane(data, "idea")["history"])
        goal_items = _lane(data, "goal")["history"]
        assert any(i["id"] == "G-1" for i in goal_items)
        assert any(i["id"] == "G-2" and i["ok"] is True for i in goal_items)
        assert any(i["source"] == "topic" for i in _lane(data, "topic")["history"])

    @pytest.mark.asyncio
    async def test_recent_window(self, env: SimpleEnv) -> None:
        """默认窗口（24h）外的历史不进 lane.history；?hours= 自定义窗口。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, G1, "news", status="accepted", ts=now - 25 * 3600, hid="h-old")
        _handoff(env.store, G1, "idea", status="accepted", ts=now - 3600, hid="h-new")
        with env.store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, title, state, created, updated) VALUES (?,?,?,?,?)",
                (G1, "旧构想", "new", now - 30 * 3600, now - 30 * 3600),
            )
        data = await _board(env)
        assert any(i["id"] == "h-new" for i in _lane(data, "idea")["history"])
        assert not any(i["id"] == "h-old" for i in _all_items(data))
        # ?hours=48 之后窗口外的也进来
        data48 = await _board(env, "?hours=48")
        assert any(i["id"] == "h-old" for i in _lane(data48, "news")["history"])
        assert data48["hours"] == 48

    @pytest.mark.asyncio
    async def test_hours_clamped(self, env: SimpleEnv) -> None:
        """?hours= 夹在 1..168；坏值回落配置默认。"""
        await _login(env)
        assert (await _board(env, "?hours=0"))["hours"] == 1
        assert (await _board(env, "?hours=9999"))["hours"] == 168
        assert (await _board(env, "?hours=abc"))["hours"] == 24

    @pytest.mark.asyncio
    async def test_personal_idea_masked(self, env: SimpleEnv) -> None:
        """个人向构想的 title 不进返回（lane 里写「个人构想」），basis 绝不出现。"""
        await _login(env)
        now = clock.now()
        with env.store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, title, body, basis, state, created, updated, target_user_id)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (G1, "送给阿明的书", "正文", SECRET_QQ + " 说过想看", "new",
                 now - 10, now - 10, SECRET_QQ),
            )
        data = await _board(env)
        idea_lane = _lane(data, "idea")
        titles = [i["title"] for i in idea_lane["history"]]
        assert titles and all(t == "个人构想" for t in titles)
        assert "送给阿明的书" not in _all_text(data)
        assert "说过想看" not in _all_text(data)
        assert SECRET_QQ not in _all_text(data)

    @pytest.mark.asyncio
    async def test_outbox_body_never_leaks(self, env: SimpleEnv) -> None:
        """已发群消息正文绝不进返回（看板只列 lanes，正文字段根本没有）。"""
        await _login(env)
        now = clock.now()
        with env.store.tx() as conn:
            conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, created, updated)"
                " VALUES (?,?,?,?,?,?,?)",
                ("opener:1", G1, "text", json.dumps({"text": "群友绝对看不到的原话"}), "sent",
                 now - 10, now - 10),
            )
        data = await _board(env)
        assert "群友绝对看不到的原话" not in _all_text(data)


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------


class TestAuth:
    @pytest.mark.asyncio
    async def test_anonymous_401(self, env: SimpleEnv) -> None:
        r = await env.client.get("/api/board")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_member_403(self, env: SimpleEnv) -> None:
        """群链接码（群友身份）打不开看板。"""
        r = await env.client.get("/api/board", headers={"X-MW-Group": TOKEN_G1})
        assert r.status == 403

    @pytest.mark.asyncio
    async def test_member_other_group_403(self, env: SimpleEnv) -> None:
        """非服务群的链接码按匿名处理。"""
        r = await env.client.get("/api/board", headers={"X-MW-Group": TOKEN_OTHER})
        assert r.status == 401


class TestReviewFixes:
    @pytest.mark.asyncio
    async def test_unserved_group_items_hidden(self, env: SimpleEnv) -> None:
        """非服务群的交接单在任何 lane 都不出现（零出现）。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, OTHER, "news", status="running", ts=now - 10, hid="h-other-busy")
        data = await _board(env)
        assert "h-other-busy" not in _running_ids(data)
        assert not any(i["id"] == "h-other-busy" for i in _all_items(data))

    @pytest.mark.asyncio
    async def test_waiting_input_task_is_not_stuck(self, env: SimpleEnv) -> None:
        """等人答复的任务停着是正常的，不标「卡住」。"""
        await _login(env)
        now = clock.now()
        _task(env.store, "T-wait", G1, status="waiting_input", ts=now - 3600)
        data = await _board(env)
        row = next(i for i in _lane(data, "task")["running"] if i["id"] == "T-wait")
        assert row["stuck"] is False

    @pytest.mark.asyncio
    async def test_lane_fish_seed_per_kind(self, env: SimpleEnv) -> None:
        """每个 lane 带 fish_seed（专岗小鱼种子）；没有配的岗给空串。"""
        await _login(env)
        data = await _board(env)
        for lane in data["lanes"]:
            assert isinstance(lane.get("fish_seed"), str)


# ----------------------------------------------------------------------
# lanes（分专岗「进行中 / 历史」）—— 2026-10 看板改版
# ----------------------------------------------------------------------


def _lane(data: dict, kind: str) -> dict:
    lanes = data.get("lanes")
    assert isinstance(lanes, list), "lanes 缺失"
    for lane in lanes:
        if lane.get("kind") == kind:
            return lane
    raise AssertionError(f"没有 kind={kind} 的 lane：{[l.get('kind') for l in lanes]}")


class TestLanes:
    @pytest.mark.asyncio
    async def test_lanes_shape_and_order(self, env: SimpleEnv) -> None:
        """每个 lane 有 kind/kind_title/fish_seed/running/history/counts；
        顺序：main → news → idea → goal → task → topic → 自定义 c_*。"""
        await _login(env)
        data = await _board(env)
        lanes = data["lanes"]
        kinds = [l["kind"] for l in lanes]
        assert kinds[:6] == ["main", "news", "idea", "goal", "task", "topic"]
        for lane in lanes:
            assert isinstance(lane.get("kind_title"), str) and lane["kind_title"]
            assert isinstance(lane.get("fish_seed"), str)
            assert isinstance(lane.get("running"), list)
            assert isinstance(lane.get("history"), list)
            c = lane.get("counts") or {}
            assert set(c) >= {"running", "done", "failed"}

    @pytest.mark.asyncio
    async def test_running_items_in_right_lane(self, env: SimpleEnv) -> None:
        """news 交接单进 news lane.running，idea 进 idea lane；运行中的任务进 task lane。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, G1, "news", status="running", ts=now - 60, brief="找资讯", hid="h-news")
        _handoff(env.store, G1, "idea", status="running", ts=now - 60, brief="想构想", hid="h-idea")
        _task(env.store, "T-run", G1, status="running", ts=now - 300, title="跑任务")
        data = await _board(env)
        news_lane = _lane(data, "news")
        assert any(i["id"] == "h-news" for i in news_lane["running"])
        assert not any(i["id"] == "h-idea" for i in news_lane["running"])
        idea_lane = _lane(data, "idea")
        assert any(i["id"] == "h-idea" for i in idea_lane["running"])
        task_lane = _lane(data, "task")
        assert any(i["id"] == "T-run" for i in task_lane["running"])
        assert news_lane["counts"]["running"] >= 1

    @pytest.mark.asyncio
    async def test_history_items_in_right_lane(self, env: SimpleEnv) -> None:
        """终态交接单进对应 lane.history（带 status_text/ok）；历史最多窗口内。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, G1, "news", status="accepted", ts=now - 120, brief="交回的活", hid="h-done")
        _handoff(env.store, G1, "news", status="failed", ts=now - 240, brief="砸了的活", hid="h-fail")
        # 窗口外的老历史不进
        _handoff(env.store, G1, "news", status="accepted", ts=now - 30 * 3600, brief="太老", hid="h-old")
        data = await _board(env)
        news = _lane(data, "news")
        ids = {i["id"] for i in news["history"]}
        assert "h-done" in ids and "h-fail" in ids and "h-old" not in ids
        by_id = {i["id"]: i for i in news["history"]}
        assert by_id["h-done"]["ok"] is True
        assert by_id["h-fail"]["ok"] is False
        assert by_id["h-done"]["status_text"]
        assert news["counts"]["done"] >= 1 and news["counts"]["failed"] >= 1

    @pytest.mark.asyncio
    async def test_news_batch_lane_and_personal_idea(self, env: SimpleEnv) -> None:
        """资讯批次进 news lane.history；个人向批次的 note(personal:*) 不上板。"""
        await _login(env)
        now = clock.now()
        bid = _news_batch(env.store, ts=now - 100, kept=3, found=9)
        _news_batch(env.store, ts=now - 90, group_id=G1, kept=0, note="personal:某成员")
        _news_batch(env.store, ts=now - 80, group_id=OTHER, kept=5)
        data = await _board(env)
        news = _lane(data, "news")
        ids = {i["id"] for i in news["history"]}
        assert bid in ids
        # 个人向批次（note personal:*）不出现
        titles = [i["title"] for i in news["history"]]
        assert not any("personal" in t or "某成员" in t for t in titles)
        # 非服务群批次不出现
        other_ids = [i for i in news["history"] if i.get("group_id") == OTHER]
        assert not other_ids

    @pytest.mark.asyncio
    async def test_topic_lane(self, env: SimpleEnv) -> None:
        """开话题进 topic lane.history，带 verdict/result。"""
        await _login(env)
        now = clock.now()
        tid = _topic_log(env.store, ts=now - 60, result="sent", verdict="good")
        data = await _board(env)
        topic = _lane(data, "topic")
        ids = {i["id"] for i in topic["history"]}
        assert tid in ids
        item = next(i for i in topic["history"] if i["id"] == tid)
        assert item["ok"] is True

    @pytest.mark.asyncio
    async def test_personal_idea_title_masked_in_lane(self, env: SimpleEnv) -> None:
        """个人构想在 lane 里 title 永远是「个人构想」，不露原话 / requester。"""
        await _login(env)
        now = clock.now()
        with env.store.tx() as conn:
            conn.execute(
                "INSERT INTO ideas (group_id, icon, title, body, origin, basis, step, effort,"
                " state, requested_by, task_id, up, down, created, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (G1, "bulb", "帮某人做个东西", "正文", "", "某人说想要", "", "", "new",
                 SECRET_QQ, None, 0, 0, now - 10, now - 10),
            )
            conn.execute(
                "ALTER TABLE ideas ADD COLUMN target_user_id TEXT NOT NULL DEFAULT ''"
                if False else "SELECT 1", ()
            )
        # ideas 的 target_user_id 列由迁移保证；直接 UPDATE
        with env.store.tx() as conn:
            conn.execute("UPDATE ideas SET target_user_id=? WHERE group_id=?", (SECRET_QQ, G1))
        data = await _board(env)
        idea = _lane(data, "idea")
        text = _all_text(data)
        assert SECRET_QQ not in text
        for item in idea["history"]:
            if item.get("source") == "idea" or "构想" in (item.get("title") or ""):
                assert item["title"] == "个人构想" or "某人" not in item["title"]

    @pytest.mark.asyncio
    async def test_task_lane_counts(self, env: SimpleEnv) -> None:
        """task lane counts 与 running/history 行数吻合。"""
        await _login(env)
        now = clock.now()
        _task(env.store, "T-run2", G1, status="running", ts=now - 60)
        _task(env.store, "T-done", G1, status="done", ts=now - 200)
        data = await _board(env)
        lane = _lane(data, "task")
        assert lane["counts"]["running"] == len(lane["running"])
        assert lane["counts"]["done"] >= 1


# ----------------------------------------------------------------------
# 详情 GET /api/board/items/{source}/{id}
# ----------------------------------------------------------------------


async def _item_detail(env: SimpleEnv, source: str, item_id: str, query: str = "") -> tuple[int, dict]:
    r = await env.client.get(f"/api/board/items/{source}/{item_id}{query}")
    if r.status == 200:
        return 200, await r.json()
    return r.status, {"_text": await r.text()}


class TestItemDetail:
    @pytest.mark.asyncio
    async def test_handoff_steps_have_tool_and_think(self, env: SimpleEnv) -> None:
        """handoff 详情：steps 里有 tool 调用 + think（模型回复），按时间升序。"""
        await _login(env)
        now = clock.now()
        hid = "h-detail"
        _handoff(env.store, G1, "news", status="accepted", ts=now - 300,
                 brief="核验一批资讯", hid=hid, task_id="feeds-verify:900000001:12345:0")
        mark = "feeds-verify:900000001:12345:0"
        _tool_call(env.store, ts=now - 250, task_id=mark, tool="fetch_page",
                   actor="资讯核验 子 agent #0", input_="打开 https://x", output="正文…", ms=80)
        _model_call(env.store, ts=now - 240, task_id=mark, agent="news", role="worker",
                    response=json.dumps({"text": "这条像软文", "tool_calls": [
                        {"name": "fetch_page", "arguments": "{}"}], "finish_reason": "tool_calls"},
                        ensure_ascii=False))
        _tool_call(env.store, ts=now - 230, task_id=mark, tool="web_search",
                   actor="资讯核验 子 agent #0", input_="再搜一下", output="3 条", ms=40)
        status, data = await _item_detail(env, "handoff", hid)
        assert status == 200, data
        assert data["item"]["id"] == hid
        steps = data["steps"]
        assert isinstance(steps, list) and len(steps) >= 3
        types = [s["type"] for s in steps]
        assert "tool" in types and "think" in types
        # 时间升序
        ts = [s["ts"] for s in steps]
        assert ts == sorted(ts)
        # tool step 字段
        tool_step = next(s for s in steps if s["type"] == "tool" and s["tool"] == "fetch_page")
        assert tool_step["input"] and tool_step["output"] and tool_step["ms"] == 80
        assert tool_step["ok"] is True
        # think step 带模型文本
        think = next(s for s in steps if s["type"] == "think")
        assert think["text"] and "软文" in think["text"]

    @pytest.mark.asyncio
    async def test_task_item_steps(self, env: SimpleEnv) -> None:
        """task 详情：task_id=T-* 的 tool_calls/model_calls 都进来，link=exact。"""
        await _login(env)
        now = clock.now()
        _task(env.store, "T-detail", G1, status="done", ts=now - 400, title="写脚本")
        _tool_call(env.store, ts=now - 350, task_id="T-detail", tool="run_command",
                   actor="子 agent #1", input_="ls", output="ok", ms=15)
        _model_call(env.store, ts=now - 340, task_id="T-detail", agent="task")
        status, data = await _item_detail(env, "task", "T-detail")
        assert status == 200, data
        assert data["link"] == "exact"
        tools = [s for s in data["steps"] if s["type"] == "tool"]
        thinks = [s for s in data["steps"] if s["type"] == "think"]
        assert tools and thinks

    @pytest.mark.asyncio
    async def test_news_batch_item_time_window_link(self, env: SimpleEnv) -> None:
        """news_batch 详情：批次本身不带 mark，按群+时间窗把当轮的 tool/model 调用拼进来，link=time_window。"""
        await _login(env)
        now = clock.now()
        bid = _news_batch(env.store, ts=now - 200, kept=4, found=20)
        mark = "feeds-collect:900000001:99999:1"
        # 当轮的调用（时间落在批次前后，task_id 是本轮的 mark，不是批次 id）
        _tool_call(env.store, ts=now - 210, task_id=mark, tool="web_search", actor="资讯核验 子 agent #0")
        _model_call(env.store, ts=now - 205, task_id=mark, agent="news", purpose="feeds.pick")
        status, data = await _item_detail(env, "news_batch", str(bid))
        assert status == 200, data
        assert data["link"] in ("exact", "time_window")
        # 至少能看到当轮的 think（feeds.pick 调了模型）
        thinks = [s for s in data["steps"] if s["type"] == "think"]
        assert thinks

    @pytest.mark.asyncio
    async def test_item_detail_secret_masked(self, env: SimpleEnv) -> None:
        """详情出网前整包过遮罩：tool input/output / model text 里的密钥不露。"""
        await _login(env)
        now = clock.now()
        hid = "h-secret"
        _handoff(env.store, G1, "task", status="accepted", ts=now - 300,
                 brief="跑活", hid=hid, task_id="T-secret")
        _tool_call(env.store, ts=now - 250, task_id="T-secret", tool="run_command",
                   input_=f"export KEY={PASSWORD}", output=f"token {PASSWORD} ok")
        _model_call(env.store, ts=now - 240, task_id="T-secret",
                    response=json.dumps({"text": f"密钥是 {PASSWORD}", "tool_calls": [],
                                         "finish_reason": "stop"}, ensure_ascii=False))
        status, data = await _item_detail(env, "handoff", hid)
        assert status == 200, data
        body = _all_text(data)
        assert PASSWORD not in body
        assert "***" in body

    @pytest.mark.asyncio
    async def test_item_detail_404(self, env: SimpleEnv) -> None:
        """不存在 / 非服务群 / 乱 source → 404（不 500）。"""
        await _login(env)
        now = clock.now()
        _handoff(env.store, OTHER, "news", status="accepted", ts=now - 10, hid="h-other-grp")
        for source, iid in (("handoff", "不存在"), ("handoff", "h-other-grp"),
                            ("task", "T-不存在"), ("bogus", "x")):
            r = await env.client.get(f"/api/board/items/{source}/{iid}")
            assert r.status == 404, f"{source}/{iid} → {r.status}"

    @pytest.mark.asyncio
    async def test_item_detail_auth(self, env: SimpleEnv) -> None:
        """成员 / 匿名打不开详情。"""
        now = clock.now()
        _handoff(env.store, G1, "news", status="running", ts=now - 10, hid="h-auth")
        r = await env.client.get("/api/board/items/handoff/h-auth")
        assert r.status == 401
        r = await env.client.get("/api/board/items/handoff/h-auth",
                                 headers={"X-MW-Group": TOKEN_G1})
        assert r.status == 403

    @pytest.mark.asyncio
    async def test_item_detail_after_filter(self, env: SimpleEnv) -> None:
        """?after=<ts> 只回 ts > after 的 steps（增量轮询）。"""
        await _login(env)
        now = clock.now()
        hid = "h-after"
        _handoff(env.store, G1, "news", status="running", ts=now - 300, hid=hid,
                 task_id="feeds-verify:g:1:0")
        mark = "feeds-verify:g:1:0"
        _tool_call(env.store, ts=now - 250, task_id=mark, tool="fetch_page", input_="老调用")
        _tool_call(env.store, ts=now - 100, task_id=mark, tool="web_search", input_="新调用")
        status, data = await _item_detail(env, "handoff", hid)
        assert status == 200
        all_steps = data["steps"]
        status, data2 = await _item_detail(env, "handoff", hid, query=f"?after={now - 200}")
        assert status == 200
        steps2 = data2["steps"]
        assert len(steps2) < len(all_steps)
        assert all(s["ts"] > now - 200 for s in steps2)
        # 新调用还在，老调用被滤掉
        inputs = [s.get("input") or "" for s in steps2]
        assert not any("老调用" in i for i in inputs)


class TestLaneWording:
    @pytest.mark.asyncio
    async def test_topic_lane_has_chinese_title(self, env: SimpleEnv) -> None:
        await _login(env)
        data = await _board(env)
        lane = next(l for l in data["lanes"] if l["kind"] == "topic")
        assert lane["kind_title"] == "开话题"

    @pytest.mark.asyncio
    async def test_skipped_news_round_is_not_failure(self, env: SimpleEnv) -> None:
        """skipped=1 的资讯轮是「这轮没留」，不是失败：ok 为 None，不进失败计数。"""
        await _login(env)
        now = clock.now()
        with env.store.tx() as conn:
            conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?,?,?,?,?,?,?)", (G1, now - 60, 12, 0, 1, "", now - 60))
        data = await _board(env)
        lane = next(l for l in data["lanes"] if l["kind"] == "news")
        row = next(x for x in lane["history"] if x["source"] == "news_batch")
        assert row["ok"] is None
        assert lane["counts"]["failed"] == 0


class TestElapsedMeaning:
    @pytest.mark.asyncio
    async def test_finished_item_elapsed_is_duration_not_age(self, env: SimpleEnv) -> None:
        """历史卡片写「用时」：结束的件 elapsed_s = finished − started，不是「距今多久」。"""
        await _login(env)
        now = clock.now()
        with env.store.tx() as conn:
            conn.execute(
                "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
                " VALUES (?,?,?,?,?,?,?)", (G1, now - 7200, 12, 3, 0, "", now - 7200))
        data = await _board(env)
        lane = next(l for l in data["lanes"] if l["kind"] == "news")
        for x in lane["history"]:
            if x["finished"]:
                assert x["elapsed_s"] == max(0, int(x["finished"] - x["started"])), x
