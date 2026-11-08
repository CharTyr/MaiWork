"""个人向资讯「随口带一句」（personal_mention_enabled）+ 构想预览接龙。

两块功能（同一份文件一起 TDD）：

1. `group_push.personal_mention_enabled`：每群默认关的反应式开关。开着的群里，
   在册关注成员**本人**刚说的那条消息命中其个人向资讯（news_items.target_user_id=
   本人）的关键词时，往 planner 备忘追加至多 1 行「你最近看到过这条」。
   全程失败关闭：默认关 / 非服务群 / focus 开关关 / 开关非真 true / 旁人说话 /
   成员已移除 / 隐私闸命中 / 频控超限 → 一律不注入。它是 Mentions 备忘，
   不是 outbox 推送——不占每日额度、不进 KIND_SWITCH。

2. 构想预览（kv `idea.preview.<id>`）：有核验过的 summary / points / sources 时，
   TopicMatcher 接龙行递「顺手查到的实情」+ 出处链接；预览缺失 / 坏掉 / 不合
   法时完全回落到原来的「想到的一个点子」写法。
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from CharTyr_MaiWork.maiwork import clock, group_push
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.delivery import Mentions, TopicMatcher
from CharTyr_MaiWork.maiwork.store import Store

NOW = 1_790_000_000.0
GID = "111"
UID = "10001"
UID2 = "20002"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    holding = [NOW]
    monkeypatch.setattr(clock, "now", lambda: holding[0])
    return holding


def _settings(*, serve: tuple[str, ...] = (GID,), cfg: dict | None = None):
    merged: dict = {"groups": {"serve": [{"group": f"qq:{g}"} for g in serve]}}
    if cfg:
        merged.update(cfg)
    settings, problems = load_settings(merged)
    assert not problems, problems
    return settings


def _make(tmp_path, *, cfg: dict | None = None, serve: tuple[str, ...] = (GID,)):
    store = Store(tmp_path / "t.db")
    store.migrate()
    settings = _settings(serve=serve, cfg=cfg)
    return store, settings, Mentions(store, lambda: settings)


def _seed_group(store: Store, gid: str = GID, session_id: str = "sess-1") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, session_id) VALUES (?, ?)",
            (gid, session_id),
        )


def _seed_focus(store: Store, user_id: str = UID, *, removed: int = 0,
                name: str = "阿帆", note: str = "", persona: str = "") -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, persona, removed, updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (GID, user_id, name, note, persona, removed, NOW),
        )


def _seed_personal_news(
    store: Store,
    *,
    user_id: str = UID,
    title: str = "像素风独立游戏发售",
    summary: str = "像素风独立游戏本周上线",
    keywords=("像素风", "独立游戏", "发售"),
    score: float = 4.2,
    created: float | None = None,
    rejected: int = 0,
    sensitive: int = 0,
    kind: str = "news",
    url: str = "https://example.com/pixel-game",
    up: int = 0,
    down: int = 0,
) -> int:
    """一条个人向资讯（target_user_id 非空）。默认参数满足全部质量门槛。"""
    with store.tx() as conn:
        conn.execute("INSERT INTO news_batches (group_id, slot_ts, created) VALUES (?, 0, 0)", (GID,))
        bid = int(conn.execute("SELECT MAX(id) AS m FROM news_batches").fetchone()["m"])
        cur = conn.execute(
            "INSERT INTO news_items (batch_id, group_id, title, summary, body, keywords,"
            " sources, rejected, sensitive, kind, score, up, down, created, target_user_id)"
            " VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bid, GID, title, summary,
                json.dumps(list(keywords), ensure_ascii=False),
                json.dumps([{"url": url}]) if url else "[]",
                int(rejected), int(sensitive), kind, float(score),
                int(up), int(down),
                (float(created) if created is not None else clock.now()),
                user_id,
            ),
        )
        return int(cur.lastrowid)


def _seed_idea(
    store: Store,
    *,
    title: str = "做个抽签小程序",
    keywords=("抽签", "小程序"),
    body: str = "构想正文：帮群里快速抽签",
    state: str = "new",
    created: float | None = None,
) -> int:
    now = clock.now()
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (group_id, title, body, keywords, state, created, updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                GID, title, body,
                json.dumps(list(keywords), ensure_ascii=False),
                state,
                (float(created) if created is not None else now), now,
            ),
        )
        return int(cur.lastrowid)


def _msg_item(text: str, *, mid: str, is_self: bool = False) -> dict:
    tag = "true" if is_self else "false"
    return {
        "item_type": "UserMessageItem",
        "meta": {},
        "parts": [{"type": "text",
                   "text": f'<message msg_id="{mid}" is_self_message="{tag}">{text}</message>'}],
    }


def _kwargs(chat: list[tuple[str, str, bool]], session_id: str = "sess-1") -> dict:
    """chat: [(文本, msg_id, 是否机器人自己)]，按时间从旧到新。"""
    items = [
        {"item_type": "SystemMessageItem", "meta": {},
         "parts": [{"type": "text", "text": "系统指令"}]},
    ]
    for text, mid, is_self in chat:
        items.append(_msg_item(text, mid=mid, is_self=is_self))
    return {
        "item_schema_version": 1,
        "tool_definitions": [],
        "session_id": session_id,
        "items": items,
    }


def _injected_text(out: dict | None) -> str:
    if out is None:
        return ""
    return out["items"][0]["parts"][0]["text"]


def _u1_speaks() -> dict:
    """u1 刚说了两条命中关键词的消息；最后一条里有具体词。"""
    return _kwargs([
        ("有人在玩什么新游戏吗", "m8", False),
        ("最近有个像素风独立游戏好像发售了", "m9", False),
    ])


def _enable_personal(store: Store, settings: Any) -> dict:
    return group_push.set_config(store, GID, {"personal_mention_enabled": True}, settings)


def _world_with_personal_news(tmp_path, *, cfg: dict | None = None,
                            user_id: str = UID, serve: tuple[str, ...] = (GID,),
                            **news_kw):
    """装齐：服务群 + 在册关注成员 + 本人个人向资讯 + 发言人登记。"""
    store, settings, m = _make(tmp_path, cfg=cfg, serve=serve)
    _seed_group(store)
    _seed_focus(store, user_id=user_id)
    _seed_personal_news(store, user_id=user_id, **news_kw)
    m.note_msg_user(GID, "m8", user_id)
    m.note_msg_user(GID, "m9", user_id)
    return store, settings, m


# ======================================================================
# group_push：personal_mention_enabled 开关本身（默认关、严格 bool、不占推送额度）
# ======================================================================


class TestSwitchDefaults:
    def test_defaults_off_everywhere(self, tmp_path):
        """默认 False：seed / 保守默认 / UNAVAILABLE / 非服务群 全是 False。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings()
        assert group_push.get_config(store, GID, settings)["personal_mention_enabled"] is False
        assert group_push.conservative_defaults()["personal_mention_enabled"] is False
        assert group_push.get_config(store, GID, group_push.UNAVAILABLE)[
            "personal_mention_enabled"] is False
        # 老数据层（settings=None）也一样默认关
        assert group_push.get_config(store, "999")["personal_mention_enabled"] is False

    def test_unserved_and_unavailable_zero_reads(self, tmp_path):
        """非服务群 / UNAVAILABLE：返回 False 且根本不去碰 kv。"""

        class NoReadStore:
            def kv_get(self, key, default=None):
                raise AssertionError("不该读库")

            def tx(self):
                raise AssertionError("不该开事务")

        settings = _settings()
        cfg = group_push.get_config(NoReadStore(), "999", settings)
        assert cfg["personal_mention_enabled"] is False
        cfg = group_push.get_config(NoReadStore(), GID, group_push.UNAVAILABLE)
        assert cfg["personal_mention_enabled"] is False

    def test_set_config_roundtrip(self, tmp_path):
        """set_config 能开能关（BOOL_FIELDS → SETTABLE 自动收下）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings()
        cfg = _enable_personal(store, settings)
        assert cfg["personal_mention_enabled"] is True
        assert group_push.get_config(store, GID, settings)["personal_mention_enabled"] is True
        cfg = group_push.set_config(store, GID, {"personal_mention_enabled": False}, settings)
        assert cfg["personal_mention_enabled"] is False
        # 不是推送开关：没有对应的 since 字段，也不影响三种 outbox 推送
        assert "personal_mention_since" not in cfg
        assert "personal_mention_enabled" not in group_push.SINCE_OF_SWITCH
        assert "personal_mention_enabled" not in group_push.KIND_SWITCH.values()

    @pytest.mark.parametrize("bad", ["true", "false", 1, 0, None, [], 1.5])
    def test_set_config_non_bool_rejected(self, tmp_path, bad):
        """写路径只认真 bool，别的一律拒（一个字段都不落库）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings()
        with pytest.raises(ValueError):
            group_push.set_config(store, GID, {"personal_mention_enabled": bad}, settings)
        assert group_push.get_config(store, GID, settings)["personal_mention_enabled"] is False

    @pytest.mark.parametrize("bad", ["true", 1, None, "false", 0])
    def test_corrupt_bool_reads_as_off(self, tmp_path, bad):
        """库里的开关是坏值（"true"/1/None…）→ 读取一律收紧成 False。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings()
        _enable_personal(store, settings)
        with store.tx() as conn:
            store.kv_set(conn, f"{group_push.KV_PREFIX}{GID}",
                         {"personal_mention_enabled": bad, "daily_max": 3,
                          "quiet_hours": "23:00-08:00"})
        cfg = group_push.get_config(store, GID, settings)
        assert cfg["personal_mention_enabled"] is False

    def test_corrupt_record_reads_as_off(self, tmp_path):
        """整条记录是坏 JSON → 保守默认，开关 False，也不重写这条记录。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        settings = _settings()
        key = f"{group_push.KV_PREFIX}{GID}"
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)",
                (key, "{这不是 json", 0.0),
            )
        cfg = group_push.get_config(store, GID, settings)
        assert cfg["personal_mention_enabled"] is False


# ======================================================================
# Mentions 注入：默认关 → 开着的正常路径 → 各种闸门
# ======================================================================


class TestPersonalMemoInjection:
    def test_default_off_injects_nothing(self, tmp_path):
        """默认关：本人说、关键词全中、资讯分够，也绝不注入。"""
        store, settings, m = _world_with_personal_news(tmp_path)
        assert group_push.get_config(store, GID, settings)["personal_mention_enabled"] is False
        out = m.inject(_u1_speaks())
        text = _injected_text(out)
        assert "像素风独立游戏发售" not in text
        assert "别说怎么留意到的" not in text

    def test_happy_path_when_on(self, tmp_path):
        """开开关 + 本人是 last_speaker + 命中 ≥2 关键词（具体词在本人这条里）→ 注入。"""
        store, settings, m = _world_with_personal_news(tmp_path)
        _enable_personal(store, settings)
        out = m.inject(_u1_speaks())
        text = _injected_text(out)
        assert "别说怎么留意到的" in text
        assert "像素风独立游戏发售" in text
        assert "（有人问出处再给：https://example.com/pixel-game）" in text
        assert "你最近看到过这条" in text
        # 不点名、不提画像（行本身就不含名字）
        assert "阿帆" not in text

    def test_wrong_speaker_never_injects(self, tmp_path):
        """旁人（u2）说同样的关键词：u1 的个人资讯绝不递给他。"""
        store, settings, m = _world_with_personal_news(tmp_path)
        _enable_personal(store, settings)
        # 最后一条变成 u2 说的
        m.note_msg_user(GID, "m9", UID2)
        out = m.inject(_u1_speaks())
        text = _injected_text(out)
        assert "像素风独立游戏发售" not in text
        assert "别说怎么留意到的" not in text

    def test_trailing_empty_message_from_bystander_never_injects(self, tmp_path):
        """u1 刚说完命中词，紧接着旁人 u2 发了纯表情/图片空文本消息（m10）→ 最后发言人是 u2，绝不递 u1 个人资讯。"""
        store, settings, m = _world_with_personal_news(tmp_path)
        _enable_personal(store, settings)
        m.note_msg_user(GID, "m10", UID2)
        kwargs = _kwargs([
            ("有人在玩什么新游戏吗", "m8", False),
            ("最近有个像素风独立游戏好像发售了", "m9", False),
            ("", "m10", False),
        ])
        out = m.inject(kwargs)
        text = _injected_text(out)
        assert "像素风独立游戏发售" not in text
        assert "别说怎么留意到的" not in text

    def test_unknown_speaker_never_injects(self, tmp_path):
        """发言人解析不到（没登记 msg_id→user）→ 不注入。"""
        store, settings, m = _make(tmp_path)
        _seed_group(store)
        _seed_focus(store)
        _seed_personal_news(store)
        _enable_personal(store, settings)
        # 不 note_msg_user：ring 是空的
        out = m.inject(_u1_speaks())
        assert "像素风独立游戏发售" not in _injected_text(out)

    def test_removed_focus_member_never_injects(self, tmp_path):
        """关注成员被移除（removed=1）→ 本人说也不注入。"""
        store, settings, m = _make(tmp_path)
        _seed_group(store)
        _seed_focus(store, removed=1)
        _seed_personal_news(store)
        m.note_msg_user(GID, "m9", UID)
        _enable_personal(store, settings)
        out = m.inject(_kwargs([("最近有个像素风独立游戏好像发售了", "m9", False)]))
        assert "像素风独立游戏发售" not in _injected_text(out)

    @pytest.mark.parametrize("flag", ["personal_profile", "personal_feeds"])
    def test_focus_section_flags_off_never_inject(self, tmp_path, flag):
        """focus.personal_profile / personal_feeds 任一关掉 → 绝不注入。"""
        store, settings, m = _world_with_personal_news(
            tmp_path, cfg={"focus": {flag: False}})
        _enable_personal(store, settings)
        out = m.inject(_u1_speaks())
        assert "像素风独立游戏发售" not in _injected_text(out)
        assert "别说怎么留意到的" not in _injected_text(out)

    def test_keyword_in_last_message_required(self, tmp_path):
        """最近聊到了但**本人这条**没提 → 不注入（≥1 命中必须在本人最后那条里）。"""
        store, settings, m = _world_with_personal_news(tmp_path)
        _enable_personal(store, settings)
        kwargs = _kwargs([
            ("最近有个像素风独立游戏好像发售了", "m8", False),
            ("今天晚饭吃什么", "m9", False),  # u1 本人这条一个字没提
        ])
        out = m.inject(kwargs)
        assert "像素风独立游戏发售" not in _injected_text(out)

    def test_low_quality_news_never_injects(self, tmp_path):
        """分不够 / 被筛 / 敏感 / 口碑差 / 太旧的个人资讯都不会递。"""
        store, settings, m = _world_with_personal_news(tmp_path, score=3.0)
        _seed_personal_news(store, title="被筛的那条", keywords=("像素风", "独立游戏"),
                            rejected=1, score=4.9)
        _seed_personal_news(store, title="敏感的那条", keywords=("像素风", "独立游戏"),
                            sensitive=1, score=4.9)
        _seed_personal_news(store, title="差评的那条", keywords=("像素风", "独立游戏"),
                            up=0, down=3, score=4.9)
        _seed_personal_news(store, title="四天前的那条", keywords=("像素风", "独立游戏"),
                            created=NOW - 4 * 86400, score=4.9)
        _enable_personal(store, settings)
        # score=3.0 < 3.5 的那条本来就过不了；其余各有毛病 → 什么都没有
        out = m.inject(_u1_speaks())
        text = _injected_text(out)
        for bad_title in ("像素风独立游戏发售", "被筛的那条", "敏感的那条",
                          "差评的那条", "四天前的那条"):
            assert bad_title not in text
        assert "别说怎么留意到的" not in text

    def test_unserved_group_never_injects(self, tmp_path):
        """非服务群：哪怕 session 映射还在，也不注入（is_served 复核先挡住）。"""
        store, settings, m = _world_with_personal_news(tmp_path, serve=("999",))
        out = m.inject(_u1_speaks())
        assert out is None

    def test_personal_line_after_priority_rows(self, tmp_path):
        """派活行永远先分预算：派活行和个人资讯都在时，派活排在前面。"""
        store, settings, m = _world_with_personal_news(tmp_path)
        _enable_personal(store, settings)
        m.add(GID, "张三刚才派的活已经接手了", key="request:R-9", ttl_s=3600, turns=3)
        out = m.inject(_u1_speaks())
        text = _injected_text(out)
        assert "张三刚才派的活" in text
        assert "别说怎么留意到的" in text
        assert text.index("张三刚才派的活") < text.index("别说怎么留意到的")


# ======================================================================
# 隐私闸：行里不许带关注成员注记 / 画像摘要片段
# ======================================================================


class TestPrivacyScrub:
    def test_note_fragment_blocks_line(self, tmp_path):
        """资讯标题撞上某个关注成员 note 的 ≥8 字片段 → 整条弃掉（换别的行也没有 → None）。"""
        store, settings, m = _make(tmp_path)
        _seed_group(store)
        _seed_focus(store, note="他最近在追像素风独立游戏动向")
        _seed_personal_news(store, title="像素风独立游戏动向一览")
        m.note_msg_user(GID, "m8", UID)
        m.note_msg_user(GID, "m9", UID)
        _enable_personal(store, settings)
        out = m.inject(_u1_speaks())
        assert "像素风独立游戏动向" not in _injected_text(out)
        assert "别说怎么留意到的" not in _injected_text(out)

    def test_persona_summary_fragment_blocks_line(self, tmp_path):
        """persona.summary 里的 ≥8 字片段出现在行里同样拦下。"""
        store, settings, m = _make(tmp_path)
        _seed_group(store)
        _seed_focus(store, persona=json.dumps(
            {"summary": "他喜欢研究像素风独立游戏发售节奏"}, ensure_ascii=False))
        _seed_personal_news(store, title="像素风独立游戏发售节奏盘点",
                            summary="盘点像素风独立游戏发售节奏")
        m.note_msg_user(GID, "m8", UID)
        m.note_msg_user(GID, "m9", UID)
        _enable_personal(store, settings)
        out = m.inject(_u1_speaks())
        assert "像素风独立游戏发售节奏" not in _injected_text(out)


# ======================================================================
# 频控：30 分钟冷却 + 24h 内最多 2 次（只记真注入的）
# ======================================================================


class TestPersonalFrequencyCap:
    def test_cooldown_and_24h_cap(self, tmp_path, fixed_clock):
        store, settings, m = _world_with_personal_news(tmp_path)
        _enable_personal(store, settings)

        # 第 1 次：注入
        out = m.inject(_u1_speaks())
        assert "别说怎么留意到的" in _injected_text(out)

        # 30 分钟内第 2 次：冷却挡住
        fixed_clock[0] = NOW + 10 * 60
        out = m.inject(_u1_speaks())
        assert "别说怎么留意到的" not in _injected_text(out)

        # 31 分钟后：冷却过了，第 2 次注入放行
        fixed_clock[0] = NOW + 31 * 60
        out = m.inject(_u1_speaks())
        assert "别说怎么留意到的" in _injected_text(out)

        # 24h 内第 3 次：哪怕过了冷却也被 2 次上限挡住
        fixed_clock[0] = NOW + 3 * 3600
        out = m.inject(_u1_speaks())
        assert "别说怎么留意到的" not in _injected_text(out)

        # 24h 窗口过了：又能递了
        fixed_clock[0] = NOW + 25 * 3600
        out = m.inject(_u1_speaks())
        assert "别说怎么留意到的" in _injected_text(out)

    def test_blocked_injection_does_not_consume_quota(self, tmp_path, fixed_clock):
        """被预算挤掉不算一次：这轮没拼进去，冷却表还是空的。"""
        store, settings, m = _world_with_personal_news(tmp_path)
        _enable_personal(store, settings)
        # 把 300 字预算先吃光：超长派活行优先占满，个人资讯根本拼不进去
        m.add(GID, "很长很长的派活说明" + "活" * 400, key="request:R-1",
              ttl_s=3600, turns=3)
        out = m.inject(_u1_speaks())
        text = _injected_text(out)
        assert "别说怎么留意到的" not in text
        # 没记上：冷却 / 24h 配额都没消耗 → 立刻再来一次（预算腾出来）就该进
        store2_m = m  # 同一个 Mentions，记账在它内部
        row = store.read().execute("SELECT turns_left FROM mentions WHERE key='request:R-1'").fetchone()
        assert int(row["turns_left"]) == 2
        fixed_clock[0] = NOW + 1 * 60
        out2 = m.inject(_u1_speaks())  # request:R-1 还在，但只有 2 条 share 预算
        # 派活行截断后仍可能把预算占满 → 这步不断言注入；直接看记账
        assert store2_m._topics._pnews == {}, "没拼进预算不许记频控账"


# ======================================================================
# 构想预览（idea.preview.<id>）对 TopicMatcher 接龙行的增强
# ======================================================================


class TestIdeaPreviewEnrichment:
    def _preview(self, **kw) -> dict:
        base = {
            "summary": "实测：主流抽签工具都不支持群成员权重",
            "points": ["实测：主流抽签工具都不支持群成员权重", "带权重的实现只需百来行"],
            "sources": [{"url": "https://example.com/draw-tool-review", "site": "toolblog"}],
        }
        base.update(kw)
        return base

    def test_valid_preview_enriches_line(self, tmp_path):
        """有核验预览：接龙行递「顺手查到的实情」+ 出处链接。"""
        store, settings, m = _make(tmp_path)
        idea_id = _seed_idea(store)
        with store.tx() as conn:
            store.kv_set(conn, f"idea.preview.{idea_id}", self._preview())
        kwargs = _kwargs([("谁能写个抽签 小程序", "m7", False)])
        lines = m._topics.memo_lines(GID, kwargs)
        assert len(lines) == 1
        line = lines[0][0]
        assert "顺手查到过一点实情" in line
        assert "主流抽签工具都不支持群成员权重" in line
        assert "（有人问出处再给：https://example.com/draw-tool-review）" in line

    def test_preview_without_summary_uses_first_point(self, tmp_path):
        """summary 缺 / 空 → 用首条 points 当正文。"""
        store, settings, m = _make(tmp_path)
        idea_id = _seed_idea(store)
        with store.tx() as conn:
            store.kv_set(conn, f"idea.preview.{idea_id}",
                         self._preview(summary="", points=["首条要点：抽签工具横评"]))
        kwargs = _kwargs([("谁能写个抽签 小程序", "m7", False)])
        lines = m._topics.memo_lines(GID, kwargs)
        assert len(lines) == 1
        assert "首条要点：抽签工具横评" in lines[0][0]

    @pytest.mark.parametrize("preview", [
        "不是对象",
        {"points": [], "sources": [{"url": "https://a.com"}]},
        {"points": ["a", ""], "sources": [{"url": "https://a.com"}]},
        {"points": ["a"], "sources": []},
        {"points": ["a"], "sources": [{"url": "ftp://a.com/x"}]},
        {"points": ["a"], "sources": [{"site": "nourl"}]},
    ])
    def test_invalid_preview_falls_back(self, tmp_path, preview):
        """预览缺 / 不是对象 / 字段不合法 → 原样回落（还是「想到的一个点子」）。"""
        store, settings, m = _make(tmp_path)
        idea_id = _seed_idea(store)
        if preview is not None:
            with store.tx() as conn:
                store.kv_set(conn, f"idea.preview.{idea_id}", preview)
        kwargs = _kwargs([("谁能写个抽签 小程序", "m7", False)])
        lines = m._topics.memo_lines(GID, kwargs)
        assert len(lines) == 1
        line = lines[0][0]
        assert "顺手查到过一点实情" not in line
        assert "想到的一个点子" in line
        assert "构想正文" in line
        assert "（有人问出处再给" not in line  # 构想没链接

    def test_corrupt_kv_never_breaks_candidates(self, tmp_path):
        """kv_get 抛异常也不影响候选加载（兜底还是原行）。"""
        store, settings, m = _make(tmp_path)
        _seed_idea(store)

        class BoomStore:
            def __init__(self, inner):
                self._inner = inner

            def kv_get(self, key, default=None):
                raise RuntimeError("kv 读炸了")

            def __getattr__(self, name):
                return getattr(self._inner, name)

        boom = BoomStore(store)
        tm = TopicMatcher(boom)
        cands = tm.candidates_for(GID)
        assert len(cands) == 1
        assert cands[0]["key"].startswith("idea:")
        assert not cands[0].get("has_preview")
