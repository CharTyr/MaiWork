"""host.py：唯一调用 ctx.call_capability 的地方。

所有宿主能力调用都从这里走。别的模块禁止出现 call_capability。
负责：
- 统一超时（默认 10 秒，knowledge 15 秒，上传用传入值 +5 秒）。
- 把 SDK 解包后的各种返回格式统一成插件内部的 dataclass。
- 一切异常包装成 HostError（中文简述 + 能力名，不带参数内容）。
"""

from __future__ import annotations

import asyncio
import base64
import time
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .config import _norm_platform, has_onebot, host_platform

logger = logging.getLogger("maiwork.host")

_DEFAULT_TIMEOUT_S = 10.0
_KNOWLEDGE_TIMEOUT_S = 15.0

# 适配器里按关键字收参的接口（SnowLuma 适配器 1.x 线上源码 apis/group.py，2026-10-10 读过）；
# 其余 adapter.napcat.* 动作直通接口签名是 api_action_xxx(params=None)，参数整包放 params。
_TYPED_ADAPTER_APIS = frozenset({
    "adapter.napcat.group.get_group_info",
    "adapter.napcat.group.get_group_member_info",
    "adapter.napcat.group.get_group_member_list",
})
# 参数形状不对时宿主转回的 TypeError 文本（Python 参数绑定失败，动作还没执行）
_SHAPE_MISMATCH_RE = re.compile(
    r"unexpected keyword argument|missing \d+ required (?:positional|keyword-only) argument"
)


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
        # bot.platforms 缓存：解析成 {规范平台名: 账号}（tg: 前缀归一为 telegram:）
        self._bot_accounts_map: dict[str, str] = {}
        self._bot_accounts_cached: bool = False
        self._session_cache: dict[str, str] = {}
        # session_id → 平台（session_for_group 解析时记下），发消息 / 读消息按它认平台
        self._session_platform: dict[str, str] = {}
        # 适配器接口名 → 实测收得下的参数形状（"params" / "flat"），见 _api_call
        self._api_shape: dict[str, str] = {}
        # 群号 → 平台（app 启动时接上 settings.platform_of）；没接就当 qq（老行为）
        self._platform_resolver: Any = None

    def set_platform_resolver(self, resolver: Any) -> None:
        """接上「群号 → 平台」查询（通常是 lambda gid: settings.platform_of(gid)）。
        之后调用方不传 platform 时，按群号自动认平台，老调用点不用改。"""
        self._platform_resolver = resolver

    def _plat(self, group_id: str, platform: str | None) -> str:
        """显式传了用传入的；否则问 resolver；都没有就是 qq。"""
        if platform:
            return str(platform).strip().lower()
        if self._platform_resolver is not None:
            try:
                p = self._platform_resolver(str(group_id))
            except Exception:
                p = ""
            if p:
                return str(p).strip().lower()
        return "qq"

    def set_session_group_resolver(self, resolver: Any) -> None:
        """接上「session_id → 服务群号」查询（app 的内存映射）。会话号来自库 / 收消息钩子、
        不是本 Host 解析出来的时候，靠它认出会话属于哪个平台。"""
        self._session_group_resolver = resolver

    def platform_of_session(self, session_id: str) -> str:
        """会话所在平台：先看 session_for_group 记下的；再按「会话 → 群 → 平台」查；都没有当 qq。"""
        sid = str(session_id)
        hit = self._session_platform.get(sid)
        if hit:
            return hit
        resolver = getattr(self, "_session_group_resolver", None)
        if resolver is not None:
            try:
                gid = str(resolver(sid) or "")
            except Exception:
                gid = ""
            if gid:
                return self._plat(gid, None)
        return "qq"

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
    # 适配器 api.call（参数形状 + 失败不吞）
    # ------------------------------------------------------------------

    async def _api_call(
        self,
        api_name: str,
        args: dict[str, Any],
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> Any:
        """调适配器公开 API；返回 SDK 解包后的 result。

        - 形状：SnowLuma 适配器 1.x 的「动作直通」接口（群文件 / 公告 / 相册……）签名是
          `api_action_xxx(params=None)`，参数要整包放进 `params`；`_TYPED_ADAPTER_APIS`
          里的按关键字收参。老适配器（0.8.x）全是平铺。默认按名字猜，宿主报「多了 / 少了
          关键字」（参数绑定失败，动作根本没执行）就换另一种形状重试一次，成功后记住。
        - 失败：宿主 api.call 失败返回 `{"success": False, "error": ...}`，这里抛 HostError
          （带宿主给的原因，截短），不再当成功吞掉。
        """
        name = str(api_name)
        flat = dict(args or {})
        learned = self._api_shape.get(name)
        if learned is None:
            learned = "flat" if name in _TYPED_ADAPTER_APIS else "params"
        shapes = [learned, "flat" if learned == "params" else "params"]
        last_error = ""
        for i, shape in enumerate(shapes):
            call_args = {"params": flat} if shape == "params" else flat
            result = await self._call(
                "api.call",
                timeout_s=timeout_s,
                api_name=name,
                version="1",
                args=call_args,
            )
            if isinstance(result, dict) and result.get("success") is False:
                last_error = str(result.get("error") or "").strip()
                if i == 0 and _SHAPE_MISMATCH_RE.search(last_error):
                    logger.info("适配器接口 %s 不收 %s 形状的参数，换一种再试", name, shape)
                    continue
                raise HostError(f"{name} 失败: {last_error[:200] or '宿主没说原因'}")
            self._api_shape[name] = shape
            return result
        raise HostError(f"{name} 失败: {last_error[:200] or '参数形状都不对'}")

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
        bot_id: str = "",
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
        # 显式传了用传入的；非 qq 会话用该平台的机器人账号；否则用缓存的 bot_qq
        sess_plat = self.platform_of_session(session_id)
        if not bot_id and sess_plat != "qq":
            try:
                bot_id = await self.bot_account(sess_plat)
            except Exception:
                bot_id = ""
        bot_qq = str(bot_id or self._bot_qq or "").strip()
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

    async def message_by_id(
        self,
        message_id: str,
        session_id: str = "",
        *,
        include_binary_data: bool = False,
        _timeout_s: float = 20.0,
    ) -> dict | None:
        """按 message_id 取一条消息（宿主能力 `message.get_by_id`，2026-10-10 实读）。

        参数：`message_id`（必填）、`chat_id`（会话 id，空就不传）、
        `include_binary_data`（要图片 / 表情的 base64 原件时传 True；宿主还留着文件才会给
        `binary_data_base64`）。返回消息 dict；失败 / 没这条 / 拿不到 → None，只记日志不抛。

        超时给 20 秒：原图 base64 走传输帧（上限 16MB），比普通读消息慢。
        """
        kwargs: dict[str, Any] = {
            "message_id": str(message_id),
            "include_binary_data": bool(include_binary_data),
        }
        sid = str(session_id or "").strip()
        if sid:
            kwargs["chat_id"] = sid
        try:
            result = await self._call(
                "message.get_by_id", timeout_s=_timeout_s, **kwargs
            )
        except HostError:
            logger.warning("按 id 取消息失败：%s", str(message_id))
            return None
        except Exception:
            logger.warning("按 id 取消息出错：%s", str(message_id), exc_info=True)
            return None
        if isinstance(result, dict) and result.get("success") is False:
            logger.info("宿主说取不到这条消息：%s", str(message_id))
            return None
        inner = result.get("message") if isinstance(result, dict) and "message" in result else result
        return inner if isinstance(inner, dict) else None

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

    async def person_id(self, user_id: str, *, platform: str = "qq") -> str:
        result = await self._call(
            "person.get_id",
            platform=host_platform(platform),
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
    # bot_qq() / bot_account(platform)
    # ------------------------------------------------------------------

    async def bot_qq(self) -> str:
        """读取 bot.qq_account 并缓存。"""
        if not self._bot_qq_cached:
            val = await self._call("config.get", key="bot.qq_account")
            self._bot_qq = str(val) if val is not None else ""
            self._bot_qq_cached = True
        return self._bot_qq

    async def _bot_accounts(self) -> dict[str, str]:
        """bot.platforms 解析成 {规范平台名: 账号}（tg: 前缀归一为 telegram:）。"""
        if not self._bot_accounts_cached:
            try:
                val = await self._call("config.get", key="bot.platforms")
            except Exception:
                val = None
            out: dict[str, str] = {}
            items = val if isinstance(val, (list, tuple)) else ([val] if isinstance(val, str) and val.strip() else [])
            for raw in items:
                s = str(raw or "").strip()
                if ":" not in s:
                    continue
                platform, _, acc = s.partition(":")
                platform = _norm_platform(platform)
                acc = acc.strip()
                if platform and acc:
                    out[platform] = acc
            self._bot_accounts_map = out
            self._bot_accounts_cached = True
        return self._bot_accounts_map

    def cached_bot_account(self, platform: str) -> str:
        """同步版：只读已缓存的值（收消息钩子里用，不发 RPC）。qq → bot_qq 缓存。"""
        plat = str(platform or "qq").strip().lower()
        if plat in ("", "qq"):
            return self._bot_qq
        return str(self._bot_accounts_map.get(plat) or "")

    async def bot_account(self, platform: str) -> str:
        """某个平台的机器人账号：qq → bot.qq_account；别的从 bot.platforms 里取。"""
        plat = str(platform or "qq").strip().lower()
        if plat in ("", "qq"):
            return await self.bot_qq()
        accounts = await self._bot_accounts()
        return str(accounts.get(plat) or "")

    # ------------------------------------------------------------------
    # session_for_group()
    # ------------------------------------------------------------------

    async def session_for_group(self, group_id: str, *, platform: str | None = None) -> str:
        """按 group_id + platform 解析 session_id，带对应平台的机器人账号路由。按 (platform, 群号) 缓存。
        platform 不传 → 按 set_platform_resolver 接上的配置认（没接就是 qq）。"""
        plat = self._plat(group_id, platform)
        cache_key = f"{plat}:{group_id}"
        if cache_key in self._session_cache:
            return self._session_cache[cache_key]
        picked = await self._pick_group_session(group_id, plat)
        if picked:
            self._remember_session(cache_key, picked, plat)
            return picked
        bot_id = await self.bot_account(plat)
        stream = await self._call(
            "chat.get_stream_by_group_id",
            group_id=group_id,
            platform=host_platform(plat),
            account_id=bot_id,
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
        self._remember_session(cache_key, sid, plat)
        return sid

    def _remember_session(self, cache_key: str, sid: str, plat: str) -> None:
        self._session_cache[cache_key] = sid
        self._session_platform[sid] = plat

    async def _group_streams(self, plat: str) -> list[dict[str, Any]]:
        """chat.get_group_streams(platform=宿主平台名) 的会话列表；拿不到抛 HostError。"""
        listed = await self._call("chat.get_group_streams", platform=host_platform(plat))
        raw = listed.get("streams") if isinstance(listed, dict) else listed
        return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    async def _pick_group_session(self, group_id: str, platform: str = "qq") -> str:
        """一个群号可能对应多条会话记录（线上实测：旧记录 account_id 空、没有消息；
        在用的那条 account_id=机器人本平台的账号）。chat.get_stream_by_group_id 只回第一条匹配，
        可能是旧的，所以这里先列出全部群会话自己挑：
        1) 只有一条 → 就用它；2) 多条 → 优先 account_id 等于机器人本平台账号的；
        3) 还分不出 → 看最近 30 天谁有最新消息。拿不到名单返回 ""（调用方回落单查）。
        """
        plat = str(platform or "qq").strip().lower()
        try:
            raw = await self._group_streams(plat)
        except HostError:
            return ""
        cands = [
            x for x in raw
            if str(x.get("group_id") or "") == str(group_id)
            and str(x.get("session_id") or x.get("stream_id") or "").strip()
        ]
        sid_of = lambda x: str(x.get("session_id") or x.get("stream_id") or "").strip()  # noqa: E731
        if not cands:
            return ""
        if len(cands) == 1:
            return sid_of(cands[0])
        try:
            bot_id = await self.bot_account(plat)
        except HostError:
            bot_id = ""
        mine = [x for x in cands if bot_id and str(x.get("account_id") or "") == str(bot_id)]
        if len(mine) == 1:
            return sid_of(mine[0])
        pool = mine or cands
        now = time.time()
        best, best_ts = sid_of(pool[0]), -1.0
        for x in pool:
            try:
                msgs = await self.messages(sid_of(x), now - 30 * 86400, now, 1, bot_id=bot_id or "-")
            except HostError:
                continue
            ts = max((m.ts for m in msgs), default=-1.0)
            if ts > best_ts:
                best, best_ts = sid_of(x), ts
        return best

    # ------------------------------------------------------------------
    # group_info()
    # ------------------------------------------------------------------

    async def group_info(self, group_id: str, *, platform: str | None = None) -> dict[str, Any]:
        """取群名称和人数。拿不到返回 {}，不抛异常。"""
        plat = self._plat(group_id, platform)
        if not has_onebot(plat):
            # 没有 napcat（Telegram 等）：群名从宿主会话列表的 group_name 取，人数拿不到 → 0
            try:
                raw = await self._group_streams(plat)
            except HostError:
                return {}
            for x in raw:
                if str(x.get("group_id") or "") == str(group_id):
                    name = str(x.get("group_name") or x.get("name") or "")
                    return {"name": name, "member_count": 0}
            return {}
        try:
            result = await self._api_call(
                "adapter.napcat.group.get_group_info", {"group_id": group_id}
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
        at_user: str = "",
        at_name: str = "",
        platform: str | None = None,
    ) -> SendResult:
        """send.hybrid，可选 reply 段 + at 段 + text 段。

        at 段形状照宿主 message_utils._component_from_dict（线上源码 2026-09-29 读过）：
        {"type": "at", "data": {"target_user_id": ...}}；群里显示为真正的 @（2026-09-29 测试群实测）。
        Telegram 适配器完全丢弃 at 段（codecs/outbound.py 的本地段规则——线上事实），
        所以对 Telegram 群要退回到正文里写「@名字 」（at_name 由调用方传，空了就不发 at）。
        QQ 官方机器人（qqbot）照发 at 段：适配器会转成 <qqbot-at-user id="openid" />（读源码，未实测）；
        reply 段它会忽略（官方没有引用回复），不影响发送。
        platform 不传 → 按 session_for_group 记下的会话平台认。
        """
        plat = str(platform).strip().lower() if platform else self.platform_of_session(session_id)
        segments: list[dict[str, Any]] = []
        if reply_to:
            segments.append(
                {"type": "reply", "data": {"target_message_id": str(reply_to)}}
            )
        body = str(text)
        if at_user:
            uid = str(at_user).strip()
            if plat == "telegram":
                name = str(at_name or "").strip()
                if name:
                    # Telegram：ad-hoc at 段会被直接丢弃；落到正文里
                    body = f"@{name} {body}"
            else:
                segments.append({"type": "at", "data": {"target_user_id": uid}})
                body = " " + body
        segments.append({"type": "text", "content": body})
        return await self._send_segments(session_id, segments, str(text))

    async def send_image(self, session_id: str, png: bytes, *, text: str = "") -> SendResult:
        """一张图（+ 可选一段文字）作为同一条消息发出：send.hybrid 的 image 段带 base64。

        image 段形状照宿主 capabilities/core.py _normalize_plugin_segment：content = base64
        → binary_data_base64（线上源码读过）；QQ 里图文同一条的实际效果待实测。
        """
        if not png:
            raise HostError("send_image 没有图片数据")
        segments: list[dict[str, Any]] = [
            {"type": "image", "content": base64.b64encode(bytes(png)).decode("ascii")}
        ]
        if text:
            segments.append({"type": "text", "content": str(text)})
        plain = "[图片]" + (" " + str(text) if text else "")
        return await self._send_segments(session_id, segments, plain)

    async def _send_segments(
        self, session_id: str, segments: list[dict[str, Any]], plain: str
    ) -> SendResult:
        result = await self._call(
            "send.hybrid",
            segments=segments,
            stream_id=session_id,
            return_details=True,
            sync_to_maisaka_history=True,
            storage_message=True,
            processed_plain_text=plain,
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
        *,
        platform: str | None = None,
    ) -> str:
        """上传群文件。上传不幂等：绝不重试。"""
        if not has_onebot(self._plat(group_id, platform)):
            raise HostError("这个平台不支持上传群文件（适配器没有群文件接口），走网页链接交付")
        result = await self._api_call(
            "adapter.napcat.file.upload_group_file",
            {"group_id": group_id, "file": path, "name": name},
            timeout_s=timeout_s + 5.0,
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

    async def group_file_url(self, group_id: str, file_id: str, *, platform: str | None = None) -> str:
        if not has_onebot(self._plat(group_id, platform)):
            raise HostError("这个平台不支持取群文件链接（适配器没有群文件接口），走网页链接交付")
        result = await self._api_call(
            "adapter.napcat.file.get_group_file_url",
            {"group_id": group_id, "file_id": file_id},
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
        *,
        platform: str | None = None,
    ) -> Any:
        """api.call 通用透传：返回 data 部分。status!=\"ok\" / retcode 非 0 → HostError。
        platform 不传 → 按 args 里的 group_id 认平台；没有 napcat 的平台直接拒。"""
        gid = str((args or {}).get("group_id") or "")
        if not has_onebot(self._plat(gid, platform)):
            raise HostError("这个平台的适配器没有 api.call 接口，不支持这个功能")
        result = await self._api_call(str(api_name), dict(args or {}), timeout_s=timeout_s)
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

    async def group_member_role(self, group_id: str, user_id: str, *, platform: str | None = None) -> str:
        """取成员在群里的角色：owner / admin / member / ""（拿不到）。没有 napcat 的平台直接 ""。"""
        if not has_onebot(self._plat(group_id, platform)):
            return ""
        try:
            result = await self._api_call(
                "adapter.napcat.group.get_group_member_info",
                {"group_id": group_id, "user_id": user_id},
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

    async def group_member_card(self, group_id: str, user_id: str, *, platform: str | None = None) -> dict[str, str]:
        """取成员的群名片（card）和 QQ 昵称（nickname）。拿不到返回 {}，不抛异常。

        和 group_member_role 同一个宿主接口（get_group_member_info），参数沿用
        现有调用的纯数字字符串；解析按「data 子表优先、否则整包」的老规矩。
        没有 napcat 的平台直接 {}。
        """
        if not has_onebot(self._plat(group_id, platform)):
            return {}
        try:
            result = await self._api_call(
                "adapter.napcat.group.get_group_member_info",
                {"group_id": group_id, "user_id": user_id},
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
