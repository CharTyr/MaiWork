"""Telegram 平台支持测试（2026-11 新增）：服务群配置、intake 平台过滤、host 平台感知、
群空间不可用、交付回落、views platform 字段、approvals 按平台认管理员。

线上事实（docs/06）：
- Telegram 适配器（exynos967.telegram-adapter 1.3.1）平台名 "telegram"。
- 入站群消息 group_info.group_id = 虚拟群 ID：普通群就是 chat_id（负数，如 "-1001234567890"），
  话题群形如 "<chat_id>::tg-topic::mt=<id>"（分隔符 "::tg-topic::"）。
- 机器人自己在 Telegram 的账号：bot_config 的 bot.platforms = ["tg:1000000003"]。
- Telegram 适配器没有任何 api.call 接口（没有群文件、公告、相册、成员身份/名片）。
- 出站：send.hybrid 平台无关，但 at 段被 Telegram 适配器丢弃，要退成正文「@名字 」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fakes import FakeCtx, hook_message

from CharTyr_MaiWork.maiwork.config import load_settings, norm_account
from CharTyr_MaiWork.maiwork.host import Host, HostError
from CharTyr_MaiWork.maiwork.intake import Intake, Signals


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _base_config(data_dir: str = "/tmp/d", *, serve=None, **over) -> dict:
    raw = {
        "plugin": {"enabled": True},
        "groups": {"serve": serve if serve is not None else [{"group": "qq:900000001"}]},
        "models": {"base_url": "https://x/v1", "api_key": "sk", "main": "m", "worker": "w"},
        "storage": {"data_dir": data_dir},
        "approval": {"required": True, "admins": ["qq:10001"]},
    }
    for k, v in over.items():
        raw.setdefault(k, {}).update(v)
    return raw


TG_PLAIN = "-1001234567890"
TG_TOPIC = "-1001234567890::tg-topic::mt=587"


# ===========================================================================
# 1. 配置解析
# ===========================================================================


class TestTelegramGroupConfig:
    def test_telegram_plain_group_accepted(self) -> None:
        s, _ = load_settings(_base_config(serve=[{"group": f"telegram:{TG_PLAIN}"}]))
        assert set(s.groups) == {TG_PLAIN}
        g = s.groups[TG_PLAIN]
        assert g.group_id == TG_PLAIN
        assert g.platform == "telegram"

    def test_telegram_tg_alias_normalized(self) -> None:
        s, _ = load_settings(_base_config(serve=[{"group": f"tg:{TG_PLAIN}"}]))
        g = s.groups[TG_PLAIN]
        assert g.platform == "telegram"

    def test_telegram_topic_group_accepted_and_workspace_sanitized(self) -> None:
        s, _ = load_settings(_base_config(serve=[{"group": f"telegram:{TG_TOPIC}"}]))
        assert set(s.groups) == {TG_TOPIC}
        g = s.groups[TG_TOPIC]
        assert g.platform == "telegram"
        assert re.match(r"^[A-Za-z0-9_-]{1,64}$", g.workspace)
        assert g.workspace != f"g{TG_TOPIC}"

    def test_qq_workspace_name_unchanged(self) -> None:
        s, _ = load_settings(_base_config(serve=[{"group": "qq:900000001"}]))
        g = s.groups["900000001"]
        assert g.workspace == "g900000001"
        assert g.platform == "qq"

    def test_qq_explicit_workspace_kept(self) -> None:
        s, _ = load_settings(_base_config(serve=[{"group": "qq:111", "workspace": "team-a"}]))
        assert s.groups["111"].workspace == "team-a"

    def test_mixed_platforms(self) -> None:
        s, _ = load_settings(
            _base_config(
                serve=[
                    {"group": "qq:900000001"},
                    {"group": f"telegram:{TG_PLAIN}"},
                    {"group": f"telegram:{TG_TOPIC}"},
                ]
            )
        )
        assert set(s.groups) == {"900000001", TG_PLAIN, TG_TOPIC}
        assert s.groups["900000001"].platform == "qq"
        assert s.groups[TG_PLAIN].platform == "telegram"
        assert s.groups[TG_TOPIC].platform == "telegram"

    def test_illegal_group_entries_dropped(self) -> None:
        s, problems = load_settings(
            _base_config(
                serve=[
                    {"group": "qq:abc"},
                    {"group": ""},
                    {"group": "-1001234567890"},
                    {"group": "telegram:"},
                    {"group": f"telegram:{TG_PLAIN}中文"},
                ]
            )
        )
        assert s.groups == {}
        assert len(problems) >= 5

    def test_duplicate_group_id_rejected(self) -> None:
        s, problems = load_settings(
            _base_config(
                serve=[
                    {"group": "telegram:-1001234567890"},
                    {"group": "tg:-1001234567890"},
                ]
            )
        )
        assert set(s.groups) == {TG_PLAIN}
        assert any("重复" in p for p in problems)

    def test_platform_of_known_groups(self) -> None:
        s, _ = load_settings(
            _base_config(
                serve=[
                    {"group": "qq:900000001"},
                    {"group": f"telegram:{TG_PLAIN}"},
                    {"group": f"telegram:{TG_TOPIC}"},
                ]
            )
        )
        assert s.platform_of("900000001") == "qq"
        assert s.platform_of(TG_PLAIN) == "telegram"
        assert s.platform_of(TG_TOPIC) == "telegram"

    def test_platform_of_unknown_defaults_qq(self) -> None:
        s, _ = load_settings(_base_config())
        assert s.platform_of("999") == "qq"
        assert s.platform_of("") == "qq"

    def test_workspace_of_qq_unchanged(self) -> None:
        s, _ = load_settings(_base_config(serve=[{"group": "qq:900000001"}]))
        assert s.workspace_of("900000001") == "g900000001"


# ===========================================================================
# 2. norm_account
# ===========================================================================


class TestNormAccountTelegram:
    def test_tg_alias_normalized(self) -> None:
        assert norm_account("tg:1000000003") == "telegram:1000000003"

    def test_telegram_preserved(self) -> None:
        assert norm_account("telegram:1000000003") == "telegram:1000000003"

    def test_case_insensitive_platform(self) -> None:
        assert norm_account("TG:1000000003") == "telegram:1000000003"

    def test_still_qq_default_for_digits(self) -> None:
        assert norm_account("100000001") == "qq:100000001"

    def test_telegram_admin_accepted(self) -> None:
        s, _ = load_settings(_base_config(approval={"admins": ["tg:1000000003"]}))
        assert s.approval.admins == ("telegram:1000000003",)

    def test_telegram_exempt_accepted(self) -> None:
        s, _ = load_settings(
            _base_config(
                approval={
                    "exempt_groups": [f"telegram:{TG_PLAIN}"],
                    "exempt_users": ["tg:123456"],
                }
            )
        )
        assert s.approval.exempt_groups == (f"telegram:{TG_PLAIN}",)
        assert s.approval.exempt_users == ("telegram:123456",)


# ===========================================================================
# 3. intake.py
# ===========================================================================


class TestIntakePlatformFiltering:
    def _mk(self, serve, **kwargs):
        settings = load_settings(_base_config(serve=[{"group": g} for g in serve]))[0]
        signals = Signals()
        defaults = dict(
            get_settings=lambda: settings,
            signals=signals,
            jev=None,
            approvals=None,
            mentions=None,
            commands=None,
            bot_qq=lambda: "99999",
            spawn=lambda c: None,
        )
        defaults.update(kwargs)
        return Intake(**defaults), signals

    @pytest.mark.asyncio
    async def test_qq_group_marked_served(self) -> None:
        intake, signals = self._mk(serve=["qq:900000001"])
        await intake.handle(hook_message(group_id="900000001", user_id="10001"))
        assert "900000001" in signals._map

    @pytest.mark.asyncio
    async def test_telegram_plain_group_marked_served(self) -> None:
        intake, signals = self._mk(serve=[f"telegram:{TG_PLAIN}"])
        msg = hook_message(group_id=TG_PLAIN, user_id="55501")
        msg["message"]["platform"] = "telegram"
        msg["message"]["message_info"]["platform"] = "telegram"
        await intake.handle(msg)
        assert TG_PLAIN in signals._map

    @pytest.mark.asyncio
    async def test_telegram_topic_group_marked_served(self) -> None:
        intake, signals = self._mk(serve=[f"telegram:{TG_TOPIC}"])
        msg = hook_message(group_id=TG_TOPIC, user_id="55501")
        msg["message"]["platform"] = "telegram"
        msg["message"]["message_info"]["platform"] = "telegram"
        await intake.handle(msg)
        assert TG_TOPIC in signals._map

    @pytest.mark.asyncio
    async def test_same_id_wrong_platform_ignored(self) -> None:
        intake, signals = self._mk(serve=["qq:900000001"])
        msg = hook_message(group_id="900000001", user_id="10001")
        msg["message"]["platform"] = "telegram"
        msg["message"]["message_info"]["platform"] = "telegram"
        await intake.handle(msg)
        assert "900000001" not in signals._map

    @pytest.mark.asyncio
    async def test_telegram_group_qq_platform_ignored(self) -> None:
        intake, signals = self._mk(serve=[f"telegram:{TG_PLAIN}"])
        msg = hook_message(group_id=TG_PLAIN, user_id="10001")
        await intake.handle(msg)
        assert TG_PLAIN not in signals._map

    @pytest.mark.asyncio
    async def test_missing_platform_defaults_qq(self) -> None:
        intake, signals = self._mk(serve=["qq:900000001"])
        msg = hook_message(group_id="900000001", user_id="10001")
        msg["message"].pop("platform", None)
        msg["message"]["message_info"].pop("platform", None)
        await intake.handle(msg)
        assert "900000001" in signals._map

    @pytest.mark.asyncio
    async def test_missing_platform_telegram_group_ignored(self) -> None:
        intake, signals = self._mk(serve=[f"telegram:{TG_PLAIN}"])
        msg = hook_message(group_id=TG_PLAIN, user_id="10001")
        msg["message"].pop("platform", None)
        msg["message"]["message_info"].pop("platform", None)
        await intake.handle(msg)
        assert TG_PLAIN not in signals._map

    @pytest.mark.asyncio
    async def test_bot_own_message_telegram_skipped(self) -> None:
        intake, signals = self._mk(
            serve=[f"telegram:{TG_PLAIN}"],
            bot_qq=lambda: "1000000003",
        )
        msg = hook_message(group_id=TG_PLAIN, user_id="1000000003", is_at=True, text="/mw 帮助")
        msg["message"]["platform"] = "telegram"
        msg["message"]["message_info"]["platform"] = "telegram"
        await intake.handle(msg)
        assert TG_PLAIN in signals._map

    @pytest.mark.asyncio
    async def test_bot_own_message_qq_still_works(self) -> None:
        intake, signals = self._mk(serve=["qq:900000001"], bot_qq=lambda: "99999")
        msg = hook_message(group_id="900000001", user_id="99999", is_at=True, text="/mw 帮助")
        await intake.handle(msg)
        assert "900000001" in signals._map


# ===========================================================================
# 4. host.py
# ===========================================================================


class TestHostPlatformAwareness:
    @pytest.mark.asyncio
    async def test_session_for_group_qq_uses_qq_platform(self) -> None:
        ctx = FakeCtx(
            {
                "chat.get_group_streams": {"streams": [
                    {"group_id": "900000001", "session_id": "sess-qq", "account_id": "99999"},
                ]},
                "config.get": "99999",
            }
        )
        h = Host(ctx)
        sid = await h.session_for_group("900000001", platform="qq")
        assert sid == "sess-qq"
        name, kw = ctx.calls[0]
        assert name == "chat.get_group_streams"
        assert kw["platform"] == "qq"

    @pytest.mark.asyncio
    async def test_session_for_group_telegram_platform_and_account(self) -> None:
        ctx = FakeCtx(
            {
                "chat.get_group_streams": {"streams": [
                    {
                        "group_id": TG_PLAIN,
                        "session_id": "sess-tg",
                        "account_id": "1000000003",
                    },
                ]},
                "config.get": lambda key, **kw: {
                    "bot.qq_account": "99999",
                    "bot.platforms": ["tg:1000000003"],
                }.get(key),
            }
        )
        h = Host(ctx)
        sid = await h.session_for_group(TG_PLAIN, platform="telegram")
        assert sid == "sess-tg"
        name, kw = ctx.calls[0]
        assert name == "chat.get_group_streams"
        assert kw["platform"] == "telegram"

    @pytest.mark.asyncio
    async def test_person_id_uses_platform(self) -> None:
        ctx = FakeCtx({"person.get_id": "person-1"})
        h = Host(ctx)
        pid = await h.person_id("1000000003", platform="telegram")
        assert pid == "person-1"
        name, kw = ctx.calls[0]
        assert name == "person.get_id"
        assert kw["platform"] == "telegram"

    @pytest.mark.asyncio
    async def test_person_id_default_qq_unchanged(self) -> None:
        ctx = FakeCtx({"person.get_id": "person-1"})
        h = Host(ctx)
        pid = await h.person_id("100000001")
        assert pid == "person-1"
        name, kw = ctx.calls[0]
        assert kw["platform"] == "qq"

    @pytest.mark.asyncio
    async def test_messages_is_bot_uses_qq_bot_by_default(self) -> None:
        ctx = FakeCtx({
            "message.get_by_time_in_chat": [
                {
                    "message_id": "1",
                    "timestamp": 1.0,
                    "message_info": {"user_info": {"user_id": "99999"}},
                    "processed_plain_text": "hi",
                },
            ],
        })
        h = Host(ctx, bot_qq="99999")
        msgs = await h.messages("sid", 0, 10, 10)
        assert msgs[0].is_bot is True

    @pytest.mark.asyncio
    async def test_messages_is_bot_with_explicit_bot_id(self) -> None:
        ctx = FakeCtx({
            "message.get_by_time_in_chat": [
                {
                    "message_id": "1",
                    "timestamp": 1.0,
                    "message_info": {"user_info": {"user_id": "1000000003"}},
                    "processed_plain_text": "hi",
                },
            ],
        })
        h = Host(ctx, bot_qq="99999")
        msgs = await h.messages("sid", 0, 10, 10, bot_id="1000000003")
        assert msgs[0].is_bot is True

    @pytest.mark.asyncio
    async def test_group_info_qq_calls_napcat(self) -> None:
        ctx = FakeCtx({
            "api.call": {
                "status": "ok",
                "retcode": 0,
                "data": {"group_name": "测试群", "member_count": 42},
            },
        })
        h = Host(ctx)
        info = await h.group_info("900000001", platform="qq")
        assert info["name"] == "测试群"
        assert info["member_count"] == 42

    @pytest.mark.asyncio
    async def test_group_info_telegram_no_napcat_uses_chat_streams(self) -> None:
        ctx = FakeCtx({
            "chat.get_group_streams": {
                "streams": [
                    {"group_id": TG_PLAIN, "session_id": "s", "account_id": "1000000003", "group_name": "TG 测试群"},
                ],
            },
            "config.get": lambda key, **kw: {
                "bot.qq_account": "99999",
                "bot.platforms": ["tg:1000000003"],
            }.get(key),
        })
        h = Host(ctx)
        info = await h.group_info(TG_PLAIN, platform="telegram")
        assert info["name"] == "TG 测试群"
        assert info["member_count"] == 0
        assert "api.call" not in ctx.names()


# ===========================================================================
# 5. host.py：at 退成文字
# ===========================================================================


class TestSendTextAtFallback:
    @pytest.mark.asyncio
    async def test_qq_at_still_segment(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "m1"}})
        h = Host(ctx)
        await h.send_text("sess", "你好", at_user="10001", at_name="群友甲")
        name, kw = ctx.calls[0]
        assert name == "send.hybrid"
        segs = kw["segments"]
        types = [s["type"] for s in segs]
        assert "at" in types
        assert not any(s["type"] == "text" and "群友甲" in str(s.get("content", "")) for s in segs)

    @pytest.mark.asyncio
    async def test_telegram_at_falls_back_to_text(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "m1"}})
        h = Host(ctx)
        await h.send_text("sess-tg", "你好世界", at_user="55501", at_name="群友甲", platform="telegram")
        name, kw = ctx.calls[0]
        segs = kw["segments"]
        types = [s["type"] for s in segs]
        assert "at" not in types
        text_seg = next(s for s in segs if s["type"] == "text")
        assert text_seg["content"].startswith("@群友甲 ")
        assert "你好世界" in text_seg["content"]


# ===========================================================================
# 6. host.py：Telegram 群的「不支持」
# ===========================================================================


class TestTelegramUnsupportedApis:
    @pytest.mark.asyncio
    async def test_upload_group_file_telegram_raises(self) -> None:
        h = Host(FakeCtx())
        with pytest.raises(HostError):
            await h.upload_group_file(TG_PLAIN, "/tmp/x.txt", "x.txt", platform="telegram")

    @pytest.mark.asyncio
    async def test_group_file_url_telegram_raises(self) -> None:
        h = Host(FakeCtx())
        with pytest.raises(HostError):
            await h.group_file_url(TG_PLAIN, "/abc", platform="telegram")

    @pytest.mark.asyncio
    async def test_group_member_role_telegram_empty_no_call(self) -> None:
        ctx = FakeCtx()
        h = Host(ctx)
        role = await h.group_member_role(TG_PLAIN, "55501", platform="telegram")
        assert role == ""
        assert "api.call" not in ctx.names()

    @pytest.mark.asyncio
    async def test_group_member_card_telegram_empty_no_call(self) -> None:
        ctx = FakeCtx()
        h = Host(ctx)
        info = await h.group_member_card(TG_PLAIN, "55501", platform="telegram")
        assert info == {}
        assert "api.call" not in ctx.names()

    @pytest.mark.asyncio
    async def test_call_adapter_telegram_raises(self) -> None:
        h = Host(FakeCtx())
        with pytest.raises(HostError):
            await h.call_adapter("adapter.telegram.get_chat", {"group_id": TG_PLAIN}, platform="telegram")

    @pytest.mark.asyncio
    async def test_list_apis_still_works_for_telegram(self) -> None:
        h = Host(FakeCtx({"api.list": ["adapter.telegram.send_message"]}))
        assert await h.list_apis() == ["adapter.telegram.send_message"]


# ===========================================================================
# 7. 群空间
# ===========================================================================


class TestTelegramGroupSpace:
    def _mk_group_space(self):
        from CharTyr_MaiWork.maiwork.platforms.qq_onebot import GroupSpace
        from CharTyr_MaiWork.maiwork.store import Store

        store = Store(Path("/tmp/nonexistent-ws/t1.sqlite"))  # 群空间闸在读库之前就拒，不会真的建库
        settings, _ = load_settings(_base_config(serve=[{"group": f"telegram:{TG_PLAIN}"}]))
        host = FakeCtx()
        return GroupSpace(host, store, lambda: settings)

    @pytest.mark.asyncio
    async def test_capabilities_telegram_all_false(self) -> None:
        gs = self._mk_group_space()
        caps = await gs.capabilities_async(TG_PLAIN)
        assert all(v is False for v in caps.values())

    def test_capabilities_sync_all_false(self) -> None:
        gs = self._mk_group_space()
        assert all(v is False for v in gs.capabilities(TG_PLAIN).values())

    def test_role_of_telegram_empty(self) -> None:
        gs = self._mk_group_space()
        assert gs.role_of(TG_PLAIN) == ""

    @pytest.mark.asyncio
    async def test_operations_telegram_raise_permission_error(self) -> None:
        gs = self._mk_group_space()
        for op in ("list_files", "send_notice", "list_albums"):
            with pytest.raises(PermissionError):
                if op == "list_files":
                    await gs.list_files(TG_PLAIN)
                elif op == "send_notice":
                    await gs.send_notice(TG_PLAIN, "公告")
                else:
                    await gs.list_albums(TG_PLAIN)

    @pytest.mark.asyncio
    async def test_operations_telegram_error_message_chinese(self) -> None:
        gs = self._mk_group_space()
        try:
            await gs.list_files(TG_PLAIN)
        except PermissionError as e:
            assert "平台" in str(e) or "不支持" in str(e)
        else:
            pytest.fail("应该抛 PermissionError")


# ===========================================================================
# 8. views
# ===========================================================================


class TestViewsPlatformField:
    def _mk_svc(self, serve):
        from types import SimpleNamespace

        from tests.fakes import FakeProfiles

        class _FakeStore:
            def read(self):
                class _R:
                    def execute(self, *a, **k):
                        return type("Cur", (), {"fetchone": lambda self: None, "fetchall": lambda self: []})()

                return _R()

        class _FakeTasks:
            def running_count(self, gid):
                return 1

            def list_view(self, gid):
                return []

        class _FakeApprovals:
            def pending_view(self, gid):
                return []

            def auto_info_by_task(self, ids):
                return {}

        class _FakeFeeds:
            def today_count(self, gid):
                return 0

            def pref(self, gid):
                return ""

        class _FakeTopics:
            def log_view(self, gid, days=3):
                return []

        class _FakeDelivery:
            def undelivered(self, task_id):
                return False

        class _FakeGoals:
            def view(self, gid):
                return {"member": (), "agent": ()}

        profiles = FakeProfiles()
        profiles.pulse_bins = []
        profiles.usual_gap_value = None
        settings, _ = load_settings(_base_config(serve=serve))
        svc = SimpleNamespace(
            get_settings=lambda: settings,
            store=_FakeStore(),
            profiles=profiles,
            signals=type("S", (), {"last_ts": lambda s, g: 0.0})(),
            tasks=_FakeTasks(),
            approvals=_FakeApprovals(),
            feeds=_FakeFeeds(),
            topics=_FakeTopics(),
            delivery=_FakeDelivery(),
            goals=_FakeGoals(),
            group_space=None,
        )
        return svc

    def test_group_summary_has_platform(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import group_summary

        svc = self._mk_svc([{"group": "qq:900000001"}])
        out = group_summary(svc, "900000001", admin=True)
        assert "platform" in out
        assert out["platform"] == "qq"

    def test_list_summaries_each_has_platform(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import list_summaries

        svc = self._mk_svc(
            [{"group": "qq:900000001"}, {"group": f"telegram:{TG_PLAIN}"}]
        )
        out = list_summaries(svc, admin=True)
        by_id = {o["id"]: o for o in out}
        assert by_id["900000001"]["platform"] == "qq"
        assert by_id[TG_PLAIN]["platform"] == "telegram"

    def test_group_view_has_platform(self) -> None:
        from CharTyr_MaiWork.maiwork.console.views import group_view

        svc = self._mk_svc([{"group": f"telegram:{TG_PLAIN}"}])
        out = group_view(svc, TG_PLAIN, admin=True)
        assert out.get("platform") == "telegram"


# ===========================================================================
# 9. approvals / group_admins 按平台
# ===========================================================================


class TestApprovalsPlatformAwareness:
    def _mk_approvals(self, serve, admins, tmp_path: Path):
        from CharTyr_MaiWork.maiwork.approvals import Approvals
        from CharTyr_MaiWork.maiwork.store import Store

        store = Store(tmp_path / "ap-telegram.sqlite")
        store.migrate()
        settings, _ = load_settings(
            _base_config(serve=[{"group": g} for g in serve], approval={"admins": admins})
        )
        return Approvals(store, lambda: settings, None, None)

    def test_is_admin_qq_default_platform_unchanged(self, tmp_path: Path) -> None:
        ap = self._mk_approvals(["qq:900000001"], ["qq:10001"], tmp_path)
        assert ap.is_admin("10001") is True
        assert ap.is_admin("10002") is False

    def test_is_admin_telegram(self, tmp_path: Path) -> None:
        ap = self._mk_approvals([f"telegram:{TG_PLAIN}"], ["telegram:1000000003"], tmp_path)
        assert ap.is_admin("1000000003", platform="telegram") is True
        assert ap.is_admin("1000000003", platform="qq") is False

    def test_is_admin_tg_alias_matches(self, tmp_path: Path) -> None:
        ap = self._mk_approvals([f"telegram:{TG_PLAIN}"], ["tg:1000000003"], tmp_path)
        assert ap.is_admin("1000000003", platform="telegram") is True

    def test_is_group_admin_telegram(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.group_admins import GroupAdmins
        from CharTyr_MaiWork.maiwork.store import Store

        store = Store(tmp_path / "ga-telegram.sqlite")
        store.migrate()
        settings, _ = load_settings(_base_config(serve=[{"group": f"telegram:{TG_PLAIN}"}]))
        ga = GroupAdmins(store, get_settings=lambda: settings)
        ga.set_accounts(TG_PLAIN, ["telegram:55501"])
        assert ga.is_group_admin(TG_PLAIN, "55501", platform="telegram") is True
        assert ga.is_group_admin(TG_PLAIN, "55501", platform="qq") is False


# ===========================================================================
# 10. outbox / delivery：Telegram 群文件上传始终抛（让现有回落接管）
# ===========================================================================


class TestTelegramDeliveryFallback:
    @pytest.mark.asyncio
    async def test_upload_group_file_telegram_error_message_chinese(self) -> None:
        h = Host(FakeCtx())
        try:
            await h.upload_group_file(TG_PLAIN, "/tmp/x.txt", "x.txt", platform="telegram")
        except HostError as e:
            assert "平台" in str(e) or "不支持" in str(e)
        else:
            pytest.fail("Telegram 群文件上传应该抛 HostError")


# ===========================================================================
# 11. 老调用点不传 platform：Host 按配置 / 会话自动认平台
# ===========================================================================


class TestHostAutoPlatform:
    def _host(self, ctx):
        settings, _ = load_settings(
            _base_config(serve=[{"group": "qq:900000001"}, {"group": f"telegram:{TG_PLAIN}"}])
        )
        h = Host(ctx)
        h.set_platform_resolver(settings.platform_of)
        h.set_session_group_resolver(lambda sid: {"sess-tg": TG_PLAIN, "sess-qq": "900000001"}.get(sid, ""))
        return h

    @pytest.mark.asyncio
    async def test_upload_without_platform_telegram_group_refused(self) -> None:
        ctx = FakeCtx()
        h = self._host(ctx)
        with pytest.raises(HostError):
            await h.upload_group_file(TG_PLAIN, "/tmp/x.txt", "x.txt")
        assert "api.call" not in ctx.names()

    @pytest.mark.asyncio
    async def test_member_role_without_platform_telegram_no_call(self) -> None:
        ctx = FakeCtx()
        h = self._host(ctx)
        assert await h.group_member_role(TG_PLAIN, "55501") == ""
        assert "api.call" not in ctx.names()

    @pytest.mark.asyncio
    async def test_call_adapter_telegram_group_arg_refused(self) -> None:
        h = self._host(FakeCtx())
        with pytest.raises(HostError):
            await h.call_adapter("adapter.napcat.file.get_group_root_files", {"group_id": TG_PLAIN})

    @pytest.mark.asyncio
    async def test_send_text_session_from_db_telegram_at_as_text(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "m1"}})
        h = self._host(ctx)
        await h.send_text("sess-tg", "看看这个", at_user="55501", at_name="群友甲")
        segs = ctx.calls[0][1]["segments"]
        assert "at" not in [s["type"] for s in segs]
        assert segs[-1]["content"].startswith("@群友甲 ")

    @pytest.mark.asyncio
    async def test_send_text_session_from_db_qq_keeps_at_segment(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "m1"}})
        h = self._host(ctx)
        await h.send_text("sess-qq", "看看这个", at_user="10001", at_name="群友甲")
        assert "at" in [s["type"] for s in ctx.calls[0][1]["segments"]]

    @pytest.mark.asyncio
    async def test_messages_telegram_session_marks_bot_by_tg_account(self) -> None:
        ctx = FakeCtx({
            "message.get_by_time_in_chat": [
                {"message_id": "1", "timestamp": 1.0,
                 "message_info": {"user_info": {"user_id": "1000000003"}}, "processed_plain_text": "hi"},
            ],
            "config.get": lambda key, **kw: {"bot.qq_account": "99999", "bot.platforms": ["tg:1000000003"]}.get(key),
        })
        h = self._host(ctx)
        msgs = await h.messages("sess-tg", 0, 10, 10)
        assert msgs[0].is_bot is True

    @pytest.mark.asyncio
    async def test_session_for_group_without_platform_uses_config(self) -> None:
        ctx = FakeCtx({"chat.get_group_streams": {"streams": [
            {"group_id": TG_PLAIN, "session_id": "sess-x", "account_id": "1000000003"}]}})
        h = self._host(ctx)
        assert await h.session_for_group(TG_PLAIN) == "sess-x"
        assert ctx.calls[0][1]["platform"] == "telegram"
        assert h.platform_of_session("sess-x") == "telegram"


class TestIntakeBotAccountPerPlatform:
    @pytest.mark.asyncio
    async def test_telegram_bot_own_message_not_treated_as_command(self) -> None:
        settings = load_settings(_base_config(serve=[{"group": f"telegram:{TG_PLAIN}"}]))[0]
        ran: list = []

        class _Cmds:
            async def handle(self, *a, **k):
                ran.append(a)
                return ""

        intake = Intake(
            lambda: settings, Signals(), jev=None, approvals=None, mentions=None,
            commands=_Cmds(), bot_qq=lambda: "99999",
            bot_account=lambda plat: "1000000003" if plat == "telegram" else "",
            spawn=lambda c: ran.append("spawn") or c.close(),
        )
        msg = hook_message(group_id=TG_PLAIN, user_id="1000000003", text="/mw 帮助")
        msg["message"]["platform"] = "telegram"
        await intake.handle(msg)
        assert ran == []  # 机器人自己发的 /mw 不当指令


class TestSettingsReadBackAndExempt:
    @pytest.mark.asyncio
    async def test_settings_page_reads_back_platform_prefix(self) -> None:
        from CharTyr_MaiWork.maiwork.rules import _flat_base_value

        s, _ = load_settings(_base_config(serve=[{"group": "qq:900000001"}, {"group": f"tg:{TG_PLAIN}"}]))
        got = [r["group"] for r in _flat_base_value(s, "groups", "serve")]
        assert got == ["qq:900000001", f"telegram:{TG_PLAIN}"]

    def test_exempt_group_telegram(self, tmp_path: Path) -> None:
        from CharTyr_MaiWork.maiwork.approvals import Approvals
        from CharTyr_MaiWork.maiwork.store import Store

        store = Store(tmp_path / "ex.sqlite")
        store.migrate()
        settings, _ = load_settings(_base_config(
            serve=[{"group": f"telegram:{TG_PLAIN}"}],
            approval={"exempt_groups": [f"telegram:{TG_PLAIN}"], "exempt_users": ["tg:55501"]},
        ))
        ap = Approvals(store, lambda: settings, None, None)
        assert ap._is_auto(TG_PLAIN, "1") is True
