"""host.py：唯一调用 ctx.call_capability 的地方。

所有宿主能力调用都从这里走。别的模块禁止出现 call_capability。
负责：
- 统一超时（默认 10 秒，knowledge 15 秒，上传用传入值 +5 秒）。
- 把 SDK 解包后的各种返回格式统一成插件内部的 dataclass。
- 一切异常包装成 HostError（中文简述 + 能力名，不带参数内容）。
"""

from __future__ import annotations

import asyncio
import time
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("maiwork.host")

_DEFAULT_TIMEOUT_S = 10.0
_KNOWLEDGE_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


class HostError(Exception):
    """统一的宿主调用失败（含超时）。message 里不带敏感内容。"""


@dataclass
class Msg:
    id: str
    ts: float
    user_id: str
    user_name: str
    text: str
    is_bot: bool
    is_at: bool
    is_picture: bool
    reply_to: str  # 拿不到就 ""


@dataclass
class SendResult:
    sent: bool
    message_id: str


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _to_ms(timeout_s: float) -> int:
    """秒 → 毫秒（int），至少 1ms。"""
    return max(1, int(timeout_s * 1000))


def _parse_reply_to(raw_message: Any) -> str:
    """从 raw_message 解析 reply_to。

    raw_message 可能是：
    - list（消息段列表）：找 type=="reply" 的段，取 data 里 target_message_id / id / message_id
    - str：找 [CQ:reply,id=xxx]
    """
    if isinstance(raw_message, str):
        m = re.search(r"\[CQ:reply,\s*id=([^\]]+)\]", raw_message)
        if m:
            return m.group(1).strip().rstrip(",]")
        return ""
    if isinstance(raw_message, list):
        for seg in raw_message:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") != "reply":
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            for key in ("target_message_id", "id", "message_id"):
                val = data.get(key)
                if val is not None and str(val).strip():
                    return str(val).strip()
        return ""
    return ""


def _extract_name(user_info: dict[str, Any]) -> str:
    """名字回落：cardname → nickname → user_id → ""。"""
    card = str(user_info.get("user_cardname") or "").strip()
    if card:
        return card
    nick = str(user_info.get("user_nickname") or "").strip()
    if nick:
        return nick
    uid = str(user_info.get("user_id") or "").strip()
    if uid:
        return uid
    return ""


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


class Host:
    """宿主能力统一入口。"""

    def __init__(
        self,
        ctx: Any,  # PluginContext / FakeCtx，测试里用 FakeCtx
        *,
        bot_qq: str = "",
    ) -> None:
        self._ctx = ctx
        self._bot_qq: str = bot_qq
        self._bot_qq_cached: bool = bool(bot_qq)
        self._session_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 通用调用
    # ------------------------------------------------------------------

    async def _call(
        self,
        capability: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        **kwargs: Any,
    ) -> Any:
        """带超时和异常包装的 call_capability。

        超时通过 asyncio.wait_for；同时把 timeout_ms 传给 SDK（SDK 支持）。
        任何异常 → HostError， message 里只带能力名，不带参数内容。
        """
        timeout_ms = _to_ms(timeout_s)
        try:
            coro = self._ctx.call_capability(capability, timeout_ms=timeout_ms, **kwargs)
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise HostError(f"调用宿主能力超时: {capability}") from None
        except HostError:
            raise
        except Exception as exc:
            logger.warning("宿主能力 %s 调用失败: %s", capability, type(exc).__name__)
            raise HostError(f"调用宿主能力失败: {capability}") from exc
        return result

    # ------------------------------------------------------------------
    # messages()
    # ------------------------------------------------------------------

    async def messages(
        self,
        session_id: str,
        start: float,
        end: float,
        limit: int,
        *,
        limit_mode: str = "latest",
        _timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> list[Msg]:
        """读取指定会话在 [start, end] 时间段内的消息，按 ts 升序。

        跳过 notice: 开头的 message_id；缺 id 的记录跳过。
        """
        raw = await self._call(
            "message.get_by_time_in_chat",
            timeout_s=_timeout_s,
            chat_id=session_id,
            start_time=start,
            end_time=end,
            limit=limit,
            # 宿主默认 "latest"（区间里最新的 N 条）；往后翻页要 "earliest"，否则会跳过中间的消息
            limit_mode="earliest" if limit_mode == "earliest" else "latest",
            filter_mai=False,
        )
        if not isinstance(raw, list):
            return []
        bot_qq = self._bot_qq  # 直接用缓存值，避免多余的 RPC
        result: list[Msg] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("message_id") or "").strip()
            if not mid:
                continue
            if mid.startswith("notice:"):
                continue
            info = item.get("message_info") or {}
            uinfo = info.get("user_info") or {}
            uid = str(uinfo.get("user_id") or "").strip()
            try:
                ts = float(item.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            # 消息库有 reply_to 列（= 被回复消息的平台 message_id，见 docs/06），优先用它
            reply_to = str(item.get("reply_to") or "").strip() or _parse_reply_to(item.get("raw_message"))
            is_bot = uid == bot_qq and bot_qq != ""
            text = str(item.get("processed_plain_text") or "")
            if not text.strip() and item.get("is_picture"):
                text = "[图片]"  # 图片描述异步生成，入库时常为空（docs/06）
            result.append(
                Msg(
                    id=mid,
                    ts=ts,
                    user_id=uid,
                    user_name=_extract_name(uinfo),
                    text=text,
                    is_bot=is_bot,
                    is_at=bool(item.get("is_at")),
                    is_picture=bool(item.get("is_picture")),
                    reply_to=reply_to,
                )
            )
        result.sort(key=lambda m: m.ts)
        return result

    # ------------------------------------------------------------------
    # knowledge()
    # ------------------------------------------------------------------

    async def knowledge(
        self,
        query: str,
        *,
        group_id: str = "",
        chat_id: str = "",
        limit: int = 5,
        _timeout_s: float = _KNOWLEDGE_TIMEOUT_S,
    ) -> str:
        """查 MaiBot 长期记忆（A_Memorix），返回文本。"""
        extra: dict[str, Any] = {}
        if chat_id:
            extra["chat_id"] = chat_id
        if group_id:
            extra["group_id"] = group_id
        result = await self._call(
            "knowledge.search",
            timeout_s=_timeout_s,
            query=query,
            limit=limit,
            **extra,
        )
        if result is None:
            return ""
        return str(result)

    # ------------------------------------------------------------------
    # person_id / person_value
    # ------------------------------------------------------------------

    async def person_id(self, user_id: str) -> str:
        result = await self._call(
            "person.get_id",
            platform="qq",
            user_id=user_id,
        )
        return str(result) if result is not None else ""

    async def person_value(self, person_id: str, field: str) -> Any:
        return await self._call(
            "person.get_value",
            person_id=person_id,
            field_name=field,
        )

    # ------------------------------------------------------------------
    # config()
    # ------------------------------------------------------------------

    async def config(self, key: str, default: Any = None) -> Any:
        result = await self._call("config.get", key=key)
        if result is None:
            return default
        return result

    # ------------------------------------------------------------------
    # bot_qq()
    # ------------------------------------------------------------------

    async def bot_qq(self) -> str:
        """读取 bot.qq_account 并缓存。"""
        if not self._bot_qq_cached:
            val = await self._call("config.get", key="bot.qq_account")
            self._bot_qq = str(val) if val is not None else ""
            self._bot_qq_cached = True
        return self._bot_qq

    # ------------------------------------------------------------------
    # session_for_group()
    # ------------------------------------------------------------------

    async def session_for_group(self, group_id: str) -> str:
        """按 group_id 解析 session_id，带账号路由。按群号缓存。"""
        if group_id in self._session_cache:
            return self._session_cache[group_id]
        botqq = await self.bot_qq()
        picked = await self._pick_group_session(group_id, botqq)
        if picked:
            self._session_cache[group_id] = picked
            return picked
        stream = await self._call(
            "chat.get_stream_by_group_id",
            group_id=group_id,
            platform="qq",
            account_id=botqq,
        )
        if not isinstance(stream, dict):
            raise HostError(
                f"chat.get_stream_by_group_id 未返回会话信息: group_id={group_id}"
            )
        sid = (
            stream.get("session_id")
            or stream.get("stream_id")
            or stream.get("id")
            or ""
        )
        sid = str(sid).strip()
        if not sid:
            raise HostError(
                f"chat.get_stream_by_group_id 返回的会话里没有 session_id: {stream}"
            )
        self._session_cache[group_id] = sid
        return sid

    async def _pick_group_session(self, group_id: str, botqq: str) -> str:
        """一个群号可能对应多条会话记录（线上实测：旧记录 account_id 空、没有消息；
        在用的那条 account_id=机器人 QQ）。chat.get_stream_by_group_id 只回第一条匹配，
        可能是旧的，所以这里先列出全部群会话自己挑：
        1) 只有一条 → 就用它；2) 多条 → 优先 account_id 等于机器人 QQ 的；
        3) 还分不出 → 看最近 30 天谁有最新消息。拿不到名单返回 ""（调用方回落单查）。
        """
        try:
            listed = await self._call("chat.get_group_streams", platform="qq")
        except HostError:
            return ""
        raw = listed.get("streams") if isinstance(listed, dict) else listed
        if not isinstance(raw, list):
            return ""
        cands = [
            x for x in raw
            if isinstance(x, dict) and str(x.get("group_id") or "") == str(group_id)
            and str(x.get("session_id") or x.get("stream_id") or "").strip()
        ]
        sid_of = lambda x: str(x.get("session_id") or x.get("stream_id") or "").strip()  # noqa: E731
        if not cands:
            return ""
        if len(cands) == 1:
            return sid_of(cands[0])
        mine = [x for x in cands if botqq and str(x.get("account_id") or "") == str(botqq)]
        if len(mine) == 1:
            return sid_of(mine[0])
        pool = mine or cands
        now = time.time()
        best, best_ts = sid_of(pool[0]), -1.0
        for x in pool:
            try:
                msgs = await self.messages(sid_of(x), now - 30 * 86400, now, 1)
            except HostError:
                continue
            ts = max((m.ts for m in msgs), default=-1.0)
            if ts > best_ts:
                best, best_ts = sid_of(x), ts
        return best

    # ------------------------------------------------------------------
    # group_info()
    # ------------------------------------------------------------------

    async def group_info(self, group_id: str) -> dict[str, Any]:
        """取群名称和人数。拿不到返回 {}，不抛异常。"""
        try:
            result = await self._call(
                "api.call",
                api_name="adapter.napcat.group.get_group_info",
                version="1",
                args={"group_id": group_id},
            )
        except HostError:
            return {}
        if not isinstance(result, dict):
            return {}
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        return {
            "name": str(data.get("group_name") or data.get("name") or ""),
            "member_count": int(data.get("member_count") or data.get("member_num") or 0),
        }

    # ------------------------------------------------------------------
    # send_text()
    # ------------------------------------------------------------------

    async def send_text(
        self,
        session_id: str,
        text: str,
        *,
        reply_to: str = "",
    ) -> SendResult:
        """send.hybrid，可选 reply 段 + text 段。"""
        segments: list[dict[str, Any]] = []
        if reply_to:
            segments.append(
                {"type": "reply", "data": {"target_message_id": str(reply_to)}}
            )
        segments.append({"type": "text", "content": str(text)})
        result = await self._call(
            "send.hybrid",
            segments=segments,
            stream_id=session_id,
            return_details=True,
            sync_to_maisaka_history=True,
            storage_message=True,
            processed_plain_text=str(text),
        )
        if not isinstance(result, dict):
            raise HostError("send.hybrid 返回格式异常")
        sent = bool(result.get("sent", False))
        message_id = str(result.get("message_id") or "")
        if not sent:
            raise HostError("send.hybrid 发送失败")
        return SendResult(sent=sent, message_id=message_id)

    # ------------------------------------------------------------------
    # upload_group_file()
    # ------------------------------------------------------------------

    async def upload_group_file(
        self,
        group_id: str,
        path: str,
        name: str,
        timeout_s: float = 60.0,
    ) -> str:
        """上传群文件。上传不幂等：绝不重试。"""
        result = await self._call(
            "api.call",
            timeout_s=timeout_s + 5.0,
            api_name="adapter.napcat.file.upload_group_file",
            version="1",
            args={"group_id": group_id, "file": path, "name": name},
        )
        if not isinstance(result, dict):
            raise HostError("adapter.napcat.file.upload_group_file 返回格式异常")
        status = str(result.get("status") or "").lower()
        retcode = result.get("retcode")
        if status != "ok" or (retcode is not None and int(retcode) != 0):
            raise HostError(
                f"adapter.napcat.file.upload_group_file 失败: status={status}, retcode={retcode}"
            )
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise HostError("adapter.napcat.file.upload_group_file 返回 data 格式异常")
        file_id = str(data.get("file_id") or "").strip()
        if not file_id:
            raise HostError("adapter.napcat.file.upload_group_file 没返回 file_id")
        return file_id

    # ------------------------------------------------------------------
    # group_file_url()
    # ------------------------------------------------------------------

    async def group_file_url(self, group_id: str, file_id: str) -> str:
        result = await self._call(
            "api.call",
            api_name="adapter.napcat.file.get_group_file_url",
            version="1",
            args={"group_id": group_id, "file_id": file_id},
        )
        if isinstance(result, dict):
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            url = str(data.get("url") or "").strip()
            if url:
                return url
        elif isinstance(result, str) and result.strip():
            return result.strip()
        raise HostError("adapter.napcat.file.get_group_file_url 没返回 url")

    # ------------------------------------------------------------------
    # proactive_trigger()
    # ------------------------------------------------------------------

    async def proactive_trigger(
        self,
        session_id: str,
        intent: str,
        reason: str = "",
        priority: str = "normal",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """maisaka.proactive.trigger：请 MaiBot 主动处理一轮。"""
        result = await self._call(
            "maisaka.proactive.trigger",
            stream_id=session_id,
            intent=intent,
            reason=reason,
            priority=priority,
            metadata=metadata if metadata is not None else {},
        )
        if not isinstance(result, dict):
            raise HostError("maisaka.proactive.trigger 返回格式异常")
        return result

    # ------------------------------------------------------------------
    # list_apis()（群空间探测：适配器开放了哪些接口）
    # ------------------------------------------------------------------

    async def list_apis(self) -> list[str]:
        """api.list（独立能力，manifest 要声明）：列出本插件可见的全部 API 名。

        线上宿主返回 {"success": True, "apis": [{"plugin_id", "name", "version", ...}]}；
        SDK 可能已按 "apis" 解包成列表。元素是 dict 取 name，是字符串直接用。
        """
        result = await self._call("api.list")
        raw: Any = result
        if isinstance(result, dict):
            raw = result.get("apis")
        if not isinstance(raw, list):
            raise HostError("api.list 返回格式异常（不是名单）")
        names: list[str] = []
        for x in raw:
            name = x.get("name") if isinstance(x, dict) else x
            if name:
                names.append(str(name))
        return names

    # ------------------------------------------------------------------
    # call_adapter()（api.call 通用透传；群空间用）
    # ------------------------------------------------------------------

    async def call_adapter(
        self,
        api_name: str,
        args: dict[str, Any] | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> Any:
        """api.call 通用透传：返回 data 部分。status!=\"ok\" / retcode 非 0 → HostError。"""
        result = await self._call(
            "api.call",
            timeout_s=timeout_s,
            api_name=str(api_name),
            version="1",
            args=dict(args or {}),
        )
        if not isinstance(result, dict):
            raise HostError(f"{api_name} 返回格式异常")
        status = str(result.get("status") or "").lower()
        retcode = result.get("retcode")
        if status and status != "ok":
            raise HostError(f"{api_name} 失败: status={status}, retcode={retcode}")
        if retcode is not None and int(retcode) != 0:
            raise HostError(f"{api_name} 失败: status={status}, retcode={retcode}")
        data = result.get("data")
        return data if data is not None else {}

    # ------------------------------------------------------------------
    # group_member_role()
    # ------------------------------------------------------------------

    async def group_member_role(self, group_id: str, user_id: str) -> str:
        """取成员在群里的角色：owner / admin / member / ""（拿不到）。"""
        try:
            result = await self._call(
                "api.call",
                api_name="adapter.napcat.group.get_group_member_info",
                version="1",
                args={"group_id": group_id, "user_id": user_id},
            )
        except HostError:
            return ""
        if not isinstance(result, dict):
            return ""
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        role = str(data.get("role") or "").strip()
        if role in ("owner", "admin", "member"):
            return role
        return ""

    # ------------------------------------------------------------------
    # group_member_card()（关注成员的群名片 / QQ 昵称；console 头像与显示名用）
    # ------------------------------------------------------------------

    async def group_member_card(self, group_id: str, user_id: str) -> dict[str, str]:
        """取成员的群名片（card）和 QQ 昵称（nickname）。拿不到返回 {}，不抛异常。

        和 group_member_role 同一个宿主接口（get_group_member_info），参数沿用
        现有调用的纯数字字符串；解析按「data 子表优先、否则整包」的老规矩。
        """
        try:
            result = await self._call(
                "api.call",
                api_name="adapter.napcat.group.get_group_member_info",
                version="1",
                args={"group_id": group_id, "user_id": user_id},
            )
        except HostError:
            return {}
        if not isinstance(result, dict):
            return {}
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        return {
            "card": str(data.get("card") or "").strip(),
            "nickname": str(data.get("nickname") or "").strip(),
        }
