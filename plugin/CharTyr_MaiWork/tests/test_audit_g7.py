"""G7 回归测试：关注成员的注记/个人画像摘要不能出现在群友可见的文字里。

privacy.scrub(group_id, text, store)：
- text 含该群任一关注成员 note / persona.summary 里长度≥8 的连续片段 → None（拒）；
- 干净 → 原样返回。
- 2026-09-27 规则调整（用户同意「能在资讯里点名群友」）：光是出现关注成员的**名字**
  不再拒绝——资讯可以点名说「这条可能对阿帆有用」（依据只能是他在群里公开说过的话）。
  但 note / persona 是管理员私下看的画像，里面的片段仍然不许进群友可见文字。

过闸的位置：
- profile 模型 add/update 条目：被拒 → 丢这一条；
- feeds 资讯 why / body / reason / audience、构想 body·basis：被拒 → 丢这一条；
- topics 开场白：被拒 → 不发；
- coordinator 交付说明：被拒 → 换兜底「做好了：<任务标题>」；
- profile 提示词里关注成员名单只在 personal_profile 为真时给。
persona 的 summary 也进片段来源（note 和它同步，但库里可能只存了 persona）。"""

from __future__ import annotations

from pathlib import Path

from CharTyr_MaiWork import clock
from CharTyr_MaiWork.config import load_settings
from CharTyr_MaiWork.privacy import scrub
from CharTyr_MaiWork.store import Store

GID = "900000001"
MEMBER_NAME = "阿帆"
MEMBER_NOTE = "他最近在备考注册建筑师考试，周三晚上没空"


def _store_with_focus(tmp_path: Path) -> Store:
    store = Store(tmp_path / "t.db")
    store.migrate()
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
            " VALUES (?, 'ufan', ?, ?, 0, 0, 0)",
            (GID, MEMBER_NAME, MEMBER_NOTE),
        )
        # 已移除的不算
        conn.execute(
            "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
            " VALUES (?, 'ugone', '老陈', '', 0, 1, 0)",
            (GID,),
        )
    return store


class TestScrubBasics:
    def test_clean_text_passes(self, tmp_path: Path) -> None:
        store = _store_with_focus(tmp_path)
        assert scrub(GID, "群里在聊数据库迁移", store) == "群里在聊数据库迁移"

    def test_name_alone_allowed(self, tmp_path: Path) -> None:
        """2026-09-27 规则调整（用户同意「能在资讯里点名群友」）：
        只出现关注成员的名字不再拒绝——资讯可以点名。note/persona 片段仍拒。"""
        store = _store_with_focus(tmp_path)
        assert scrub(GID, f"这个点子和{MEMBER_NAME}上次说的很像", store) == f"这个点子和{MEMBER_NAME}上次说的很像"

    def test_short_name_not_rejected(self, tmp_path: Path) -> None:
        """长度<2 的名字不参与（误伤太大）。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
                " VALUES (?, 'u1', '天', '', 0, 0, 0)",
                (GID,),
            )
        assert scrub(GID, "今天天气不错", store) == "今天天气不错"

    def test_note_fragment_rejected(self, tmp_path: Path) -> None:
        store = _store_with_focus(tmp_path)
        # 「备考注册建筑师考试」是 note 里 9 个字的连续片段，命中
        assert scrub(GID, "建议给备考注册建筑师考试的人提前排期", store) is None
        # 同义改写但不含 8 字连续片段的放行（闸只拦「照搬片段」）
        assert scrub(GID, "最近在备考资格考试，别排晚上", store) is not None

    def test_short_note_ignored(self, tmp_path: Path) -> None:
        """长度<8 的 note 不参与。"""
        store = Store(tmp_path / "t.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, pinned, removed, updated)"
                " VALUES (?, 'u1', '老王xyz', '爱钓鱼', 0, 0, 0)",
                (GID,),
            )
        assert scrub(GID, "周末去爱钓鱼的地方", store) is not None

    def test_removed_member_ignored(self, tmp_path: Path) -> None:
        store = _store_with_focus(tmp_path)
        assert scrub(GID, "记得老陈上次提过这个", store) is not None

    def test_group_isolation(self, tmp_path: Path) -> None:
        """别的群的关注成员不影响本群的判定。"""
        store = _store_with_focus(tmp_path)
        assert scrub("999888777", f"{MEMBER_NAME}就是这样的", store) is not None


class TestScrubPersonaSummary:
    """persona 的 summary 也是隐私闸来源（和 note 同一套 8 字片段规则）。"""

    PERSONA_SUMMARY = "最近在折腾自托管的照片备份方案"

    def _store_with_persona(self, tmp_path: Path) -> Store:
        import json as _json

        store = Store(tmp_path / "p.db")
        store.migrate()
        persona = _json.dumps(
            {"summary": self.PERSONA_SUMMARY, "doing": [], "cares": [], "asked": [], "style": ""},
            ensure_ascii=False,
        )
        with store.tx() as conn:
            # 只存了 persona、note 还是空的：旧部署刚升级的样子
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, persona, pinned, removed, updated)"
                " VALUES (?, 'upg', '老王abc', '', ?, 0, 0, 0)",
                (GID, persona),
            )
        return store

    def test_persona_summary_fragment_rejected(self, tmp_path: Path) -> None:
        store = self._store_with_persona(tmp_path)
        # 「自托管的照片备份」是 summary 里 ≥8 字的连续片段 → 拦
        assert scrub(GID, "可以考虑自托管的照片备份的工具", store) is None
        # 不含 8 字连续片段的放行
        assert scrub(GID, "群里在聊照片整理", store) is not None

    def test_removed_member_persona_ignored(self, tmp_path: Path) -> None:
        import json as _json

        store = Store(tmp_path / "p2.db")
        store.migrate()
        persona = _json.dumps({"summary": self.PERSONA_SUMMARY}, ensure_ascii=False)
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, persona, pinned, removed, updated)"
                " VALUES (?, 'u1', '老王xyz', '', ?, 0, 1, 0)",
                (GID, persona),
            )
        assert scrub(GID, "自托管的照片备份确实可以", store) is not None

    def test_bad_persona_json_treated_as_plain_text(self, tmp_path: Path) -> None:
        """库里 persona 不是 JSON（极端旧数据）→ 整个串当 summary 参与片段判断，不炸。"""
        store = Store(tmp_path / "p3.db")
        store.migrate()
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO focus_members (group_id, user_id, name, note, persona, pinned, removed, updated)"
                " VALUES (?, 'u1', '', '', '备考注册建筑师中，周三没空', 0, 0, 0)",
                (GID,),
            )
        # 「备考注册建筑」和裸文本共享 6 字片段——不够 8。用「备考注册建筑师中」补齐 8 字才拦
        assert scrub(GID, "听他说在备考注册建筑", store) is not None
        assert scrub(GID, "这周备考注册建筑师中", store) is None


class TestProfileEntriesScrubbed:
    """2026-09-27 起：名字本身放行，但 note / persona 的片段（私下画像）仍不许进画像条目。"""

    def _profiles(self, tmp_path: Path):
        from CharTyr_MaiWork.profile import Profiles

        store = _store_with_focus(tmp_path)
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        return store, Profiles(store, host=None, models=None, get_settings=lambda: settings)

    def test_model_add_with_member_name_allowed(self, tmp_path: Path) -> None:
        """名字本身可以在画像条目里出现（能点名群友）；但带 note 片段的仍丢。"""
        store, profiles = self._profiles(tmp_path)
        now = clock.now()
        with store.tx() as conn:
            counts = profiles._apply_ops(
                conn,
                GID,
                [
                    {"op": "add", "category": "interest", "text": f"{MEMBER_NAME}喜欢的项目", "evidence": []},
                    {"op": "add", "category": "interest", "text": "他最近在备考注册建筑师考试的事", "evidence": []},
                    {"op": "add", "category": "interest", "text": "NAS 折腾", "evidence": []},
                ],
                [],
                now,
            )
        # 第 1 条（只带名字）+ 第 3 条（干净）进；第 2 条（note 片段「备考注册建筑师考试」9 字）丢
        assert counts["add"] == 2
        rows = store.read().execute("SELECT text FROM profile_entries WHERE group_id=?", (GID,)).fetchall()
        texts = [r["text"] for r in rows]
        assert any("NAS" in t for t in texts)
        assert any(MEMBER_NAME in t for t in texts)
        assert not any("注册建筑师" in t for t in texts)

    def test_model_update_with_note_fragment_dropped(self, tmp_path: Path) -> None:
        store, profiles = self._profiles(tmp_path)
        now = clock.now()
        with store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO profile_entries (group_id, category, text, first_ts, last_ts, updated)"
                " VALUES (?, 'interest', '原条目', 0, 0, 0)",
                (GID,),
            )
            eid = int(cur.lastrowid)
            profiles._apply_ops(
                conn,
                GID,
                [{"op": "update", "id": eid, "text": "建议大家备考注册建筑师考试时结伴"}],
                [],
                now,
            )
        row = store.read().execute("SELECT text FROM profile_entries WHERE id=?", (eid,)).fetchone()
        assert row["text"] == "原条目"  # 更新被拒（note 片段），保持原文


class TestTopicsOpenerScrubbed:
    def test_opener_with_member_name_now_allowed(self, tmp_path: Path) -> None:
        """topics 的隐私辅助：2026-09-27 起名字本身放行；note 片段仍拦。"""
        from CharTyr_MaiWork.topics import Topics

        store = _store_with_focus(tmp_path)
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        topics = Topics(
            store, host=None, models=None, jev=None, profiles=None,
            mentions=None, pushes=None, get_settings=lambda: settings, signals=None,
        )
        # 名字本身可以出现（能点名群友）
        assert topics._scrub_group_text(GID, f"{MEMBER_NAME}快看这个") == f"{MEMBER_NAME}快看这个"
        assert topics._scrub_group_text(GID, "冷场了聊聊这个") == "冷场了聊聊这个"
        # note 片段（「备考注册建筑师考试」9 字连续）仍拦
        assert topics._scrub_group_text(GID, "谁给备考注册建筑师考试的朋友带句话") is None


class TestCoordinatorNoteScrubbed:
    def test_note_with_name_allowed_fragment_falls_back(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.coordinator import Coordinator

        store = _store_with_focus(tmp_path)
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        coord = Coordinator(
            store, models=None, workers=None, tools=None, tasks=None, goals=None,
            delivery=None, outbox=None, env=None, profiles=None, get_settings=lambda: settings,
        )
        # 名字本身放行（2026-09-27 调整）
        assert coord._scrub_note(GID, f"做好了，{MEMBER_NAME}看看", "整理 NAS 清单") == f"做好了，{MEMBER_NAME}看看"
        # note 片段仍拦 → 兜底「做好了：<任务标题>」
        assert coord._scrub_note(GID, "给备考注册建筑师考试的朋友顺了一份", "整理 NAS 清单") == "做好了：整理 NAS 清单"
        # 干净 → 原样
        assert coord._scrub_note(GID, "做好了，清单在链接里", "整理 NAS 清单") == "做好了，清单在链接里"


class TestFeedsScrubbed:
    def test_feeds_scrub_helper(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.feeds import Feeds

        store = _store_with_focus(tmp_path)
        settings, _ = load_settings({"groups": {"serve": [{"group": f"qq:{GID}"}]}})
        feeds = Feeds(store, models=None, workers=None, profiles=None, topics=None, get_settings=lambda: settings)
        # 名字本身放行（能点名群友）
        assert feeds._scrub_item_text(GID, f"{MEMBER_NAME}最关心这个") == f"{MEMBER_NAME}最关心这个"
        assert feeds._scrub_item_text(GID, "跟群里的折腾方向一致") is not None
        # note 片段仍拒
        assert feeds._scrub_item_text(GID, "这条适合备考注册建筑师考试的人") is None


class TestProfilePromptFocusGate:
    def _profiles(self, tmp_path: Path, personal: bool, suffix: str = "a"):
        from CharTyr_MaiWork.profile import Profiles

        store = _store_with_focus(tmp_path / suffix)  # 同 tmp_path 多次造库要分开目录
        settings, _ = load_settings(
            {
                "groups": {"serve": [{"group": f"qq:{GID}"}]},
                "focus": {"personal_profile": personal},
            }
        )
        return store, Profiles(store, host=None, models=None, get_settings=lambda: settings)

    def test_focus_list_only_when_personal_profile(self, tmp_path: Path) -> None:
        """personal_profile 开：提示词里有关注成员名单；关：没有。"""
        _, profiles_on = self._profiles(tmp_path, True, suffix="on")
        _, profiles_off = self._profiles(tmp_path, False, suffix="off")

        class M:
            id = "m1"
            ts = clock.now()
            is_bot = False
            user_name = "群友"
            text = "随便一句"

        msgs = [M()]
        prompt_on = profiles_on._build_prompt(GID, msgs)
        prompt_off = profiles_off._build_prompt(GID, msgs)
        text_on = "\n".join(str(m.get("content")) for m in prompt_on)
        text_off = "\n".join(str(m.get("content")) for m in prompt_off)
        assert MEMBER_NAME in text_on
        assert MEMBER_NAME not in text_off
