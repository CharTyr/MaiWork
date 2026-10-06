"""封面图过滤：news_card 挑封面时先问这里一句，别把站点 logo / 带字横幅当封面。

`judge(im, url)` 返回 "" 表示这张图可以当封面；返回一句简短中文原因（如「像 logo /
平涂图」「像带字横幅」）表示别用——宁可不配图。**从不抛异常**。

三条规则（阈值都是下面这些常量，改之前先看 tests/test_cover_filter.py，那里存着每
条规则的样例图）：

1. 地址关键词：只看文件名和最后两段路径，命中 logo / favicon / icon / avatar /
   placeholder / default / brand 之类 → 「地址像 logo」。
2. 平涂图（logo / 图标 / 纯色底大字）：缩到 64×64、每通道量化成 32 级后，主色占比
   过半、颜色种类不多，而且主色是一整块（不是渐变里的一小段）→「像 logo / 平涂图」。
3. 带字横幅：背景平（占三成以上但没到平涂那么极端）+ 256 宽下有成片的强边缘
   （文字笔画）→「像带字横幅」。

实测数据（2026-10 本地）：真照片（含峡谷、人像、截图）64×64 主色占比都 < 0.25，
设计图（arXiv logo 0.80、Gleam 0.58、ElevenLabs 0.51、纯色底 1.0）都 > 0.45 —— 这条
线给两边都留了余量。

只用 Pillow（不依赖 numpy）。图读不出 / 尺寸怪 / 参数怪一律不抛：过滤器自己出问题时
返回 ""（宁可放过，也不要因为过滤器出 bug 杀掉一张真照片）。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from PIL import Image, ImageFilter

__all__ = ["judge"]

# --- 规则 1：地址关键词 -------------------------------------------------------
# 只看文件名 + 最后两段路径：更靠前的目录名可能只是栏目名（/logo-contest/winners/）。
_URL_WORDS = frozenset(
    {
        "logo",
        "logotype",
        "brandmark",
        "brand",
        "favicon",
        "icon",
        "avatar",
        "placeholder",
        "default",
        "og-default",
        "share-default",
    }
)
# 再放宽一点点：以 logo 结尾的长词（sitelogo / CompanyLogo）也算。icon 不放宽，
# 免得误伤 silicon / iconic。
_URL_SUFFIX_WORDS = ("logo",)

# --- 规则 2/3：图像统计 -------------------------------------------------------
_SMALL = 64                # 平涂统计的边长
_QUANT_SHIFT = 3           # 每通道 >>3 → 32 级
_NEAR_TOL = 24             # 主色邻域容差（每通道），用来判断「是不是一整块」
_EDGE_SIZE = 256           # 边缘统计边长（细笔画要看得见）
_EDGE_BORDER = 2           # FIND_EDGES 在外圈有伪影，切掉
_EDGE_STRONG = 48          # 边缘强度到这个值算「强边缘」

_FLAT_DOM = 0.45           # 平涂：主色占比下限
_FLAT_MAX_COLORS = 320     # 平涂：量化后颜色种类上限
_FLAT_PLATEAU = 2.2        # 平涂：主色邻域占比/主色占比 上限（超了说明是渐变的一段）

_TEXT_DOM = 0.30           # 带字横幅：背景占比下限（上界就是 _FLAT_DOM）
_TEXT_MAX_COLORS = 256     # 带字横幅：颜色种类上限
_TEXT_EDGE = 0.025         # 带字横幅：强边缘占比下限
_TEXT_PLATEAU = 1.6        # 带字横幅：背景要够平


def judge(im: "Image.Image", url: str = "") -> str:
    """可以当封面返回 ""；否则返回简短中文原因。从不抛。"""
    try:
        why = _url_reason(url)
    except Exception:  # noqa: BLE001  地址再怪也不该影响出图
        why = ""
    if why:
        return why
    try:
        if not isinstance(im, Image.Image):
            return ""
        return _image_reason(im)
    except Exception:  # noqa: BLE001  过滤器自己出问题 → 放过，不要连累真照片
        return ""


# ------------------------------------------------------------------ 规则 1：地址


def _url_reason(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    path = urlsplit(raw).path or raw.split("?", 1)[0]
    segs = [s for s in path.split("/") if s]
    if not segs:
        return ""
    for seg in segs[-2:]:  # 文件名 + 上一级目录
        for word in re.split(r"[^a-z0-9]+", seg.lower()):
            if not word:
                continue
            if word in _URL_WORDS:
                return "地址像 logo"
            if len(word) > 4 and word.endswith(_URL_SUFFIX_WORDS):
                return "地址像 logo"
    return ""


# ------------------------------------------------------------------ 规则 2/3：图像


def _as_rgb(im: "Image.Image") -> "Image.Image":
    """统一转 RGB；带透明通道的先贴到白底上（透明 logo 常是「白图案 + 透明底」）。"""
    if im.mode == "RGB":
        return im
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    if has_alpha:
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return im.convert("RGB")


def _stats(im: "Image.Image") -> tuple[float, int, float, float]:
    """返回 (主色占比, 颜色种类数, 主色邻域占比/主色占比, 强边缘占比)。"""
    rgb = _as_rgb(im)
    small = rgb.resize((_SMALL, _SMALL), Image.BILINEAR)
    data = small.tobytes()  # RGB：每像素 3 字节
    total = _SMALL * _SMALL
    shift, mask = _QUANT_SHIFT, (1 << _QUANT_SHIFT) - 1

    counts: dict[tuple[int, int, int], int] = {}
    for i in range(0, total * 3, 3):
        key = (data[i] >> shift, data[i + 1] >> shift, data[i + 2] >> shift)
        counts[key] = counts.get(key, 0) + 1
    dom_key, dom_n = max(counts.items(), key=lambda kv: kv[1])
    dom = dom_n / total
    dome = tuple((c << shift) | mask for c in dom_key)

    near_n = 0
    for i in range(0, total * 3, 3):
        if (
            abs(data[i] - dome[0]) <= _NEAR_TOL
            and abs(data[i + 1] - dome[1]) <= _NEAR_TOL
            and abs(data[i + 2] - dome[2]) <= _NEAR_TOL
        ):
            near_n += 1
    plateau = near_n / total / dom if dom > 0 else 0.0

    side = _EDGE_SIZE
    edges = rgb.convert("L").resize((side, side), Image.BILINEAR).filter(ImageFilter.FIND_EDGES)
    edges = edges.crop((_EDGE_BORDER, _EDGE_BORDER, side - _EDGE_BORDER, side - _EDGE_BORDER))
    inner = (side - 2 * _EDGE_BORDER) ** 2
    edge = sum(edges.histogram()[_EDGE_STRONG:]) / inner
    return dom, len(counts), plateau, edge


def _image_reason(im: "Image.Image") -> str:
    dom, ncol, plateau, edge = _stats(im)
    if dom >= _FLAT_DOM and ncol <= _FLAT_MAX_COLORS and plateau <= _FLAT_PLATEAU:
        return "像 logo / 平涂图"
    if (
        _TEXT_DOM <= dom < _FLAT_DOM
        and ncol <= _TEXT_MAX_COLORS
        and edge >= _TEXT_EDGE
        and plateau <= _TEXT_PLATEAU
    ):
        return "像带字横幅"
    return ""
