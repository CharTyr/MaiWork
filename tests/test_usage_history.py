"""用量历史：GET /api/usage/history（管理员）。

数据源只有 MaiWork 自己的库：
- `usage`（models.py 每次模型尝试一行：ts/day/role/model/purpose/group_id/prompt_tokens/
  completion_tokens/ok/ms）；
- `judgments`（jev.py 每次 Jev 调用一行）。
「天」一律是北京时间的 day_key（clock.day_key），范围内没数据的天补 0。

起 app + TestClient 的写法照 tests/test_console.py / tests/test_model_logs.py：
真 ConsoleServer + 假 svc（只要有 store / get_settings），登录走真的 POST /api/login。

覆盖：days 默认 7 与夹到 1..30、date 单日、坏 date 400 中文、未登录 401 / 群友 403、
补 0 天、by_hour 长度 24、非服务群和空 group_id 归到「其他」。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.store import Store

G1 = "900000001"     # 服务群一
G2 = "123456789"     # 服务群二
OTHER = "555555555"  # 配置里没有的群
TOKEN_G1 = "tok-g1"
PASSWORD = "用量历史测试密码-不要出现在日志里"


def _day(offset: int) -> str:
    """今天（北京时间）往前 offset 天的 day_key；offset=0 就是今天。"""
    return (clock.bj(clock.now()).date() - timedelta(days=offset)).isoformat()


def _ts(day: str, hour: int) -> float:
    """某天（北京）整点的 epoch 秒。"""
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, 0, tzinfo=clock.BJ).timestamp()


def _usage(
    store: Store,
    *,
    day: str,
    hour: int,
    role: str,
    model: str,
    purpose: str,
    group_id: str,
    prompt: int,
    completion: int,
    ok: int = 1,
    ms: int = 0,
) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO usage (ts, day, role, model, purpose, group_id, task_id,"
            " prompt_tokens, completion_tokens, ok, ms, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_ts(day, hour), day, role, model, purpose, group_id, "", prompt, completion, ok, ms,
             "" if ok else "端点出错"),
        )


def _judgment(store: Store, *, day: str, hour: int, ok: int = 1, ms: int = 0) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO judgments (ts, day, purpose, group_id, state_summary, answers, ms, ok, error)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_ts(day, hour), day, "chat.verdict", "", "{}", "{}", ms, ok, "" if ok else "Jev 挂了"),
        )


class _AppSvc:
    """console 需要的最小集合：store / get_settings（照 test_model_logs.py 的 _AppSvc）。"""

    def __init__(self, store: Store, settings) -> None:
        self.store = store
        self.get_settings = lambda: settings
        self.models = None
        self.host = None
        self.profiles = None
        self.scheduler = None
        self.jev = None
        self.signals = None


@pytest_asyncio.fixture
async def usage_client(tmp_path):
    """真 console + 预置 fake 数据：d2（前天）、d1（昨天）、今天、d_old（10 天前）。"""
    from CharTyr_MaiWork.maiwork.console.server import ConsoleServer

    store = Store(tmp_path / "t.db")
    store.migrate()
    now = clock.now()
    today, d1, d2, d_old = _day(0), _day(1), _day(2), _day(10)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, name, token, created) VALUES (?,?,?,?)",
            (G1, "测试群一", TOKEN_G1, now),
        )
        conn.execute(
            "INSERT INTO groups (group_id, name, token, created) VALUES (?,?,?,?)",
            (G2, "测试群二", "tok-g2", now),
        )
    # d2：主模型成功 150 + 子 agent 15 + 非服务群主模型失败 10
    _usage(store, day=d2, hour=3, role="main", model="main-m", purpose="feeds.score",
           group_id=G1, prompt=100, completion=50, ms=100)
    _usage(store, day=d2, hour=15, role="worker", model="worker-m", purpose="worker",
           group_id=G1, prompt=10, completion=5, ms=40)
    _usage(store, day=d2, hour=15, role="main", model="main-m", purpose="feeds.score",
           group_id=OTHER, prompt=7, completion=3, ok=0, ms=300)
    # d1：不属于任何群的调用（空 group_id → 其他）
    _usage(store, day=d1, hour=9, role="main", model="main-m", purpose="opener",
           group_id="", prompt=20, completion=10, ms=60)
    # 今天：G2 的子 agent
    _usage(store, day=today, hour=1, role="worker", model="worker-m", purpose="worker",
           group_id=G2, prompt=15, completion=15, ms=80)
    # 10 天前：默认 7 天窗口外，days=30 才看得到
    _usage(store, day=d_old, hour=12, role="main", model="main-m", purpose="opener",
           group_id=G1, prompt=999, completion=999)
    _judgment(store, day=d2, hour=4, ok=0, ms=100)
    _judgment(store, day=d2, hour=5, ok=1, ms=300)
    _judgment(store, day=today, hour=2, ok=1, ms=50)
    _judgment(store, day=d_old, hour=12, ok=1, ms=10)
    settings = load_settings(
        {
            "plugin": {"enabled": True},
            "groups": {"serve": [{"group": f"qq:{G1}"}, {"group": f"qq:{G2}"}]},
            "console": {"listen": "127.0.0.1:18650", "password": PASSWORD},
            "storage": {"data_dir": str(tmp_path / "data")},
        }
    )[0]
    server = ConsoleServer(_AppSvc(store, settings))
    client = TestClient(TestServer(server.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield client
    await client.close()


async def _login(client: TestClient) -> None:
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status == 200


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------


class TestAuth:
    @pytest.mark.asyncio
    async def test_anonymous_401(self, usage_client: TestClient) -> None:
        r = await usage_client.get("/api/usage/history")
        assert r.status == 401
        assert (await r.json())["error"]

    @pytest.mark.asyncio
    async def test_member_403(self, usage_client: TestClient) -> None:
        r = await usage_client.get("/api/usage/history", headers={"X-MW-Group": TOKEN_G1})
        assert r.status == 403


# ----------------------------------------------------------------------
# days 模式
# ----------------------------------------------------------------------


class TestDaysMode:
    @pytest.mark.asyncio
    async def test_default_7_days_zero_filled(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        r = await usage_client.get("/api/usage/history")
        assert r.status == 200
        data = await r.json()
        assert data["range"] == {"from": _day(6), "to": _day(0)}
        days = data["days"]
        # 范围内每天一条、按日期升序、没数据的天补 0
        assert [d["day"] for d in days] == [_day(i) for i in range(6, -1, -1)]
        assert all(
            set(d.keys()) == {"day", "main", "worker", "calls", "errors", "jev"} for d in days
        )
        by = {d["day"]: d for d in days}
        assert by[_day(2)] == {"day": _day(2), "main": 160, "worker": 15, "calls": 3, "errors": 1, "jev": 2}
        assert by[_day(1)] == {"day": _day(1), "main": 30, "worker": 0, "calls": 1, "errors": 0, "jev": 0}
        assert by[_day(0)] == {"day": _day(0), "main": 0, "worker": 30, "calls": 1, "errors": 0, "jev": 1}
        assert by[_day(5)] == {"day": _day(5), "main": 0, "worker": 0, "calls": 0, "errors": 0, "jev": 0}
        assert by[_day(6)]["calls"] == 0
        assert _day(10) not in by  # 7 天窗口外
        assert data["totals"] == {
            "main": 190, "worker": 45, "calls": 5, "errors": 1, "jev": 3,
            "prompt_tokens": 152, "completion_tokens": 83,
        }

    @pytest.mark.asyncio
    async def test_days_clamped_1_to_30(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        # days=0 → 夹到 1（只看今天）
        data = await (await usage_client.get("/api/usage/history?days=0")).json()
        assert data["range"] == {"from": _day(0), "to": _day(0)}
        assert len(data["days"]) == 1
        assert data["totals"]["main"] == 0 and data["totals"]["worker"] == 30
        # days=-5 → 夹到 1
        data = await (await usage_client.get("/api/usage/history?days=-5")).json()
        assert len(data["days"]) == 1
        # days=999 → 夹到 30，10 天前那笔算进来
        data = await (await usage_client.get("/api/usage/history?days=999")).json()
        assert len(data["days"]) == 30
        assert data["range"] == {"from": _day(29), "to": _day(0)}
        assert data["days"][0]["day"] == _day(29) and data["days"][-1]["day"] == _day(0)
        assert data["totals"]["main"] == 190 + 1998
        assert data["totals"]["calls"] == 6 and data["totals"]["jev"] == 4

    @pytest.mark.asyncio
    async def test_days_not_a_number_falls_back_to_default(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        data = await (await usage_client.get("/api/usage/history?days=abc")).json()
        assert data["range"] == {"from": _day(6), "to": _day(0)}
        assert len(data["days"]) == 7


# ----------------------------------------------------------------------
# date 模式
# ----------------------------------------------------------------------


class TestDateMode:
    @pytest.mark.asyncio
    async def test_single_day(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        r = await usage_client.get(f"/api/usage/history?date={_day(2)}")
        assert r.status == 200
        data = await r.json()
        assert data["day"] == _day(2)
        assert data["range"] == {"from": _day(2), "to": _day(2)}
        assert [d["day"] for d in data["days"]] == [_day(2)]
        assert data["days"][0] == {
            "day": _day(2), "main": 160, "worker": 15, "calls": 3, "errors": 1, "jev": 2
        }
        assert data["totals"] == {
            "main": 160, "worker": 15, "calls": 3, "errors": 1, "jev": 2,
            "prompt_tokens": 117, "completion_tokens": 58,
        }
        # Jev：顶层 jev 是整数（当天的次数），汇总在 jev_stats
        assert data["jev"] == 2
        assert data["jev_stats"] == {"calls": 2, "errors": 1, "avg_ms": 200}

    @pytest.mark.asyncio
    async def test_single_day_breakdowns(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        data = await (await usage_client.get(f"/api/usage/history?date={_day(2)}")).json()
        assert data["by_model"] == [
            {"model": "main-m", "role": "main", "calls": 2, "prompt": 107, "completion": 53,
             "tokens": 160, "errors": 1, "avg_ms": 200},
            {"model": "worker-m", "role": "worker", "calls": 1, "prompt": 10, "completion": 5,
             "tokens": 15, "errors": 0, "avg_ms": 40},
        ]
        assert data["by_purpose"] == [
            {"purpose": "资讯打分", "calls": 2, "tokens": 160},
            {"purpose": "子 agent", "calls": 1, "tokens": 15},
        ]
        assert data["by_group"] == [
            {"group_id": G1, "name": "测试群一", "calls": 2, "tokens": 165},
            {"group_id": "", "name": "其他", "calls": 1, "tokens": 10},
        ]
        hours = data["by_hour"]
        assert len(hours) == 24 and all(isinstance(x, int) for x in hours)
        assert hours[3] == 150 and hours[15] == 25 and sum(hours) == 175

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["2026-13-01", "abc", "2026-02-30", "20260901", "2026-9-1"])
    async def test_bad_date_400(self, usage_client: TestClient, bad: str) -> None:
        await _login(usage_client)
        r = await usage_client.get(f"/api/usage/history?date={bad}")
        assert r.status == 400
        assert "日期" in (await r.json())["error"]


# ----------------------------------------------------------------------
# 分类明细
# ----------------------------------------------------------------------


class TestBreakdowns:
    @pytest.mark.asyncio
    async def test_by_group_only_served_plus_other(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        data = await (await usage_client.get("/api/usage/history")).json()
        assert data["by_group"] == [
            {"group_id": G1, "name": "测试群一", "calls": 2, "tokens": 165},
            {"group_id": "", "name": "其他", "calls": 2, "tokens": 40},
            {"group_id": G2, "name": "测试群二", "calls": 1, "tokens": 30},
        ]
        # 非服务群（OTHER）自己的群号不出现
        assert OTHER not in [g["group_id"] for g in data["by_group"]]

    @pytest.mark.asyncio
    async def test_by_hour_is_24_ints(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        data = await (await usage_client.get("/api/usage/history")).json()
        hours = data["by_hour"]
        assert len(hours) == 24
        assert all(isinstance(x, int) for x in hours)
        assert hours[0] == 0
        assert hours[1] == 30 and hours[3] == 150 and hours[9] == 30 and hours[15] == 25
        assert sum(hours) == 235

    @pytest.mark.asyncio
    async def test_by_model_and_purpose_sorted_by_tokens(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        data = await (await usage_client.get("/api/usage/history")).json()
        assert [m["model"] for m in data["by_model"]] == ["main-m", "worker-m"]
        mm = data["by_model"][0]
        assert set(mm.keys()) == {
            "model", "role", "calls", "prompt", "completion", "tokens", "errors", "avg_ms"
        }
        assert mm["role"] == "main" and mm["calls"] == 3 and mm["errors"] == 1
        assert mm["prompt"] == 127 and mm["completion"] == 63 and mm["tokens"] == 190
        assert mm["avg_ms"] == 153  # (100+300+60)/3 = 153.33 → 153
        assert data["by_purpose"] == [
            {"purpose": "资讯打分", "calls": 2, "tokens": 160},
            {"purpose": "子 agent", "calls": 2, "tokens": 45},
            {"purpose": "开场白", "calls": 1, "tokens": 30},
        ]
        assert data["jev"] == 3
        assert data["jev_stats"] == {"calls": 3, "errors": 1, "avg_ms": 150}

    @pytest.mark.asyncio
    async def test_empty_range_is_all_zeros(self, usage_client: TestClient) -> None:
        """window 里没有任何数据也不许 500：全 0、by_hour 仍是 24 个 0。"""
        await _login(usage_client)
        # 用一个绝对没数据的旧日期，确认全 0 不炸
        data = await (await usage_client.get("/api/usage/history?date=2020-01-01")).json()
        assert data["days"] == [
            {"day": "2020-01-01", "main": 0, "worker": 0, "calls": 0, "errors": 0, "jev": 0}
        ]
        assert data["totals"] == {
            "main": 0, "worker": 0, "calls": 0, "errors": 0, "jev": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
        }
        assert data["by_model"] == [] and data["by_purpose"] == [] and data["by_group"] == []
        assert data["by_hour"] == [0] * 24
        assert data["jev_stats"] == {"calls": 0, "errors": 0, "avg_ms": 0}

    @pytest.mark.asyncio
    async def test_days_and_date_together_date_wins(self, usage_client: TestClient) -> None:
        await _login(usage_client)
        data = await (await usage_client.get(f"/api/usage/history?days=30&date={_day(2)}")).json()
        assert data["day"] == _day(2)
        assert len(data["days"]) == 1
