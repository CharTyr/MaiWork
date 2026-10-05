"""RSS 资讯源（rss.py）。

- 数据：kv["feeds.rss.<群号>"] = [{id, url, title, enabled, added_ts, last_ok_ts, last_error,
  auto, origin, reason, label, trial_until}]；
  每群最多 20 个，url 必须为无 userinfo 的公开 http(s) 地址。
  后五个是自动订阅（docs/10 §九 第二步，auto_sources.py）加的：auto=True = 自动加的源，
  origin = trusted|map|push（来源：门槛 / 来源地图 / 固定 push 清单），reason = 推荐理由
  （给网页看），label = 来源标签（source_name 口径，用来跟拒绝名单比对），
  trial_until = 试用期截止（epoch 秒）。老条目没有这几个键，读的时候补默认值，照常工作。
- 解析：RSS 2.0（channel/item）和 Atom（feed/entry）都认；先用标准库 Expat
  的 DTD 事件拒绝真正的 DOCTYPE（不误伤注释），再交给 xml.etree 解析。
- 取最近 lookback_days 内的条目 {title, url, published, summary(去 HTML 截 500)}；
  每源每轮最多 10 条；超时 15s；响应 ≤2MB。
- 条目并进备料：和搜索子 agent 的候选**合并后走同一套质量门槛**（见 feeds 接线处注释）。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.parsers import expat

import httpx

from . import clock

logger = logging.getLogger("maiwork.rss")

_TIMEOUT_S = 15.0
_MAX_BYTES = 2 * 1024 * 1024  # 2MB
_PER_SOURCE_LIMIT = 10
_GROUP_MAX = 20
_UA = "MaiWork-RSS/1.0"

class RssError(ValueError):
    """中文网名为准的 RSS 业务拒绝（URL 不合法、解析不了、DOCTYPE、太大等）。"""

# ----------------------------------------------------------------------
# kv 存取
# ----------------------------------------------------------------------

def _key(gid: str) -> str:
    return f"feeds.rss.{gid}"

def _load_list(store: Any, gid: str) -> list[dict[str, Any]]:
    raw = store.kv_get(_key(gid))
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for e in raw:
        if isinstance(e, dict) and str(e.get("id") or "") and str(e.get("url") or ""):
            out.append(dict(e))
    return out

def _save_list(store: Any, gid: str, feeds: list[dict[str, Any]]) -> None:
    with store.tx() as conn:
        store.kv_set(conn, _key(gid), feeds)

def list_feeds(store: Any, gid: str) -> list[dict[str, Any]]:
    """按 added_ts/id 稳定排序返回已加源（管理员视图）。

    老条目（第一步之前的 kv 数据）没有 auto/origin/reason/label/trial_until 这几个键，
    这里统一补默认值——网页 / 接线处拿到的形状始终一样。
    """
    return sorted(
        (_with_defaults(e) for e in _load_list(store, gid)),
        key=lambda e: (float(e.get("added_ts") or 0.0), str(e.get("id") or "")),
    )

def add_feed(
    store: Any, gid: str, *, url: str, title: str, feed_id: str, now: float | None = None,
    auto: bool = False, origin: str = "", reason: str = "", label: str = "", trial_until: float = 0.0,
) -> dict[str, Any]:
    """加源（不试取——试取是接口层「先试取，成功才保存」在做）。RssError 拒绝。

    auto/origin/reason/label/trial_until 只有自动订阅（auto_sources.py）会传；
    手动加源不传，落库就是默认值（auto=False、origin=""）。
    """
    url_s = _require_http(url)
    _public_url(url_s)  # 即使绕过网页接口直接保存，也不能存入明显的内网地址或 userinfo。
    now = float(clock.now() if now is None else now)
    feeds = _load_list(store, gid)
    if len(feeds) >= _GROUP_MAX:
        raise RssError(f"每群最多 {_GROUP_MAX} 个 RSS 源，已经加满了")
    if any(str(e.get("url")) == url_s for e in feeds):
        raise RssError("这个 RSS 源已经加过了")
    feed_id_s = str(feed_id or "").strip() or _new_id(feeds)
    if any(str(e.get("id")) == feed_id_s for e in feeds):
        feed_id_s = _new_id(feeds)
    entry = _plain(feed_id_s, url_s, title, True, now, 0.0, "", auto=auto, origin=origin,
                   reason=reason, label=label, trial_until=trial_until)
    feeds.append(entry)
    _save_list(store, gid, feeds)
    return entry

def remove_feed(store: Any, gid: str, feed_id: str) -> dict[str, Any] | None:
    """删源；返回删掉的条目，没有返回 None。"""
    fid = str(feed_id or "")
    feeds = _load_list(store, gid)
    victim = next((e for e in feeds if str(e.get("id")) == fid), None)
    if victim is None:
        return None
    _save_list(store, gid, [e for e in feeds if str(e.get("id")) != fid])
    return victim

def toggle_feed(store: Any, gid: str, feed_id: str, *, enabled: bool) -> dict[str, Any]:
    """开关。没有抛 RssError。"""
    fid = str(feed_id or "")
    feeds = _load_list(store, gid)
    victim = next((e for e in feeds if str(e.get("id")) == fid), None)
    if victim is None:
        raise RssError("没有这个 RSS 源")
    victim["enabled"] = bool(enabled)
    _save_list(store, gid, feeds)
    return victim

def end_trial(store: Any, gid: str, feed_id: str) -> None:
    """自动源过了试用期：trial_until 清零（网页不再显示「试用到」，退订也不再看试用期那条）。没有这个源就不动。"""
    fid = str(feed_id or "")
    feeds = _load_list(store, gid)
    victim = next((e for e in feeds if str(e.get("id")) == fid), None)
    if victim is None or not float(victim.get("trial_until") or 0.0):
        return
    victim["trial_until"] = 0.0
    _save_list(store, gid, feeds)

def mark_checked(store: Any, gid: str, feed_id: str, *, ok_ts: float | None, error: str) -> None:
    """记一次取源结果：ok_ts 非空记 last_ok_ts；error 非空记 last_error（保留上次成功时间）。"""
    fid = str(feed_id or "")
    feeds = _load_list(store, gid)
    victim = next((e for e in feeds if str(e.get("id")) == fid), None)
    if victim is None:
        return
    if ok_ts is not None:
        victim["last_ok_ts"] = float(ok_ts)
    if error:
        victim["last_error"] = str(error)[:200]
    else:
        victim["last_error"] = ""
    _save_list(store, gid, feeds)

def _plain(
    feed_id: str, url: str, title: str, enabled: bool, added_ts: float, last_ok_ts: float, last_error: str,
    *, auto: bool = False, origin: str = "", reason: str = "", label: str = "", trial_until: float = 0.0,
) -> dict[str, Any]:
    return {
        "id": str(feed_id),
        "url": str(url),
        "title": str(title or ""),
        "enabled": bool(enabled),
        "added_ts": float(added_ts),
        "last_ok_ts": float(last_ok_ts or 0.0),
        "last_error": str(last_error or ""),
        # 自动订阅标记（docs/10 §九 第二步）：手动加的源一律默认值。
        "auto": bool(auto),
        "origin": str(origin or ""),
        "reason": str(reason or "")[:200],
        "label": str(label or ""),
        "trial_until": float(trial_until or 0.0),
    }

# 老条目缺的字段：读的时候补上（写入侧照旧只写自己有的键，不动别人的数据）。
_AUTO_DEFAULTS: dict[str, Any] = {
    "auto": False, "origin": "", "reason": "", "label": "", "trial_until": 0.0,
}

def _with_defaults(entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    for key, default in _AUTO_DEFAULTS.items():
        if key not in out or out[key] is None:
            out[key] = default
    out["auto"] = bool(out.get("auto"))
    out["origin"] = str(out.get("origin") or "")
    out["reason"] = str(out.get("reason") or "")
    out["label"] = str(out.get("label") or "")
    out["trial_until"] = float(out.get("trial_until") or 0.0)
    return out

def _new_id(existing: list[dict[str, Any]]) -> str:
    import secrets

    have = {str(e.get("id")) for e in existing}
    for _ in range(20):
        fid = "r" + secrets.token_hex(4)
        if fid not in have:
            return fid
    raise RssError("分配 RSS 源 id 失败")

def _require_http(url: Any) -> str:
    s = str(url or "").strip()
    if not s.startswith(("http://", "https://")):
        raise RssError("RSS 源地址必须以 http:// 或 https:// 开头")
    return s

def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """兼容 inet_aton 的 127.1 / 整数 / 十六进制 IPv4 写法，不让它们冒充域名。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            return None

def _public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # IPv4-mapped IPv6 也按里面的 IPv4 判断，拒绝保留/环回/私网/链路本地地址。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global

def _public_url(url: str) -> httpx.URL:
    """无网络预检；注入 MockTransport 时不做真实 DNS，生产请求还要在连接前查 DNS。"""
    try:
        parsed = httpx.URL(url)
        authority = urlsplit(url).netloc
    except (httpx.InvalidURL, ValueError) as e:
        raise RssError(f"RSS 源地址格式不合法：{e}") from None
    host = parsed.host or ""
    if parsed.scheme not in ("http", "https") or not host:
        raise RssError("RSS 源地址必须有 http(s) 协议和主机名")
    if "@" in authority or parsed.userinfo:
        raise RssError("RSS 源地址不能带用户名或密码")
    # 先规范化再判断：百分号解码、去掉结尾的点、小写。「127.0.0.1.」「%31%32%37.0.0.1」
    # 这类写法以前能存进库（真去取时 DNS 那道会拦）；加源时就拦住（外部审查 2026-10-02）
    host = unquote(host).rstrip(".").lower()
    if not host:
        raise RssError("RSS 源地址必须有 http(s) 协议和主机名")
    if host.endswith(".localhost") or host == "localhost":
        raise RssError("RSS 源地址不能指向本机或内网")
    ip = _literal_ip(host)
    if ip is not None and not _public_address(str(ip)):
        raise RssError("RSS 源地址不能指向本机或内网")
    return parsed

def _resolve_public_ip(host: str, port: int) -> str:
    """生产直连：所有 DNS 答案须为公开地址；失败或混有内网地址一律不连接。"""
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError, UnicodeError):
        raise RssError("RSS 域名解析失败，无法安全取源") from None
    addresses = [str(answer[4][0]) for answer in answers]
    if not addresses:
        raise RssError("RSS 域名解析无结果，无法安全取源")
    if any(not _public_address(address) for address in addresses):
        raise RssError("RSS 源域名解析到本机或内网地址，拒绝取源")
    return addresses[0]

# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------

_TAG_LIKE_RE = re.compile(r"<[^>]+>")
_SUMMARY_MAX = 500

def _reject_doctype(xml_text: str) -> None:
    """按 XML 语法识别 DTD，跨越任意长度注释，且不误伤注释中的字面量。"""
    parser = expat.ParserCreate()

    def on_doctype(*_args: Any) -> None:
        raise RssError("RSS 文档带 <!DOCTYPE（可能藏外部实体），禁止解析")

    parser.StartDoctypeDeclHandler = on_doctype
    try:
        parser.Parse(xml_text, True)
    except expat.ExpatError as e:
        raise RssError(f"XML 解析失败：{e}") from None

def parse_feed(xml_text: str, *, now: float, lookback_days: int) -> dict[str, Any]:
    """RSS 2.0 / Atom → {"title", "items":[{title,url,published,summary}]}；解析不了抛 RssError。"""
    _reject_doctype(xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RssError(f"XML 解析失败：{e}") from None
    title = _feed_title(root)
    since = float(now) - max(1, int(lookback_days)) * 86400.0
    out: list[dict[str, Any]] = []
    for node in _item_nodes(root):
        it = _item_of(node)
        if it is None:
            continue
        pub = it.get("published")
        if isinstance(pub, (int, float)) and float(pub) < since:
            continue
        out.append(it)
        if len(out) >= _PER_SOURCE_LIMIT:
            break
    return {"title": title, "items": out}

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]

def _text(node: Any) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""

def _feed_title(root: Any) -> str:
    tag = _strip_ns(root.tag)
    if tag == "rss":
        for ch in root:
            if _strip_ns(ch.tag) == "channel":
                for c in ch:
                    if _strip_ns(c.tag) == "title":
                        return _text(c)
    elif tag == "feed":
        for c in root:
            if _strip_ns(c.tag) == "title":
                return _text(c)
    return ""

def _item_nodes(root: Any) -> list[Any]:
    tag = _strip_ns(root.tag)
    if tag == "rss":
        for ch in root:
            if _strip_ns(ch.tag) == "channel":
                return [c for c in ch if _strip_ns(c.tag) == "item"]
        return []
    if tag == "feed":
        return [c for c in root if _strip_ns(c.tag) == "entry"]
    return []

def _strip_html(s: str) -> str:
    s = html.unescape(str(s or ""))
    s = _TAG_LIKE_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()[:_SUMMARY_MAX]

def _parse_published(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    # ISO
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        pass
    # RFC822
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return None

def _item_of(node: Any) -> dict[str, Any] | None:
    tag = _strip_ns(node.tag)
    if tag == "item":  # RSS 2.0
        title = link = pub_raw = desc = ""
        for c in node:
            t = _strip_ns(c.tag)
            if t == "title":
                title = _text(c)
            elif t == "link":
                link = _text(c)
            elif t == "pubDate" or t == "published" or t == "updated" or t == "dc:date":
                if not pub_raw:
                    pub_raw = _text(c)
            elif t == "description" or t == "summary":
                if not desc:
                    desc = _text(c)
        # dc:date 带命名空间（itertext 能取到但 strip_ns 之后名字才是 date）
        if not pub_raw:
            for c in node.iter():
                if _strip_ns(c.tag) == "date":
                    pub_raw = _text(c)
                    break
        if not title or not link:
            return None
        return {
            "title": title,
            "url": link,
            "published": _parse_published(pub_raw),
            "summary": _strip_html(desc),
        }
    if tag == "entry":  # Atom
        title = link = pub_raw = desc = ""
        for c in node:
            t = _strip_ns(c.tag)
            if t == "title":
                title = _text(c)
            elif t == "link" and c.get("href"):
                link = str(c.get("href") or "").strip()
            elif t == "id" and not link:
                link = _text(c)
            elif t in ("updated", "published"):
                if not pub_raw:
                    pub_raw = _text(c)
            elif t in ("summary", "content") and not desc:
                desc = _text(c)
        if not title or not link:
            return None
        return {
            "title": title,
            "url": link,
            "published": _parse_published(pub_raw),
            "summary": _strip_html(desc),
        }
    return None

# ----------------------------------------------------------------------
# 取源
# ----------------------------------------------------------------------

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w\-]+)", re.I)


def _decode_text(body: bytes, content_type: str = "") -> str:
    """按响应声明的 charset 解码（没有就 utf-8、坏字节替换）——中文站的 HTML 常是 gbk。"""
    match = _CHARSET_RE.search(str(content_type or ""))
    if match:
        try:
            return body.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    return body.decode("utf-8", errors="replace")


async def fetch_bytes(
    url: str,
    *,
    transport: Any = None,
    timeout_s: float = _TIMEOUT_S,
    max_bytes: int = _MAX_BYTES,
    what: str = "RSS",
) -> dict[str, Any]:
    """安全取一次字节（全模块唯一一处出网请求；第二步取首页 HTML 也走这里）。

    和取 RSS 同一套限制：生产先把域名解析成公开 IP 再连固定 IP（不在客户端里做第二次
    DNS）、不跟随重定向（302 也只当失败）、trust_env=False（禁环境代理）、全程限时、
    响应限字节。**别在别处另写一套 httpx 调用**。
    返回 {"body": bytes|None, "status": int, "content_type": str, "error": str}；
    url 不是合法的公开 http(s) 地址 → 抛 RssError（调用方自己决定 400 还是记 error）。
    """
    url_s = _require_http(url)
    parsed = _public_url(url_s)
    try:
        # 全程限时；to_thread 的系统 DNS 调用即使超时仍可能在后台结束，但不会发 HTTP 请求。
        async with asyncio.timeout(timeout_s):
            request_url = parsed
            headers = {"User-Agent": _UA}
            extensions: dict[str, Any] = {}
            network_transport = transport
            if transport is None:
                host = parsed.host or ""
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                address = await asyncio.to_thread(_resolve_public_ip, host, port)
                # 固定连接到已经审核过的 IP，而非在客户端里对原域名做第二次 DNS 查询。
                request_url = parsed.copy_with(host=address)
                headers["Host"] = parsed.netloc.decode("ascii")
                extensions["sni_hostname"] = parsed.raw_host.decode("ascii").rstrip(".")
                # 禁止环境 HTTP(S)_PROXY / ALL_PROXY 将安全连接转交给未校验的代理。
                network_transport = httpx.AsyncHTTPTransport(trust_env=False)
            async with httpx.AsyncClient(transport=network_transport, trust_env=False) as client:
                async with client.stream(
                    "GET", request_url, timeout=timeout_s, headers=headers,
                    extensions=extensions, follow_redirects=False,
                ) as resp:
                    content_type = str(resp.headers.get("content-type") or "")
                    status = int(resp.status_code)
                    if status != 200:
                        return {"body": None, "status": status, "content_type": content_type,
                                "error": f"取 {what} 失败（HTTP {status}）"}
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            mb = max(1, int(max_bytes // (1024 * 1024)))
                            return {"body": None, "status": status, "content_type": content_type,
                                    "error": f"{what} 响应太大了（超过 {mb}MB）"}
                        chunks.append(chunk)
    except RssError as e:
        return {"body": None, "status": 0, "content_type": "", "error": str(e)}
    except (TimeoutError, httpx.TimeoutException):
        return {"body": None, "status": 0, "content_type": "",
                "error": f"取 {what} 超时（{timeout_s:g} 秒）"}
    except httpx.HTTPError as e:
        return {"body": None, "status": 0, "content_type": "",
                "error": f"取 {what} 失败：{type(e).__name__}"}
    return {"body": b"".join(chunks), "status": 200, "content_type": content_type, "error": ""}


async def fetch_feed_source(
    url: str,
    *,
    transport: Any = None,
    lookback_days: int = 14,
    now: float | None = None,
    limit: int = _PER_SOURCE_LIMIT,
) -> dict[str, Any]:
    """取一次并解析。返回 {"title", "items", "error"}；业务/网络失败走 error（不抛）。
    url 不合法抛 RssError（接口层要 400）。"""
    url_s = _require_http(url)
    now = float(clock.now() if now is None else now)
    out: dict[str, Any] = {"title": "", "items": [], "error": ""}
    try:
        got = await fetch_bytes(url_s, transport=transport)
    except RssError as e:
        # 网页 POST 取源阶段用 error 返回 400；这里直接抛会变成未处理的 500。
        out["error"] = str(e)
        return out
    if got.get("error"):
        out["error"] = str(got["error"])
        return out
    text = _decode_text(got.get("body") or b"", str(got.get("content_type") or ""))
    try:
        parsed = parse_feed(text, now=now, lookback_days=int(lookback_days))
    except RssError as e:
        out["error"] = str(e)
        return out
    out["title"] = parsed["title"]
    out["items"] = parsed["items"][: max(0, int(limit))]
    return out
