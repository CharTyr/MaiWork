"""资讯卡片的管理员补救：CardPush.resend / recent_cards + 两个网页接口。

用户 2026-10-15 定：
- `POST /api/news/cards/{id}/resend`：只允许 failed / uncertain 的卡片，重新画图
  （有缓存图 card-<id>.png/.jpg 就用缓存），走发件箱；一张卡片每天最多手动重发 1 次。
- `GET /api/news/cards?group_id=&status=`：列出最近失败 / 不确定的卡片（id、群、时间、
  状态、error、条目标题），给网页做补救入口。
两个接口都只给总管理员（_require_admin）。

第一段是数据层（真 Store + 真 Outbox + 假渲染器 / 假 host），第二段起真网页服务器。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import card_push
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes
from CharTyr_MaiWork.maiwork.host import HostError
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.store import Store

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "123456789"
PASSWORD = "总管理员密码-不要外传-2468"


def _ts(hour: int, minute: int = 0, *, day: int = 15) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOON = _ts(12)
JPEG = b"\xff\xd8\xff\xe0" + b"card-bytes"


# ----------------------------------------------------------------------
# 数据层夹具
# ----------------------------------------------------------------------


class CardHost:
    def __init__(self) -> None:
        self.images: list[dict] = []
        self.texts: list[dict] = []
        self.image_errors: list[Exception] = []

    async def send_image(self, session_id, data, *, text=""):
        self.images.append({"session_id": session_id, "data": data, "text": text})
        if self.image_errors:
            raise self.image_errors.pop(0)
        return type("R", (), {"sent": True, "message_id": f"img{len(self.images)}"})()

    async def send_text(self, session_id, text, *, reply_to="", at_user="", at_name=""):
        self.texts.append({"session_id": session_id, "text": text})
        return type("R", (), {"sent": True, "message_id": f"t{len(self.texts)}"})()


class Renderer:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    async def __call__(self, data):
        self.calls.append(data)
        if self.error is not None:
            raise self.error
        return JPEG


def _make(tmp_path, *, groups=(GID, OTHER)):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings, _ = load_settings({
        "groups": {"serve": [{"group": f"qq:{g}"} for g in groups]},
        "environments": {"workspace_root": str(tmp_path)},
        "console": {"public_url": "https://mw.example"},
    })
    host = CardHost()
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    renderer = Renderer()
    ob = Outbox(store, host, pushes, mentions, lambda: settings)
    cp = card_push.CardPush(store, host, pushes, mentions, lambda: settings,
                            renderer=renderer, outbox=ob)
    with store.tx() as conn:
        for g in (GID, OTHER):
            conn.execute(
                "INSERT INTO groups (group_id, session_id, name, token) VALUES (?, ?, ?, ?)",
                (g, f"sess-{g}", "测试群", f"tok{g}"),
            )
    return store, settings, host, pushes, mentions, renderer, cp, ob


def _batch(store, gid=GID, *, created=NOON):
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_batches (group_id, slot_ts, found, kept, skipped, note, created)"
            " VALUES (?, ?, 10, 2, 0, '', ?)",
            (gid, created, created),
        )
        bid = int(cur.lastrowid)
        ids = []
        for i, score in enumerate((4.5, 4.0)):
            cur = conn.execute(
                "INSERT INTO news_items (batch_id, group_id, title, summary, why, sources, score, created,"
                " kind, image_url, keywords, target_user_id, rejected)"
                " VALUES (?, ?, ?, '摘要', '值得看', ?, ?, ?, 'news', '', '[]', '', 0)",
                (bid, gid, f"标题{bid}-{i}",
                 json.dumps([{"url": f"https://s.example/{bid}/{i}", "site": "s.example"}]),
                 score, created),
            )
            ids.append(int(cur.lastrowid))
    return bid, ids


def _card(store, gid=GID, *, status="failed", item_ids=(), created=NOON, batch_id=None,
          error="图发不出去"):
    # news_cards.batch_id 是 UNIQUE：不指定就自己挑一个没用过的号
    if batch_id is None:
        row = store.read().execute(
            "SELECT COALESCE(MAX(batch_id), 0) AS m FROM news_cards"
        ).fetchone()
        batch_id = int(row["m"]) + 1
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, due_ts, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gid, int(batch_id), status, json.dumps(list(item_ids)), created, created, error),
        )
        return int(cur.lastrowid)


def _box(store, cid, gid=GID, *, status="failed", payload=None, updated=NOON):
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, result, error,"
            " not_before, created, updated) VALUES (?, ?, 'image', ?, ?, 2, '{}', 'boom', 0, ?, ?)",
            (f"news_card:{cid}", gid,
             json.dumps(payload or {"push_kind": "news_card", "path": "/nope.jpg"}),
             status, updated, updated),
        )


def _enable(store, gid=GID, now=NOON - 3600):
    return card_push.set_config(store, gid, {"news_card_enabled": True}, now=now)


def _cards(store):
    return store.read().execute("SELECT * FROM news_cards ORDER BY id").fetchall()


def _boxes(store):
    return store.read().execute("SELECT * FROM outbox ORDER BY id").fetchall()


# ---------------------------------------------------------------- resend 数据层


async def test_resend_uses_cached_jpeg(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store)
    _, ids = _batch(store)
    cid = _card(store, item_ids=ids)
    _box(store, cid)
    cache = cp._image_path(cid, JPEG)
    cache.write_bytes(JPEG)

    out = await cp.resend(cid, now=NOON)

    assert out["ok"] is True and out["used_cache"] is True and out["mode"] == "image"
    assert out["card_id"] == cid and out["group_id"] == GID
    assert _cards(store)[0]["status"] == "queued"
    box = _boxes(store)[0]
    assert box["status"] == "pending" and int(box["attempts"]) == 3
    payload = json.loads(box["payload"])
    assert str(payload["path"]).endswith(f"card-{cid}.jpg")
    assert payload["push_kind"] == "news_card" and float(payload["expires_ts"]) > NOON
    # 真发出去
    await ob.flush(NOON + 1)
    assert len(host.images) == 1 and host.images[0]["data"] == JPEG
    assert _cards(store)[0]["status"] == "sent"


async def test_resend_rerenders_when_no_cache(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store)
    _, ids = _batch(store)
    cid = _card(store, item_ids=ids)
    _box(store, cid)

    out = await cp.resend(cid, now=NOON)

    assert out["used_cache"] is False and out["mode"] == "image"
    assert len(cp._render.calls) == 1
    path = Path(json.loads(_boxes(store)[0]["payload"])["path"])
    assert path.is_file() and path.name == f"card-{cid}.jpg"


async def test_resend_render_failure_falls_back_to_text(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store)
    _, ids = _batch(store)
    cid = _card(store, item_ids=ids)
    _box(store, cid)
    cp._render.error = HostError("没浏览器")

    out = await cp.resend(cid, now=NOON)

    assert out["mode"] == "text" and out["used_cache"] is False
    payload = json.loads(_boxes(store)[0]["payload"])
    assert "标题1-0" in payload["text"] and "https://mw.example/#/tok900000001/news" in payload["text"]
    await ob.flush(NOON + 1)
    assert len(host.texts) == 1 and host.images == []


async def test_resend_creates_outbox_row_when_missing(tmp_path):
    """入队前就失败的卡片没有 outbox 行：重发要新建一条。"""
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store)
    _, ids = _batch(store)
    cid = _card(store, item_ids=ids)
    assert _boxes(store) == []

    out = await cp.resend(cid, now=NOON)

    assert out["ok"] is True and out["outbox_id"]
    assert len(_boxes(store)) == 1 and _boxes(store)[0]["status"] == "pending"


async def test_resend_only_failed_or_uncertain(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _, ids = _batch(store)
    for status in ("sent", "queued", "dropped", "pending"):
        cid = _card(store, status=status, item_ids=ids)
        with pytest.raises(ValueError):
            await cp.resend(cid, now=NOON)
    # uncertain 允许
    cid = _card(store, status="uncertain", item_ids=ids)
    assert (await cp.resend(cid, now=NOON))["ok"] is True


async def test_resend_unknown_card_raises_keyerror(tmp_path):
    store, *_r, cp, ob = _make(tmp_path)
    with pytest.raises(KeyError):
        await cp.resend(9999, now=NOON)


async def test_resend_at_most_once_per_day(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path)
    _enable(store)
    _, ids = _batch(store)
    cid = _card(store, item_ids=ids)
    _box(store, cid)
    await cp.resend(cid, now=NOON)
    # 又失败了（比如群里还是收不到）：同一天不许再点
    with store.tx() as conn:
        conn.execute("UPDATE news_cards SET status='failed' WHERE id=?", (cid,))
        conn.execute("UPDATE outbox SET status='failed' WHERE key=?", (f"news_card:{cid}",))
    with pytest.raises(ValueError) as e:
        await cp.resend(cid, now=NOON + 3600)
    assert "今天" in str(e.value)
    # 第二天可以
    assert (await cp.resend(cid, now=NOON + 86400))["ok"] is True


async def test_resend_unserved_group_rejected(tmp_path):
    store, _, host, *_r, cp, ob = _make(tmp_path, groups=(GID,))
    cid = _card(store, gid=OTHER, status="failed")
    with pytest.raises(ValueError):
        await cp.resend(cid, now=NOON)


# ---------------------------------------------------------------- recent_cards


async def test_recent_cards_lists_failed_and_uncertain(tmp_path):
    store, *_r, cp, ob = _make(tmp_path)
    _, ids = _batch(store)
    failed = _card(store, item_ids=ids, status="failed", error="图发不出去")
    uncertain = _card(store, item_ids=ids[:1], status="uncertain", error="发送超时", created=NOON + 10)
    _card(store, item_ids=ids, status="sent", created=NOON + 20)
    other = _card(store, gid=OTHER, item_ids=(), status="failed", created=NOON + 30)

    rows = cp.recent_cards(group_id=GID)

    assert [r["id"] for r in rows] == [uncertain, failed]      # 新的在前，sent 不列
    first = rows[0]
    assert first["group_id"] == GID and first["status"] == "uncertain"
    assert first["error"] == "发送超时" and first["count"] == 1
    assert first["titles"] == ["标题1-0"]
    assert first["created"] == NOON + 10
    assert first["group_name"] == "测试群"
    # 不给群号：跨群都列，新的在前
    assert [r["id"] for r in cp.recent_cards()] == [other, uncertain, failed]


async def test_recent_cards_filters_group_and_status(tmp_path):
    store, *_r, cp, ob = _make(tmp_path)
    _, ids = _batch(store)
    _card(store, item_ids=ids, status="failed")
    _card(store, item_ids=ids, status="uncertain", created=NOON + 5)
    _card(store, gid=OTHER, item_ids=(), status="failed", created=NOON + 6)

    assert len(cp.recent_cards()) == 3
    assert len(cp.recent_cards(group_id=OTHER)) == 1
    rows = cp.recent_cards(status="failed")
    assert len(rows) == 2 and all(r["status"] == "failed" for r in rows)
    assert cp.recent_cards(group_id="999") == []


# ----------------------------------------------------------------------
# 网页接口（真 aiohttp 服务器）
# ----------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_PORT: list[int] = []


def _port() -> int:
    if not _PORT:
        _PORT.append(_free_port())
    return _PORT[0]


class SimpleEnv:
    def __init__(self, app: MaiWorkApp, client: TestClient, tmp_path: Path) -> None:
        self.app = app
        self.client = client
        self.tmp_path = tmp_path

    async def login(self, password: str = PASSWORD):
        return await self.client.post("/api/login", json={"password": password})


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}"}, {"group": f"qq:{OTHER}"}]},
        "console": {"listen": f"127.0.0.1:{_port()}", "password": PASSWORD, "public_url": ""},
        "models": {"base_url": "https://ep.test/v1", "api_key": "sk-test", "main": "m", "worker": "m"},
        "storage": {"data_dir": str(tmp_path / "data")},
        "environments": {"workspace_root": str(tmp_path / "workspaces")},
    }
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    import tomlkit

    doc = tomlkit.document()
    for section, values in raw.items():
        t = tomlkit.table()
        for k, v in values.items():
            t[k] = v
        doc[section] = t
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")
    app = MaiWorkApp(FakeCtx({"config.get": "987654321"}), raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()
    client = TestClient(TestServer(app.console.app), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    yield SimpleEnv(app, client, tmp_path)
    await client.close()
    await app.stop()


def _app_card(env: SimpleEnv, *, status="failed", error="图发不出去") -> int:
    """往真 app 的库里插一张卡片（连带一张失败的 outbox 行 + 缓存图，避开真浏览器）。"""
    store = env.app.store
    cid = _card(store, status=status, error=error)
    _box(store, cid, status=status)
    cp = env.app.card_push
    assert cp is not None, "app 里没建起 card_push"
    cp._image_path(cid, JPEG).write_bytes(JPEG)
    return cid


async def test_resend_route_requires_admin(env: SimpleEnv):
    cid = _app_card(env)
    r = await env.client.post(f"/api/news/cards/{cid}/resend")
    assert r.status == 401
    await env.login()
    r2 = await env.client.post(f"/api/news/cards/{cid}/resend")
    assert r2.status == 200, await r2.text()
    body = await r2.json()
    assert body["ok"] is True and body["card_id"] == cid and body["mode"] == "image"


async def test_resend_route_refusals(env: SimpleEnv):
    await env.login()
    r = await env.client.post("/api/news/cards/999999/resend")
    assert r.status == 404
    cid = _app_card(env, status="sent")
    r2 = await env.client.post(f"/api/news/cards/{cid}/resend")
    assert r2.status == 409 and "失败" in (await r2.json())["error"]


async def test_resend_route_once_per_day(env: SimpleEnv):
    await env.login()
    cid = _app_card(env)
    assert (await env.client.post(f"/api/news/cards/{cid}/resend")).status == 200
    r = await env.client.post(f"/api/news/cards/{cid}/resend")
    assert r.status == 409


async def test_cards_list_route(env: SimpleEnv):
    cid = _app_card(env, error="图发不出去")
    r0 = await env.client.get("/api/news/cards")
    assert r0.status == 401
    await env.login()
    r = await env.client.get("/api/news/cards")
    assert r.status == 200
    body = await r.json()
    assert [c["id"] for c in body["cards"]] == [cid]
    assert body["cards"][0]["group_id"] == GID
    assert body["cards"][0]["status"] == "failed"
    assert body["cards"][0]["error"] == "图发不出去"
    # 按群 / 按状态过滤
    r2 = await env.client.get(f"/api/news/cards?group_id={OTHER}")
    assert (await r2.json())["cards"] == []
    r3 = await env.client.get("/api/news/cards?status=sent")
    assert (await r3.json())["cards"] == []
    r4 = await env.client.get("/api/news/cards?status=乱写")
    assert r4.status == 400
