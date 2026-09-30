"""QQ 官方机器人（社区 qq-official-adapter）支持测试（2026-09-30）。

读源码得来的事实（docs/06，未实测收发）：
- 宿主平台名也是 "qq"；群 ID 是 group_openid（字母数字串），用户 ID 是 member_openid。
- 没有 api.call：群成员 / 群信息 / 群文件 / 公告 / 相册一概没有。
- at 段适配器会转成 <qqbot-at-user id="openid" />；reply 段忽略。
MaiWork 写法：服务群 "qqbot:<群 openid>"，内部平台 qqbot，对宿主用 qq。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeCtx, hook_message

from CharTyr_MaiWork.maiwork.config import has_onebot, host_platform, load_settings
from CharTyr_MaiWork.maiwork.host import Host, HostError
from CharTyr_MaiWork.maiwork.intake import Intake, Signals

OPENID = "C9F778FE6ADF9D1D1DBE395BF744A33A"


def _cfg(serve):
    return {
        "plugin": {"enabled": True},
        "groups": {"serve": serve},
        "models": {"base_url": "https://x/v1", "api_key": "sk", "main": "m", "worker": "w"},
        "storage": {"data_dir": "/tmp/d"},
    }


class TestQQBotConfig:
    def test_qqbot_group_accepted(self) -> None:
        s, problems = load_settings(_cfg([{"group": f"qqbot:{OPENID}"}]))
        assert not problems
        assert s.platform_of(OPENID) == "qqbot"
        assert s.workspace_of(OPENID) == f"g{OPENID}"

    def test_qqbot_digits_rejected_with_hint(self) -> None:
        s, problems = load_settings(_cfg([{"group": "qqbot:900000001"}]))
        assert s.groups == {}
        assert any("qq:900000001" in p for p in problems)

    def test_qqbot_bad_chars_rejected(self) -> None:
        s, _ = load_settings(_cfg([{"group": "qqbot:abc::x"}]))
        assert s.groups == {}

    def test_mixed_with_snowluma(self) -> None:
        s, _ = load_settings(_cfg([{"group": "qq:900000001"}, {"group": f"qqbot:{OPENID}"}]))
        assert s.platform_of("900000001") == "qq"
        assert s.platform_of(OPENID) == "qqbot"

    def test_platform_helpers(self) -> None:
        assert host_platform("qqbot") == "qq"
        assert host_platform("telegram") == "telegram"
        assert has_onebot("qq") and not has_onebot("qqbot") and not has_onebot("telegram")


class TestQQBotIntake:
    def _mk(self, serve):
        settings = load_settings(_cfg([{"group": g} for g in serve]))[0]
        signals = Signals()
        return Intake(lambda: settings, signals, bot_qq=lambda: "100000002", spawn=lambda c: c.close()), signals

    @pytest.mark.asyncio
    async def test_official_message_platform_qq_accepted(self) -> None:
        intake, signals = self._mk([f"qqbot:{OPENID}"])
        msg = hook_message(group_id=OPENID, user_id="E1F2A3B4C5D6")  # 宿主平台名是 qq
        await intake.handle(msg)
        assert OPENID in signals._map

    @pytest.mark.asyncio
    async def test_official_group_telegram_platform_ignored(self) -> None:
        intake, signals = self._mk([f"qqbot:{OPENID}"])
        msg = hook_message(group_id=OPENID, user_id="E1F2")
        msg["message"]["platform"] = "telegram"
        await intake.handle(msg)
        assert OPENID not in signals._map


class TestQQBotHost:
    def _host(self, ctx):
        settings, _ = load_settings(_cfg([{"group": "qq:900000001"}, {"group": f"qqbot:{OPENID}"}]))
        h = Host(ctx, bot_qq="100000002")
        h.set_platform_resolver(settings.platform_of)
        return h

    @pytest.mark.asyncio
    async def test_session_lookup_uses_host_platform_qq(self) -> None:
        ctx = FakeCtx({"chat.get_group_streams": {"streams": [
            {"group_id": "900000001", "session_id": "s-snow", "account_id": "100000002"},
            {"group_id": OPENID, "session_id": "s-off", "account_id": "1000000000000000001"},
        ]}})
        h = self._host(ctx)
        assert await h.session_for_group(OPENID) == "s-off"
        assert ctx.calls[0][1]["platform"] == "qq"
        assert h.platform_of_session("s-off") == "qqbot"

    @pytest.mark.asyncio
    async def test_no_napcat_calls(self) -> None:
        ctx = FakeCtx()
        h = self._host(ctx)
        assert await h.group_member_role(OPENID, "E1F2") == ""
        assert await h.group_member_card(OPENID, "E1F2") == {}
        with pytest.raises(HostError):
            await h.upload_group_file(OPENID, "/tmp/x", "x")
        with pytest.raises(HostError):
            await h.call_adapter("adapter.napcat.group.get_group_info", {"group_id": OPENID})
        assert "api.call" not in ctx.names()

    @pytest.mark.asyncio
    async def test_group_info_from_streams_no_api_call(self) -> None:
        ctx = FakeCtx({"chat.get_group_streams": {"streams": [
            {"group_id": OPENID, "session_id": "s-off", "group_name": OPENID}]}})
        h = self._host(ctx)
        info = await h.group_info(OPENID)
        assert info == {"name": OPENID, "member_count": 0}
        assert ctx.calls[0][1]["platform"] == "qq"

    @pytest.mark.asyncio
    async def test_at_stays_segment_for_qqbot(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "m"},
                       "chat.get_group_streams": {"streams": [{"group_id": OPENID, "session_id": "s-off"}]}})
        h = self._host(ctx)
        sid = await h.session_for_group(OPENID)
        await h.send_text(sid, "看看", at_user="E1F2", at_name="群友")
        segs = [c for c in ctx.calls if c[0] == "send.hybrid"][0][1]["segments"]
        assert {"type": "at", "data": {"target_user_id": "E1F2"}} in segs


class TestQQBotGroupSpace:
    @pytest.mark.asyncio
    async def test_group_space_refused(self) -> None:
        from CharTyr_MaiWork.maiwork.platforms.qq_onebot import GroupSpace
        from CharTyr_MaiWork.maiwork.store import Store

        settings, _ = load_settings(_cfg([{"group": f"qqbot:{OPENID}"}]))
        gs = GroupSpace(FakeCtx(), Store(Path("/tmp/nonexistent-ws/q.sqlite")), lambda: settings)
        assert all(v is False for v in (await gs.capabilities_async(OPENID)).values())
        with pytest.raises(PermissionError, match="平台"):
            await gs.list_files(OPENID)
