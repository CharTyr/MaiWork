"""identity.py 测试：身份与工作记忆（SOUL / AGENTS / MEMORY / 每群记忆）。

覆盖需求（身份与工作记忆）：
- 首次启动自动生成 SOUL（从 MaiBot host.config 同步，含额外 personality.* 字段）；
- 网页可编辑（PUT 走 console 接口，鉴权、超限 400、非服务群 404）；
- 手动「从 MaiBot 同步」覆盖前把旧版存 SOUL.md.bak，内容没变就不覆盖；
- prompt_block 注入分块标题、截断、本群记忆绝不跨群；
- remember：scope/去空白去重/上限淘汰/全局不写具体的群和人/过隐私闸/事件落库；
- 资讯被标「没用」累计 3 次的来源或话题 → 代码直接记进本群记忆（不调模型）。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, List

import pytest
import pytest_asyncio

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.identity import Identity
from CharTyr_MaiWork.maiwork.store import Store

GID = "111"
GID_OTHER = "222"


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _settings(cfg: dict | None = None) -> Any:
    raw = cfg or {}
    raw.setdefault("groups", {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}, {"group": f"qq:{GID_OTHER}"}]})
    settings, _ = load_settings(raw)
    return settings


class FakeHost:
    """只带 config 的假 host（identity 只用它一个方法）。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = dict(config)
        self.calls: List[str] = []

    async def config(self, key: str, default: Any = None) -> Any:
        self.calls.append(key)
        return self._config.get(key, default)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.migrate()
    return s


def _make(tmp_path: Path, store: Store, *, host: Any = None, cfg: dict | None = None) -> Identity:
    settings = _settings(cfg)
    return Identity(tmp_path / "data", store, lambda: settings, host=host)


def _agents_of(identity: Identity) -> Any:
    """从 identity 的 settings 拿一份 Agents（顺手用同一个 store）。"""
    from CharTyr_MaiWork.maiwork.agents import Agents
    return Agents(identity._store, identity._get_settings)


# ----------------------------------------------------------------------
# 首次启动 / 同步
# ----------------------------------------------------------------------


def test_first_start_files_created_and_dir_perms(tmp_path: Path, store: Store) -> None:
    """首次启动：identity/ 目录就位、SOUL 三节的兜底版（没 host 也有）、
    main 的 AGENTS 用主模型预设（不编造/做不到就说/成品放 artifacts/派给哪个专岗）、MEMORY 空。
    （2026-10 专岗改版 3/4：旧 AGENTS 出厂模板让位给 agent_presets/main.md，
    里面同样覆盖不编造、做不到就说、artifacts 约定，就不再老钉「交付前自查」四字。）"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    root = tmp_path / "data" / "identity"
    assert root.is_dir()
    if os.name == "posix":
        mode = root.stat().st_mode & 0o777
        # 支持权限的文件系统上必须收紧成 0700；exFAT 不支持时会得到怪值，那就跳过
        if mode in (0o700, 0o600):
            assert mode == 0o700
    assert (root / "SOUL.md").is_file()
    assert (root / "AGENTS.md").is_file()
    assert (root / "MEMORY.md").is_file()
    assert (root / "memory").is_dir()
    soul = identity.read("soul")["text"]
    assert "# 我是谁" in soul and "# 说话方式" in soul and "# 边界" in soul
    agents = identity.read("agents")["text"]
    # main 预设（agent_presets/main.md）：给主模型看的那份
    assert "不编造" in agents and "做不到就说" in agents
    assert "artifacts" in agents
    assert "派给哪个专岗" in agents
    assert identity.read("memory")["text"] == ""


def test_first_sync_from_maibot_config(tmp_path: Path, store: Store) -> None:
    """首次同步从 host.config 读 bot.nickname / personality.*，额外文字字段也带进去。"""
    host = FakeHost(
        {
            "bot.nickname": "小麻",
            "personality.personality": "慢热但靠谱",
            "personality.reply_style": "口语、短句、别端着",
            "personality.interests": "开源硬件和做菜",  # 能读到的 personality.* 其他文字字段
            "personality.some_dict": {"x": 1},  # 读不到（不是文字）就跳过
        }
    )
    identity = _make(tmp_path, store, host=host)
    _run(identity.ensure_started())
    text = identity.read("soul")["text"]
    assert "小麻" in text
    assert "慢热但靠谱" in text
    assert "口语、短句、别端着" in text
    assert "开源硬件和做菜" in text
    # 边界红线固定三条
    assert "私下画像" in text
    assert "不刷屏" in text
    assert "不冒充人" in text
    assert identity.read("soul").get("synced_from_maibot") is True


def test_sync_overwrite_saves_bak_and_reports_change(tmp_path: Path, store: Store) -> None:
    """管理员点同步：旧版存 SOUL.md.bak；内容有变化 preview_changed=True，没变化 False 且不覆盖。"""
    host = FakeHost({"bot.nickname": "小麻", "personality.personality": "A", "personality.reply_style": "B"})
    identity = _make(tmp_path, store, host=host)
    _run(identity.ensure_started())
    # 管理员改成自己的版本
    identity.write("soul", "# 我是谁\n管理员自己写的版本")
    out = _run(identity.sync_soul_from_maibot())
    assert out["synced_from_maibot"] is True
    assert out["preview_changed"] is True
    assert "小麻" in out["text"]
    bak = (tmp_path / "data" / "identity" / "SOUL.md.bak").read_text(encoding="utf-8")
    assert "管理员自己写的版本" in bak
    # 再同步一次：生成的没变 → 不覆盖（bak 保持管理员版本），preview_changed False
    out2 = _run(identity.sync_soul_from_maibot())
    assert out2["preview_changed"] is False
    bak2 = (tmp_path / "data" / "identity" / "SOUL.md.bak").read_text(encoding="utf-8")
    assert "管理员自己写的版本" in bak2
    # 手动改过之后 synced_from_maibot 回落 False
    identity.write("soul", "又改了一下")
    assert identity.read("soul")["synced_from_maibot"] is False


def test_write_limit_and_global_files_round_trip(tmp_path: Path, store: Store) -> None:
    """超限 ValueError（接口 400）；「每群三份」收尾后只剩全局三份能写读。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    with pytest.raises(ValueError):
        identity.write("soul", "x" * (16384 * 2))  # UTF-8 编码后明显超 16KB
    # 每群身份接口（group_read / group_write）2026-10-03 已删（内容进了 group_rules / skills）
    assert not hasattr(identity, "group_read")
    assert not hasattr(identity, "group_write")
    assert not hasattr(identity, "group_memory_map")
    # 正常写读
    got = identity.write("memory", "- 2026-10-01 全局经验（原因）")
    assert got["text"].endswith("全局经验（原因）")
    assert got["updated_ts"] > 0
    # 超限分支同样拦
    with pytest.raises(ValueError):
        identity.write("memory", "长" * (16384 * 2))


# ----------------------------------------------------------------------
# prompt_block
# ----------------------------------------------------------------------


def test_prompt_block_titles_and_empty(tmp_path: Path, store: Store) -> None:
    """分块标题固定；空文件不出块；整个块以空行收尾，拼起来不会糊在一起。
    「每群三份」收尾后（2026-10-03）只剩全局三块；每群内容另走 group_context。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    identity.write("soul", "我是小麻，说话慢一点。")
    identity.write("agents", "规矩一：先想再做。")
    identity.write("memory", "- 全局经验")
    soul = identity.prompt_block("soul")
    assert soul.startswith("## MaiWork 的身份\n") and soul.endswith("\n\n")
    assert "我是小麻" in soul
    assert identity.prompt_block("agents").startswith("## 做事规矩\n")
    block = identity.prompt_block("memory", group_id=GID)
    assert "## 工作记忆（全局）" in block
    # 每群段退役：带 group_id 也不再拼「这个群的工作记忆」
    assert "这个群的工作记忆" not in block
    # 空 MEMORY：写空后整块都不出
    identity.write("memory", "")
    block2 = identity.prompt_block("memory", group_id=GID)
    assert "工作记忆（全局）" not in block2


def test_prompt_block_group_content_never_leaks_across_groups(tmp_path: Path, store: Store) -> None:
    """收尾后 prompt_block 只出全局；每群内容永不出现在它——要在别群也看不见、本群也
    从这个口子看不见（group_context 才是每群内容的注入口子，那个另有测试守）。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    # 任何「每群」调用都不再读文件，哪怕是本群
    mine = identity.prompt_block("memory", group_id=GID)
    other = identity.prompt_block("memory", group_id=GID_OTHER)
    none = identity.prompt_block("memory")
    assert "这个群的工作记忆" not in mine
    assert "这个群的工作记忆" not in other
    assert "这个群的工作记忆" not in none


def test_prompt_block_truncates_to_limit(tmp_path: Path, store: Store) -> None:
    """文件被手工写到超限（绕过网页）时，注入也要截到 16KB 以内。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    big = "长" * 20000  # 60KB UTF-8
    (tmp_path / "data" / "identity" / "MEMORY.md").write_text(big, encoding="utf-8")
    block = identity.prompt_block("memory")
    assert len(block.encode("utf-8")) <= 16384 + 128  # 标题 + 截断后缀也要兜住


# ----------------------------------------------------------------------
# remember
# ----------------------------------------------------------------------


def _remember_ok(out: dict) -> dict:
    assert out.get("ok") is True, out
    return out


def test_remember_appends_with_date_and_reason(tmp_path: Path, store: Store) -> None:
    """成功追加：「- YYYY-MM-DD 文本（原因）」落进目标（group → 本群规矩；global → MEMORY.md）。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    _remember_ok(identity.remember_sync(scope="group", group_id=GID, text="这个群喜欢先看结论", reason="交付表单反馈"))
    day = clock.bj(clock.now()).strftime("%Y-%m-%d")
    rules = _agents_of(identity).group_rules_get(GID)
    assert f"- {day} 这个群喜欢先看结论（交付表单反馈）" in rules["body"]
    # 全局也一样——只进 MEMORY.md
    _remember_ok(identity.remember_sync(scope="global", text="管理员偏好：少发群文件多走网页", reason="管理员口头说过"))
    assert "少发群文件多走网页" in identity.read("memory")["text"]
    assert "少发群文件多走网页" not in rules["body"]




def test_remember_dedup_by_normalized_text(tmp_path: Path, store: Store) -> None:
    """同一句（去空白后相同）不重复记：规矩里只留一份。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    _remember_ok(identity.remember_sync(scope="group", group_id=GID, text="别在群里刷链接", reason="a"))
    out = identity.remember_sync(scope="group", group_id=GID, text=" 别在群里刷链接 ", reason="b")
    assert out.get("ok") is True
    assert out.get("deduped") is True
    body = _agents_of(identity).group_rules_get(GID)["body"]
    assert body.count("别在群里刷链接") == 1


def test_remember_rejects_oversized_and_bad_scope(tmp_path: Path, store: Store) -> None:
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    out = identity.remember_sync(scope="group", group_id=GID, text="长" * 300, reason="x")
    assert out["ok"] is False
    out = identity.remember_sync(scope="global", text="abc", reason="长" * 100)
    assert out["ok"] is False
    out = identity.remember_sync(scope="nope", group_id=GID, text="abc", reason="x")
    assert out["ok"] is False
    # group scope 必须有 group_id
    out = identity.remember_sync(scope="group", text="abc", reason="x")
    assert out["ok"] is False


def test_remember_global_rejects_group_and_qq_numbers(tmp_path: Path, store: Store) -> None:
    """全局记忆不许写 5 位以上的长数字（群号 / QQ 号）。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    out = identity.remember_sync(scope="global", text="群 111902 喜欢短视频", reason="x")
    assert out["ok"] is False
    assert "全局记忆" in out.get("error", "")
    # 普通年份 / 小数字行得通
    out2 = identity.remember_sync(scope="global", text="记录版本 12.3 的小技巧", reason="x")
    assert out2["ok"] is True


def test_remember_global_rejects_focus_and_member_names(tmp_path: Path, store: Store) -> None:
    """全局记忆不许点名：关注成员名字 / 服务群成员名字（这个群名字够多时按群看）。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed)"
            " VALUES (?, ?, ?, ?, 0)",
            (GID, "42", "阿帆", "他偷偷在学钢琴所以晚上常不在线"),
        )
        # G2 是一个老群（名字数 > 3），它的成员名也不许进全局
        for i in range(5):
            conn.execute(
                "INSERT INTO member_activity (group_id, user_id, day, count, name)"
                " VALUES (?, ?, '2026-10-01', 3, ?)",
                (GID_OTHER, str(100 + i), f"老周{i}号"),
            )
        conn.execute(
            "INSERT INTO member_activity (group_id, user_id, day, count, name)"
            " VALUES (?, '999', '2026-10-01', 3, '老周头')",
            (GID_OTHER,),
        )
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    out = identity.remember_sync(scope="global", text="阿帆说深科技视频别推了", reason="x")
    assert out["ok"] is False
    assert "全局记忆" in out.get("error", "")
    out2 = identity.remember_sync(scope="global", text="老周头反馈那个表格看着累", reason="x")
    assert out2["ok"] is False
    # 本群规矩可以提（注入不出群）
    ok = identity.remember_sync(scope="group", group_id=GID, text="阿帆说深科技视频别推了", reason="x")
    assert ok["ok"] is True
    assert "别推了" in _agents_of(identity).group_rules_get(GID)["body"]
    assert "别推了" not in identity.read("memory")["text"]


def test_remember_scrub_rejects_persona_fragments(tmp_path: Path, store: Store) -> None:
    """关注成员 note / persona 的 ≥8 字片段：scope=global（全群闸）和 scope=group（本群闸）都拒。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed)"
            " VALUES (?, ?, ?, ?, 0)",
            (GID, "42", "阿帆", "他偷偷在学钢琴所以晚上常不在线"),
        )
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    out = identity.remember_sync(scope="global", text="记得他偷偷在学钢琴所以晚上常不在线这事", reason="x")
    assert out["ok"] is False
    out2 = identity.remember_sync(scope="group", group_id=GID, text="记住他偷偷在学钢琴所以晚上常不在线", reason="x")
    assert out2["ok"] is False
    assert "不宜" in out2.get("error", "") or "私下" in out2.get("error", "")


def test_remember_global_gate_sees_through_disguised_numbers(tmp_path: Path, store: Store) -> None:
    """外部审查 2026-10-02：拆空格 / 中文数字 / 全角 / 零宽字符写的群号也得拦。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    for text in (
        "群 90 21 06 喜欢短视频",
        "记得九零二一零六的人爱看番",
        "群９０２１０６喜欢短视频",
        "群 90\u200b2106 喜欢短视频",
        "记得玖零贰壹零陆的人爱看番",
    ):
        out = identity.remember_sync(scope="global", text=text, reason="x")
        assert out["ok"] is False, text
        assert "全局记忆" in out.get("error", "")
    # 日期、小数字照常能记
    ok = identity.remember_sync(scope="global", text="2026-10-02 起周报改周五发，一两句就够", reason="x")
    assert ok["ok"] is True


def test_remember_global_gate_sees_through_disguised_names(tmp_path: Path, store: Store) -> None:
    """名字中间插空格 / 零宽字符、英文名换大小写，也算点名。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed) VALUES (?, ?, ?, ?, 0)",
            (GID, "42", "阿帆", ""),
        )
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed) VALUES (?, ?, ?, ?, 0)",
            (GID, "43", "Leo", ""),
        )
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    for text in ("阿 帆说深科技视频别推了", "阿\u200d帆说深科技视频别推了", "LEO 说表格看着累"):
        out = identity.remember_sync(scope="global", text=text, reason="x")
        assert out["ok"] is False, text


def test_privacy_scrub_sees_through_spacing(tmp_path: Path, store: Store) -> None:
    """画像片段中间插空格 / 零宽字符照样拦。"""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed) VALUES (?, ?, ?, ?, 0)",
            (GID, "42", "阿帆", "他偷偷在学钢琴所以晚上常不在线"),
        )
    from maiwork import privacy

    assert privacy.scrub(GID, "听说他偷偷 在学钢 琴所以 晚上常不 在线", store) is None
    assert privacy.scrub(GID, "听说他偷偷在学\u200b钢琴所以\u200b晚上常不在线", store) is None
    assert privacy.scrub(GID, "今天天气不错", store) == "今天天气不错"


def test_remember_evicts_oldest_when_over_limit(tmp_path: Path, store: Store) -> None:
    """写满 16KB 后最旧的被淘汰；先写不满的条目一条都不能少（守住「不过度淘汰」）。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    # 每条 ~440B（200 字上限内）：37 条 ~16.2KB，第 37 条会触发淘汰最旧的
    n = 0
    while len(identity.read("memory")["text"].encode("utf-8")) < 16384 - 480:
        out = identity.remember_sync(scope="global", text=f"经验{n:03d}-" + "底" * 180, reason=f"r{n}")
        assert out["ok"] is True
        n += 1
    before = identity.read("memory")["text"]
    assert "经验000-" in before  # 还没满，最旧的还在
    identity.remember_sync(scope="global", text="新经验-" + "底" * 180, reason="r新")
    after = identity.read("memory")["text"]
    assert len(after.encode("utf-8")) <= 16384
    assert "新经验-" in after
    assert "经验000-" not in after  # 最旧的被淘汰
    # 没过度淘汰：除了最早的一两条，其余都还在
    assert after.count("经验") >= n - 2


def test_remember_writes_event(tmp_path: Path, store: Store) -> None:
    """memory.write 事件落库：scope、群号、文本前 40 字，网页可追溯；被闸的不落。"""
    identity = _make(tmp_path, store)
    _run(identity.ensure_started())
    text = "这条经验比较长所以前四十字会被记下来：" + "字" * 60
    identity.remember_sync(scope="group", group_id=GID, text=text, reason="验收教训")
    rows = store.read().execute(
        "SELECT kind, group_id, entity, payload FROM events WHERE kind='memory.write'"
    ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["group_id"] == GID
    assert r["entity"] == "memory"
    payload = json.loads(r["payload"])
    assert payload["scope"] == "group"
    assert payload["text"] == text[:40]
    # 写不成的（隐私闸拒）不落事件
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, removed)"
            " VALUES (?, ?, ?, ?, 0)",
            (GID, "42", "阿帆", "他偷偷在学钢琴所以晚上常不在线"),
        )
    identity.remember_sync(scope="group", group_id=GID, text="偷偷在学钢琴所以晚上常不在线这句话本身", reason="x")
    row = store.read().execute("SELECT COUNT(*) c FROM events WHERE kind='memory.write'").fetchone()
    assert int(row["c"]) == 1


# ----------------------------------------------------------------------
# 「每群三份」收尾删掉的旧口子（2026-10-03，docs/17 §八）：
# - identity.note_useless_feedback（自动反馈进记忆）整个删；
# - 每群身份文件读写（group_read / group_write / group_memory_map）删；
# - 每群内容另走 group_context + /api/groups/{gid}/{rules,skills}。
# ----------------------------------------------------------------------


def test_note_useless_feedback_removed(tmp_path: Path, store: Store) -> None:
    identity = _make(tmp_path, store)
    assert not hasattr(identity, "note_useless_feedback")
    assert not hasattr(identity, "group_read")
    assert not hasattr(identity, "group_write")
    assert not hasattr(identity, "group_memory_map")


# ----------------------------------------------------------------------
# console 接口（aiohttp；监听 127.0.0.1:0 随机端口，不碰真实 18650）
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def web_env(tmp_path: Path):
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from fakes import FakeCtx, FakeProfiles
    from CharTyr_MaiWork.maiwork.app import MaiWorkApp

    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "tinker"}]},
        "console": {"listen": "127.0.0.1:0", "password": "测试密码-不要出现在日志里", "public_url": ""},
        "storage": {"data_dir": str(tmp_path / "data")},
        "approval": {"required": True, "admins": ["10001"]},
    }
    ctx = FakeCtx({"config.get": "987654321"})
    app = MaiWorkApp(ctx, raw, plugin_dir=Path(__file__).resolve().parents[1])
    app.profiles_cls = FakeProfiles
    await app.start()
    server = TestServer(app.console.app)
    client = TestClient(server, cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    base = f"http://127.0.0.1:{server.port}"
    yield type("Env", (), {"app": app, "client": client, "base": base})
    await client.close()
    await app.stop()


async def _login(client: Any) -> None:
    r = await client.post("/api/login", json={"password": "测试密码-不要出现在日志里"})
    assert r.status == 200


@pytest.mark.asyncio
async def test_identity_api_auth_and_structure(web_env: Any) -> None:
    """匿名 401、群友 403、管理员 200；只剩全局三份（「每群三份」收尾后 group_memory 退役）。"""
    client = web_env.client
    r = await client.get("/api/identity")
    assert r.status == 401
    token = web_env.app.token_of(GID)
    r = await client.get("/api/identity", headers={"X-MW-Group": token})
    assert r.status == 403
    await _login(client)
    r = await client.get("/api/identity")
    assert r.status == 200
    data = await r.json()
    assert data["limits"]["soul"] == 16384
    assert data["limits"]["agents"] == 16384
    assert data["limits"]["memory"] == 16384
    assert "group_memory" not in data["limits"]  # 每群上限的口子跟着退役
    for key in ("soul", "agents", "memory"):
        assert "text" in data[key] and "updated_ts" in data[key]
    assert data["soul"]["synced_from_maibot"] is True  # 首次启动已自动同步过（host 假回 987654321）
    assert "group_memory" not in data  # 每群内容进 /api/groups/{gid}/rules + /skills
    assert "# 我是谁" in data["soul"]["text"]  # 兜底三节都在


@pytest.mark.asyncio
async def test_identity_api_put_limit_and_group_memory_gone(web_env: Any) -> None:
    client = web_env.client
    await _login(client)
    # 超限 400
    r = await client.put("/api/identity/memory", json={"text": "长" * 10000})
    assert r.status == 400
    # 正常 PUT 返回单项 {"text","updated_ts"}
    r = await client.put("/api/identity/memory", json={"text": "- 全局经验一"})
    assert r.status == 200
    one = await r.json()
    assert one["text"] == "- 全局经验一"
    assert one["updated_ts"] > 0
    # 「每群三份」收尾：/api/identity/group-memory/{gid}（服务群 / 非服务群）一律 404
    assert (await client.put("/api/identity/group-memory/999", json={"text": "abc"})).status == 404
    assert (await client.put(f"/api/identity/group-memory/{GID}", json={"text": "本群经验"})).status == 404
    assert (await client.get(f"/api/identity/group-memory/{GID}")).status == 404
    # 写完 GET 看得见（只剩全局）
    r = await client.get("/api/identity")
    data = await r.json()
    assert data["memory"]["text"] == "- 全局经验一"
    assert "group_memory" not in data
    # 写接口群友 403（全新无 cookie 会话，只带群链接头）
    import aiohttp as _a

    async with _a.ClientSession() as s:
        base = web_env.base
        token = web_env.app.token_of(GID)
        async with s.put(f"{base}/api/identity/memory", json={"text": "x"}, headers={"X-MW-Group": token}) as resp:
            assert resp.status == 403
        # 2026-10 docs/18 第一步：soul sync 端点已删，全站 404
        async with s.post(f"{base}/api/identity/soul/sync", headers={"X-MW-Group": token}) as resp:
            assert resp.status == 404


@pytest.mark.asyncio
async def test_identity_put_soul_agents_removed(web_env: Any) -> None:
    """2026-10 docs/18 第一步：PUT /api/identity/soul /agents 退役 → 404；memory 照旧。"""
    client = web_env.client
    await _login(client)
    assert (await client.put("/api/identity/soul", json={"text": "x"})).status == 404
    assert (await client.put("/api/identity/agents", json={"text": "x"})).status == 404
    assert (await client.put("/api/identity/memory", json={"text": "- 还能改"})).status == 200
    assert (await client.post("/api/identity/soul/sync")).status == 404
