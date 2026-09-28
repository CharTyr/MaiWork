"""RSS 资讯源（rss.py）。

- 数据：kv["feeds.rss.<群号>"] = [{id, url, title, enabled, added_ts, last_ok_ts, last_error}]；
  每群最多 20 个，url 必须 http(s)。
- 解析：RSS 2.0（channel/item）和 Atom（feed/entry）都认；标准库 xml.etree 解析，
  **禁用外部实体**：defusedxml 不可用就手动拒绝含 <!DOCTYPE 的文档（守住 XXE，不用引第三方）。
- 取最近 lookback_days 内的条目 {title, url, published, summary(去 HTML 截 500)}；
  每源每轮最多 10 条；超时 15s；响应 ≤2MB。
- 条目并进备料：和搜索子 agent 的候选**合并后走同一套质量门槛**（见 feeds 接线处注释）。
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

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
    """按 added_ts/id 稳定排序返回已加源（管理员视图）。"""
    return sorted(_load_list(store, gid), key=lambda e: (float(e.get("added_ts") or 0.0), str(e.get("id") or "")))


def add_feed(store: Any, gid: str, *, url: str, title: str, feed_id: str, now: float | None = None) -> dict[str, Any]:
    """加源（不试取——试取是接口层「先试取，成功才保存」在做）。RssError 拒绝。"""
    url_s = _require_http(url)
    now = float(clock.now() if now is None else now)
    feeds = _load_list(store, gid)
    if len(feeds) >= _GROUP_MAX:
        raise RssError(f"每群最多 {_GROUP_MAX} 个 RSS 源，已经加满了")
    if any(str(e.get("url")) == url_s for e in feeds):
        raise RssError("这个 RSS 源已经加过了")
    feed_id_s = str(feed_id or "").strip() or _new_id(feeds)
    if any(str(e.get("id")) == feed_id_s for e in feeds):
        feed_id_s = _new_id(feeds)
    entry = _plain(feed_id_s, url_s, title, True, now, 0.0, "")
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


def _plain(feed_id: str, url: str, title: str, enabled: bool, added_ts: float, last_ok_ts: float, last_error: str) -> dict[str, Any]:
    return {
        "id": str(feed_id),
        "url": str(url),
        "title": str(title or ""),
        "enabled": bool(enabled),
        "added_ts": float(added_ts),
        "last_ok_ts": float(last_ok_ts or 0.0),
        "last_error": str(last_error or ""),
    }


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


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------

_DOCTYPE_RE = re.compile(r"<!DOCTYPE", re.IGNORECASE)
_TAG_LIKE_RE = re.compile(r"<[^>]+>")
_SUMMARY_MAX = 500


def parse_feed(xml_text: str, *, now: float, lookback_days: int) -> dict[str, Any]:
    """RSS 2.0 / Atom → {"title", "items":[{title,url,published,summary}]}；解析不了抛 RssError。"""
    head = xml_text[:4096]
    if _DOCTYPE_RE.search(head):
        raise RssError("RSS 文档带 <!DOCTYPE（可能藏外部实体），禁止解析")
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


def _children(node: Any, local: str) -> list[Any]:
    return [c for c in node if _strip_ns(c.tag) == local]


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
        async with httpx.AsyncClient(transport=transport) as client:
            async with client.stream(
                "GET", url_s, timeout=_TIMEOUT_S, headers={"User-Agent": _UA}, follow_redirects=False
            ) as resp:
                if resp.status_code != 200:
                    out["error"] = f"取 RSS 失败（HTTP {resp.status_code}）"
                    return out
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        out["error"] = "RSS 响应太大了（超过 2MB）"
                        return out
                    chunks.append(chunk)
    except httpx.TimeoutException:
        out["error"] = f"取 RSS 超时（{_TIMEOUT_S:g} 秒）"
        return out
    except httpx.HTTPError as e:
        out["error"] = f"取 RSS 失败：{type(e).__name__}"
        return out
    text = b"".join(chunks).decode("utf-8", errors="replace")
    try:
        parsed = parse_feed(text, now=now, lookback_days=int(lookback_days))
    except RssError as e:
        out["error"] = str(e)
        return out
    out["title"] = parsed["title"]
    out["items"] = parsed["items"][: max(0, int(limit))]
    return out
