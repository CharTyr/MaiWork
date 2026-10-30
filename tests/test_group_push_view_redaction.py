"""发件视图的密钥 / 本机绝对路径遮罩：三条出口必须是同一份安全结果（0.8.0 收尾）。

同一份「往群里发」数据有三条出口，以前只有第一条过遮罩：

1. ``GET /api/groups/{gid}/push``      新接口（server 的 _push_view）
2. ``GET /api/groups/{gid}/card-push`` 老接口（card_push.web_view）
3. ``GET /api/groups/{gid}``           群快照（views.group_view → card_push.group_push）

前端只读轮询用的是第 3 条（snapshot.card_push.group_push），所以第 2、3 条不遮等于没遮。

先写先红（真 Store + 真 aiohttp 客户端，本机回环）：outbox / news_cards / idea_mentions
里塞假密钥和本机绝对路径，断言出网的正文（JSON 解码后的那一串，中文按原样比）
找不到它们，URL / 相对路径 / 中文「开/关」不误伤，而且库里那几行原样
（遮罩只作用于出网的那份，绝不回写数据库）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from fakes import FakeCtx, FakeProfiles

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.app import MaiWorkApp

G1 = "900000001"
G2 = "123456789"
ADMIN_PW = "总管理员密码-遮罩用例-1234"
GA_PW = "群一管理员密码-遮罩用例"

# 假密钥：故意不带 sk- 前缀，证明遮罩靠「已知密钥名单」而不是靠 sk- 正则兜底。
SECRET_ENDPOINT = "leakcanary-endpoint-key-9f7a1c"
SECRET_OLD_MODELS = "leakcanary-old-models-key-2b8d4e"
SECRET_DB = "leakcanary-secrets-table-77c1"
# 只在文本里出现、不在任何配置里的 api_key= 形态（best-effort 兜底）
SECRET_INLINE = "plainvalue-not-known-4d2f"

# 本机绝对路径（要藏）与 URL / 相对路径 / 中文斜杠（不许误伤）
ABS_ROOT = "/root/etc/maiwork.conf"
ABS_SRV = "/srv/maiwork/data/private/note.txt"
KEEP_URL = "https://legal.example/path"
KEEP_REL = "data/out/1.txt"
KEEP_CN = "开/关"
# `api_key=<值>` 后面紧跟的这句正常说明（含全角标点）不许被一起吃掉
KEEP_AFTER_INLINE = "端点"

# 发件正文里的假密钥分布：text（会被 recent 截到 200 字，所以写短）、error（不截断）。
# 四种来源各来一份：endpoints.api_key / 旧 models.api_key / secrets 表 / 配置外的 api_key= 形态。
# 另外把密钥塞进一条 URL 的查询串里：已带上下文的密钥字符串也得藏（不是只藏「整段等于密钥」）。
PAYLOAD_TEXT = f"发一条 {SECRET_DB} 看 {ABS_ROOT} 相对 {KEEP_REL} 说明 {KEEP_CN} 链接 {KEEP_URL}"
PAYLOAD_NOTE = f"备注 {SECRET_DB} 参考 {KEEP_URL}"
ERROR_TEXT = (
    f"渲染失败 {SECRET_ENDPOINT} {SECRET_OLD_MODELS} 打不开 {ABS_ROOT} 与 {ABS_SRV} "
    f"api_key={SECRET_INLINE}；{KEEP_AFTER_INLINE} https://api.test/v1?key={SECRET_ENDPOINT}；{KEEP_CN}；{KEEP_URL}"
)


def _free_port() -> int:
    import socket as _s

    with _s.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _raw_config(data_dir: Path) -> dict:
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{G1}", "workspace": "tinker"}, {"group": f"qq:{G2}"}]},
        "console": {"listen": f"127.0.0.1:{_free_port()}", "password": ADMIN_PW, "public_url": ""},
        # 旧 [models] 字段 + 新 [[endpoints]] 都放一份假密钥（两条都是 views.secret_list 的来源）
        "models": {
            "base_url": "https://ep.test/v1",
            "api_key": SECRET_OLD_MODELS,
            "main": "m",
            "worker": "w",
        },
        "endpoints": [
            {
                "id": "e1",
                "name": "端点一",
                "protocol": "openai",
                "base_url": "https://ep.test/v1",
                "api_key": SECRET_ENDPOINT,
            }
        ],
        "storage": {"data_dir": str(data_dir)},
        "approval": {"required": True, "admins": ["10001"]},
    }


class _Env:
    def __init__(self, app: MaiWorkApp, clients: dict[str, TestClient], tmp_path: Path) -> None:
        self.app = app
        self.clients = clients
        self.tmp_path = tmp_path

    @property
    def admin(self) -> TestClient:
        return self.clients["admin"]

    @property
    def ga(self) -> TestClient:
        return self.clients["ga"]

    @property
    def ga_other(self) -> TestClient:
        return self.clients["ga_other"]

    @property
    def member(self) -> TestClient:
        return self.clients["member"]

    @property
    def anon(self) -> TestClient:
        return self.clients["anon"]

    def seed_outbox(self, gid: str = G1) -> None:
        now = clock.now()
        with self.app.store.tx() as conn:
            conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, result,"
                " error, task_id, not_before, created, updated)"
                " VALUES (?, ?, 'message', ?, 'failed', 1, '{}', ?, NULL, 0, ?, ?)",
                (
                    f"redact-{gid}-1",
                    gid,
                    json.dumps({"text": PAYLOAD_TEXT, "note": PAYLOAD_NOTE, "push_kind": "opener"}),
                    ERROR_TEXT,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO news_cards (group_id, batch_id, status, item_ids, created, error)"
                " VALUES (?, 1, 'failed', '[1]', ?, ?)",
                (gid, now, ERROR_TEXT),
            )
            conn.execute(
                "INSERT INTO idea_mentions (group_id, idea_id, status, text, at_user, created, error)"
                " VALUES (?, 1, 'failed', ?, '', ?, ?)",
                (gid, PAYLOAD_TEXT, now, ERROR_TEXT),
            )

    def raw_rows(self, gid: str = G1) -> dict[str, Any]:
        """库里那几行的原文（拿来证明遮罩没回写）。"""
        conn = self.app.store.read()
        ob = conn.execute(
            "SELECT payload, error FROM outbox WHERE group_id=? ORDER BY id", (gid,)
        ).fetchall()
        nc = conn.execute(
            "SELECT error FROM news_cards WHERE group_id=? ORDER BY id", (gid,)
        ).fetchall()
        im = conn.execute(
            "SELECT error FROM idea_mentions WHERE group_id=? ORDER BY id", (gid,)
        ).fetchall()
        return {
            "outbox": [(str(r["payload"]), str(r["error"])) for r in ob],
            "news_cards": [str(r["error"]) for r in nc],
            "idea_mentions": [str(r["error"]) for r in im],
        }

    def db_secret_values(self) -> list[str]:
        return [
            str(r["value"])
            for r in self.app.store.read().execute("SELECT value FROM secrets").fetchall()
        ]


async def _make_client(app: MaiWorkApp) -> TestClient:
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    return client


@pytest_asyncio.fixture
async def env(tmp_path: Path):
    import tomlkit

    raw = _raw_config(tmp_path / "data")
    plug_dir = tmp_path / "plug"
    plug_dir.mkdir()
    doc = tomlkit.document()
    for section, values in raw.items():
        if isinstance(values, dict):
            t = tomlkit.table()
            for k, v in values.items():
                t[k] = v
            doc[section] = t
        else:
            doc[section] = values
    (plug_dir / "config.toml").write_text(tomlkit.dumps(doc), encoding="utf-8")
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=plug_dir)
    app.profiles_cls = FakeProfiles
    await app.start()

    # 起完再种数据：recover() 之类不会动它
    clients: dict[str, TestClient] = {}
    for name in ("admin", "ga", "ga_other", "member", "anon"):
        clients[name] = await _make_client(app)

    e = _Env(app=app, clients=clients, tmp_path=tmp_path)
    # secrets 表里也放一份假密钥（_secret_list 的来源之一；线上这表存组件的密钥）
    with app.store.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO secrets (name, value, updated) VALUES ('test.redact', ?, ?)",
            (SECRET_DB, clock.now()),
        )
    e.seed_outbox()

    # 登录：总管理员 / 本群群管理员 / 另一个群的群管理员
    app.group_admins.set_password(G1, GA_PW)
    app.group_admins.set_password(G2, "群二管理员密码-遮罩用例")
    r = await clients["admin"].post("/api/login", json={"password": ADMIN_PW})
    assert r.status == 200, await r.text()
    r = await clients["ga"].post("/api/login", json={"password": GA_PW})
    assert r.status == 200, await r.text()
    r = await clients["ga_other"].post("/api/login", json={"password": "群二管理员密码-遮罩用例"})
    assert r.status == 200, await r.text()

    try:
        yield e
    finally:
        for c in clients.values():
            await c.close()
        await app.stop()


def _push_paths(gid: str = G1) -> tuple[str, str, str]:
    return (f"/api/groups/{gid}/push", f"/api/groups/{gid}/card-push", f"/api/groups/{gid}")


def _visible(body: str) -> str:
    """把响应体还原成「人眼看到的那串」：JSON 里的中文默认被 \\uXXXX 转义，
    直接查原始字节会把「中文没被误伤」误判成误伤。解不开就按原文查。"""
    try:
        return json.dumps(json.loads(body), ensure_ascii=False)
    except Exception:
        return body


def _clean_problems(body: str, *, where: str, keep: bool = True) -> list[str]:
    """出网的正文里：假密钥 / 本机绝对路径一个都不许有；URL 与中文斜杠不许被误伤。

    返回问题清单（不直接断言）：三条路一次跑完，谁漏了全看得见。
    """
    seen = _visible(body)
    problems: list[str] = []
    for secret in (SECRET_ENDPOINT, SECRET_OLD_MODELS, SECRET_DB, SECRET_INLINE):
        if secret in seen:
            problems.append(f"{where}: 密钥漏了 {secret}")
    for abs_path in (ABS_ROOT, ABS_SRV, "/root/etc", "/srv/maiwork"):
        if abs_path in seen:
            problems.append(f"{where}: 本机绝对路径漏了 {abs_path}")
    if keep:
        if KEEP_URL not in seen:
            problems.append(f"{where}: URL 被误伤")
        if KEEP_REL not in seen:
            problems.append(f"{where}: 相对路径被误伤")
        if KEEP_CN not in seen:
            problems.append(f"{where}: 中文「开/关」被误伤")
        if KEEP_AFTER_INLINE not in seen:
            problems.append(f"{where}: api_key= 后面那句正常说明被误伤")
    return problems


@pytest.mark.asyncio
async def test_admin_three_paths_are_redacted(env: _Env) -> None:
    """总管理员：三条出口都不回密钥 / 本机绝对路径，URL 与中文斜杠不误伤。"""
    problems: list[str] = []
    for path in _push_paths():
        r = await env.admin.get(path)
        assert r.status == 200, (path, r.status, await r.text())
        problems += _clean_problems(await r.text(), where=f"admin {path}")
    assert problems == [], "\n".join(problems)


@pytest.mark.asyncio
async def test_group_admin_three_paths_are_redacted(env: _Env) -> None:
    """本群群管理员：同三条出口，同样遮罩（他有本群管理权，但不出密钥 / 绝对路径）。"""
    problems: list[str] = []
    for path in _push_paths():
        r = await env.ga.get(path)
        assert r.status == 200, (path, r.status, await r.text())
        problems += _clean_problems(await r.text(), where=f"ga {path}")
    assert problems == [], "\n".join(problems)


@pytest.mark.asyncio
async def test_snapshot_and_card_push_carry_the_push_view(env: _Env) -> None:
    """第 2、3 条出口真的带发件数据（避免「没有数据所以当然不泄漏」的假绿）。"""
    r = await env.admin.get(f"/api/groups/{G1}/card-push")
    cp = await r.json()
    gp = cp["group_push"]
    assert gp["recent"], "card_push.group_push.recent 是空的，这条用例没在测东西"
    assert cp["recent"], "card_push.recent 是空的"
    assert cp["mention"]["recent"], "card_push.mention.recent 是空的"
    assert cp["has_link"] in (True, False)

    r = await env.admin.get(f"/api/groups/{G1}")
    snap = await r.json()
    assert snap["card_push"]["group_push"]["recent"], "群快照里的 group_push.recent 是空的"
    assert snap["card_push"]["recent"], "群快照里的 card_push.recent 是空的"
    assert "has_link" in snap["card_push"]


@pytest.mark.asyncio
async def test_only_sender_data_changes_other_fields_survive(env: _Env) -> None:
    """只动发件数据部分：recent 的其它字段、has_link、config 键都在。"""
    r = await env.admin.get(f"/api/groups/{G1}/card-push")
    cp = await r.json()
    entry = cp["group_push"]["recent"][0]
    for key in ("id", "key", "kind", "push_kind", "status", "state", "uncertain", "text", "ts", "error"):
        assert key in entry, f"recent 少了字段 {key}"
    assert entry["status"] == "failed" and entry["state"]
    assert entry["ts"] > 0
    assert isinstance(cp["group_push"]["config"], dict)
    assert cp["config"] == cp["group_push"]["config"]
    for key in ("id", "status", "count", "created", "mode", "error"):
        assert key in cp["recent"][0], f"card recent 少了字段 {key}"
    assert "has_link" in cp and "daily_max" in cp and "quota_used" in cp


@pytest.mark.asyncio
async def test_member_snapshot_has_no_push_view(env: _Env) -> None:
    """群友：快照本来就零信息（连 card_push 键都没有），三条路正文都不许出密钥 / 路径。"""
    token = env.app.token_of(G1)
    r = await env.member.get(f"/api/groups/{token}", headers={"X-MW-Group": token})
    assert r.status == 200, await r.text()
    snap = await r.json()
    assert "card_push" not in snap, "群友快照不该出现 card_push"
    assert "focus" not in snap and "token" not in snap
    # 群友快照本来就没有发件数据，所以只查「不许漏」，不查 URL 保留
    assert _clean_problems(await r.text(), where="member snapshot", keep=False) == []

    for path in (f"/api/groups/{G1}/push", f"/api/groups/{G1}/card-push"):
        r = await env.member.get(path, headers={"X-MW-Group": token})
        assert r.status == 403, (path, r.status)
        assert _clean_problems(await r.text(), where=f"member {path}", keep=False) == []


@pytest.mark.asyncio
async def test_cross_group_and_anonymous_denied_without_leak(env: _Env) -> None:
    """群管理员只能本群、匿名 401：拒绝正文同样不许漏。"""
    other_token = env.app.token_of(G2)
    for path in _push_paths(G1):
        r = await env.ga_other.get(path)
        assert r.status == 403, (path, r.status, await r.text())
        assert _clean_problems(await r.text(), where=f"ga_other {path}", keep=False) == []
        r = await env.anon.get(path)
        assert r.status == 401, (path, r.status)
        assert _clean_problems(await r.text(), where=f"anon {path}", keep=False) == []

    token = env.app.token_of(G1)
    r = await env.member.get(f"/api/groups/{other_token}", headers={"X-MW-Group": token})
    assert r.status == 403, r.status
    assert _clean_problems(await r.text(), where="member cross-group", keep=False) == []


@pytest.mark.asyncio
async def test_raw_db_is_not_masked_back(env: _Env) -> None:
    """遮罩只作用于出网的那一份：库里的原文（含假密钥）必须原样。"""
    before = env.raw_rows()
    assert SECRET_DB in before["outbox"][0][0]        # payload.text（会被 recent 截到 200 字那份）
    assert SECRET_ENDPOINT in before["outbox"][0][1]  # outbox.error
    assert SECRET_ENDPOINT in before["news_cards"][0]
    assert SECRET_ENDPOINT in before["idea_mentions"][0]

    for client in (env.admin, env.ga):
        for path in _push_paths():
            assert (await client.get(path)).status == 200
    token = env.app.token_of(G1)
    assert (await env.member.get(f"/api/groups/{token}", headers={"X-MW-Group": token})).status == 200

    assert env.raw_rows() == before, "响应正文的遮罩被写回数据库了"
    assert SECRET_DB in env.db_secret_values()


def test_free_text_helper_boundaries() -> None:
    """遮罩 helper 的边界（不经 HTTP 直接调）：该遮的遮，正常文字一个字都不许动。"""
    from CharTyr_MaiWork.maiwork.console import views

    a, b, c = "aaa1111", "bbb2222", "ccc3333"
    text = (
        f"失败 OPENAI_API_KEY={a} 说明api_key={b} myapi_key={c} "
        f"开/关 {KEEP_URL} {KEEP_REL} {ABS_ROOT}"
    )
    out = views.redact_free_text(text, ["aaa1111"])
    assert a not in out and "OPENAI_API_KEY=***" in out, out
    assert b not in out and "说明api_key=***" in out, out
    # 不是键名（myapi_key 的片段）不动，相对路径 / URL / 中文照旧
    assert f"myapi_key={c}" in out, out
    assert KEEP_URL in out and KEEP_REL in out and KEEP_CN in out, out
    assert ABS_ROOT not in out and "[路径已略]" in out, out


# ======================================================================
# 追加（0.8.0 收尾）：控制台三个出口和 app.known_secrets() 必须是同一份名单
# ======================================================================
#
# 复审结论：2e64 只把「模型端点密钥 + 高级请求头 + secrets 表」并进了出口遮罩，
# `_known_secrets()` 里还有几类没并进来：搜索绑定、Jev 密钥、网页总密码、
# [[extensions.mcp]] 请求头。工具摘要吃得到这些、网页三条出口吃不到 = 漏。
# 现在 `views.secret_list` 会并 `svc.known_secrets()`（公开方法），本组用例钉死：
# 四类「不在 sk- 里」的假密钥同时出现在 payload 与 error 里，三条出口全遮，
# 合法 URL / 相对路径 / 中文一个字不动；取密钥的方法炸了也不许把老来源丢光。

EXTRA_JEV_KEY = "leakcanary-jev-key-5e2b"            # [jev] api_key：只在已知密钥名单里
EXTRA_EXT_HEADER = "Bearer leakcanary-mcp-header-8a1f"  # [[extensions.mcp]] headers 的值
EXTRA_EXT_TOKEN = "leakcanary-mcp-header-8a1f"       # 同一份的 token 片段（也得遮）
EXTRA_SEARCH_SQL = "leakcanary-old-search-sql-3c9d"  # 旧 [search] 密钥：只在 secrets 表里

EXTRA_PAYLOAD_TEXT = f"发一条 {EXTRA_JEV_KEY} 看 {EXTRA_SEARCH_SQL} 链接 {KEEP_URL} 中文 {KEEP_CN}"
EXTRA_ERROR_TEXT = (
    f"渲染失败 {ADMIN_PW} {EXTRA_EXT_HEADER} {EXTRA_JEV_KEY} "
    f"https://api.test/v1?key={EXTRA_SEARCH_SQL}；{KEEP_REL}；{KEEP_CN}；{KEEP_URL}"
)


def _extra_secrets() -> tuple[str, ...]:
    # ADMIN_PW 就是这份 FakeApp 配置里的 console.password（网页总密码）
    return (ADMIN_PW, EXTRA_JEV_KEY, EXTRA_EXT_HEADER, EXTRA_EXT_TOKEN, EXTRA_SEARCH_SQL)


def _extra_problems(body: str, *, where: str) -> list[str]:
    seen = _visible(body)
    problems = [f"{where}: 密钥漏了 {s}" for s in _extra_secrets() if s in seen]
    for keep in (KEEP_URL, KEEP_REL, KEEP_CN):
        if keep not in seen:
            problems.append(f"{where}: 正常文字被误伤（{keep}）")
    return problems


@pytest_asyncio.fixture
async def env_extra(env: _Env):
    """在真 FakeApp 上补四类「不在 sk- 里」的假密钥 + 一条同时含它们的发件行。"""
    import dataclasses
    from types import MappingProxyType

    from CharTyr_MaiWork.maiwork.config import ExtensionsSetting, McpExtensionSetting

    settings = env.app._settings
    # 网页总密码本来就在配置里（ADMIN_PW）；这里再把 Jev 密钥与扩展请求头加到设置上。
    # 扩展 enabled=False → 只进遮罩名单，不连任何网络。
    env.app._settings = dataclasses.replace(
        settings,
        jev=dataclasses.replace(settings.jev, api_key=EXTRA_JEV_KEY),
        extensions=ExtensionsSetting(mcp=(
            McpExtensionSetting(
                name="extx",
                url="https://mcp.test/x",
                enabled=False,
                headers=MappingProxyType({"Authorization": EXTRA_EXT_HEADER}),
                tools=(),
                roles=("worker",),
                timeout_s=20,
            ),
        )),
    )
    now = clock.now()
    with env.app.store.tx() as conn:
        conn.execute(
            "INSERT INTO outbox (key, group_id, kind, payload, status, attempts, result,"
            " error, task_id, not_before, created, updated)"
            " VALUES (?, ?, 'message', ?, 'failed', 1, '{}', ?, NULL, 0, ?, ?)",
            (
                f"redact-extra-{G1}",
                G1,
                json.dumps({"text": EXTRA_PAYLOAD_TEXT, "note": "", "push_kind": "opener"}),
                EXTRA_ERROR_TEXT,
                now + 1,
                now + 1,
            ),
        )
        # 旧 [search] 密钥：老库 secrets 表里的孤儿行（SQL 来源）
        conn.execute(
            "INSERT OR REPLACE INTO secrets (name, value, updated) VALUES ('search_api_key', ?, ?)",
            (EXTRA_SEARCH_SQL, now),
        )
    yield env


@pytest.mark.asyncio
async def test_known_secrets_extra_four_are_masked_on_three_paths(env_extra: _Env) -> None:
    """网页总密码 / Jev 密钥 / 扩展请求头 / 旧搜索密钥：payload + error 三条出口全遮。"""
    from CharTyr_MaiWork.maiwork.console import views

    env = env_extra
    # 先证明这四类真在出口用的那份名单里（否则下面的隐藏是假绿）：
    # Jev / 网页总密码 / 扩展请求头来自 app.known_secrets()，旧搜索密钥来自 secrets 表。
    known = env.app.known_secrets()
    assert EXTRA_JEV_KEY in known and ADMIN_PW in known
    assert EXTRA_EXT_HEADER in known and EXTRA_EXT_TOKEN in known
    both = views.secret_list(env.app)
    for secret in _extra_secrets():
        assert secret in known or secret in both, (secret, known, both)

    # 这条发件行真被读到了（recent 里排第一），且 text / error 都遮了
    r = await env.admin.get(f"/api/groups/{G1}/push")
    assert r.status == 200, await r.text()
    push = await r.json()
    first = push["recent"][0]
    assert first["key"] == f"redact-extra-{G1}", first
    assert "***" in first["text"], first["text"]
    assert "***" in first["error"], first["error"]

    problems: list[str] = []
    for path in _push_paths():
        rr = await env.admin.get(path)
        assert rr.status == 200, (path, rr.status, await rr.text())
        problems += _extra_problems(await rr.text(), where=f"admin {path}")
    assert problems == [], "\n".join(problems)

    # 只遮出网的那一份：库里那几行原样（绝不回写）
    row = env.app.store.read().execute(
        "SELECT payload, error FROM outbox WHERE key=?", (f"redact-extra-{G1}",)
    ).fetchone()
    assert EXTRA_JEV_KEY in str(row["payload"]) and EXTRA_SEARCH_SQL in str(row["payload"])
    assert ADMIN_PW in str(row["error"]) and EXTRA_EXT_HEADER in str(row["error"])
    assert env.app.store.secret_get("search_api_key") == EXTRA_SEARCH_SQL


@pytest.mark.asyncio
async def test_known_secrets_raise_keeps_old_sources_zero_model_leak(
    env_extra: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取已知密钥的方法整个炸了：老来源（端点 / 旧 models / secrets 表）照旧遮，绝不返回空表。"""
    from CharTyr_MaiWork.maiwork.console import views

    env = env_extra

    def boom() -> list[str]:
        raise RuntimeError("取密钥炸了（模拟）")

    monkeypatch.setattr(env.app, "known_secrets", boom)

    secrets = views.secret_list(env.app)
    assert SECRET_ENDPOINT in secrets and SECRET_OLD_MODELS in secrets and SECRET_DB in secrets
    assert secrets, "取密钥失败就返回空表 = 整个控制台的遮罩全丢"

    problems: list[str] = []
    for path in _push_paths():
        rr = await env.admin.get(path)
        assert rr.status == 200, (path, rr.status, await rr.text())
        seen = _visible(await rr.text())
        for model_secret in (SECRET_ENDPOINT, SECRET_OLD_MODELS, SECRET_DB):
            if model_secret in seen:
                problems.append(f"admin {path}: 模型/库密钥漏了 {model_secret}")
        for keep in (KEEP_URL, KEEP_CN):
            if keep not in seen:
                problems.append(f"admin {path}: 正常文字被误伤（{keep}）")
    assert problems == [], "\n".join(problems)


def test_secret_list_filters_non_string_and_keeps_getter_failure() -> None:
    """非字符串项按 models._redact_full 的合同过滤；getter 炸了老来源仍非空。"""
    from CharTyr_MaiWork.maiwork.console import views

    class _Cur:
        def fetchall(self):
            return [{"value": "store-secret-keepme"}]

    class _Con:
        def execute(self, *a):
            return _Cur()

    class _Store:
        def read(self):
            return _Con()

    class _Svc:
        store = _Store()

        def get_settings(self):
            return None

        def known_secrets(self):
            return [None, 123, "", "jevy-key", "jevy-key"]  # type: ignore[list-item]

    assert views.secret_list(_Svc()) == ["store-secret-keepme", "jevy-key"]

    class _Raising(_Svc):
        def known_secrets(self):
            raise RuntimeError("炸")

    assert views.secret_list(_Raising()) == ["store-secret-keepme"]


def test_redact_only_touches_sender_text_and_error() -> None:
    """遮罩只动发件 text / error：群名 / accounts / 规则 / 计数这些正常字段一字不动。"""
    from CharTyr_MaiWork.maiwork.console import views

    payload = {
        "config": {"quiet_hours": "23:00-08:00", "daily_max": 3},
        "group_name": "测试群-开/关",
        "accounts": ["qq:10001"],
        "rules": "本群规矩：不刷屏",
        "recent": [
            {"id": 1, "key": "k1", "status": "failed", "count": 2,
             "text": f"发 {EXTRA_JEV_KEY} 和 {KEEP_URL}", "error": f"失败 {EXTRA_JEV_KEY}"},
        ],
    }
    out = views.redact_push_view(payload, [EXTRA_JEV_KEY])
    assert out["group_name"] == payload["group_name"]
    assert out["config"] == payload["config"]
    assert out["accounts"] == payload["accounts"]
    assert out["rules"] == payload["rules"]
    assert out["recent"][0]["status"] == "failed" and out["recent"][0]["count"] == 2
    assert out["recent"][0]["key"] == "k1"
    assert EXTRA_JEV_KEY not in out["recent"][0]["text"] and KEEP_URL in out["recent"][0]["text"]
    assert EXTRA_JEV_KEY not in out["recent"][0]["error"]
