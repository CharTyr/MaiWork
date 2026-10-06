"""资讯卡片渲染（主会话写；card_push.py 调它）。

render_html(data) -> str：把一批资讯精选拼成一张卡片的 HTML（纯本地，不联网：图标内嵌 data URI，
    不加载外链图片 / 网页字体，服务器上用系统里的 Noto CJK）。
render_png(data) -> bytes：用 playwright 起一个无头 chromium 截图（2 倍像素），同一时间只画一张。
    没装 playwright / 浏览器起不来 / 超时 → 抛 RenderError，调用方退回纯文字。

data（card_push 组装）：
    {"group_name", "slot_label": "9月29日 · 下午", "slot_ts": float, "link", "total": int,
     "items": [{"title","brief","summary","why","site","published_ts","icon","keywords":[...],"topic","kind",
                "image_url", "url"}]}

配图：render_png 先给每条补封面（prepare_covers）——有 image_url 就下它，没有就打开原文 url 找
og:image；只收公开地址（和 fetch_page 同一套防内网）、只收位图、太小 / 太扁的不要，缩到 1200 宽
转 JPEG 内嵌成 data URI（卡片本身仍不联网）。拿不到就不配图，版式自动退回图标。

版式：顶上一块「此刻的天空」——按批次所在时段换天色（凌晨 / 早上 / 中午 / 下午 / 晚上）
和日月位置，大字写时段；下面是 1~3 条，第一条最大（分数最高）。
2026-10-06 用户定：每条只放一两句短摘要（item["brief"]，没有就从 summary 按句截，见
short_summary）；「值得看」只放头条；最后一块是本群网页的二维码（qr.py 自带编码器，
没有链接就不画）。链接原文不进图片。
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

__all__ = ["RenderError", "cover_tint", "fetch_cover", "prepare_covers", "render_html", "render_png", "slot_word", "tint_palette"]

_ICON_DIR = Path(__file__).resolve().parent / "console" / "static" / "assets" / "icons"
_WIDTH = 600          # CSS 像素；截图 2 倍 → 1200 宽，手机上点开看得清
_SCALE = 2
_RENDER_TIMEOUT_S = 60.0
_VIZ_W = 520          # 图解截图宽度 = 卡片里正文的宽度（600 - 两边 16 - 卡片内边距 24×2）
_VIZ_MAX_H = 520      # 图解太长只截上面这么高（头条）
_VIZ_SUB_MAX_H = 300  # 第二、三条的图解最多截这么高
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
_COVER_WIDE_W = 480         # 够这么宽才拉满整幅；不够就按原大小居中（不拉伸）
_COVER_MAX_RATIO = 3.2      # 宽高比超过这个（横幅条）不要
_COVER_OUT_W = 1200
_COVER_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")
_UA = "Mozilla/5.0 (compatible; MaiWork/1.0; +news-card)"


def _to_jpeg_uri(raw: bytes, url: str = "") -> tuple[str, int]:
    """位图 → 检查尺寸 → 不像 logo / 带字横幅 → 缩到 1200 宽 → (JPEG data URI, 原图宽)；不合格返回 ("", 0)。"""
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
        # 2026-10-06 用户要：站点 logo / 平涂图 / 带大字横幅当封面没信息量，宁可不放图
        from .cover_filter import judge

        why_not = judge(im, url)
        if why_not:
            logger.info("资讯卡片封面不用（%s）：%s", why_not, url)
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
            best = _to_jpeg_uri(raw, image_url) if raw else ("", 0)
            if best[1] >= _COVER_WIDE_W:
                return best
        if page_url:
            page, _, final = await _get_public(page_url, transport=transport, resolver=resolve, want_image=False)
            og = _extract_og_image(page.decode("utf-8", errors="replace"), final) if page else ""
            if og and og != image_url:
                raw, _, _ = await _get_public(og, transport=transport, resolver=resolve, want_image=True)
                got = _to_jpeg_uri(raw, og) if raw else ("", 0)
                if got[1] > best[1]:
                    best = got
    except Exception as e:  # noqa: BLE001  配图失败只是不配图
        logger.info("资讯卡片封面没拿到（%s）：%s", image_url or page_url, e)
    return best


def tint_palette(rgb: Any) -> dict | None:
    """主色 → 这条资讯自己的配色（方案 4，2026-10-06 用户定）：{"bg" 浅底, "ink" 深字, "glow" 卡片光晕}。

    同一色相：底 = 很浅（亮度 0.94）、字 = 很深（亮度 0.20），对比度 ≥ 7；几乎没饱和度的灰图返回 None
    （沿用天空配色，不硬染一层脏灰）。"""
    import colorsys

    try:
        r, g, b = (max(0, min(255, int(c))) / 255 for c in rgb)
    except Exception:  # noqa: BLE001
        return None
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    if sat < 0.18 or l < 0.06 or l > 0.97:
        return None

    def hx(light: float, satur: float) -> str:
        rr, gg, bb = colorsys.hls_to_rgb(h, light, satur)
        return "#%02x%02x%02x" % (round(rr * 255), round(gg * 255), round(bb * 255))

    gr, gg, gb = colorsys.hls_to_rgb(h, 0.5, min(0.8, max(0.45, sat)))
    return {
        "bg": hx(0.94, min(0.75, max(0.45, sat))),
        "ink": hx(0.2, min(0.7, max(0.4, sat))),
        "glow": f"rgba({round(gr * 255)},{round(gg * 255)},{round(gb * 255)},.32)",
    }


def cover_tint(uri: str) -> dict | None:
    """从内嵌封面（data URI）里挑一个「有颜色」的主色，交给 tint_palette；失败 / 灰图 → None。

    做法：缩到 48×48、量化成 8 色，按「像素数 × 饱和度」挑最显眼的那一个（不取平均色——平均色发灰）。"""
    import colorsys

    try:
        from PIL import Image

        raw = base64.b64decode(str(uri or "").split(",", 1)[1], validate=True)
        im = Image.open(io.BytesIO(raw)).convert("RGB").resize((48, 48))
        q = im.quantize(colors=8)
        pal = q.getpalette() or []
        best, best_score = None, 0.0
        for count, idx in q.getcolors() or []:
            rgb = pal[idx * 3: idx * 3 + 3]
            if len(rgb) < 3:
                continue
            _, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
            if light < 0.12 or light > 0.92:
                continue  # 太黑 / 太白的是背景，不是「这张图的颜色」
            score = count * sat
            if score > best_score:
                best, best_score = rgb, score
        return tint_palette(best) if best else None
    except Exception:  # noqa: BLE001  取不出就用天空配色
        return None


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
        it["tint"] = cover_tint(it["cover"]) if it["cover"] else None

    try:
        await asyncio.wait_for(asyncio.gather(*(one(it) for it in todo)), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.info("资讯卡片封面超时，没拿到的就不配图")


# 时段 → 天色（2026-09-29 改成苹果天气那种：几团柔光叠成的渐变天空，白字压在上面）。
# base = 从上到下的底色；glows = 叠在上面的柔光团 (x%, y%, 颜色, 半径px)；orb = 太阳 / 月亮；
# orb_x / orb_y = 天体中心在天空区里的相对位置（0 顶 ~ 1 底）——底边故意落进卡片压住的那一截，
# 被第一张卡片挡住一部分，前后层次就出来了。
_SKIES: dict[str, dict[str, Any]] = {
    "凌晨": {"base": ("#04060f", "#0c1433", "#1c2a5e"),
             "glows": ((82, 78, "rgba(88,110,220,.45)", 260), (10, 10, "rgba(40,30,90,.55)", 220)),
             "orb": "moon", "orb_x": 0.80, "orb_y": 0.62, "stars": 18, "why": ("#eef1ff", "#1f2a5c")},
    "早上": {"base": ("#5b7fd6", "#c79bc4", "#ffcfa8"),
             "glows": ((84, 88, "rgba(255,196,140,.95)", 240), (20, 20, "rgba(120,150,235,.55)", 240)),
             "orb": "sun", "orb_x": 0.80, "orb_y": 0.64, "stars": 0, "why": ("#fff3ea", "#6b3a1c")},
    "中午": {"base": ("#1c64d0", "#3f93ec", "#8fc8ff"),
             "glows": ((82, 14, "rgba(255,255,255,.75)", 190), (12, 90, "rgba(120,200,255,.55)", 260)),
             "orb": "sun", "orb_x": 0.82, "orb_y": 0.30, "stars": 0, "why": ("#eaf4ff", "#0f3a66")},
    "下午": {"base": ("#2b63b8", "#d9906a", "#ffd59a"),
             "glows": ((82, 70, "rgba(255,190,110,.95)", 250), (14, 18, "rgba(90,130,210,.55)", 240)),
             "orb": "sun", "orb_x": 0.82, "orb_y": 0.60, "stars": 0, "why": ("#fff4e4", "#6b4410")},
    "晚上": {"base": ("#0a0e2e", "#1d2263", "#4a3a8e"),
             "glows": ((86, 86, "rgba(150,110,230,.55)", 260), (8, 12, "rgba(20,30,90,.7)", 240)),
             "orb": "moon", "orb_x": 0.80, "orb_y": 0.60, "stars": 14, "why": ("#f1efff", "#3b3370")},
}
_SKY_H = 300          # 天空区高度（CSS px；2026-10-06 由 360 收：天空只是氛围，别占太多）
_OVERLAP = 100        # 资讯卡片往上压住天空的高度
_ORB_SIZE = {"sun": 120, "moon": 96}
_BRIEF_MAX = 110      # 没有 brief 的老资讯：从 summary 截出的短摘要最多这么多字（2026-10-06 由 72 放宽）
_QR_PX = 116          # 二维码边长（CSS px；截图 2 倍 → 232px，手机长按 / 扫码都够）

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


def _squash(s: Any) -> str:
    return " ".join(str(s or "").split())


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


_SENT_END = "。！？!?；;"
_CLAUSE = "，,、：:"


def short_summary(text: Any, limit: int = _BRIEF_MAX) -> str:
    """没有 brief 的老资讯：从 summary 取开头一两句（≤ limit 字）。

    整句放得下就按句号收，不加省略号；第一句就超长才在逗号处断开、补「…」。"""
    s = _squash(text)
    if len(s) <= limit:
        return s
    out = ""
    buf = ""
    for ch in s:
        buf += ch
        if ch in _SENT_END:
            if len(out) + len(buf) > limit:
                break
            out += buf
            buf = ""
    if out:
        return out.strip()
    head = s[:limit]
    cut = max(head.rfind(c) for c in _CLAUSE)
    if cut >= limit // 2:
        head = head[:cut]
    return head.rstrip(_CLAUSE + " ") + "…"


def _card_text(item: dict) -> str:
    """卡片上的那一两句：模型写的 brief 优先，没有就从 summary 截。"""
    brief = _squash(item.get("brief"))
    return brief if brief else short_summary(item.get("summary"))


def _qr_uri(link: str) -> str:
    """本群网页链接 → 二维码 SVG data URI；没链接 / 编码失败 → ""（卡片照出，只是没码）。"""
    link = str(link or "").strip()
    if not link:
        return ""
    try:
        from . import qr

        return qr.svg_data_uri(link, ecc="M", border=2)
    except Exception as e:  # noqa: BLE001  二维码是锦上添花
        logger.info("资讯卡片二维码没画出来：%s", e)
        return ""


_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:transparent}
body{width:%(w)dpx;font-family:"SF Pro Display","PingFang SC","Noto Sans CJK SC","Source Han Sans SC","HarmonyOS Sans SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;color:#1d1d1f}
.card{width:%(w)dpx;background:#f2f2f7;overflow:hidden}
.sky{position:relative;height:%(sky)dpx;padding:34px 34px 0;overflow:hidden;color:var(--ink);background:var(--sky)}
.sky::after{content:"";position:absolute;left:0;right:0;bottom:0;height:84px;
  background:linear-gradient(180deg,rgba(242,242,247,0) 0%%,rgba(242,242,247,.55) 70%%,#f2f2f7 100%%)}
.glow{position:absolute;border-radius:50%%;filter:blur(38px)}
.orb{position:absolute;border-radius:50%%}
.orb.sun{background:radial-gradient(circle at 50%% 50%%,#fffef6 0%%,#fff6d6 34%%,rgba(255,236,190,.55) 52%%,rgba(255,226,170,0) 72%%)}
.orb.sun::before{content:"";position:absolute;inset:-60px;border-radius:50%%;
  background:radial-gradient(circle,rgba(255,244,214,.45) 0%%,rgba(255,244,214,0) 68%%)}
.orb.moon{background:radial-gradient(circle at 36%% 34%%,#fffdf6 0%%,#f3efe2 46%%,#d8d2c2 100%%);
  box-shadow:0 0 0 1px rgba(255,255,255,.06),0 0 70px 26px rgba(200,200,255,.16),inset -10px -12px 22px rgba(120,110,150,.22)}
.orb.moon::before{content:"";position:absolute;width:18px;height:18px;left:54%%;top:30%%;border-radius:50%%;background:rgba(170,160,150,.18)}
.orb.moon::after{content:"";position:absolute;width:11px;height:11px;left:30%%;top:60%%;border-radius:50%%;background:rgba(170,160,150,.16)}
.star{position:absolute;border-radius:50%%;background:#fff}
.eyebrow{position:relative;z-index:1;font-size:16px;font-weight:600;letter-spacing:.02em;color:var(--sub)}
.word{position:relative;z-index:1;margin-top:6px;font-weight:700;font-size:80px;line-height:1.02;letter-spacing:-.02em;
  text-shadow:0 2px 24px rgba(0,0,0,.12)}
.lede{position:relative;z-index:1;margin-top:12px;max-width:400px;display:flex;align-items:baseline;gap:6px;
  font-size:16px;line-height:1.45;font-weight:500;color:var(--sub);white-space:nowrap}
.lede b{min-width:0;overflow:hidden;text-overflow:ellipsis;font-weight:700;color:var(--ink)}
.lede span{flex:none}
.lede em{font-style:normal;color:var(--ink);font-weight:700}
.list{position:relative;z-index:2;margin-top:-%(overlap)dpx;padding:0 16px 16px;display:flex;flex-direction:column;gap:12px}
.item{background:#fff;border-radius:24px;padding:22px 24px 20px;
  box-shadow:0 0 0 .5px rgba(0,0,0,.04),0 2px 6px rgba(0,0,0,.04),0 18px 40px -22px var(--glow,rgba(20,20,40,.28))}
.list .item:first-child{box-shadow:0 0 0 .5px rgba(0,0,0,.05),0 4px 14px rgba(10,10,40,.10),0 30px 60px -24px var(--glow,rgba(10,10,40,.45))}
.head{display:flex;gap:12px;align-items:flex-start}
.head img{flex:none;width:40px;height:40px;margin-top:1px}
.rank{flex:none;width:22px;font-size:15px;font-weight:700;color:#aeaeb2;margin-top:4px;font-variant-numeric:tabular-nums}
.title{font-size:21px;line-height:1.38;font-weight:700;letter-spacing:-.005em;color:#1d1d1f;
  text-wrap:pretty;word-break:break-word}
.lead .title{font-size:26px;line-height:1.3;letter-spacing:-.01em}
.sum{margin-top:8px;font-size:17px;line-height:1.62;color:#48484a;text-wrap:pretty;word-break:break-word}
.lead .sum{margin-top:10px;font-size:18px;color:#3a3a3c}
.why{margin-top:14px;padding:11px 14px 12px;border-radius:14px;background:var(--why-bg);font-size:16px;line-height:1.55;color:var(--why-ink)}
.why i{font-style:normal;font-weight:700;margin-right:6px}
.meta{margin-top:12px;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;font-size:14px;color:#8a8a8e}
.meta .tag{padding:2px 9px;border-radius:999px;background:var(--why-bg);color:var(--why-ink);font-weight:600}
.meta .src{font-weight:600;color:#6b6b70}
.item.has-cover{padding:0;overflow:hidden}
.has-cover .body{padding:18px 24px 20px}
.frame{position:relative;overflow:hidden;aspect-ratio:16/9;background:#1c1c1e;display:flex;align-items:center;justify-content:center}
.item:not(.lead) .frame{aspect-ratio:2/1}
.frame .bg{position:absolute;inset:-40px;background-size:100%% 100%%;filter:blur(28px) saturate(1.3) brightness(.82);transform:scale(1.1)}
.frame img{position:relative;display:block;width:100%%;height:100%%;object-fit:contain;filter:drop-shadow(0 6px 18px rgba(0,0,0,.3))}
.frame img.small{width:auto;height:auto;max-width:100%%;max-height:100%%;border-radius:8px}
.txt{flex:1;min-width:0}
.viz{position:relative;margin-top:14px;border-radius:16px;overflow:hidden}
.viz img{display:block;width:100%%}
.viz.cut::after{content:"";position:absolute;left:0;right:0;bottom:0;height:56px;
  background:linear-gradient(180deg,rgba(255,255,255,0),#fff)}
.go{display:flex;align-items:center;gap:18px;background:#fff;border-radius:24px;padding:16px 16px 16px 24px;
  box-shadow:0 0 0 .5px rgba(0,0,0,.04),0 2px 6px rgba(0,0,0,.04)}
.go .t{flex:1;min-width:0}
.go b{display:block;font-size:19px;line-height:1.35;font-weight:700;color:#1d1d1f}
.go span{display:block;margin-top:6px;font-size:15px;line-height:1.5;color:#6b6b70}
.go .qr{flex:none;width:%(qr)dpx;height:%(qr)dpx;image-rendering:pixelated}
.foot{display:flex;justify-content:space-between;align-items:center;padding:4px 32px 22px;font-size:15px;color:#8a8a8e}
.foot.solo{justify-content:flex-end}
.brand{font-weight:800;letter-spacing:.08em;color:#b0b0b6}
"""


def _sky_background(sky: dict) -> str:
    """底色三段渐变 + 柔光团（CSS 多层背景，从上往下画：先写的在上面）。"""
    top, mid, bottom = sky["base"]
    glows = ",".join(
        f"radial-gradient({r}px {r}px at {x}% {y}%,{c} 0%,rgba(0,0,0,0) 100%)" for x, y, c, r in sky["glows"]
    )
    return f"{glows},linear-gradient(180deg,{top} 0%,{mid} 58%,{bottom} 100%)"


# 星星：固定散点（不随机，同一时段每次一样），(x, y, 大小, 透明度)
_STARS = [(46, 30, 2, .8), (120, 58, 1.5, .55), (200, 22, 2, .7), (262, 74, 1.5, .5), (330, 40, 2.5, .85),
          (398, 16, 1.5, .6), (438, 92, 2, .55), (520, 58, 1.5, .7), (560, 128, 2, .5), (300, 118, 1.5, .45),
          (84, 110, 2, .5), (486, 150, 1.5, .4), (160, 150, 1.5, .35), (372, 170, 2, .35), (24, 176, 1.5, .3),
          (228, 196, 1.5, .3), (580, 26, 2, .6), (430, 210, 1.5, .25)]


def _sky_html(word: str, sky: dict, data: dict, shown: int) -> str:
    total = int(data.get("total") or 0)
    # 群名太长只截群名（一行），后面的条数不折行、不被挤进卡片
    lede = (
        f'<b>{_e(data.get("group_name") or "本群")}</b><span>· 本期精选 <em>{shown}</em> 条'
        + (f" · 共找到 {total} 条" if total > shown else "")
        + "</span>"
    )
    size = _ORB_SIZE[sky["orb"]]
    left = int(_WIDTH * sky["orb_x"] - size / 2)
    top = int(_SKY_H * sky["orb_y"] - size / 2)
    stars = "".join(
        f'<span class="star" style="left:{x}px;top:{y}px;width:{d}px;height:{d}px;opacity:{o}"></span>'
        for x, y, d, o in _STARS[: int(sky.get("stars") or 0)]
    )
    return (
        f'<div class="sky">{stars}'
        f'<span class="orb {sky["orb"]}" style="left:{left}px;top:{top}px;width:{size}px;height:{size}px"></span>'
        f'<div class="eyebrow">{_e(_date_line(data.get("slot_ts"), data.get("slot_label", "")))}</div>'
        f'<div class="word">{_e(word)}</div>'
        f'<div class="lede">{lede}</div></div>'
    )


def _item_html(i: int, item: dict, now: float, lead: bool) -> str:
    # 2026-10-06 用户定：摘要只放一两句（brief，没有就从 summary 截）；「值得看」只放头条。
    # 标题、「值得看」仍完整显示（2026-09-29 用户定），只把多余空白并成一个。
    title = _squash(item.get("title"))
    text = _card_text(item)
    why = _squash(item.get("why")) if lead else ""
    meta = []
    if item.get("site"):
        meta.append(f'<span class="src">{_e(item["site"])}</span>')
    # 文章（kind=guide）不显示发布日期（2026-10-05 用户定：老文章挂着「几月几日」显得旧）；资讯照旧
    ago = "" if item.get("kind") == "guide" else _ago(item.get("published_ts"), now)
    if ago:
        meta.append(f"<span>{_e(ago)}</span>")
    meta += [f'<span class="tag">{_e(t)}</span>' for t in _tags(item)[:1]]
    cover = str(item.get("cover") or "")
    if not cover.startswith("data:image/"):
        cover = ""  # 卡片里只许内嵌图，外链一律不画
    why_html = f'<div class="why"><i>值得看</i>{_e(why)}</div>' if why else ""
    meta_html = f'<div class="meta">{"".join(meta)}</div>' if meta else ""
    sum_html = f'<div class="sum">{_e(text)}</div>' if text else ""
    lead_cls = " lead" if lead else ""
    # 方案 4：有配图就用从图里取的颜色染「值得看」、标签和卡片光晕；没有就沿用天空配色
    tint = item.get("tint") if cover else None
    tint_style = ""
    if isinstance(tint, dict) and all(isinstance(tint.get(k), str) for k in ("bg", "ink", "glow")):
        vals = [tint["bg"], tint["ink"], tint["glow"]]
        if all(re.fullmatch(r"#[0-9a-f]{6}|rgba\(\d{1,3},\d{1,3},\d{1,3},\.\d+\)", v) for v in vals):
            tint_style = f' style="--why-bg:{vals[0]};--why-ink:{vals[1]};--glow:{vals[2]}"'
    if cover:
        # 有配图的每一条都整幅放在上面（2026-10-06 用户定：第二、三条也和头条一样），按原比例
        # 完整显示不裁；原图不够宽的按原大小居中，不硬拉满变糊。
        small = ' class="small"' if int(item.get("cover_w") or 0) < _COVER_WIDE_W else ""
        # 统一比例画框（头条 16:9、其余 2:1）：图完整居中，空出来的地方用同一张图模糊放大垫底
        frame = (f'<div class="frame"><div class="bg" style="background-image:url({cover})"></div>'
                 f'<img{small} src="{cover}" alt=""></div>')
        return (
            f'<div class="item{lead_cls} has-cover"{tint_style}>{frame}'
            f'<div class="body"><div class="title">{_e(title)}</div>{sum_html}{why_html}{meta_html}</div></div>'
        )
    icon = _icon_uri(str(item.get("icon") or "newspaper"))
    viz = str(item.get("viz_img") or "")
    cut = " cut" if item.get("viz_cut") else ""
    viz_html = f'<div class="viz{cut}"><img src="{viz}" alt=""></div>' if viz.startswith("data:image/") else ""
    return (
        f'<div class="item{lead_cls}"><div class="head">'
        + (f'<img src="{icon}" alt="">' if icon else f'<span class="rank">{i}</span>')
        + f'<div class="title">{_e(title)}</div></div>'
        + sum_html + viz_html + why_html + meta_html
        + "</div>"
    )


def _go_html(data: dict, shown: int) -> str:
    """二维码那一块（2026-10-06 用户要：扫码进本群 MaiWork 网页）。链接原文不画进图里。"""
    qr = _qr_uri(str(data.get("link") or ""))
    if not qr:
        return ""
    more = int(data.get("total") or 0) - shown
    sub = f"网页里还有另外 {more} 条，以及原文链接" if more > 0 else "网页里有原文链接和更多资讯"
    return (
        f'<div class="go"><div class="t"><b>扫码看本群全部资讯</b><span>{_e(sub)}</span></div>'
        f'<img class="qr" src="{qr}" alt=""></div>'
    )


def render_html(data: dict, *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    items = [it for it in (data.get("items") or []) if isinstance(it, dict) and it.get("title")][:3]
    word = slot_word(data.get("slot_ts"), str(data.get("slot_label") or ""))
    sky = _SKIES[word]
    why_bg, why_ink = sky["why"]
    style = (
        f'--sky:{_sky_background(sky)};--ink:#fff;--sub:rgba(255,255,255,.76);'
        f"--why-bg:{why_bg};--why-ink:{why_ink};"
    )
    go = _go_html(data, len(items))
    if go:
        foot = '<div class="foot solo"><span class="brand">MAIWORK</span></div>'
    else:
        foot_left = "点消息里的链接，看全部资讯和详情" if data.get("link") else "在 MaiWork 网页里看全部资讯"
        foot = f'<div class="foot"><span>{_e(foot_left)}</span><span class="brand">MAIWORK</span></div>'
    body = (
        f'<div class="card" style="{style}">'
        + _sky_html(word, sky, data, len(items))
        + '<div class="list">'
        + "".join(_item_html(i + 1, it, now, i == 0) for i, it in enumerate(items))
        + go
        + "</div>"
        + foot
        + "</div>"
    )
    css = _CSS % {"w": _WIDTH, "sky": _SKY_H, "overlap": _OVERLAP, "qr": _QR_PX}
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<style>{css}</style></head><body>{body}</body></html>"
    )


def _viz_todo(data: dict) -> list[dict]:
    """要截图解的条目：前 3 条里没封面（内嵌图）、但有核对过的图解整页的。"""
    out = []
    for it in [x for x in (data.get("items") or []) if isinstance(x, dict)][:3]:
        if str(it.get("cover") or "").startswith("data:image/"):
            continue
        if str(it.get("viz_html") or "").strip():
            out.append(it)
    return out


async def _shoot_viz(browser: Any, data: dict) -> None:
    """把图解整页截成图，塞进 item["viz_img"]（浅色、白底、正文宽）；哪条失败就那条不画。"""
    first = next((x for x in (data.get("items") or []) if isinstance(x, dict)), None)
    for it in _viz_todo(data):
        max_h = _VIZ_MAX_H if it is first else _VIZ_SUB_MAX_H  # 非头条的图解截短一点，整张别太长
        page = None
        try:
            page = await browser.new_page(
                viewport={"width": _VIZ_W, "height": 400}, device_scale_factor=_SCALE, color_scheme="light"
            )
            await page.route("**/*", lambda route: route.abort()
                             if not route.request.url.startswith(("data:", "about:")) else route.continue_())
            await page.set_content(str(it["viz_html"]), wait_until="load")
            await page.add_style_tag(content="html,body{background:#fff!important}")
            h = int(await page.evaluate("Math.ceil(document.documentElement.scrollHeight)") or 0)
            if h < 40:
                continue
            png = await page.screenshot(type="png", clip={"x": 0, "y": 0, "width": _VIZ_W, "height": min(h, max_h)})
            it["viz_img"] = "data:image/png;base64," + base64.b64encode(png).decode()
            it["viz_cut"] = h > max_h  # 截断了：底部加一道渐隐，别在半截硬切
        except Exception as e:  # noqa: BLE001  图解截不出来就不画，不影响卡片
            logger.info("资讯卡片图解截图失败：%s", e)
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass


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

    async def _shot() -> bytes:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                # 没封面的条目有图解 → 先把图解截成图（2026-09-29 用户要：免得连配图都没有）
                await _shoot_viz(browser, data)
                page_html = render_html(data)
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
