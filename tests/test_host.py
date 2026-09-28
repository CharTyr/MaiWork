"""host.py 的单元测试：用 FakeCtx 验证能力名、参数、解包、超时、异常包装。"""

from __future__ import annotations

import asyncio
import time

import pytest

from fakes import FakeCtx

from CharTyr_MaiWork.maiwork.host import Host, HostError, Msg, SendResult

# ----------------------------------------------------------------------
# messages()
# ----------------------------------------------------------------------


class TestMessages:
    @pytest.mark.asyncio
    async def test_calls_correct_capability_with_params(self) -> None:
        ctx = FakeCtx({"message.get_by_time_in_chat": []})
        h = Host(ctx)
        result = await h.messages("sid-1", 1_790_000_000.0, 1_790_000_600.0, 50)
        assert result == []
        assert ctx.names() == ["message.get_by_time_in_chat"]
        name, kw = ctx.calls[0]
        assert name == "message.get_by_time_in_chat"
        assert kw["chat_id"] == "sid-1"
        assert kw["start_time"] == 1_790_000_000.0
        assert kw["end_time"] == 1_790_000_600.0
        assert kw["limit"] == 50
        assert kw["filter_mai"] is False
        assert "timeout_ms" in kw

    @pytest.mark.asyncio
    async def test_normalizes_messages_and_sorts_ascending(self) -> None:
        raw = [
            {
                "message_id": "3",
                "timestamp": 1790000600.0,
                "message_info": {"user_info": {"user_id": "u3", "user_nickname": "丙"}},
                "processed_plain_text": "third",
            },
            {
                "message_id": "1",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "u1", "user_nickname": "甲", "user_cardname": "大佬"}},
                "processed_plain_text": "first",
                "is_at": True,
            },
            {
                "message_id": "2",
                "timestamp": 1790000300.0,
                "message_info": {"user_info": {"user_id": "u2", "user_nickname": "乙"}},
                "processed_plain_text": "second",
                "is_picture": True,
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx, bot_qq="99999")
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert [m.id for m in msgs] == ["1", "2", "3"]
        assert msgs[0].user_name == "大佬"
        assert msgs[0].is_at is True
        assert msgs[1].is_picture is True
        assert all(isinstance(m, Msg) for m in msgs)

    @pytest.mark.asyncio
    async def test_name_fallback_cardname_then_nickname_then_userid(self) -> None:
        raw = [
            {
                "message_id": "1",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "u1", "user_cardname": "Card"}},
                "processed_plain_text": "a",
            },
            {
                "message_id": "2",
                "timestamp": 1790000001.0,
                "message_info": {"user_info": {"user_id": "u2", "user_nickname": "Nick"}},
                "processed_plain_text": "b",
            },
            {
                "message_id": "3",
                "timestamp": 1790000002.0,
                "message_info": {"user_info": {"user_id": "u3"}},
                "processed_plain_text": "c",
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx)
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert msgs[0].user_name == "Card"
        assert msgs[1].user_name == "Nick"
        assert msgs[2].user_name == "u3"

    @pytest.mark.asyncio
    async def test_is_bot_flag(self) -> None:
        raw = [
            {
                "message_id": "1",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "99999", "user_nickname": "Bot"}},
                "processed_plain_text": "bot says",
            },
            {
                "message_id": "2",
                "timestamp": 1790000001.0,
                "message_info": {"user_info": {"user_id": "10001", "user_nickname": "Human"}},
                "processed_plain_text": "human says",
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx, bot_qq="99999")
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert msgs[0].is_bot is True
        assert msgs[1].is_bot is False

    @pytest.mark.asyncio
    async def test_skips_notice_prefix(self) -> None:
        raw = [
            {
                "message_id": "notice:group_msg_emoji_like:123",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "u1", "user_nickname": "A"}},
                "processed_plain_text": "notice",
            },
            {
                "message_id": "42",
                "timestamp": 1790000001.0,
                "message_info": {"user_info": {"user_id": "u2", "user_nickname": "B"}},
                "processed_plain_text": "real",
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx)
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert len(msgs) == 1
        assert msgs[0].id == "42"

    @pytest.mark.asyncio
    async def test_reply_to_from_segment_list(self) -> None:
        raw = [
            {
                "message_id": "10",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "u1", "user_nickname": "A"}},
                "processed_plain_text": "reply",
                "raw_message": [
                    {"type": "reply", "data": {"target_message_id": "99"}},
                    {"type": "text", "content": "reply"},
                ],
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx)
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert msgs[0].reply_to == "99"

    @pytest.mark.asyncio
    async def test_reply_to_from_segment_list_alt_keys(self) -> None:
        raw = [
            {
                "message_id": "10",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "u1", "user_nickname": "A"}},
                "processed_plain_text": "reply",
                "raw_message": [
                    {"type": "reply", "data": {"id": "88"}},
                ],
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx)
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert msgs[0].reply_to == "88"

    @pytest.mark.asyncio
    async def test_reply_to_from_cq_string(self) -> None:
        raw = [
            {
                "message_id": "10",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "u1", "user_nickname": "A"}},
                "processed_plain_text": "reply",
                "raw_message": "[CQ:reply,id=77] 你好",
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx)
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert msgs[0].reply_to == "77"

    @pytest.mark.asyncio
    async def test_reply_to_empty_when_not_found(self) -> None:
        raw = [
            {
                "message_id": "10",
                "timestamp": 1790000000.0,
                "message_info": {"user_info": {"user_id": "u1", "user_nickname": "A"}},
                "processed_plain_text": "no reply",
            },
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx)
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert msgs[0].reply_to == ""

    @pytest.mark.asyncio
    async def test_missing_fields_tolerated(self) -> None:
        raw = [
            {"message_id": "1", "timestamp": 1790000000.0},
            {"message_id": "2", "timestamp": 1790000001.0, "message_info": {}},
            {"timestamp": 1790000002.0},
        ]
        ctx = FakeCtx({"message.get_by_time_in_chat": raw})
        h = Host(ctx)
        msgs = await h.messages("sid-1", 0, 2000000000, 100)
        assert len(msgs) == 2
        assert msgs[0].user_name == ""
        assert msgs[0].text == ""

    @pytest.mark.asyncio
    async def test_messages_timeout_raises_host_error(self) -> None:
        async def slow(**kw):
            await asyncio.sleep(60)

        ctx = FakeCtx({"message.get_by_time_in_chat": slow})
        h = Host(ctx)
        with pytest.raises(HostError, match="message.get_by_time_in_chat"):
            await h.messages("sid-1", 0, 2000000000, 100, _timeout_s=0.05)

    @pytest.mark.asyncio
    async def test_messages_exception_raises_host_error(self) -> None:
        ctx = FakeCtx({"message.get_by_time_in_chat": ValueError("boom")})
        h = Host(ctx)
        with pytest.raises(HostError, match="message.get_by_time_in_chat"):
            await h.messages("sid-1", 0, 2000000000, 100)


# ----------------------------------------------------------------------
# knowledge()
# ----------------------------------------------------------------------


class TestKnowledge:
    @pytest.mark.asyncio
    async def test_calls_knowledge_search(self) -> None:
        ctx = FakeCtx({"knowledge.search": "memory text"})
        h = Host(ctx)
        result = await h.knowledge("quantum")
        assert result == "memory text"
        assert ctx.names() == ["knowledge.search"]
        name, kw = ctx.calls[0]
        assert name == "knowledge.search"
        assert kw["query"] == "quantum"
        assert kw["limit"] == 5
        assert "timeout_ms" in kw

    @pytest.mark.asyncio
    async def test_group_id_and_chat_id_passed(self) -> None:
        ctx = FakeCtx({"knowledge.search": "abc"})
        h = Host(ctx)
        await h.knowledge("q", group_id="123", chat_id="sid-9")
        _, kw = ctx.calls[0]
        assert kw["group_id"] == "123"
        assert kw["chat_id"] == "sid-9"

    @pytest.mark.asyncio
    async def test_none_result_returns_empty_string(self) -> None:
        ctx = FakeCtx({"knowledge.search": None})
        h = Host(ctx)
        result = await h.knowledge("quantum")
        assert result == ""

    @pytest.mark.asyncio
    async def test_knowledge_timeout_raises_host_error(self) -> None:
        async def slow(**kw):
            await asyncio.sleep(60)

        ctx = FakeCtx({"knowledge.search": slow})
        h = Host(ctx)
        with pytest.raises(HostError, match="knowledge.search"):
            await h.knowledge("q", _timeout_s=0.05)


# ----------------------------------------------------------------------
# person_id / person_value
# ----------------------------------------------------------------------


class TestPerson:
    @pytest.mark.asyncio
    async def test_person_id(self) -> None:
        ctx = FakeCtx({"person.get_id": "p-123"})
        h = Host(ctx)
        result = await h.person_id("10001")
        assert result == "p-123"
        name, kw = ctx.calls[0]
        assert name == "person.get_id"
        assert kw["platform"] == "qq"
        assert kw["user_id"] == "10001"

    @pytest.mark.asyncio
    async def test_person_value(self) -> None:
        ctx = FakeCtx({"person.get_value": "阿柒"})
        h = Host(ctx)
        result = await h.person_value("p-123", "name")
        assert result == "阿柒"
        name, kw = ctx.calls[0]
        assert name == "person.get_value"
        assert kw["person_id"] == "p-123"
        assert kw["field_name"] == "name"


# ----------------------------------------------------------------------
# config()
# ----------------------------------------------------------------------


class TestConfig:
    @pytest.mark.asyncio
    async def test_config_get(self) -> None:
        ctx = FakeCtx({"config.get": "bar"})
        h = Host(ctx)
        assert await h.config("foo") == "bar"
        name, kw = ctx.calls[0]
        assert name == "config.get"
        assert kw["key"] == "foo"

    @pytest.mark.asyncio
    async def test_config_none_returns_default(self) -> None:
        ctx = FakeCtx({"config.get": None})
        h = Host(ctx)
        assert await h.config("foo", default="dflt") == "dflt"


# ----------------------------------------------------------------------
# bot_qq()
# ----------------------------------------------------------------------


class TestBotQQ:
    @pytest.mark.asyncio
    async def test_bot_qq_from_constructor(self) -> None:
        ctx = FakeCtx()
        h = Host(ctx, bot_qq="12345")
        assert await h.bot_qq() == "12345"
        assert ctx.calls == []

    @pytest.mark.asyncio
    async def test_bot_qq_reads_config_and_caches(self) -> None:
        ctx = FakeCtx({"config.get": "67890"})
        h = Host(ctx)
        assert await h.bot_qq() == "67890"
        assert await h.bot_qq() == "67890"
        assert len(ctx.calls) == 1
        name, kw = ctx.calls[0]
        assert name == "config.get"
        assert kw["key"] == "bot.qq_account"


# ----------------------------------------------------------------------
# session_for_group()
# ----------------------------------------------------------------------


class TestSessionForGroup:
    @pytest.mark.asyncio
    async def test_calls_with_account_id_and_extracts_session_id(self) -> None:
        ctx = FakeCtx(
            {
                "config.get": "12345",
                "chat.get_stream_by_group_id": {"session_id": "sess-abc"},
            }
        )
        h = Host(ctx)
        sid = await h.session_for_group("900000001")
        assert sid == "sess-abc"
        # 先列群会话（没拿到名单就回落到单查）
        assert ctx.names() == ["config.get", "chat.get_group_streams", "chat.get_stream_by_group_id"]
        name, kw = ctx.calls[2]
        assert name == "chat.get_stream_by_group_id"
        assert kw["group_id"] == "900000001"
        assert kw["platform"] == "qq"
        assert kw["account_id"] == "12345"

    @pytest.mark.asyncio
    async def test_stream_id_fallback(self) -> None:
        ctx = FakeCtx(
            {
                "config.get": "12345",
                "chat.get_stream_by_group_id": {"stream_id": "sess-def"},
            }
        )
        h = Host(ctx)
        assert await h.session_for_group("900000001") == "sess-def"

    @pytest.mark.asyncio
    async def test_id_fallback(self) -> None:
        ctx = FakeCtx(
            {
                "config.get": "12345",
                "chat.get_stream_by_group_id": {"id": "sess-ghi"},
            }
        )
        h = Host(ctx)
        assert await h.session_for_group("900000001") == "sess-ghi"

    @pytest.mark.asyncio
    async def test_no_session_id_raises_host_error(self) -> None:
        ctx = FakeCtx(
            {
                "config.get": "12345",
                "chat.get_stream_by_group_id": {"something_else": "x"},
            }
        )
        h = Host(ctx)
        with pytest.raises(HostError, match="chat.get_stream_by_group_id"):
            await h.session_for_group("900000001")

    @pytest.mark.asyncio
    async def test_cached_by_group(self) -> None:
        ctx = FakeCtx(
            {
                "config.get": "12345",
                "chat.get_stream_by_group_id": {"session_id": "sess-abc"},
            }
        )
        h = Host(ctx)
        sid1 = await h.session_for_group("900000001")
        sid2 = await h.session_for_group("900000001")
        assert sid1 == sid2
        assert ctx.names() == ["config.get", "chat.get_group_streams", "chat.get_stream_by_group_id"]

    # 线上实测（2026-09-27）：同一个群号在 MaiBot 里有两条会话记录——
    # 2025 年的旧记录（account_id 空、没有任何消息）和正在用的（account_id=机器人 QQ）。
    # chat.get_stream_by_group_id 只返回第一条匹配（旧的），MaiWork 就一直读空会话。
    @pytest.mark.asyncio
    async def test_multiple_sessions_prefers_bot_account(self) -> None:
        streams = {"success": True, "streams": [
            {"session_id": "old", "group_id": "900000001", "account_id": None},
            {"session_id": "other-group", "group_id": "111", "account_id": "12345"},
            {"session_id": "live", "group_id": "900000001", "account_id": "12345"},
        ]}
        ctx = FakeCtx({"config.get": "12345", "chat.get_group_streams": streams,
                       "chat.get_stream_by_group_id": {"session_id": "old"}})
        h = Host(ctx)
        assert await h.session_for_group("900000001") == "live"
        assert "chat.get_stream_by_group_id" not in ctx.names()

    @pytest.mark.asyncio
    async def test_multiple_sessions_no_account_match_picks_latest_message(self) -> None:
        streams = {"success": True, "streams": [
            {"session_id": "old", "group_id": "900000001", "account_id": None},
            {"session_id": "live", "group_id": "900000001", "account_id": None},
        ]}

        def msgs(**kw):
            if kw.get("chat_id") == "live":
                return [{"message_id": "1", "time": 1_790_000_000.0, "user_info": {"user_id": "1"}, "processed_plain_text": "hi"}]
            return []

        ctx = FakeCtx({"config.get": "12345", "chat.get_group_streams": streams,
                       "message.get_by_time_in_chat": msgs})
        h = Host(ctx)
        assert await h.session_for_group("900000001") == "live"

    @pytest.mark.asyncio
    async def test_single_listed_session_used_directly(self) -> None:
        streams = {"success": True, "streams": [{"session_id": "only", "group_id": "900000001", "account_id": None}]}
        ctx = FakeCtx({"config.get": "12345", "chat.get_group_streams": streams})
        assert await Host(ctx).session_for_group("900000001") == "only"

    def test_manifest_declares_get_group_streams(self) -> None:
        import json
        from pathlib import Path
        m = json.loads((Path(__file__).resolve().parents[1] / "_manifest.json").read_text("utf-8"))
        assert "chat.get_group_streams" in m["capabilities"]


# ----------------------------------------------------------------------
# group_info()
# ----------------------------------------------------------------------


class TestGroupInfo:
    @pytest.mark.asyncio
    async def test_returns_name_and_member_count(self) -> None:
        ctx = FakeCtx(
            {
                "api.call": {
                    "group_name": "折腾研究所",
                    "member_count": 214,
                }
            }
        )
        h = Host(ctx)
        info = await h.group_info("900000001")
        assert info == {"name": "折腾研究所", "member_count": 214}
        name, kw = ctx.calls[0]
        assert name == "api.call"
        assert kw["api_name"] == "adapter.napcat.group.get_group_info"
        assert kw["version"] == "1"
        assert kw["args"]["group_id"] == "900000001"

    @pytest.mark.asyncio
    async def test_unavailable_returns_empty_dict(self) -> None:
        ctx = FakeCtx({"api.call": None})
        h = Host(ctx)
        assert await h.group_info("900000001") == {}

    @pytest.mark.asyncio
    async def test_exception_returns_empty_dict(self) -> None:
        ctx = FakeCtx({"api.call": RuntimeError("no adapter")})
        h = Host(ctx)
        assert await h.group_info("900000001") == {}


# ----------------------------------------------------------------------
# send_text()
# ----------------------------------------------------------------------


class TestSendText:
    @pytest.mark.asyncio
    async def test_calls_send_hybrid_with_correct_params(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "777"}})
        h = Host(ctx)
        result = await h.send_text("sid-1", "hello")
        assert isinstance(result, SendResult)
        assert result.sent is True
        assert result.message_id == "777"
        name, kw = ctx.calls[0]
        assert name == "send.hybrid"
        assert kw["stream_id"] == "sid-1"
        assert kw["return_details"] is True
        assert kw["sync_to_maisaka_history"] is True
        assert kw["storage_message"] is True
        assert kw["processed_plain_text"] == "hello"
        assert kw["segments"] == [{"type": "text", "content": "hello"}]

    @pytest.mark.asyncio
    async def test_reply_segment_added(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": True, "message_id": "888"}})
        h = Host(ctx)
        await h.send_text("sid-1", "hello", reply_to="42")
        name, kw = ctx.calls[0]
        assert kw["segments"][0] == {"type": "reply", "data": {"target_message_id": "42"}}
        assert kw["segments"][1] == {"type": "text", "content": "hello"}

    @pytest.mark.asyncio
    async def test_send_failure_raises_host_error(self) -> None:
        ctx = FakeCtx({"send.hybrid": {"sent": False}})
        h = Host(ctx)
        with pytest.raises(HostError, match="send.hybrid"):
            await h.send_text("sid-1", "hello")


# ----------------------------------------------------------------------
# upload_group_file()
# ----------------------------------------------------------------------


class TestUploadGroupFile:
    @pytest.mark.asyncio
    async def test_upload_success(self) -> None:
        ctx = FakeCtx(
            {
                "api.call": {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"file_id": "abc-123"},
                }
            }
        )
        h = Host(ctx)
        fid = await h.upload_group_file("900000001", "/tmp/file.txt", "readme.txt")
        assert fid == "abc-123"
        name, kw = ctx.calls[0]
        assert name == "api.call"
        assert kw["api_name"] == "adapter.napcat.file.upload_group_file"
        assert kw["version"] == "1"
        assert kw["args"]["group_id"] == "900000001"
        assert kw["args"]["file"] == "/tmp/file.txt"
        assert kw["args"]["name"] == "readme.txt"
        # timeout_s=60 → timeout_ms=(60+5)*1000
        assert kw["timeout_ms"] == 65000

    @pytest.mark.asyncio
    async def test_upload_retcode_nonzero_raises(self) -> None:
        ctx = FakeCtx(
            {
                "api.call": {
                    "status": "failed",
                    "retcode": 1400,
                    "data": {},
                }
            }
        )
        h = Host(ctx)
        with pytest.raises(HostError, match="adapter.napcat.file.upload_group_file"):
            await h.upload_group_file("900000001", "/tmp/file.txt", "readme.txt")

    @pytest.mark.asyncio
    async def test_upload_does_not_retry(self) -> None:
        call_count = 0

        async def flaky(**kw):
            nonlocal call_count
            call_count += 1
            return {"status": "ok", "retcode": 0, "data": {"file_id": "x"}}

        ctx = FakeCtx({"api.call": flaky})
        h = Host(ctx)
        await h.upload_group_file("900000001", "/tmp/file.txt", "readme.txt")
        assert call_count == 1


# ----------------------------------------------------------------------
# group_file_url()
# ----------------------------------------------------------------------


class TestGroupFileUrl:
    @pytest.mark.asyncio
    async def test_returns_url(self) -> None:
        ctx = FakeCtx({"api.call": {"url": "https://example.com/file"}})
        h = Host(ctx)
        url = await h.group_file_url("900000001", "abc-123")
        assert url == "https://example.com/file"
        name, kw = ctx.calls[0]
        assert name == "api.call"
        assert kw["api_name"] == "adapter.napcat.file.get_group_file_url"
        assert kw["version"] == "1"
        assert kw["args"]["group_id"] == "900000001"
        assert kw["args"]["file_id"] == "abc-123"


# ----------------------------------------------------------------------
# proactive_trigger()
# ----------------------------------------------------------------------


class TestProactiveTrigger:
    @pytest.mark.asyncio
    async def test_calls_maisaka_proactive_trigger(self) -> None:
        ctx = FakeCtx({"maisaka.proactive.trigger": {"success": True, "task_id": "t-1"}})
        h = Host(ctx)
        result = await h.proactive_trigger("sid-9", "open_topic")
        assert result["success"] is True
        name, kw = ctx.calls[0]
        assert name == "maisaka.proactive.trigger"
        assert kw["stream_id"] == "sid-9"
        assert kw["intent"] == "open_topic"
        assert kw["reason"] == ""
        assert kw["priority"] == "normal"
        assert kw["metadata"] is None or kw["metadata"] == {}

    @pytest.mark.asyncio
    async def test_custom_params(self) -> None:
        ctx = FakeCtx({"maisaka.proactive.trigger": {"success": True}})
        h = Host(ctx)
        await h.proactive_trigger(
            "sid-9", "announce", reason="cold", priority="high", metadata={"k": "v"}
        )
        _, kw = ctx.calls[0]
        assert kw["reason"] == "cold"
        assert kw["priority"] == "high"
        assert kw["metadata"] == {"k": "v"}


# ----------------------------------------------------------------------
# group_member_role()
# ----------------------------------------------------------------------


class TestGroupMemberRole:
    @pytest.mark.asyncio
    async def test_owner(self) -> None:
        ctx = FakeCtx({"api.call": {"role": "owner"}})
        h = Host(ctx)
        assert await h.group_member_role("900000001", "10001") == "owner"

    @pytest.mark.asyncio
    async def test_admin(self) -> None:
        ctx = FakeCtx({"api.call": {"role": "admin"}})
        h = Host(ctx)
        assert await h.group_member_role("900000001", "10001") == "admin"

    @pytest.mark.asyncio
    async def test_member(self) -> None:
        ctx = FakeCtx({"api.call": {"role": "member"}})
        h = Host(ctx)
        assert await h.group_member_role("900000001", "10001") == "member"

    @pytest.mark.asyncio
    async def test_unavailable_returns_empty(self) -> None:
        ctx = FakeCtx({"api.call": None})
        h = Host(ctx)
        assert await h.group_member_role("900000001", "10001") == ""

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self) -> None:
        ctx = FakeCtx({"api.call": RuntimeError("nope")})
        h = Host(ctx)
        assert await h.group_member_role("900000001", "10001") == ""

    @pytest.mark.asyncio
    async def test_calls_correct_api_params(self) -> None:
        ctx = FakeCtx({"api.call": {"role": "owner"}})
        h = Host(ctx)
        await h.group_member_role("900000001", "10001")
        name, kw = ctx.calls[0]
        assert name == "api.call"
        assert kw["api_name"] == "adapter.napcat.group.get_group_member_info"
        assert kw["version"] == "1"
        assert kw["args"]["group_id"] == "900000001"
        assert kw["args"]["user_id"] == "10001"


# ----------------------------------------------------------------------
# timeout_ms 通用
# ----------------------------------------------------------------------


class TestTimeoutMs:
    @pytest.mark.asyncio
    async def test_default_timeout_ms_10000(self) -> None:
        ctx = FakeCtx({"config.get": "x"})
        h = Host(ctx)
        await h.config("foo")
        _, kw = ctx.calls[0]
        assert kw["timeout_ms"] == 10000

    @pytest.mark.asyncio
    async def test_knowledge_timeout_ms_15000(self) -> None:
        ctx = FakeCtx({"knowledge.search": ""})
        h = Host(ctx)
        await h.knowledge("q")
        _, kw = ctx.calls[0]
        assert kw["timeout_ms"] == 15000
