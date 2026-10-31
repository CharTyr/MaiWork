"""share.py：没填公开网址时，把「群友要看的那一页」自动发布到 here.now。

规则（docs/02 群链接一节）：
- `[console] public_url` 填了 → 照旧用 card_push.group_link（自己的网页 + 群链接码）；
- 没填且 `[console] herenow_share` 开（默认开）且有 here.now 客户端 → 本模块把页面
  渲染成单文件静态 HTML（全内联、不引外部资源、手机可读）匿名发布，24 小时过期；
- 都不满足 → ""（调用方就只发文字）。

隐私边界（2026-10-25 用户定：不比群友网页保守）：内容只取群友版视图
（console/views.group_view(admin=False)），群友网页上看得到的都放（群名、群友原话、构想依据、
敏感资讯、任务需求）；仍按白名单挑字段，群链接码、工作区、画像、QQ 号、管理员字段一律不进页面。所有文本 HTML 转义，链接只放 http(s)。

缓存：kv `share.<群号>.<tab>.<item>` = {url, ts, digest}；12 小时内内容没变就复用。
发布失败（含 429）记 warning、返回 ""，不抛。
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from html import escape
from pathlib import Path
from typing import Any, Callable, Optional

from . import clock

logger = logging.getLogger("maiwork.share")

REUSE_S = 12 * 3600          # here.now 24 小时过期，留足余量：12 小时内复用
TAB_LIMIT = 8                # tab 级页面最多放几条
_TEXT_MAX = 1200
_TITLE_MAX = 200
_TABS = ("news", "ideas", "tasks", "goals")


def _kv_key(gid: str, tab: str, item: str) -> str:
    return f"share.{gid}.{tab}.{item}"


def _t(v: Any, limit: int = _TEXT_MAX) -> str:
    s = str(v or "").strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def _e(v: Any, limit: int = _TEXT_MAX) -> str:
    return escape(_t(v, limit), quote=True)


def _http(url: Any) -> str:
    s = str(url or "").strip()
    return s if re.match(r"^https?://[^\s\"'<>]+$", s) else ""


# ----------------------------------------------------------------------
# 白名单抽取：只拿群友能看、且适合放到公开网页上的字段
# ----------------------------------------------------------------------


def _spec_fragment(store: Any, item_id: Any) -> str:
    """news_viz 里的 spec 行 → 静态 HTML 片段（vs-static，无脚本无 iframe）。没有/不是 spec → ""。"""
    if store is None:
        return ""
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return ""
    try:
        row = store.read().execute(
            "SELECT html FROM news_viz WHERE item_id=? AND status='ok'", (iid,)
        ).fetchone()
    except Exception:
        return ""
    if row is None or not str(row["html"]).startswith("spec:"):
        return ""
    try:
        from . import news_viz, viz_spec
        spec = news_viz._load_spec(str(row["html"]))
        if spec is None:
            return ""
        return viz_spec.render(spec, uid=f"sv-{iid}", static=True)
    except Exception:
        logger.exception("spec 静态渲染失败（资讯 %s）", iid)
        return ""


def _news_entries(view: dict, store: Any = None) -> list[dict]:
    out: list[dict] = []
    for batch in view.get("news") or []:
        if not isinstance(batch, dict):
            continue
        for it in batch.get("items") or []:
            if not isinstance(it, dict):
                continue
            links = []
            for s in it.get("sources") or []:
                if isinstance(s, dict) and _http(s.get("url")):
                    links.append((_http(s.get("url")), _t(s.get("site") or s.get("title") or "原文", 60)))
            quotes = []
            for r in it.get("refs") or []:
                if isinstance(r, dict) and _t(r.get("text")):
                    who = _t(r.get("who") or r.get("name"), 30)
                    quotes.append((f"{who}：" if who else "") + _t(r.get("text"), 200))
            entry: dict = {
                "id": str(it.get("id") or ""),
                "title": it.get("title"),
                "paras": [it.get("summary"), it.get("why") and f"为什么推荐：{it.get('why')}"],
                "bullets": [f"群里说过 · {q}" for q in quotes[:5]],
                "links": links[:5],
            }
            # §4.2：spec 型图解 → 内联静态渲染（vs-static，无脚本无 iframe）
            if it.get("viz") == "spec":
                frag = _spec_fragment(store, it.get("id"))
                if frag:
                    entry["viz_html"] = frag
            out.append(entry)
    return out  # 好文（guides）另有入口，这一页不放


def _idea_entries(view: dict) -> list[dict]:
    out = []
    for it in view.get("ideas") or []:
        if not isinstance(it, dict):
            continue
        state = str(it.get("state") or "new")
        if state == "dismissed":
            continue
        iid = str(it.get("id") or "").strip()
        out.append({
            "id": f"I-{iid}",
            "num": iid,
            "actionable": state in ("new", "wanted") and str(it.get("kind") or "") != "talk",
            "items": [x for x in (it.get("items") or []) if isinstance(x, dict)],
            "title": it.get("title"),
            "paras": [it.get("body"),
                      (it.get("basis") or it.get("reason")) and f"依据：{it.get('basis') or it.get('reason')}",
                      it.get("step") and f"第一步：{it.get('step')}"],
            "links": [],
        })
    return out


_GOAL_STATE = {"active": "进行中", "done": "已完成", "paused": "已暂停", "cancelled": "已取消"}
_TASK_STATE = {"done": "已完成", "running": "进行中", "queued": "排队中", "failed": "没做成",
               "cancelled": "已取消", "paused": "已暂停"}


def _goal_entries(view: dict) -> list[dict]:
    out = []
    goals = view.get("goals") or {}
    for g in (goals.get("agent") or []) if isinstance(goals, dict) else []:
        if not isinstance(g, dict):
            continue
        crit = []
        for c in g.get("criteria") or []:
            if isinstance(c, dict) and _t(c.get("text")):
                crit.append(("✅ " if c.get("backed") else "⬜ ") + _t(c.get("text"), 200))
        last = g.get("last") if isinstance(g.get("last"), dict) else {}
        out.append({
            "id": str(g.get("id") or ""),
            "title": g.get("title"),
            "badge": _GOAL_STATE.get(str(g.get("state") or ""), ""),
            "paras": [g.get("body"), last.get("text") and f"最近进展：{last.get('text')}"],
            "bullets": crit,
            "links": [],
        })
    return out


def _task_entries(view: dict) -> list[dict]:
    out = []
    tasks = view.get("tasks") or {}
    for t in (tasks.get("list") or []) if isinstance(tasks, dict) else []:
        if not isinstance(t, dict):
            continue
        out.append({
            "id": str(t.get("id") or ""),
            "title": t.get("title") or t.get("question"),
            "badge": _TASK_STATE.get(str(t.get("state") or t.get("status") or ""), ""),
            "paras": [t.get("req") and t.get("req") != (t.get("title") or t.get("question"))
                      and f"要做的事：{t.get('req')}",
                      t.get("summary") or t.get("result"),
                      t.get("requester_name") and f"发起人：{t.get('requester_name')}",
                      isinstance(t.get("meta"), str) and t.get("meta")],
            "links": [],
        })
    return out


def _entries(view: dict, tab: str, item: str, store: Any = None) -> tuple[str, list[dict]]:
    if tab == "news":
        heading, entries = "资讯", _news_entries(view, store=store)
        if item:
            want = item.split("-")[-1]
            entries = [e for e in entries if e["id"] == want]
    elif tab == "ideas":
        heading, entries = "构想", _idea_entries(view)
        if item:
            entries = [e for e in entries if e["id"] == item or e["id"] == f"I-{item}"]
    else:  # tasks / goals：目标在前、任务在后
        heading = "目标与任务"
        entries = _goal_entries(view) + _task_entries(view)
        if item:
            entries = [e for e in entries if e["id"] == item]
            heading = "目标" if item and not item.upper().startswith("T") else "任务"
    return heading, (entries if item else entries[:TAB_LIMIT])


def render_page(data: dict, *, tab: str, item: str = "", store: Any = None) -> str:
    """群友版视图 → 单文件 HTML（纯函数：同样内容出同样字节，digest 才有意义）。

    store 给了就能顺带把 spec 图解渲进资讯卡片（tests/test_share.py 红线：无脚本）。
    """
    view = data.get("view") if isinstance(data.get("view"), dict) else data
    name = _t(data.get("name"), 60) or "本群"
    heading, entries = _entries(view or {}, tab, item, store=store)
    has_viz = any(e.get("viz_html") for e in entries)
    cards = []
    for e in entries:
        num = str(e.get("num") or "")
        title_html = f'<h2>{_e(e.get("title"), _TITLE_MAX) or "（无标题）"}</h2>'
        if num:
            title_html = f'<h2><span class="num">#{_e(num, 20)}</span> {_e(e.get("title"), _TITLE_MAX) or "（无标题）"}</h2>'
        parts = [title_html]
        if e.get("badge"):
            parts.append(f'<span class="badge">{_e(e["badge"], 20)}</span>')
        for p in e.get("paras") or []:
            if _t(p):
                parts.append(f"<p>{_e(p)}</p>")
        if e.get("bullets"):
            parts.append("<ul>" + "".join(f"<li>{_e(b, 220)}</li>" for b in e["bullets"]) + "</ul>")
        if e.get("items"):
            parts.append('<ol class="idea-items">' + "".join(
                f'<li>{_e(x.get("no") or n, 10)}. {_e(x.get("title"), 200)}'
                + (f'<p>{_e(x.get("desc"))}</p>' if x.get("desc") else "") + '</li>'
                for n, x in enumerate(e["items"], 1)
            ) + '</ol>')
        if e.get("viz_html"):
            parts.append(str(e["viz_html"]))  # viz_spec 自己转义过
        if e.get("links"):
            parts.append('<p class="links">' + " · ".join(
                f'<a href="{escape(u, quote=True)}" rel="noopener noreferrer nofollow">{_e(label, 60)}</a>'
                for u, label in e["links"]) + "</p>")
        if num and e.get("actionable", True):
            # 复制口令：纯 CSS user-select:all 一键全选（无脚本）
            cmd_at = f"@机器人 构想 #{_e(num, 20)}"
            cmd_mw = f"/mw 派活 构想 #{_e(num, 20)}"
            parts.append(
                '<p class="cmd">想做这个？复制口令发给机器人：'
                f'<code class="copy">{cmd_at}</code> 或 <code class="copy">{cmd_mw}</code></p>'
            )
        cards.append('<section class="card">' + "".join(parts) + "</section>")
    body = "\n".join(cards) or '<p class="empty">这里暂时没有内容。</p>'
    safe_name = escape(name, quote=True)
    viz_css = ""
    if has_viz:
        try:
            from . import viz_spec
            viz_css = viz_spec.CSS + viz_spec._STATIC_CSS
        except Exception:
            viz_css = ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>{safe_name} · {escape(heading)}</title>
<style>
  body {{ margin:0; background:#f7f7f8; color:#1a1a1a;
         font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }}
  main {{ max-width:640px; margin:0 auto; padding:24px 16px 40px; }}
  header h1 {{ font-size:22px; margin:0 0 4px; word-break:break-word; }}
  header p {{ font-size:13px; color:#888; margin:0 0 18px; }}
  .card {{ background:#fff; border-radius:14px; padding:16px; margin:0 0 14px;
           box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  .card h2 {{ font-size:17px; line-height:1.45; margin:0 0 8px; word-break:break-word; }}
  .card p, .card li {{ font-size:15px; line-height:1.7; margin:0 0 8px; white-space:pre-wrap; word-break:break-word; }}
  .card ul {{ padding-left:4px; list-style:none; margin:0 0 8px; }}
  .idea-items {{ padding-left:4px; list-style:none; margin:0 0 8px; }}
  .badge {{ display:inline-block; font-size:12px; color:#07c160; background:#e8f8ef;
            border-radius:999px; padding:2px 10px; margin:0 0 8px; }}
  .links a {{ color:#576b95; text-decoration:none; word-break:break-all; }}
  .cmd {{ font-size:13px; color:#666; margin-top:10px; }}
  .copy {{ user-select:all; -webkit-user-select:all; background:#f0f0f2;
           border-radius:6px; padding:1px 6px; font-size:13px;
           font-family:ui-monospace,Menlo,monospace; }}
  .num {{ color:#888; font-weight:500; font-size:14px; margin-right:4px; }}
  .empty {{ color:#888; text-align:center; }}
{viz_css}
</style>
</head>
<body>
<main>
  <header><h1>{safe_name} · {escape(heading)}</h1><p>由 MaiWork 生成 · 24 小时内有效</p></header>
{body}
</main>
</body>
</html>
"""


# ----------------------------------------------------------------------
# 发布 + 缓存
# ----------------------------------------------------------------------


def _default_view_fn(svc: Any) -> Callable[[str], dict]:
    def _fn(gid: str) -> dict:
        from .console import views

        return views.group_view(svc, gid, admin=False)
    return _fn


class Share:
    """把群友版页面发到 here.now；svc 给了就用真 group_view，测试可直接给 view_fn。"""

    def __init__(self, store: Any, herenow: Any, get_settings: Callable[[], Any], *,
                 svc: Any = None, view_fn: Optional[Callable[[str], dict]] = None) -> None:
        self._store = store
        self.herenow = herenow
        self._get_settings = get_settings
        self._view_fn = view_fn or (_default_view_fn(svc) if svc is not None else None)

    async def publish(self, group_id: Any, *, tab: str = "news", item: str = "") -> str:
        gid = str(group_id)
        tab = tab if tab in _TABS else "news"
        item = str(item or "")
        if self.herenow is None or self._view_fn is None:
            return ""
        try:
            html = render_page(self._view_fn(gid) or {}, tab=tab, item=item, store=self._store)
        except Exception:
            logger.warning("生成公开页失败（群 %s %s/%s）", gid, tab, item, exc_info=True)
            return ""
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        key = _kv_key(gid, tab, item)
        now = clock.now()
        try:
            rec = self._store.kv_get(key)
        except Exception:
            rec = None
        if (isinstance(rec, dict) and rec.get("digest") == digest and rec.get("url")
                and now - float(rec.get("ts") or 0) < REUSE_S):
            return str(rec["url"])
        try:
            with tempfile.TemporaryDirectory(prefix="maiwork-share-") as d:
                (Path(d) / "index.html").write_text(html, encoding="utf-8")
                pub = await self.herenow.publish(Path(d))
            url = _http((pub or {}).get("url"))
        except Exception as e:  # 含 429（HereNowError.retry_after）
            logger.warning("发布群公开页到 here.now 失败（群 %s %s/%s）：%s", gid, tab, item, e)
            return ""
        if not url:
            logger.warning("here.now 没返回链接（群 %s %s/%s）", gid, tab, item)
            return ""
        try:
            with self._store.tx() as conn:
                self._store.kv_set(conn, key, {"url": url, "ts": now, "digest": digest})
        except Exception:
            logger.warning("记公开页缓存失败（群 %s）", gid, exc_info=True)
        return url


def cached_link(store: Any, group_id: Any, *, tab: str = "news", item: str = "") -> str:
    """同步场景（备忘、兜底文字）用：只读缓存里 12 小时内的链接，不发布。"""
    try:
        rec = store.kv_get(_kv_key(str(group_id), tab, str(item or "")))
    except Exception:
        return ""
    if isinstance(rec, dict) and rec.get("url") and clock.now() - float(rec.get("ts") or 0) < REUSE_S:
        return str(rec["url"])
    return ""


def _public_url(settings: Any) -> str:
    return str(getattr(getattr(settings, "console", None), "public_url", "") or "").strip()


def _share_on(settings: Any, share: Any) -> bool:
    return (bool(getattr(getattr(settings, "console", None), "herenow_share", True))
            and share is not None and getattr(share, "herenow", None) is not None)


def link_available(settings: Any, share: Any) -> bool:
    """群友能不能拿到链接：public_url 有，或 here.now 自动公开开着且客户端在。"""
    return bool(_public_url(settings)) or _share_on(settings, share)


async def group_link_async(store: Any, settings: Any, group_id: Any, *, tab: str = "news",
                           item: str = "", share: Any = None) -> str:
    from .card_push import group_link

    if settings is None:
        return ""
    if _public_url(settings):
        return group_link(store, settings, group_id, tab=tab, item=item)
    if not _share_on(settings, share):
        return ""
    try:
        return await share.publish(group_id, tab=tab, item=item)
    except Exception as e:
        logger.warning("群公开页发布出错（群 %s）：%s", group_id, e)
        return ""


def group_link_cached(store: Any, settings: Any, group_id: Any, *, tab: str = "news",
                      item: str = "", share: Any = None) -> str:
    """同步版：public_url → 老链接；否则只用缓存里的 here.now 链接（不现发布）。"""
    from .card_push import group_link

    if settings is None:
        return ""
    if _public_url(settings):
        return group_link(store, settings, group_id, tab=tab, item=item)
    if not _share_on(settings, share):
        return ""
    return cached_link(store, group_id, tab=tab, item=item)
