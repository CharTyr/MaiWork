"""资讯卡片渲染（主会话写；card_push.py 调它）。

render_html(data) -> str：把一批资讯精选拼成一张卡片的 HTML（纯本地，不联网：图标内嵌 data URI，
    不加载外链图片 / 网页字体，服务器上用系统里的 Noto CJK）。
render_png(data) -> bytes：用 playwright 起一个无头 chromium 截图（2 倍像素），同一时间只画一张。
    没装 playwright / 浏览器起不来 / 超时 → 抛 RenderError，调用方退回纯文字。

data（card_push 组装）：
    {"group_name", "slot_label": "9月29日 · 下午", "slot_ts": float, "link", "total": int,
     "items": [{"title","summary","why","site","published_ts","icon","keywords":[...],"topic","kind",
                "image_url", "url"}]}

配图：render_png 先给每条补封面（prepare_covers）——有 image_url 就下它，没有就打开原文 url 找
og:image；只收公开地址（和 fetch_page 同一套防内网）、只收位图、太小 / 太扁的不要，缩到 1200 宽
转 JPEG 内嵌成 data URI（卡片本身仍不联网）。拿不到就不配图，版式自动退回图标。

版式：顶上一块「此刻的天空」——按批次所在时段换天色（凌晨 / 早上 / 中午 / 下午 / 晚上）
和日月位置，大字写时段；下面是 1~3 条，第一条最大（分数最高），每条带「为什么值得看」。
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

__all__ = ["RenderError", "fetch_cover", "prepare_covers", "render_html", "render_png", "slot_word"]

_ICON_DIR = Path(__file__).resolve().parent / "console" / "static" / "assets" / "icons"
_WIDTH = 600          # CSS 像素；截图 2 倍 → 1200 宽，手机上点开看得清
_SCALE = 2
_RENDER_TIMEOUT_S = 45.0
_LOCK = asyncio.Lock()


class RenderError(Exception):
    """卡片没画出来（调用方退回纯文字）。"""


# ----------------------------------------------------------------------
# 封面图
# ----------------------------------------------------------------------

_COVER_MAX_BYTES = 6 * 1024 * 1024
_COVER_TIMEOUT_S = 10.0
_COVER_MAX_REDIRECTS = 4
_COVER_MIN_W = 160          # 比这窄的多半是站点 logo / 图标（游民星空的预览图就是 200×112，当缩略图够用）
_COVER_MIN_H = 90
_COVER_WIDE_W = 480         # 够这么宽才当头条整幅封面，不够就退成右侧缩略图
_COVER_MAX_RATIO = 3.2      # 宽高比超过这个（横幅条）不要
_COVER_OUT_W = 1200
_COVER_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")
_UA = "Mozilla/5.0 (compatible; MaiWork/1.0; +news-card)"


def _to_jpeg_uri(raw: bytes) -> tuple[str, int]:
    """位图 → 检查尺寸 → 缩到 1200 宽 → (JPEG data URI, 原图宽)；不合格返回 ("", 0)。"""
    try:
        from PIL import Image  # 宿主 venv 自带 Pillow
    except Exception:  # noqa: BLE001
        return "", 0
    try:
        im = Image.open(io.BytesIO(raw))
        im.seek(0)
        w, h = im.size
        if w < _COVER_MIN_W or h < _COVER_MIN_H or max(w / h, h / w) > _COVER_MAX_RATIO:
            return "", 0
        im = im.convert("RGB")
        if w > _COVER_OUT_W:
            im = im.resize((_COVER_OUT_W, max(1, round(h * _COVER_OUT_W / w))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=84, optimize=True, progressive=True)
    except Exception:  # noqa: BLE001  坏图 / 不认识的格式
        return "", 0
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), int(w)


async def _get_public(url: str, *, transport: Any, resolver: Callable, want_image: bool) -> tuple[bytes, str, str]:
    """跟随跳转取一个公开地址，每跳都查内网；返回 (内容, content-type, 最终地址)，失败 (b"", "", "")。"""
    import httpx

    from .tools_builtin import _host_is_forbidden

    current = str(url or "").strip()
    for _ in range(_COVER_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return b"", "", ""
        if _host_is_forbidden(parsed.hostname, resolver):
            return b"", "", ""
        async with httpx.AsyncClient(transport=transport, timeout=_COVER_TIMEOUT_S,
                                     headers={"User-Agent": _UA}) as client:
            async with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location") or ""
                    if not loc:
                        return b"", "", ""
                    current = urljoin(current, loc)
                    continue
                if resp.status_code != 200:
                    return b"", "", ""
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if want_image and ctype and not ctype.startswith(_COVER_TYPES):
                    return b"", "", ""  # svg 之类一律不要
                if not want_image and ctype and not ctype.startswith("text/html"):
                    return b"", "", ""
                buf = bytearray()
                cap = _COVER_MAX_BYTES if want_image else 1024 * 1024
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > cap:
                        if want_image:
                            return b"", "", ""  # 图太大不要
                        break  # 网页只要前 1MB 找 og:image
                return bytes(buf), ctype, current
    return b"", "", ""


async def fetch_cover(
    image_url: str, page_url: str = "", *, transport: Any = None, resolver: Callable | None = None
) -> tuple[str, int]:
    """一条资讯的封面：image_url 优先，不行（或太小当不了头图）再去原文找 og:image。

    返回 (JPEG data URI, 原图宽)，拿不到 ("", 0)。从不抛。"""
    from .tools_builtin import _default_resolver, _extract_og_image

    resolve = resolver or _default_resolver
    best: tuple[str, int] = ("", 0)
    try:
        if image_url:
            raw, _, _ = await _get_public(image_url, transport=transport, resolver=resolve, want_image=True)
            best = _to_jpeg_uri(raw) if raw else ("", 0)
            if best[1] >= _COVER_WIDE_W:
                return best
        if page_url:
            page, _, final = await _get_public(page_url, transport=transport, resolver=resolve, want_image=False)
            og = _extract_og_image(page.decode("utf-8", errors="replace"), final) if page else ""
            if og and og != image_url:
                raw, _, _ = await _get_public(og, transport=transport, resolver=resolve, want_image=True)
                got = _to_jpeg_uri(raw) if raw else ("", 0)
                if got[1] > best[1]:
                    best = got
    except Exception as e:  # noqa: BLE001  配图失败只是不配图
        logger.info("资讯卡片封面没拿到（%s）：%s", image_url or page_url, e)
    return best


async def prepare_covers(data: dict, *, fetch: Callable | None = None, timeout_s: float = 20.0) -> None:
    """给前 3 条补 cover（并发、总时限）；已有 cover 的不动。就地修改 data。"""
    fetch = fetch or fetch_cover
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)][:3]
    todo = [it for it in items if not it.get("cover") and (it.get("image_url") or it.get("url"))]
    if not todo:
        return

    async def one(it: dict) -> None:
        try:
            uri, w = await fetch(str(it.get("image_url") or ""), str(it.get("url") or ""))
        except Exception:  # noqa: BLE001
            uri, w = "", 0
        it["cover"], it["cover_w"] = uri or "", int(w or 0)

    try:
        await asyncio.wait_for(asyncio.gather(*(one(it) for it in todo)), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.info("资讯卡片封面超时，没拿到的就不配图")


# 时段 → 天色。bg 上下两色、字色、日/月（sun / moon）、天体高度（0 顶 ~ 1 地平线）
_SKIES: dict[str, dict[str, Any]] = {
    "凌晨": {"top": "#15122b", "bottom": "#3b2f6e", "ink": "#f4f1ff", "sub": "rgba(244,241,255,.66)",
             "orb": "moon", "orb_y": 0.58, "orb_x": 0.78, "stars": True},
    "早上": {"top": "#ffc7b0", "bottom": "#fff1e3", "ink": "#3a2519", "sub": "rgba(58,37,25,.62)",
             "orb": "sun", "orb_y": 0.78, "orb_x": 0.82, "stars": False},
    "中午": {"top": "#8fcbff", "bottom": "#e7f4ff", "ink": "#0d2c47", "sub": "rgba(13,44,71,.62)",
             "orb": "sun", "orb_y": 0.34, "orb_x": 0.80, "stars": False},
    "下午": {"top": "#ffb45c", "bottom": "#ffe9c9", "ink": "#3b2507", "sub": "rgba(59,37,7,.62)",
             "orb": "sun", "orb_y": 0.46, "orb_x": 0.84, "stars": False},
    "晚上": {"top": "#1d2350", "bottom": "#54408a", "ink": "#f3f1ff", "sub": "rgba(243,241,255,.68)",
             "orb": "moon", "orb_y": 0.50, "orb_x": 0.82, "stars": True},
}

_WEEK = "一二三四五六日"


def slot_word(slot_ts: float | None, label: str = "") -> str:
    """时段词：优先 slot_label 里「·」后面那个词，认不出就按 slot_ts 本地钟点算。"""
    tail = (label or "").split("·")[-1].strip()
    if tail in _SKIES:
        return tail
    h = datetime.fromtimestamp(slot_ts).hour if slot_ts else datetime.now().hour
    if h < 6:
        return "凌晨"
    if h < 11:
        return "早上"
    if h < 13:
        return "中午"
    if h < 18:
        return "下午"
    return "晚上"


def _date_line(slot_ts: float | None, label: str) -> str:
    if slot_ts:
        d = datetime.fromtimestamp(slot_ts)
        return f"{d.month}月{d.day}日 · 周{_WEEK[d.weekday()]}"
    head = (label or "").split("·")[0].strip()
    return head


def _ago(ts: Any, now: float) -> str:
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    d = now - t
    if d < 0:
        return ""
    if d < 3600:
        return f"{max(1, int(d // 60))} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 30:
        return f"{int(d // 86400)} 天前"
    dt = datetime.fromtimestamp(t)
    return f"{dt.month}月{dt.day}日"


_ICON_CACHE: dict[str, str] = {}


def _icon_uri(name: str) -> str:
    name = name if (_ICON_DIR / f"{name}.png").is_file() else "sparkles"
    if name not in _ICON_CACHE:
        try:
            raw = (_ICON_DIR / f"{name}.png").read_bytes()
        except OSError:
            _ICON_CACHE[name] = ""
        else:
            # 素材实际是 webp（扩展名是 png），按内容判断 MIME
            mime = "image/webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else "image/png"
            _ICON_CACHE[name] = f"data:{mime};base64," + base64.b64encode(raw).decode()
    return _ICON_CACHE[name]


def _e(s: Any) -> str:
    return html.escape(str(s or "").strip())


def _clip(s: Any, n: int) -> str:
    """按「显示宽度」截：中文算 1，英文数字算 0.55（英文标题同样字数占的地方少一半）。"""
    s = " ".join(str(s or "").split())
    width = 0.0
    for i, ch in enumerate(s):
        width += 0.55 if ord(ch) < 128 else 1.0
        if width > n:
            return s[: max(0, i - 1)].rstrip("，。、；：,.;: ") + "…"
    return s


def _tags(item: dict) -> list[str]:
    """最多两个小标签：先 topic，再关键词里短的、和标题不重复的。"""
    out: list[str] = []
    title = str(item.get("title") or "")
    for k in [item.get("topic"), *(item.get("keywords") or [])]:
        k = str(k or "").strip()
        if not k or len(k) > 8 or k in out or (k in title and k != item.get("topic")):
            continue
        out.append(k)
        if len(out) >= 2:
            break
    return out


_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:transparent}
body{width:%(w)dpx;font-family:"Noto Sans CJK SC","Source Han Sans SC","PingFang SC","HarmonyOS Sans SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;color:#16161a}
.card{width:%(w)dpx;background:#f4f5f8;border-radius:0;overflow:hidden}
.sky{position:relative;height:236px;padding:30px 34px 0;overflow:hidden;
  background:linear-gradient(180deg,var(--top) 0%%,var(--bottom) 100%%);color:var(--ink)}
.orb{position:absolute;border-radius:50%%}
.orb.sun{width:150px;height:150px;background:radial-gradient(circle at 50%% 50%%,#fffbe8 0%%,#fff1b8 38%%,rgba(255,236,170,0) 70%%)}
.orb.moon{width:96px;height:96px;background:radial-gradient(circle at 38%% 38%%,#fffdf4 0%%,#f1ecd8 55%%,#d9d2bc 100%%);
  box-shadow:0 0 60px 18px rgba(255,248,220,.18)}
.star{position:absolute;width:3px;height:3px;border-radius:50%%;background:#fff;opacity:.7}
.hill{position:absolute;left:-40px;right:-40px;bottom:-1px;height:64px}
.eyebrow{position:relative;display:flex;gap:10px;align-items:baseline;max-width:400px;font-size:17px;letter-spacing:.04em;color:var(--sub);white-space:nowrap}
.eyebrow b{font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis}
.eyebrow span{flex:none}
.word{position:relative;margin-top:10px;font-family:"Noto Serif CJK SC","Source Han Serif SC","Songti SC",serif;font-weight:900;
  font-size:92px;line-height:1;letter-spacing:.06em}
.lede{position:relative;margin-top:14px;font-size:18px;color:var(--sub)}
.lede em{font-style:normal;color:var(--ink);font-weight:700}
.list{position:relative;margin-top:-20px;padding:0 18px 18px;display:flex;flex-direction:column;gap:12px}
.item{background:#fff;border-radius:22px;padding:22px 24px 20px;box-shadow:0 1px 0 rgba(17,17,20,.04),0 8px 24px -12px rgba(17,17,20,.18)}
.head{display:flex;gap:14px;align-items:flex-start}
.head img{flex:none;width:46px;height:46px;margin-top:2px}
.rank{flex:none;width:24px;font-size:15px;font-weight:700;color:#aeaeb2;margin-top:6px;font-variant-numeric:tabular-nums}
.title{font-size:23px;line-height:1.36;font-weight:700;letter-spacing:.005em;color:#111114}
.lead .title{font-size:27px;line-height:1.3}
.sum{margin-top:12px;font-size:17px;line-height:1.62;color:#3a3a3c}
.why{margin-top:14px;padding:11px 14px 12px;border-radius:14px;background:var(--why-bg);font-size:16px;line-height:1.55;color:var(--why-ink)}
.why i{font-style:normal;font-weight:700;margin-right:6px}
.meta{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;font-size:14px;color:#8a8a8e}
.meta .tag{padding:3px 10px;border-radius:999px;background:#f2f2f5;color:#3a3a3c}
.meta .src{font-weight:600;color:#6b6b70}
.item.has-cover{padding:0;overflow:hidden}
.cover{height:300px;background-size:cover;background-position:center;background-color:#e9e9ee}
.has-cover .body{padding:20px 24px 20px}
.row{display:flex;gap:16px;align-items:flex-start}
.txt{flex:1;min-width:0}
.thumb{flex:none;width:132px;height:132px;border-radius:16px;background-size:cover;background-position:center;background-color:#e9e9ee}
.row .sum{margin-top:10px}
.foot{display:flex;justify-content:space-between;align-items:center;padding:4px 34px 26px;font-size:15px;color:#8a8a8e}
.foot b{color:#3a3a3c;font-weight:600}
.brand{font-weight:800;letter-spacing:.08em;color:#b0b0b6}
"""


def _sky_html(word: str, sky: dict, data: dict, shown: int) -> str:
    total = int(data.get("total") or 0)
    lede = f"本期精选 <em>{shown}</em> 条" + (f" · 共找到 {total} 条" if total > shown else "")
    size = 150 if sky["orb"] == "sun" else 96
    left = int(_WIDTH * sky["orb_x"] - size / 2)
    top = int(236 * sky["orb_y"] - size / 2)
    stars = ""
    if sky.get("stars"):
        # 固定散点（不随机，同一时段每次一样）
        pts = [(46, 30), (120, 58), (200, 22), (262, 74), (330, 40), (398, 16), (438, 92),
               (520, 58), (560, 128), (300, 118), (84, 110), (486, 150)]
        stars = "".join(
            f'<span class="star" style="left:{x}px;top:{y}px;opacity:{0.35 + (i % 4) * 0.15:.2f}"></span>'
            for i, (x, y) in enumerate(pts)
        )
    hill = (
        '<svg class="hill" viewBox="0 0 680 64" preserveAspectRatio="none">'
        '<path d="M0 40 C120 14 230 12 340 30 S560 52 680 22 V64 H0Z" fill="rgba(255,255,255,.28)"/>'
        '<path d="M0 52 C150 36 260 44 380 50 S580 42 680 40 V64 H0Z" fill="#f4f5f8"/></svg>'
    )
    return (
        f'<div class="sky">{stars}<span class="orb {sky["orb"]}" style="left:{left}px;top:{top}px"></span>'
        f'<div class="eyebrow"><b>{_e(data.get("group_name") or "本群")}</b>'
        f'<span>{_e(_date_line(data.get("slot_ts"), data.get("slot_label", "")))}</span></div>'
        f'<div class="word">{_e(word)}</div>'
        f'<div class="lede">{lede}</div>{hill}</div>'
    )


def _item_html(i: int, item: dict, now: float, lead: bool) -> str:
    title = _clip(item.get("title"), 60)
    summary = _clip(item.get("summary"), 110 if lead else 72)
    why = _clip(item.get("why"), 70)
    meta = []
    if item.get("site"):
        meta.append(f'<span class="src">{_e(item["site"])}</span>')
    ago = _ago(item.get("published_ts"), now)
    if ago:
        meta.append(f"<span>{_e(ago)}</span>")
    meta += [f'<span class="tag">{_e(t)}</span>' for t in _tags(item)]
    cover = str(item.get("cover") or "")
    if not cover.startswith("data:image/"):
        cover = ""  # 卡片里只许内嵌图，外链一律不画
    why_html = f'<div class="why"><i>值得看</i>{_e(why)}</div>' if why else ""
    meta_html = f'<div class="meta">{"".join(meta)}</div>' if meta else ""
    sum_html = f'<div class="sum">{_e(summary)}</div>' if summary else ""
    if cover and lead and int(item.get("cover_w") or 0) >= _COVER_WIDE_W:
        # 头条：整幅封面在上，标题压在图下
        return (
            f'<div class="item lead has-cover"><div class="cover" style="background-image:url({cover})"></div>'
            f'<div class="body"><div class="title">{_e(title)}</div>{sum_html}{why_html}{meta_html}</div></div>'
        )
    if cover:
        # 其余：右边一张方缩略图
        return (
            f'<div class="item"><div class="row"><div class="txt"><div class="title">{_e(title)}</div>{sum_html}</div>'
            f'<div class="thumb" style="background-image:url({cover})"></div></div>{why_html}{meta_html}</div>'
        )
    icon = _icon_uri(str(item.get("icon") or "newspaper"))
    return (
        f'<div class="item{" lead" if lead else ""}"><div class="head">'
        + (f'<img src="{icon}" alt="">' if icon else f'<span class="rank">{i}</span>')
        + f'<div class="title">{_e(title)}</div></div>'
        + sum_html + why_html + meta_html
        + "</div>"
    )


def render_html(data: dict, *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    items = [it for it in (data.get("items") or []) if isinstance(it, dict) and it.get("title")][:3]
    word = slot_word(data.get("slot_ts"), str(data.get("slot_label") or ""))
    sky = _SKIES[word]
    dark = sky["orb"] == "moon"
    style = (
        f'--top:{sky["top"]};--bottom:{sky["bottom"]};--ink:{sky["ink"]};--sub:{sky["sub"]};'
        + ("--why-bg:#f1efff;--why-ink:#3b3370;" if dark else "--why-bg:#fff5e6;--why-ink:#6b4410;"
           if word in ("下午", "早上") else "--why-bg:#eef5ff;--why-ink:#173e66;")
    )
    foot_left = "点消息里的链接，看全部资讯和详情" if data.get("link") else "在 MaiWork 网页里看全部资讯"
    body = (
        f'<div class="card" style="{style}">'
        + _sky_html(word, sky, data, len(items))
        + '<div class="list">'
        + "".join(_item_html(i + 1, it, now, i == 0) for i, it in enumerate(items))
        + "</div>"
        + f'<div class="foot"><span>{_e(foot_left)}</span><span class="brand">MAIWORK</span></div>'
        + "</div>"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<style>{_CSS % {'w': _WIDTH}}</style></head><body>{body}</body></html>"
    )


async def render_png(data: dict, *, timeout_s: float = _RENDER_TIMEOUT_S) -> bytes:
    """无头 chromium 截图；同一时间只画一张（省内存）。失败一律抛 RenderError。"""
    try:
        from playwright.async_api import async_playwright  # 宿主 venv 自带；本地测试可能没有
    except Exception as e:  # noqa: BLE001
        raise RenderError(f"没有 playwright：{e}") from e
    try:
        await prepare_covers(data)
    except Exception as e:  # noqa: BLE001  配图失败不影响出卡片
        logger.info("资讯卡片配图失败：%s", e)
    page_html = render_html(data)

    async def _shot() -> bytes:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = await browser.new_page(
                    viewport={"width": _WIDTH, "height": 800}, device_scale_factor=_SCALE
                )
                # 不许卡片去拉任何外部资源（内容全是内嵌的）
                await page.route("**/*", lambda route: route.abort()
                                 if not route.request.url.startswith(("data:", "about:")) else route.continue_())
                await page.set_content(page_html, wait_until="load")
                return await page.locator(".card").screenshot(type="png")
            finally:
                await browser.close()

    async with _LOCK:
        try:
            return await asyncio.wait_for(_shot(), timeout=timeout_s)
        except RenderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RenderError(f"卡片截图失败：{type(e).__name__}: {e}") from e
