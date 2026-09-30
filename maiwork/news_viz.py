"""资讯图解（2026-09-29 用户定，参考 Muse）：没配图、内容里有数字 / 对比 / 时间线的资讯，
做一张小小的「数据图」放在网页上配图的位置。

流程（app 每群后台长活 kind=viz，同群同时只跑一个）：
1. 候选：本群 12 小时内、通过的 news/guide、没配图、不是个人向、还没处理过的（最多 8 条）。
2. 主模型（json_mode）挑最适合画的一条（有具体数字 / 对比 / 时间线）；都不适合就 -1。
   这批里没被挑中的记 skip，不再反复问。每群每天最多做 viz_per_day 张（[feeds]，0 = 关）。
3. 程序用 fetch_page 工具重新打开原文（同一套安全抓取），把原文正文交给子 agent
   （不给工具，只交回 JSON {"html"}）写一段 HTML+CSS。**不许脚本**；以一眼能看完为主
   （高度尽量 ≤480px，默认不做交互），内容实在放不下才用 CSS 互动
   （单选框做标签页、:hover 显示数值、details 展开），第一屏要能单独看懂。
4. 程序核对 check_html：不超过 40KB；没有脚本 / 事件属性 / 外部资源 / 表单 / 内嵌框；
   看得见的文字和 title/aria-label/alt/data-* 里的每个数字，都要原样在原文里；带 +/− 号的
   还可以是原文两数之差或变化百分比（要对得上），0~10 的小整数不查。不过就整张不要（rejected，原因记下）。
5. 网页按需取（GET /api/news/{id}/viz），外面包一层 wrap()：CSP default-src 'none'、
   只放行我们自己那段报高度的脚本（按 sha256）；前端放进 sandbox="allow-scripts"
   （没有 allow-same-origin）的 iframe，碰不到网页登录信息、不能联网、不能跳转页面。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Optional

from . import clock

logger = logging.getLogger("maiwork.news_viz")

VIZ_MAX_BYTES = 40_000
SOURCE_MAX_CHARS = 12_000
SCAN_HOURS = 12
CANDIDATE_MAX = 8
WORK_MINUTES = 5

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_viz (
    item_id INTEGER PRIMARY KEY,
    group_id TEXT NOT NULL,
    status TEXT NOT NULL,            -- ok / skip / rejected / failed
    html TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL DEFAULT 0,
    updated REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_news_viz_group ON news_viz(group_id, status, created);
"""

VIZ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"html": {"type": "string"}},
    "required": ["html"],
}

# ----------------------------------------------------------------------
# 外壳：CSP + 报高度（唯一放行的脚本，按 sha256）
# ----------------------------------------------------------------------

_REPORTER = (
    "(function(){var f=function(){parent.postMessage({mwviz:1,"
    "h:Math.ceil(document.documentElement.scrollHeight)},'*')};"
    "addEventListener('load',f);if(window.ResizeObserver){new ResizeObserver(f).observe(document.body)}"
    "setTimeout(f,60)})();"
)
_REPORTER_HASH = base64.b64encode(hashlib.sha256(_REPORTER.encode("utf-8")).digest()).decode("ascii")
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
    f"script-src 'sha256-{_REPORTER_HASH}'; form-action 'none'; base-uri 'none'"
)
_BASE_CSS = """
:root{--bg:transparent;--card:#f4f4f6;--ink:#16161a;--ink-2:#44444c;--muted:#8a8a94;--line:rgba(0,0,0,.09);
--accent:#2f6df6;--good:#1f9d55;--bad:#d64545;--warn:#d98a00;color-scheme:light dark}
@media (prefers-color-scheme: dark){:root{--card:#1c1c21;--ink:#f2f2f5;--ink-2:#c9c9d1;--muted:#8e8e98;
--line:rgba(255,255,255,.1);--accent:#6f9bff;--good:#4ccf86;--bad:#ff7a7a;--warn:#ffb84d}}
*{box-sizing:border-box}html,body{margin:0;padding:0;background:var(--bg);color:var(--ink)}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased;overflow:hidden}
"""


def wrap(html: str) -> str:
    """把核对过的片段包成一整页（给 iframe srcdoc 用）。"""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{_CSP}\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{_BASE_CSS}</style></head><body>"
        + str(html)
        + f"<script>{_REPORTER}</script></body></html>"
    )


# ----------------------------------------------------------------------
# 核对
# ----------------------------------------------------------------------

_FORBIDDEN = [
    (re.compile(r"(?i)<\s*script\b"), "不许用脚本"),
    (re.compile(r"(?i)\son[a-z]+\s*="), "不许用事件属性（onclick 之类）"),
    (re.compile(r"(?i)<\s*(iframe|frame|object|embed|link|meta|base|form|portal|audio|video|source|track)\b"), "不许用这种标签"),
    (re.compile(r"(?i)https?:"), "不许引用外部地址"),
    (re.compile(r"(?i)(?:src|href|action|xlink:href)\s*=\s*['\"]?\s*(?://|javascript:)"), "不许引用外部地址"),
    (re.compile(r"(?i)url\(\s*['\"]?\s*(?://|https?:|javascript:)"), "不许引用外部地址"),
    (re.compile(r"(?i)@import"), "不许 @import"),
    (re.compile(r"(?i)javascript:"), "不许 javascript: 链接"),
    (re.compile(r"(?i)<\s*img\b[^>]*\bsrc\s*=\s*['\"]?(?!data:)"), "图片只能内嵌（data:）"),
]

_NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_CHECK_ATTRS = ("title", "aria-label", "alt", "value", "placeholder")


class _TextParser(HTMLParser):
    """收看得见的文字 + title/aria-label/alt/data-* 属性；跳过 <style> 内容。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in ("style", "script"):
            self._skip += 1
        for k, v in attrs:
            if v and (k in _CHECK_ATTRS or k.startswith("data-")):
                self.parts.append(v)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        for k, v in attrs:
            if v and (k in _CHECK_ATTRS or k.startswith("data-")):
                self.parts.append(v)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _css_content_strings(html: str) -> list[str]:
    """<style> 里 content:"…" 写死的字（也会显示出来）。"""
    out: list[str] = []
    for block in re.findall(r"(?is)<style[^>]*>(.*?)</style>", html):
        out.extend(m.group(2) for m in re.finditer(r"content\s*:\s*(['\"])(.*?)\1", block))
    return out


def _num(tok: str) -> Optional[float]:
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def numbers_in(text: str) -> list[float]:
    return [n for n in (_num(m.group(0)) for m in _NUM_RE.finditer(str(text or ""))) if n is not None]


def _allowed(source: str) -> tuple[set[float], set[float], set[float]]:
    """(原文里的数, 原文两数之差, 原文两数的变化百分比〔取整和一位小数〕)。"""
    uniq = sorted({round(x, 4) for x in numbers_in(source)})[:300]
    base = set(uniq)
    diffs: set[float] = set()
    pcts: set[float] = set()
    for a in uniq:
        for b in uniq:
            if a == b:
                continue
            diffs.add(round(abs(a - b), 4))
            if b:
                p = abs(100.0 * (a - b) / b)
                pcts.add(float(round(p)))
                pcts.add(round(p, 1))
    return base, diffs, pcts


_SIGN_BEFORE = re.compile(r"[+\-−–↑↓▲▼]\s*$")


def check_html(html: str, source: str) -> tuple[bool, str]:
    """(过不过, 不过的原因)。

    数字规则：不带正负号的数必须原样在原文里；带 + / − 号的（「+2 分」「−50%」）还可以是原文两数之差，
    或（后面跟 %）原文两数的变化百分比（取整或一位小数要对上）。0~10 的小整数不查。
    """
    h = str(html or "").strip()
    if not h:
        return False, "空的"
    if len(h.encode("utf-8")) > VIZ_MAX_BYTES:
        return False, f"太大了（超过 {VIZ_MAX_BYTES // 1000}KB）"
    for pat, why in _FORBIDDEN:
        if pat.search(h):
            return False, why
    parser = _TextParser()
    try:
        parser.feed(h)
        parser.close()
    except Exception:
        return False, "HTML 解析不了"
    base, diffs, pcts = _allowed(source)
    bad: list[str] = []
    for part in parser.parts + _css_content_strings(h):
        for m in _NUM_RE.finditer(part):
            v = _num(m.group(0))
            if v is None:
                continue
            if v == int(v) and 0 <= v <= 10:
                continue
            r = round(v, 4)
            if r in base:
                continue
            signed = bool(_SIGN_BEFORE.search(part[: m.start()]))
            is_pct = part[m.end(): m.end() + 2].lstrip().startswith("%")
            if signed and (r in diffs or (is_pct and r in pcts)):
                continue
            tok = m.group(0)
            if tok not in bad:
                bad.append(tok)
    if bad:
        return False, "这些数字原文里找不到：" + "、".join(bad[:8])
    return True, ""


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------


def status_of(store: Any, item_ids: Iterable[int]) -> dict[int, str]:
    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    rows = store.read().execute(
        f"SELECT item_id, status FROM news_viz WHERE item_id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    return {int(r["item_id"]): str(r["status"]) for r in rows}


def html_for(store: Any, group_id: Any, item_id: int) -> Optional[str]:
    """核对过的图解整页（wrap 过）；不是本群 / 没有 / 没过 → None。"""
    row = store.read().execute(
        "SELECT v.html FROM news_viz v JOIN news_items i ON i.id=v.item_id"
        " WHERE v.item_id=? AND v.group_id=? AND i.group_id=? AND v.status='ok' AND i.rejected=0",
        (int(item_id), str(group_id), str(group_id)),
    ).fetchone()
    if row is None or not row["html"]:
        return None
    return wrap(str(row["html"]))


def _set(store: Any, gid: str, iid: int, status: str, *, html: str = "", reason: str = "", now: float) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO news_viz (item_id, group_id, status, html, reason, created, updated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(item_id) DO UPDATE SET status=excluded.status, html=excluded.html,"
            " reason=excluded.reason, updated=excluded.updated",
            (int(iid), gid, status, html, str(reason)[:300], float(now), float(now)),
        )


def _day_start(now: float) -> float:
    t = clock.bj(float(now))
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ----------------------------------------------------------------------
# 流程
# ----------------------------------------------------------------------


class NewsViz:
    def __init__(self, store: Any, models: Any, workers: Any, tools: Any,
                 get_settings: Callable[[], Any]) -> None:
        self._store = store
        self._models = models
        self._workers = workers
        self._tools = tools
        self._get_settings = get_settings

    def _per_day(self) -> int:
        try:
            return max(0, int(getattr(self._get_settings().feeds, "viz_per_day", 0)))
        except Exception:
            return 0

    def _candidates(self, gid: str, now: float) -> list[Any]:
        return self._store.read().execute(
            "SELECT i.* FROM news_items i WHERE i.group_id=? AND i.rejected=0 AND i.kind IN ('news','guide')"
            " AND i.target_user_id='' AND COALESCE(i.image_url,'')='' AND i.created>=?"
            " AND NOT EXISTS (SELECT 1 FROM news_viz v WHERE v.item_id=i.id)"
            " ORDER BY i.score DESC, i.id ASC LIMIT ?",
            (gid, float(now) - SCAN_HOURS * 3600.0, CANDIDATE_MAX),
        ).fetchall()

    def made_today(self, gid: str, now: float) -> int:
        row = self._store.read().execute(
            "SELECT COUNT(*) AS c FROM news_viz WHERE group_id=? AND status='ok' AND created>=?",
            (gid, _day_start(now)),
        ).fetchone()
        return int(row["c"]) if row else 0

    def has_work(self, group_id: Any, now: float) -> bool:
        gid = str(group_id)
        cap = self._per_day()
        if cap <= 0 or self.made_today(gid, now) >= cap:
            return False
        return bool(self._candidates(gid, now))

    async def _pick(self, gid: str, cands: list[Any]) -> int:
        lines = [
            "下面是这个群刚出的几条资讯（都没有配图）。挑出**最适合画成一张小数据图**的一条："
            "正文里要有具体数字、前后对比、多项对照或时间线，画出来比光看文字更清楚。"
            "没有这种内容的（纯观点、纯事件、只有一个数）都不适合。",
            "",
        ]
        for n, r in enumerate(cands):
            body = str(r["body"] or r["summary"] or "").replace("\n", " ")[:300]
            lines.append(f"[{n}] {r['title']}\n    {body}")
        lines += ["", '只回 JSON：{"pick": 编号（都不适合就 -1）, "why": "一句话理由"}']
        try:
            res = await self._models.chat(
                agent="news", messages=[{"role": "user", "content": "\n".join(lines)}],
                json_mode=True, purpose="news_viz.pick", group_id=gid,
            )
            data = json.loads(str(getattr(res, "text", "") or ""))
            pick = int(data.get("pick", -1)) if isinstance(data, dict) else -1
        except Exception as e:
            logger.info("图解挑选失败（群 %s）：%s", gid, e)
            return -2  # 出错：这批不记 skip，下一轮再试
        return pick if 0 <= pick < len(cands) else -1

    @staticmethod
    def _first_url(row: Any) -> str:
        try:
            src = json.loads(row["sources"] or "[]")
        except (TypeError, ValueError):
            return ""
        if isinstance(src, list):
            for s in src:
                if isinstance(s, dict) and str(s.get("url") or "").startswith(("http://", "https://")):
                    return str(s["url"])
        return ""

    @staticmethod
    def _brief(row: Any, page: str) -> str:
        return (
            "给群里的一条资讯做一张「图解」小图，放在资讯页配图的位置（宽约 360~640 像素，手机上也要好看）。\n\n"
            f"资讯标题：{row['title']}\n资讯正文：{str(row['body'] or row['summary'] or '')[:800]}\n\n"
            "要求：\n"
            "1. 只交一段 HTML 片段（可以带 <style>），不要 <html>/<head>/<body>。**不许写 <script>、不许 on 开头的事件属性**，"
            "不许引用任何外部地址（图片、字体、样式都不行）、不许放链接。\n"
            "2. **以一眼能看完为主**：一张图只讲一件事，挑最重要的几项数据直接摆出来，"
            "整张高度尽量不超过 480 像素（按 516 像素宽算）；默认不做交互——群里的资讯卡片只截图解最上面一段，"
            "藏在别的标签页、折叠里的内容在卡片上看不到。只有内容实在放不下、而且分开看更清楚时，"
            "才用 CSS 做交互：单选框 + label 做标签页、:hover 显示数值、<details> 展开；"
            "这时第一眼看到的那一屏（默认标签 / 没展开时）也要能单独看懂重点。\n"
            "3. **图上的每个数字都必须原样来自下面的原文**（可以写两个原文数字的差、变化百分比），不许估算、不许编；"
            "原文没有的数据就别画。数字写法照原文（比如 43%、$0.10）。\n"
            "4. 颜色只用这些变量，深浅色模式会自动切换：var(--card) 卡片底、var(--ink) 主字、var(--ink-2) 次字、"
            "var(--muted) 弱字、var(--line) 分隔线、var(--accent) 强调、var(--good) 涨/好、var(--bad) 跌/差、var(--warn)。"
            "圆角 14~18px，留白足，中文，字号 12~18px，一眼能看懂重点；最后一行用一句话说结论。\n"
            "5. 整段不超过 30KB。用 submit_result 交回，data 为 {\"html\": \"...\"}。\n\n"
            f"原文（程序刚打开的，只以这个为准）：\n{page}"
        )

    async def run(self, group_id: Any, now: Optional[float] = None) -> None:
        gid = str(group_id)
        now = clock.now() if now is None else float(now)
        if not self.has_work(gid, now):
            return
        cands = self._candidates(gid, now)
        pick = await self._pick(gid, cands)
        if pick == -2:
            return
        for n, r in enumerate(cands):
            if n != pick:
                _set(self._store, gid, int(r["id"]), "skip", reason="没挑中", now=now)
        if pick < 0:
            return
        row = cands[pick]
        iid = int(row["id"])
        url = self._first_url(row)
        if not url:
            _set(self._store, gid, iid, "failed", reason="没有原文链接", now=now)
            return
        from .tools import ToolContext

        ctx = ToolContext(group_id=gid, task_id=f"viz:{iid}", actor="图解·打开原文", role="worker")
        try:
            got = await self._tools.call("fetch_page", {"url": url}, ctx)
        except Exception as e:
            got = None
            logger.info("图解打开原文出错（群 %s 条 %s）：%s", gid, iid, e)
        if got is None or not getattr(got, "ok", False) or not str(getattr(got, "output", "") or "").strip():
            _set(self._store, gid, iid, "failed", reason=f"原文打不开：{getattr(got, 'error', '') or ''}"[:200], now=now)
            return
        page = str(got.output)[:SOURCE_MAX_CHARS]
        try:
            report = await self._workers.run(
                self._brief(row, page), group_id=gid, tools=[], output_schema=VIZ_SCHEMA,
                task_id=f"viz:{iid}", actor="子 agent · 图解", deadline_ts=now + WORK_MINUTES * 60,
            )
        except Exception as e:
            logger.info("图解子 agent 出错（群 %s 条 %s）：%s", gid, iid, e)
            _set(self._store, gid, iid, "failed", reason="子 agent 出错", now=now)
            return
        data = getattr(report, "data", None)
        html = str(data.get("html") or "") if isinstance(data, dict) else ""
        if not getattr(report, "ok", False) or not html.strip():
            _set(self._store, gid, iid, "failed", reason="子 agent 没交回图", now=now)
            return
        source = "\n".join([str(row["title"] or ""), str(row["summary"] or ""), str(row["body"] or ""), page])
        ok, why = check_html(html, source)
        if not ok:
            logger.info("图解没过核对（群 %s 条 %s）：%s", gid, iid, why)
            _set(self._store, gid, iid, "rejected", reason=why, now=now)
            return
        _set(self._store, gid, iid, "ok", html=html.strip(), now=now)
        logger.info("图解做好了（群 %s 条 %s）", gid, iid)
