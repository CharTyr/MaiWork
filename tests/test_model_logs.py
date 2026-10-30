"""最近模型请求日志（model_calls 表 + /api/logs/* 管理员接口）。

- 每次 chat() 尝试（成功或失败）写一行，attempt 从 1 起；
- 密钥绝不入库也不出接口：已知密钥、Bearer xxx、sk-... 一律遮掉；
- messages 存 role/content（截 4000）/tool_calls（名字+参数截 500），整份请求 JSON 截 80KB；
- response 存 text（截 20000）/tool_calls/ finish_reason；
- 保留 3 天或最多 2000 行（prune 清 3 天的；每写 200 行顺手清超量的）。
接口：仅管理员（群友 403、匿名 401）。
"""

from __future__ import annotations

import json

import aiohttp
import httpx
import pytest
import pytest_asyncio

from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.models import Models
from CharTyr_MaiWork.maiwork.store import Store

from test_models import SECRET, FakeEndpoint, _settings, _transport

G1 = "900000001"
PASSWORD = "日志测试密码不要打印"


def _make_models(tmp_path, cfg: dict, ep: FakeEndpoint):
    store = Store(tmp_path / "test.db")
    store.migrate()
    settings = _settings(cfg)
    models = Models(store, lambda: settings, transport=_transport(ep))
    return store, models


def _call_rows(store: Store) -> list[dict]:
    rows = store.read().execute("SELECT * FROM model_calls ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _cfg(**models_over):
    data = {
        "base_url": "https://api.test/v1",
        "api_key": SECRET,
        "main": "m1",
        "main_backup": "m-bak",
        "worker": "w",
        "retries": 0,
    }
    data.update(models_over)
    return {"models": data}


# ----------------------------------------------------------------------
# 每次尝试写一行
# ----------------------------------------------------------------------


class TestOneRowPerAttempt:
    @pytest.mark.asyncio
    async def test_success_row(self, tmp_path) -> None:
        ep = FakeEndpoint({"m1": [{"kind": "ok", "content": "你好", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        await models.chat(
            "main", [{"role": "user", "content": "打个招呼"}],
            purpose="opener", group_id=G1, task_id="T-1",
        )
        rows = _call_rows(store)
        assert len(rows) == 1
        r = rows[0]
        assert r["ok"] == 1
        assert r["attempt"] == 1
        assert r["status"] == 200
        assert r["model"] == "m1"
        assert r["role"] == "main"
        assert r["purpose"] == "opener"
        assert r["group_id"] == G1
        assert r["task_id"] == "T-1"
        assert r["prompt_tokens"] == 3 and r["completion_tokens"] == 2
        assert r["error"] == ""
        assert r["ms"] >= 0 and r["ts"] > 0
        request = json.loads(r["request"])
        assert request["messages"][0]["content"] == "打个招呼"
        assert request["json_mode"] is False
        assert request["tools"] == []
        assert request["max_tokens"] == 32768  # 这次请求实际带的输出上限（没传用设置默认）
        response = json.loads(r["response"])
        assert response["text"] == "你好"
        assert response["finish_reason"] == "stop"
        await models.close()

    @pytest.mark.asyncio
    async def test_attempt_increments_and_status_zero_on_network_error(self, tmp_path) -> None:
        """retries=1：主模型两次网络错误 → attempt 1/2，status=0，再换备用 attempt 仍递增。"""
        ep = FakeEndpoint({"m1": [{"kind": "timeout"}], "m-bak": [{"kind": "ok", "content": "备用接住"}]})
        store, models = _make_models(tmp_path, _cfg(retries=1), ep)
        r = await models.chat("main", [{"role": "user", "content": "x"}], timeout=5)
        assert r.text == "备用接住"
        rows = _call_rows(store)
        assert len(rows) == 3
        assert [row["attempt"] for row in rows] == [1, 2, 3]
        assert [row["model"] for row in rows] == ["m1", "m1", "m-bak"]
        assert [row["status"] for row in rows] == [0, 0, 200]
        assert rows[0]["error"]
        await models.close()

    @pytest.mark.asyncio
    async def test_4xx_row_has_status(self, tmp_path) -> None:
        ep = FakeEndpoint({"m1": [{"kind": "status", "status": 400, "body": {"error": {"message": "请求坏"}}}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        with pytest.raises(Exception):
            await models.chat("main", [{"role": "user", "content": "x"}])
        rows = _call_rows(store)
        assert len(rows) == 1
        assert rows[0]["ok"] == 0 and rows[0]["status"] == 400
        await models.close()


# ----------------------------------------------------------------------
# 密钥只进不出
# ----------------------------------------------------------------------


class TestNoSecretsInLog:
    @pytest.mark.asyncio
    async def test_secret_in_content_and_response_redacted(self, tmp_path) -> None:
        """请求/响应里混入密钥都要遮：已知密钥 + Bearer + sk- 形式。"""
        leaked = f"key 是 {SECRET}，还有 Bearer abcdef0123456789 和 sk-live999888"
        ep = FakeEndpoint({"m1": [{"kind": "ok", "content": f"回答也不该带 {SECRET}"}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        await models.chat(
            "main",
            [{"role": "user", "content": leaked}],
            tools=[{"type": "function", "function": {"name": "run_cmd", "parameters": {"type": "object"}}}],
            json_mode=True,
        )
        rows = _call_rows(store)
        assert len(rows) == 1
        whole = rows[0]["request"] + rows[0]["response"] + rows[0]["error"]
        assert SECRET not in whole
        assert "abcdef0123456789" not in whole
        assert "sk-live999888" not in whole
        assert "sk-***" in whole
        assert "Bearer ***" in whole
        # 工具只存名字；json_mode 记下
        request = json.loads(rows[0]["request"])
        assert request["tools"] == ["run_cmd"]
        assert request["json_mode"] is True
        await models.close()

    @pytest.mark.asyncio
    async def test_headers_never_stored(self, tmp_path) -> None:
        """请求的 Authorization 头绝不入库（结构里只有 messages/tools/json_mode）。"""
        ep = FakeEndpoint({"m1": [{"kind": "ok"}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        await models.chat("main", [{"role": "user", "content": "x"}])
        row = _call_rows(store)[0]
        assert SECRET not in row["request"]
        assert "uthorization" not in row["request"]  # Authorization 这个词都不该出现
        request = json.loads(row["request"])
        assert sorted(request.keys()) == ["json_mode", "max_tokens", "messages", "tools"]
        await models.close()

    @pytest.mark.asyncio
    async def test_caller_max_tokens_logged(self, tmp_path) -> None:
        """调用方传的 max_tokens 以它为准，记录里也是它。"""
        ep = FakeEndpoint({"m1": [{"kind": "ok"}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        await models.chat("main", [{"role": "user", "content": "x"}], max_tokens=5000)
        request = json.loads(_call_rows(store)[0]["request"])
        assert request["max_tokens"] == 5000
        await models.close()

    @pytest.mark.asyncio
    async def test_tool_calls_in_messages_and_response(self, tmp_path) -> None:
        """assistant 的 tool_calls 消息：只记名字+参数（截 500）；响应 tool_calls 同。"""
        tc = {"id": "call_1", "type": "function",
              "function": {"name": "run_cmd", "arguments": '{"cmd": "cat ' + "x" * 600 + '"}'}}
        resp_tc = {"id": "call_2", "type": "function",
                   "function": {"name": "write_file", "arguments": '{"path": "/a", "content": "' + "y" * 1500 + '"}'}}
        ep = FakeEndpoint({"m1": [{"kind": "ok", "content": None, "tool": resp_tc}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        messages = [
            {"role": "user", "content": "干活"},
            {"role": "assistant", "content": "", "tool_calls": [tc]},
            {"role": "tool", "tool_call_id": "call_1", "name": "run_cmd", "content": "done"},
        ]
        await models.chat("main", messages)
        row = _call_rows(store)[0]
        request = json.loads(row["request"])
        assert len(request["messages"]) == 3
        tcs = request["messages"][1]["tool_calls"]
        assert len(tcs) == 1 and tcs[0]["name"] == "run_cmd" and len(tcs[0]["arguments"]) == 500
        assert "y" not in tcs[0]["arguments"]
        assert request["messages"][2]["tool_call_id"] == "call_1"
        response = json.loads(row["response"])
        assert len(response["tool_calls"]) == 1
        assert response["tool_calls"][0]["name"] == "write_file"
        assert len(response["tool_calls"][0]["arguments"]) == 1000
        await models.close()


# ----------------------------------------------------------------------
# 截断
# ----------------------------------------------------------------------


class TestTruncation:
    @pytest.mark.asyncio
    async def test_long_truncate(self, tmp_path) -> None:
        long_text = "啊" * 5000
        ep = FakeEndpoint({"m1": [{"kind": "ok", "content": "回" * 21000}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        await models.chat("main", [{"role": "user", "content": long_text}])
        row = _call_rows(store)[0]
        request = json.loads(row["request"])
        assert len(request["messages"][0]["content"]) == 4000
        response = json.loads(row["response"])
        assert len(response["text"]) == 20000
        await models.close()

    @pytest.mark.asyncio
    async def test_request_json_capped_80kb(self, tmp_path) -> None:
        messages = [{"role": "user", "content": "包" * 4000} for _ in range(30)]
        ep = FakeEndpoint({"m1": [{"kind": "ok"}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        await models.chat("main", messages)
        row = _call_rows(store)[0]
        assert len(row["request"]) <= 80 * 1024
        await models.close()

    @pytest.mark.asyncio
    async def test_error_capped_1000(self, tmp_path) -> None:
        big = "错" * 5000
        ep = FakeEndpoint({"m1": [{"kind": "status", "status": 400, "body": big}]})
        store, models = _make_models(tmp_path, _cfg(), ep)
        with pytest.raises(Exception):
            await models.chat("main", [{"role": "user", "content": "x"}])
        row = _call_rows(store)[0]
        assert 0 < len(row["error"]) <= 1000
        await models.close()


# ----------------------------------------------------------------------
# 保留清理
# ----------------------------------------------------------------------


class TestRetention:
    def _seed(self, store: Store, n: int, ts: float) -> None:
        with store.tx() as conn:
            for i in range(n):
                conn.execute(
                    "INSERT INTO model_calls (ts, purpose, role, model, group_id, task_id, attempt, ok,"
                    " status, ms, prompt_tokens, completion_tokens, error, request, response)"
                    " VALUES (?, '', 'main', 'm1', '', '', 1, 1, 200, 1, 0, 0, '', '{}', '{}')",
                    (float(ts),),
                )

    def test_prune_deletes_older_than_3_days(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        now = 1_900_000_000.0
        self._seed(store, 2, now - 4 * 86400)
        self._seed(store, 3, now - 3600)
        counts = store.prune(now)
        assert counts.get("model_calls") == 2
        assert len(_call_rows(store)) == 3

    def test_prune_caps_row_count_at_2000(self, tmp_path) -> None:
        store = Store(tmp_path / "t.db")
        store.migrate()
        now = 1_900_000_000.0
        self._seed(store, 2010, now - 3600)
        counts = store.prune(now)
        assert counts.get("model_calls") >= 10
        assert len(_call_rows(store)) == 2000

    @pytest.mark.asyncio
    async def test_write_side_cleanup_every_200_rows(self, tmp_path) -> None:
        """chat 里每写 200 行顺手清一次超量（等不了每天的 prune 就把日志库撑爆）。

        满 2000 行后再写：第 200 次写入（COUNT % 200 == 0）触发顺手清，清回 2000 行。
        """
        store = Store(tmp_path / "t.db")
        store.migrate()
        self._seed(store, 2000, clock.now() - 100)
        ep = FakeEndpoint({"m1": [{"kind": "ok"}]})
        settings = _settings(_cfg())
        models = Models(store, lambda: settings, transport=_transport(ep))
        # 直接走模型里写日志那条路（不走 HTTP，快）：连写 200 行
        api_key = "k"
        for i in range(200):
            models._log_attempt(
                "m1", "main", 1, ok=True, status=200, ms=1,
                prompt_tokens=0, completion_tokens=0, error="",
                request="{}", response="{}", keys=[api_key],
                purpose="", group_id="", task_id="",
            )
        assert len(_call_rows(store)) == 2000, "每写 200 行顺手清一次超量后应留回 2000 行"
        await models.close()


# ----------------------------------------------------------------------
# HTTP 接口（仅管理员）
# ----------------------------------------------------------------------


class _AppSvc:
    """只带 console 必需的东西：store / get_settings / models。"""

    def __init__(self, store: Store, settings, models) -> None:
        self.store = store
        self.get_settings = lambda: settings
        self.models = models
        self.host = None
        self.profiles = None
        self.scheduler = None
        self.jev = None
        self.signals = None


@pytest_asyncio.fixture
async def logs_client(tmp_path):
    """起真 console（假 svc），库里有预置的 model_calls / tool_calls 数据。"""
    from CharTyr_MaiWork.maiwork.console.server import ConsoleServer

    store = Store(tmp_path / "t.db")
    store.migrate()
    now = clock.now()
    with store.tx() as conn:
        # 两个群 + 一条模型调用（归属 G1），一条失败的（归属其他群）
        conn.execute("INSERT INTO groups (group_id, name, token, created) VALUES (?, ?, ?, ?)", (G1, "测试群一", "tok1", now))
        conn.execute("INSERT INTO groups (group_id, name, token, created) VALUES (?, ?, ?, ?)", ("777", "测试群二", "tok2", now))
        conn.execute(
            "INSERT INTO secrets (name, value, updated) VALUES ('model_api_key', ?, ?)", (SECRET, now)
        )
        conn.execute(
            "INSERT INTO model_calls (ts, purpose, role, model, group_id, task_id, attempt, ok, status, ms,"
            " prompt_tokens, completion_tokens, error, request, response)"
            " VALUES (?, 'opener', 'main', 'm1', ?, 'T-1', 1, 1, 200, 123, 12, 5, '', ?, ?)",
            (now - 60, G1, json.dumps({"messages": [{"role": "user", "content": f"开个话题 key={SECRET}"}], "tools": [], "json_mode": False}, ensure_ascii=False),
             json.dumps({"text": "大家好", "tool_calls": [], "finish_reason": "stop"}, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO model_calls (ts, purpose, role, model, group_id, task_id, attempt, ok, status, ms,"
            " prompt_tokens, completion_tokens, error, request, response)"
            " VALUES (?, 'coordinator.plan', 'main', 'm1', ?, 'T-2', 2, 0, 500, 77, 0, 0, '端点出错', '{}', '{}')",
            (now - 30, "777"),
        )
        conn.execute(
            "INSERT INTO model_calls (ts, purpose, role, model, group_id, task_id, attempt, ok, status, ms,"
            " prompt_tokens, completion_tokens, error, request, response)"
            " VALUES (?, 'weird.purpose', 'worker', 'w', ?, '', 1, 1, 200, 10, 3, 4, '', '{}', '{}')",
            (now - 10, G1),
        )
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
            " VALUES (?, ?, 'T-1', 'worker', 'run_cmd', '短输入', '短输出', 5, 1, '')",
            (now - 50, G1),
        )
        long_input = "入" * 400
        long_output = "出" * 400
        conn.execute(
            "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
            " VALUES (?, ?, 'T-2', 'worker', 'write_file', ?, ?, 9, 0, '写坏了')",
            (now - 40, "777", long_input, long_output),
        )
    settings = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": "qq:777"}]},
            "console": {"listen": "127.0.0.1:18650", "password": PASSWORD},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
    )[0]
    models = Models(store, lambda: settings, transport=_transport(FakeEndpoint({})))
    svc = _AppSvc(store, settings, models)
    server = ConsoleServer(svc)
    test_client = TestClient(TestServer(server.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await test_client.start_server()
    yield test_client, store
    await test_client.close()


async def _login(client: TestClient) -> None:
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


class TestModelCallsApi:
    @pytest.mark.asyncio
    async def test_anonymous_401_member_403(self, logs_client, monkeypatch) -> None:
        client, _ = logs_client
        from CharTyr_MaiWork.maiwork.console import views as _views

        monkeypatch.setattr(_views, "group_id_by_token", lambda _svc, token: G1 if token == "tok-member" else None)
        for path in ("/api/logs/model-calls", "/api/logs/model-calls/1", "/api/logs/tool-calls", "/api/logs/tool-calls/1", "/api/logs/summary"):
            r = await client.get(path)
            assert r.status == 401, (path, r.status)
            r = await client.get(path, headers={"X-MW-Group": "tok-member"})
            assert r.status == 403, (path, r.status)

    @pytest.mark.asyncio
    async def test_list_newest_first_and_fields(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/model-calls")
        assert r.status == 200
        data = await r.json()
        assert "items" in data and "next_before_id" in data
        items = data["items"]
        assert len(items) == 3
        ids = [it["id"] for it in items]
        assert ids == sorted(ids, reverse=True)
        it = items[2]  # 最早那条（G1 opener 成功）
        for k in ("id", "ts", "purpose", "role", "model", "group_id", "group_name",
                  "task_id", "attempt", "ok", "status", "ms", "prompt_tokens", "completion_tokens", "error"):
            assert k in it, k
        assert it["group_name"] == "测试群一"
        assert it["purpose_name"] == "开场白"
        # 列表不带 request/response 全文
        assert "request" not in it and "response" not in it
        # 未知 purpose 显示原名
        assert items[0]["purpose_name"] == "weird.purpose"

    @pytest.mark.asyncio
    async def test_filters(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/model-calls?failed=1")
        assert len((await r.json())["items"]) == 1
        r = await client.get(f"/api/logs/model-calls?failed=0")
        assert len((await r.json())["items"]) == 2
        r = await client.get("/api/logs/model-calls?purpose=coordinator.plan")
        items = (await r.json())["items"]
        assert len(items) == 1 and items[0]["purpose"] == "coordinator.plan"
        r = await client.get(f"/api/logs/model-calls?group={G1}")
        items = (await r.json())["items"]
        assert len(items) == 2
        assert all(it["group_id"] == G1 for it in items)

    @pytest.mark.asyncio
    async def test_detail_with_request_response(self, logs_client) -> None:
        client, store = logs_client
        await _login(client)
        first = store.read().execute("SELECT id FROM model_calls WHERE purpose='opener'").fetchone()
        r = await client.get(f"/api/logs/model-calls/{first['id']}")
        assert r.status == 200
        data = await r.json()
        assert data["request"]["messages"][0]["role"] == "user"
        assert data["response"]["text"] == "大家好"
        assert data["purpose_name"] == "开场白"
        # 密钥绝不出接口（库里这条的 content 就带密钥）
        dumped = json.dumps(data, ensure_ascii=False)
        assert SECRET not in dumped

    @pytest.mark.asyncio
    async def test_detail_404(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/model-calls/999999")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_pagination(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/model-calls?limit=2")
        data = await r.json()
        assert len(data["items"]) == 2
        assert data["next_before_id"] is not None
        r2 = await client.get(f"/api/logs/model-calls?limit=2&before_id={data['next_before_id']}")
        data2 = await r2.json()
        assert len(data2["items"]) == 1
        assert data2["next_before_id"] is None
        ids = [it["id"] for it in data["items"]] + [it["id"] for it in data2["items"]]
        assert ids == sorted(ids, reverse=True)
        # limit 上限 200
        r3 = await client.get("/api/logs/model-calls?limit=500")
        assert r3.status == 200

    @pytest.mark.asyncio
    async def test_purpose_map_covers_known(self) -> None:
        """views.PURPOSE_NAMES 把代码里实际出现的 purpose 全列上。"""
        from CharTyr_MaiWork.maiwork.console.views import PURPOSE_NAMES
        for p in ("profile.refresh", "feeds.focus", "feeds.score", "feeds.post", "feeds.idea",
                  "feeds.verify_plan", "worker", "coordinator.plan", "coordinator.review",
                  "persona.refresh", "personal.focus", "opener"):
            assert p in PURPOSE_NAMES, p


class TestToolCallsApi:
    @pytest.mark.asyncio
    async def test_list_and_name(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/tool-calls")
        assert r.status == 200
        data = await r.json()
        assert len(data["items"]) == 2
        it = data["items"][0]
        for k in ("id", "ts", "group_id", "group_name", "task_id", "actor", "tool", "ok", "ms", "input", "output", "error"):
            assert k in it, k
        assert it["group_name"] == "测试群二"
        # 列表里 input/output 截 300
        assert len(it["input"]) == 300 and len(it["output"]) == 300

    @pytest.mark.asyncio
    async def test_list_failed_filter(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/tool-calls?failed=1")
        data = await r.json()
        assert len(data["items"]) == 1 and data["items"][0]["ok"] == 0

    @pytest.mark.asyncio
    async def test_detail_full(self, logs_client) -> None:
        client, store = logs_client
        await _login(client)
        row = store.read().execute("SELECT id FROM tool_calls WHERE ok=0").fetchone()
        r = await client.get(f"/api/logs/tool-calls/{row['id']}")
        assert r.status == 200
        data = await r.json()
        assert len(data["input"]) == 400 and len(data["output"]) == 400
        assert data["error"] == "写坏了"

    @pytest.mark.asyncio
    async def test_detail_404(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/tool-calls/999999")
        assert r.status == 404


class TestSummaryApi:
    @pytest.mark.asyncio
    async def test_summary_shape(self, logs_client) -> None:
        client, _ = logs_client
        await _login(client)
        r = await client.get("/api/logs/summary")
        assert r.status == 200
        data = await r.json()
        assert set(data["today"].keys()) == {"calls", "failed", "retried", "tokens"}
        assert data["today"]["calls"] == 3
        assert data["today"]["failed"] == 1
        assert data["today"]["retried"] == 1  # attempt=2 那次
        assert data["today"]["tokens"] == 12 + 5 + 3 + 4
        lf = data["last_failure"]
        assert lf is not None
        assert lf["purpose"] == "coordinator.plan" and lf["model"] == "m1"
        assert lf["error"] == "端点出错" and lf["ts"] > 0
        by = data["by_purpose"]
        assert isinstance(by, list) and len(by) == 3
        plan = next(b for b in by if b["purpose"] == "coordinator.plan")
        assert plan["calls"] == 1 and plan["failed"] == 1 and plan["avg_ms"] == 77
        opener = next(b for b in by if b["purpose"] == "opener")
        assert opener["calls"] == 1 and opener["failed"] == 0 and opener["avg_ms"] == 123
