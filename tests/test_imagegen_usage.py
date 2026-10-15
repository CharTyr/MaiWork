"""生图用量（2026-10-15 任务 A5）：今天 / 近 7 天调用次数、出图张数、失败次数、最近一次失败原因。

数据源是 `kv["imagegen.calls"]`（tools_imagegen 每次调用记一次）。2026-10-15 起记录格式
多了一个按天的 `days`（向后兼容：老记录没有这个键 → 今天 / 近 7 天算 0，累计照旧能看）。

JSON 字段（给网页用，两个接口同一份）：
    usage.imagegen = {
      "configured": bool,               # 现在配没配生图模型
      "model": str,                     # 当前在用的生图模型显示名（没配就是 ""）
      "model_id": str,                  # 服务商模型名（如 gpt-image-1）
      "today": {"calls": int, "images": int, "failed": int},
      "week":  {"calls": int, "images": int, "failed": int},   # 近 7 天含今天
      "days":  [{"day": "YYYY-MM-DD", "calls": int, "images": int, "failed": int}],  # 近 7 天升序，缺的天补 0
      "total": {"calls": int, "images": int, "failed": int},   # 累计（含老记录）
      "last_failure": {"ts": float, "model": str, "error": str} | null
    }
"""

from __future__ import annotations

import base64
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.console import views
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools
from CharTyr_MaiWork.maiwork.tools_imagegen import KV_CALLS, register_image_tools

from test_console import SimpleEnv, env as console_env  # noqa: F401  （复用真 console 的 fixture）
from test_usage_history import usage_client  # noqa: F401  （复用轻量 /api/usage/history 的 fixture）

KEY = "sk-imagegen-USAGE-DO-NOT-LEAK-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
IMG_ENTRY = {"id": "img1", "endpoint": "default", "model": "gpt-image-1", "name": "生图",
             "use": "image", "image_api": "openai", "image_max_n": 2}


class _Svc:
    """views.imagegen_usage 要的最小集合：store + get_settings。"""

    def __init__(self, store, settings) -> None:
        self.store = store
        self.get_settings = lambda: settings


def _settings(root, model_list=()):
    raw = {
        "endpoints": [{"id": "default", "name": "默认", "protocol": "openai",
                       "base_url": "https://api.test/v1", "api_key": KEY}],
        "model_list": list(model_list),
        "environments": {"local_mode": "direct", "workspace_root": str(root / "ws-root")},
    }
    settings, problems = load_settings(raw)
    assert not problems, problems
    return settings


def _day(offset: int) -> str:
    return (clock.bj(clock.now()).date() - timedelta(days=offset)).isoformat()


def _seed(store: Store, days: dict) -> None:
    with store.tx() as conn:
        store.kv_set(conn, KV_CALLS, days)


# ----------------------------------------------------------------------
# 记录格式（tools_imagegen._bump）
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def img_tools(tmp_path):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(tmp_path, [IMG_ENTRY])
    env = LocalEnv(lambda: settings)
    tools = Tools(store)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"data": [
        {"b64_json": base64.b64encode(PNG).decode()}
    ]}))
    register_image_tools(tools, get_settings=lambda: settings, env=env, store=store,
                         http_transport=transport, resolver=lambda host: ["93.184.216.34"])
    ctx = ToolContext(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker",
                      workspace=env.workspace("ws-1"), artifact_scope=("artifacts/T-1",))
    yield tools, ctx, store, settings
    store.close()


@pytest.mark.asyncio
async def test_bump_records_per_day_counts(img_tools) -> None:
    tools, ctx, store, _s = img_tools
    r = await tools.call("generate_image", {"prompt": "猫"}, ctx)
    assert r.ok, r.error
    stat = store.kv_get(KV_CALLS)
    today = _day(0)
    assert stat["days"][today] == {"calls": 1, "ok": 1, "failed": 0, "images": 1}
    # 累计字段照旧（老网页 / 老测试认这些键）
    assert stat["total"] == 1 and stat["ok"] == 1 and stat["images"] == 1


@pytest.mark.asyncio
async def test_bump_keeps_last_failure_after_a_later_success(img_tools) -> None:
    tools, ctx, store, settings = img_tools
    object.__setattr__(settings.endpoints[0], "api_key", KEY)  # 端点密钥别被改
    boom = httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    tools2 = Tools(store)
    register_image_tools(tools2, get_settings=lambda: settings, env=LocalEnv(lambda: settings),
                         store=store, http_transport=boom,
                         resolver=lambda host: ["93.184.216.34"])
    r = await tools2.call("generate_image", {"prompt": "猫"}, ctx)
    assert not r.ok
    r2 = await tools.call("generate_image", {"prompt": "猫"}, ctx)
    assert r2.ok, r2.error
    stat = store.kv_get(KV_CALLS)
    today = _day(0)
    assert stat["days"][today] == {"calls": 2, "ok": 1, "failed": 1, "images": 1}
    assert stat["last_ok"] is True
    # 最近一次失败原因留着（后面成功了也不清）
    assert stat["last_failure_error"] and "失败" in stat["last_failure_error"]
    assert stat["last_failure_model"] == "gpt-image-1"


# ----------------------------------------------------------------------
# 视图
# ----------------------------------------------------------------------


def test_view_today_week_and_last_failure(tmp_path) -> None:
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed(store, {
        "total": 30, "ok": 27, "failed": 3, "images": 41,
        "days": {
            _day(0): {"calls": 2, "ok": 1, "failed": 1, "images": 3},
            _day(1): {"calls": 1, "ok": 1, "failed": 0, "images": 1},
            _day(6): {"calls": 4, "ok": 4, "failed": 0, "images": 4},
            _day(9): {"calls": 9, "ok": 9, "failed": 0, "images": 9},  # 7 天以外：只进累计
        },
        "last_failure_error": "HTTP 429：请求太频繁",
        "last_failure_model": "gpt-image-1",
        "last_failure_ts": 1700000000.0,
    })
    svc = _Svc(store, _settings(tmp_path, [IMG_ENTRY]))
    out = views.imagegen_usage(svc)
    store.close()

    assert out["configured"] is True
    assert out["model"] == "生图" and out["model_id"] == "gpt-image-1"
    assert out["today"] == {"calls": 2, "images": 3, "failed": 1}
    assert out["week"] == {"calls": 7, "images": 8, "failed": 1}
    assert [d["day"] for d in out["days"]] == [_day(6 - i) for i in range(7)]
    assert out["days"][-1] == {"day": _day(0), "calls": 2, "images": 3, "failed": 1}
    assert out["days"][0] == {"day": _day(6), "calls": 4, "images": 4, "failed": 0}
    assert out["total"] == {"calls": 30, "images": 41, "failed": 3}
    assert out["last_failure"] == {"ts": 1700000000.0, "model": "gpt-image-1",
                                   "error": "HTTP 429：请求太频繁"}


def test_view_is_backwards_compatible_with_old_record(tmp_path) -> None:
    """老记录（没有 days / last_failure_*）：不炸，今天 / 近 7 天算 0，累计照旧。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed(store, {"total": 5, "ok": 4, "failed": 1, "images": 6,
                  "last_ts": 1.0, "last_ok": False, "last_model": "old",
                  "last_error": "老记录里的失败"})
    svc = _Svc(store, _settings(tmp_path, [IMG_ENTRY]))
    out = views.imagegen_usage(svc)
    store.close()

    assert out["today"] == {"calls": 0, "images": 0, "failed": 0}
    assert out["week"] == {"calls": 0, "images": 0, "failed": 0}
    assert out["total"] == {"calls": 5, "images": 6, "failed": 1}
    # 老记录只有 last_error（最后一次调用是失败的）→ 当成最近一次失败原因给出来
    assert out["last_failure"]["error"] == "老记录里的失败"


def test_view_without_any_record(tmp_path) -> None:
    store = Store(tmp_path / "t.db")
    store.migrate()
    svc = _Svc(store, _settings(tmp_path, ()))
    out = views.imagegen_usage(svc)
    store.close()
    assert out["configured"] is False
    assert out["model"] == "" and out["model_id"] == ""
    assert out["today"] == {"calls": 0, "images": 0, "failed": 0}
    assert out["total"] == {"calls": 0, "images": 0, "failed": 0}
    assert out["last_failure"] is None


def test_view_survives_broken_record(tmp_path) -> None:
    """kv 里是坏数据（不是 dict）也不许 500。"""
    store = Store(tmp_path / "t.db")
    store.migrate()
    _seed(store, "坏数据")
    svc = _Svc(store, _settings(tmp_path, ()))
    out = views.imagegen_usage(svc)
    store.close()
    assert out["total"]["calls"] == 0


def test_view_survives_no_store() -> None:
    class _NoStore:
        store = None
        get_settings = staticmethod(lambda: None)

    out = views.imagegen_usage(_NoStore())
    assert out["today"]["calls"] == 0 and out["last_failure"] is None


# ----------------------------------------------------------------------
# 接线：/api/settings 与 /api/usage/history 都带这一节
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_view_carries_imagegen_usage(console_env: SimpleEnv) -> None:
    await console_env.login()
    today = _day(0)
    with console_env.app.store.tx() as conn:
        console_env.app.store.kv_set(conn, KV_CALLS, {
            "total": 2, "ok": 2, "failed": 0, "images": 2,
            "days": {today: {"calls": 2, "ok": 2, "failed": 0, "images": 2}},
        })
    s = await (await console_env.client.get("/api/settings")).json()
    ig = s["usage"]["imagegen"]
    assert set(ig) == {"configured", "model", "model_id", "today", "week", "days", "total",
                       "last_failure"}
    assert ig["today"] == {"calls": 2, "images": 2, "failed": 0}
    assert ig["week"]["calls"] == 2


@pytest.mark.asyncio
async def test_usage_history_carries_imagegen_usage(usage_client) -> None:
    await usage_client.post("/api/login", json={"password": "用量历史测试密码-不要出现在日志里"})
    data = await (await usage_client.get("/api/usage/history")).json()
    ig = data["imagegen"]
    assert set(ig) == {"configured", "model", "model_id", "today", "week", "days", "total",
                       "last_failure"}
    assert len(ig["days"]) == 7
    assert ig["today"] == {"calls": 0, "images": 0, "failed": 0}
