"""图解零件清单（intelligent UI，2026-10-15 用户定「方案 A」）。

参考 Thesys/OpenUI 的做法：模型不再自由写 HTML，而是从一份**固定的零件清单**里挑零件、
填数据，交回一个 JSON 规格（spec）；程序校验后用**自己的**渲染器画出来。好处：
- 样式统一（全站同一套颜色/字号/深浅色），手机上不会排版崩；
- 没有脚本、没有外部资源——渲染器只输出转义过的文字 + 固定结构，模型写不进任何标签；
- 数字可以逐个对原文（资讯图解），交付报告也能用同一套零件。
**没有「自由 HTML」后门**：零件拼不出来的就不画（资讯回到纯文字）。

同一份清单给三处用：给模型的说明（catalog_prompt）、校验（validate）、渲染（render / page）。
样式只有一份：console/static/viz.css（网页 <link>，单独成页时内嵌）。

两种用途（profile）：
- "news"：资讯图解。零件少、块数少（一眼看完），不许标题/段落/来源链接；
- "report"：任务交付的报告网页（render_report 工具）。可以有小标题、段落、来源链接、图片、更多块。
  图片（image 零件）只在 report 里有：path 指向**本任务成品目录**里已有的图（generate_image 的产物），
  由 render_report 读成 data URI 内联进网页（CSP 仍是 img-src data:，不放宽）。
"""

from __future__ import annotations

import html as _html
import math
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "SpecError",
    "validate",
    "render",
    "page",
    "texts_of",
    "catalog_prompt",
    "CSS",
]

_CSS_PATH = Path(__file__).resolve().parent / "console" / "static" / "viz.css"


def _load_css() -> str:
    try:
        return _CSS_PATH.read_text(encoding="utf-8")
    except OSError:  # 打包缺文件：照样能画，只是素一点
        return ""


CSS = _load_css()


class SpecError(ValueError):
    """规格不合格（中文原因，直接回给模型看）。"""


# ----------------------------------------------------------------------
# 零件清单
# ----------------------------------------------------------------------

# 每个零件：说明（给模型看）、哪些用途能用
CATALOG: dict[str, dict[str, Any]] = {
    "stat": {
        "desc": '大数字：{"type":"stat","value":数字,"unit":"单位(可省)","label":"这是什么","change":"变化(可省,如 +12%)","tone":"good|bad|neutral(可省)","note":"小字补充(可省)"}',
        "profiles": ("news", "report"),
    },
    "stats": {
        "desc": '数字组（2~4 个并排）：{"type":"stats","items":[同 stat 的字段(不写 type)…]}',
        "profiles": ("news", "report"),
    },
    "compare": {
        "desc": '对比条（横条，2~8 项，同一单位）：{"type":"compare","unit":"单位(可省)","items":[{"label":"…","value":数字,"tone":"good|bad|mute(可省)"}…]}',
        "profiles": ("news", "report"),
    },
    "bars": {
        "desc": '柱状图（竖柱，2~12 项，按顺序，如逐年/逐月）：{"type":"bars","unit":"单位(可省)","items":[{"label":"…","value":数字}…]}',
        "profiles": ("news", "report"),
    },
    "line": {
        "desc": '折线图（3~24 个点，按时间顺序）：{"type":"line","unit":"单位(可省)","points":[{"label":"…","value":数字}…]}',
        "profiles": ("news", "report"),
    },
    "share": {
        "desc": '占比条（2~6 项，非负，各部分加起来是整体）：{"type":"share","unit":"单位(可省,如 %)","items":[{"label":"…","value":数字}…]}',
        "profiles": ("news", "report"),
    },
    "timeline": {
        "desc": '时间线（2~8 个节点）：{"type":"timeline","items":[{"when":"时间","text":"发生了什么"}…]}',
        "profiles": ("news", "report"),
    },
    "table": {
        "desc": '对比表（2~6 列、1~12 行，单元格是短文字或数字）：{"type":"table","columns":["…"…],"rows":[["…",数字…]…]}',
        "profiles": ("news", "report"),
    },
    "list": {
        "desc": '要点列表（2~8 条）：{"type":"list","ordered":false,"items":["…"…]}',
        "profiles": ("news", "report"),
    },
    "takeaway": {
        "desc": '结论句（一句话）：{"type":"takeaway","text":"…"}',
        "profiles": ("news", "report"),
    },
    "tabs": {
        "desc": '标签页（2~4 页；只在一屏放不下、分开看更清楚时用；第一页要能单独看懂重点）：{"type":"tabs","tabs":[{"label":"…","blocks":[零件…(不能再套 tabs)]}…]}',
        "profiles": ("news", "report"),
    },
    "heading": {"desc": '小标题：{"type":"heading","text":"…"}', "profiles": ("report",)},
    "text": {"desc": '一段正文：{"type":"text","text":"…"}', "profiles": ("report",)},
    "note": {"desc": '提醒/风险/不确定之处：{"type":"note","text":"…"}', "profiles": ("report",)},
    "image": {
        "desc": (
            '图片（**只许引用这次任务产物目录里已经有的图**，比如 generate_image 画出来的）：'
            '{"type":"image","path":"images/hero.png","alt":"图说（给看不到图的人，必填）",'
            '"caption":"图下小字(可省)"}。path 写**相对本任务成品目录**的路径'
            "（如 images/hero.png；照抄生成图片时返回的 artifacts/<任务>/images/hero.png 也行）。"
            "图会读进网页里（不引外部地址、单个 html 也不会裂图）；"
            "只认 PNG / JPEG / WebP / GIF，单张 ≤3MB、整页图加起来 ≤3MB（网页要落成一个文件，"
            "还有 5MB 上限），超了换小图或把图拆到多个报告页。"
        ),
        "profiles": ("report",),
    },
    "sources": {
        "desc": '来源（链接只许 http/https）：{"type":"sources","items":[{"title":"…","url":"https://…"}…]}',
        "profiles": ("report",),
    },
}

LIMITS = {
    "news": {"blocks": 4, "text": 160, "label": 40, "title": 40, "tabs_total": 1},
    "report": {"blocks": 80, "text": 1500, "label": 80, "title": 80, "tabs_total": 12},
}
_TONES = {"good", "bad", "neutral", "mute", ""}


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------


def _txt(v: Any, where: str, cap: int, *, required: bool = True) -> str:
    if v is None or v == "":
        if required:
            raise SpecError(f"{where} 不能为空")
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise SpecError(f"{where} 要是文字")
    s = str(v).strip()
    if required and not s:
        raise SpecError(f"{where} 不能为空")
    if len(s) > cap:
        raise SpecError(f"{where} 太长了（最多 {cap} 字）")
    return s


def _num(v: Any, where: str) -> float:
    if isinstance(v, bool):
        raise SpecError(f"{where} 要是数字")
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip().replace(",", ""))
        except ValueError:
            raise SpecError(f"{where} 要是数字（不要带单位，单位写在 unit 里）") from None
    else:
        raise SpecError(f"{where} 要是数字")
    if not math.isfinite(f):
        raise SpecError(f"{where} 要是正常的数字")
    return f


def _items(v: Any, where: str, lo: int, hi: int) -> list:
    if not isinstance(v, list):
        raise SpecError(f"{where} 要是列表")
    if not lo <= len(v) <= hi:
        raise SpecError(f"{where} 要有 {lo}~{hi} 项（现在 {len(v)} 项）")
    return v


def _tone(v: Any, where: str) -> str:
    t = str(v or "").strip()
    if t not in _TONES:
        raise SpecError(f"{where} 只能是 good / bad / neutral / mute")
    return t


# image 零件认的图片后缀（真伪按内容在 render_report 那层判，这里先挡明显的笔误）
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_IMAGE_PATH_MAX = 200


def _image_path(v: Any, where: str) -> str:
    """image 零件的 path：只能是**本任务成品目录里的相对路径**（结构性校验，不碰磁盘）。

    - 绝对路径 / `..` / 反斜杠一律拒（这里只放行干净的相对路径）；
    - 后缀必须是图片（.png/.jpg/.jpeg/.webp/.gif）；
    - 写没写 `artifacts/<任务>/` 前缀都行（generate_image 返回的就是带前缀的），
      真正「在不在本任务目录里」由 render_report 读文件时判。
    """
    s = _txt(v, where, _IMAGE_PATH_MAX)
    if s.startswith(("/", "\\")) or (len(s) > 1 and s[1] == ":"):
        raise SpecError(f"{where} 只能是**相对路径**（相对本任务成品目录，如 images/hero.png）：{s}")
    if "\\" in s:
        raise SpecError(f"{where} 只能是**相对路径**（用 / 分隔，如 images/hero.png）：{s}")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise SpecError(f"{where} 只能是**相对路径**（不能有 ..）：{s}")
    if not s.lower().endswith(_IMAGE_SUFFIXES):
        raise SpecError(
            f"{where} 要是图片文件（只认 .png / .jpg / .jpeg / .webp / .gif）：{s}"
        )
    return s


def _stat(d: Any, where: str, lim: dict) -> dict:
    if not isinstance(d, dict):
        raise SpecError(f"{where} 要是对象")
    return {
        "value": _num(d.get("value"), f"{where}.value"),
        "unit": _txt(d.get("unit"), f"{where}.unit", 12, required=False),
        "label": _txt(d.get("label"), f"{where}.label", lim["label"]),
        "change": _txt(d.get("change"), f"{where}.change", 24, required=False),
        "tone": _tone(d.get("tone"), f"{where}.tone"),
        "note": _txt(d.get("note"), f"{where}.note", lim["label"], required=False),
    }


def _labeled(d: Any, where: str, lim: dict, *, tone: bool = False, nonneg: bool = False) -> dict:
    if not isinstance(d, dict):
        raise SpecError(f"{where} 要是对象")
    out = {"label": _txt(d.get("label"), f"{where}.label", lim["label"]), "value": _num(d.get("value"), f"{where}.value")}
    if nonneg and out["value"] < 0:
        raise SpecError(f"{where}.value 不能是负数")
    if tone:
        out["tone"] = _tone(d.get("tone"), f"{where}.tone")
    return out


def _block(b: Any, where: str, profile: str, lim: dict, counter: dict, *, nested: bool) -> dict:
    if not isinstance(b, dict):
        raise SpecError(f"{where} 要是对象")
    t = str(b.get("type") or "").strip()
    if t not in CATALOG:
        raise SpecError(f"{where}.type「{t}」不在零件清单里（可用：{'、'.join(k for k, v in CATALOG.items() if profile in v['profiles'])}）")
    if profile not in CATALOG[t]["profiles"]:
        raise SpecError(f"{where}.type「{t}」这里不能用")
    unit = _txt(b.get("unit"), f"{where}.unit", 12, required=False)
    if t == "stat":
        return {"type": t, **_stat(b, where, lim)}
    if t == "stats":
        return {"type": t, "items": [_stat(x, f"{where}.items[{i}]", lim) for i, x in enumerate(_items(b.get("items"), f"{where}.items", 2, 4))]}
    if t == "compare":
        return {"type": t, "unit": unit, "items": [_labeled(x, f"{where}.items[{i}]", lim, tone=True) for i, x in enumerate(_items(b.get("items"), f"{where}.items", 2, 8))]}
    if t == "bars":
        return {"type": t, "unit": unit, "items": [_labeled(x, f"{where}.items[{i}]", lim) for i, x in enumerate(_items(b.get("items"), f"{where}.items", 2, 12))]}
    if t == "line":
        return {"type": t, "unit": unit, "points": [_labeled(x, f"{where}.points[{i}]", lim) for i, x in enumerate(_items(b.get("points"), f"{where}.points", 3, 24))]}
    if t == "share":
        items = [_labeled(x, f"{where}.items[{i}]", lim, nonneg=True) for i, x in enumerate(_items(b.get("items"), f"{where}.items", 2, 6))]
        if sum(x["value"] for x in items) <= 0:
            raise SpecError(f"{where}.items 加起来要大于 0")
        return {"type": t, "unit": unit, "items": items}
    if t == "timeline":
        out = []
        for i, x in enumerate(_items(b.get("items"), f"{where}.items", 2, 8)):
            if not isinstance(x, dict):
                raise SpecError(f"{where}.items[{i}] 要是对象")
            out.append({"when": _txt(x.get("when"), f"{where}.items[{i}].when", 24), "text": _txt(x.get("text"), f"{where}.items[{i}].text", lim["text"])})
        return {"type": t, "items": out}
    if t == "table":
        cols = [_txt(c, f"{where}.columns[{i}]", lim["label"]) for i, c in enumerate(_items(b.get("columns"), f"{where}.columns", 2, 6))]
        rows = []
        for r, row in enumerate(_items(b.get("rows"), f"{where}.rows", 1, 12 if profile == "news" else 40)):
            if not isinstance(row, list) or len(row) != len(cols):
                raise SpecError(f"{where}.rows[{r}] 要是和表头一样多（{len(cols)} 格）的列表")
            rows.append([_txt(c, f"{where}.rows[{r}][{k}]", lim["label"], required=False) for k, c in enumerate(row)])
        return {"type": t, "columns": cols, "rows": rows}
    if t == "list":
        items = [_txt(x, f"{where}.items[{i}]", lim["text"]) for i, x in enumerate(_items(b.get("items"), f"{where}.items", 2 if profile == "news" else 1, 8 if profile == "news" else 30))]
        return {"type": t, "ordered": bool(b.get("ordered")), "items": items}
    if t in ("takeaway", "heading", "text", "note"):
        cap = lim["title"] if t == "heading" else lim["text"]
        return {"type": t, "text": _txt(b.get("text"), f"{where}.text", cap)}
    if t == "image":
        # 只留认识的字段；src（内联出来的 data URI）由 render_report 读文件后填，
        # 校验这一层不碰磁盘、也不接受模型自己给 src。
        return {
            "type": t,
            "path": _image_path(b.get("path"), f"{where}.path"),
            "alt": _txt(b.get("alt"), f"{where}.alt", lim["text"]),
            "caption": _txt(b.get("caption"), f"{where}.caption", lim["text"], required=False),
        }
    if t == "sources":
        out = []
        for i, x in enumerate(_items(b.get("items"), f"{where}.items", 1, 40)):
            if not isinstance(x, dict):
                raise SpecError(f"{where}.items[{i}] 要是对象")
            url = _txt(x.get("url"), f"{where}.items[{i}].url", 600)
            if not url.lower().startswith(("http://", "https://")) or any(c in url for c in " \"'<>\n\r\t"):
                raise SpecError(f"{where}.items[{i}].url 只能是 http/https 网址")
            out.append({"title": _txt(x.get("title"), f"{where}.items[{i}].title", lim["text"], required=False) or url, "url": url})
        return {"type": t, "items": out}
    if t == "tabs":
        if nested:
            raise SpecError(f"{where}：标签页里不能再套标签页")
        counter["tabs"] += 1
        if counter["tabs"] > lim["tabs_total"]:
            raise SpecError(f"标签页最多用 {lim['tabs_total']} 组")
        tabs = []
        for i, tab in enumerate(_items(b.get("tabs"), f"{where}.tabs", 2, 4)):
            if not isinstance(tab, dict):
                raise SpecError(f"{where}.tabs[{i}] 要是对象")
            blocks = _items(tab.get("blocks"), f"{where}.tabs[{i}].blocks", 1, lim["blocks"])
            tabs.append({
                "label": _txt(tab.get("label"), f"{where}.tabs[{i}].label", 16),
                "blocks": [_block(x, f"{where}.tabs[{i}].blocks[{k}]", profile, lim, counter, nested=True) for k, x in enumerate(blocks)],
            })
        return {"type": t, "tabs": tabs}
    raise SpecError(f"{where}.type「{t}」不支持")  # pragma: no cover


def validate(spec: Any, profile: str = "news") -> dict:
    """校验并规整一份规格；不合格抛 SpecError（中文原因）。返回干净的副本（只留认识的字段）。"""
    if profile not in LIMITS:
        raise SpecError(f"不认识的用途 {profile}")
    lim = LIMITS[profile]
    if not isinstance(spec, dict):
        raise SpecError("规格要是一个 JSON 对象")
    blocks = spec.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise SpecError("blocks 要是至少 1 个零件的列表")
    if len(blocks) > lim["blocks"]:
        raise SpecError(f"零件太多了（最多 {lim['blocks']} 个；一张图只讲一件事）")
    counter = {"tabs": 0}
    out = {
        "title": _txt(spec.get("title"), "title", lim["title"], required=profile == "report"),
        "caption": _txt(spec.get("caption"), "caption", lim["text"], required=False),
        "blocks": [_block(b, f"blocks[{i}]", profile, lim, counter, nested=False) for i, b in enumerate(blocks)],
    }
    return out


def texts_of(spec: dict) -> list[str]:
    """规格里所有会显示出来的文字和数字（给资讯图解逐个对原文用）。"""
    out: list[str] = []

    def num(v: float) -> str:
        return _fmt(v)

    def walk(b: dict) -> None:
        t = b.get("type")
        if t == "stat":
            out.extend([num(b["value"]), b["unit"], b["label"], b["change"], b["note"]])
        elif t == "stats":
            for x in b["items"]:
                walk({"type": "stat", **x})
        elif t in ("compare", "bars", "share"):
            out.append(b.get("unit", ""))
            for x in b["items"]:
                out.extend([x["label"], num(x["value"])])
        elif t == "line":
            out.append(b.get("unit", ""))
            for x in b["points"]:
                out.extend([x["label"], num(x["value"])])
        elif t == "timeline":
            for x in b["items"]:
                out.extend([x["when"], x["text"]])
        elif t == "table":
            out.extend(b["columns"])
            for row in b["rows"]:
                out.extend(row)
        elif t == "list":
            out.extend(b["items"])
        elif t in ("takeaway", "heading", "text", "note"):
            out.append(b["text"])
        elif t == "image":
            out.extend([b.get("alt", ""), b.get("caption", "")])
        elif t == "sources":
            out.extend(x["title"] for x in b["items"])
        elif t == "tabs":
            for tab in b["tabs"]:
                out.append(tab["label"])
                for x in tab["blocks"]:
                    walk(x)

    out.extend([spec.get("title", ""), spec.get("caption", "")])
    for b in spec.get("blocks", []):
        walk(b)
    return [s for s in out if s]


# ----------------------------------------------------------------------
# 渲染（只输出转义过的文字 + 固定结构）
# ----------------------------------------------------------------------

e = _html.escape


def _fmt(v: float) -> str:
    """数字原样写（不加千分位、不四舍五入成原文没有的数）。"""
    if float(v).is_integer():
        return str(int(v))
    return repr(float(v)).rstrip("0").rstrip(".") if "e" not in repr(float(v)) else f"{v:.6g}"


_PREFIX_UNITS = ("$", "¥", "￥", "€", "£", "US$", "HK$")


def _nu(v: float, unit: str) -> str:
    """数字 + 单位（纯文字，已转义）：货币符号放前面，其余放后面。"""
    if unit in _PREFIX_UNITS:
        return f"{e(unit)}{e(_fmt(v))}"
    return f"{e(_fmt(v))}{e(unit)}"


def _tone_cls(t: str) -> str:
    return {"good": " vs-good", "bad": " vs-bad"}.get(t, "")


def _stat_html(s: dict) -> str:
    if s["unit"] in _PREFIX_UNITS:
        val = f"<small>{e(s['unit'])}</small>{e(_fmt(s['value']))}"
    else:
        val = e(_fmt(s["value"])) + (f"<small>{e(s['unit'])}</small>" if s["unit"] else "")
    change = f'<div class="vs-stat-c{_tone_cls(s["tone"]) or (" vs-good" if s["change"].startswith("+") else " vs-bad" if s["change"][:1] in "-−" else "")}">{e(s["change"])}</div>' if s["change"] else ""
    note = f'<div class="vs-stat-n">{e(s["note"])}</div>' if s["note"] else ""
    return (
        f'<div class="vs-stat"><div class="vs-stat-v vs-num{_tone_cls(s["tone"])}">{val}</div>'
        f'<div class="vs-stat-l">{e(s["label"])}</div>{change}{note}</div>'
    )


def _pct(v: float, top: float) -> str:
    return f"{max(0.0, min(100.0, 100.0 * abs(v) / top)) if top else 0:.1f}%"


def _block_html(b: dict, uid: str, idx: list, static: bool = False) -> str:
    t = b["type"]
    u = b.get("unit", "")
    if t == "stat":
        return f'<div class="vs-stats one">{_stat_html(b)}</div>'
    if t == "stats":
        return '<div class="vs-stats">' + "".join(_stat_html(x) for x in b["items"]) + "</div>"
    if t == "compare":
        top = max(abs(x["value"]) for x in b["items"]) or 1.0
        rows = "".join(
            f'<div class="vs-cmp-row"><span class="vs-cmp-l">{e(x["label"])}</span>'
            f'<span class="vs-cmp-t"><i class="vs-cmp-b {e(x["tone"] or ("bad" if x["value"] < 0 else ""))}" style="width:{_pct(x["value"], top)}"></i></span>'
            f'<span class="vs-cmp-v vs-num">{_nu(x["value"], u)}</span></div>'
            for x in b["items"]
        )
        return f'<div class="vs-cmp">{rows}</div>'
    if t == "bars":
        top = max(abs(x["value"]) for x in b["items"]) or 1.0
        cols = "".join(
            f'<div class="vs-bar"><span class="vs-bar-v vs-num">{_nu(x["value"], u)}</span>'
            f'<span class="vs-bar-b{" bad" if x["value"] < 0 else ""}" style="height:{_pct(x["value"], top)}"></span>'
            f'<span class="vs-bar-l">{e(x["label"])}</span></div>'
            for x in b["items"]
        )
        return f'<div class="vs-bars">{cols}</div>'
    if t == "line":
        vals = [x["value"] for x in b["points"]]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        n = len(vals)
        pts = [(100.0 * i / (n - 1), 38.0 - 34.0 * (v - lo) / span) for i, v in enumerate(vals)]
        poly = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        area = f"0,40 {poly} 100,40"
        first, last = b["points"][0], b["points"][-1]
        hi_pt = max(b["points"], key=lambda p: p["value"])
        mid = f'<span>最高 <b class="vs-num">{_nu(hi_pt["value"], u)}</b>（{e(hi_pt["label"])}）</span>' if hi_pt not in (first, last) else ""
        return (
            '<div class="vs-line"><svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true">'
            f'<polygon class="area" points="{area}"></polygon><polyline points="{poly}"></polyline></svg>'
            f'<div class="vs-line-x"><span>{e(first["label"])} <b class="vs-num">{_nu(first["value"], u)}</b></span>{mid}'
            f'<span>{e(last["label"])} <b class="vs-num">{_nu(last["value"], u)}</b></span></div></div>'
        )
    if t == "share":
        total = sum(x["value"] for x in b["items"]) or 1.0
        segs = "".join(f'<i class="vs-c{i % 6}" style="width:{_pct(x["value"], total)}"></i>' for i, x in enumerate(b["items"]) if x["value"] > 0)
        keys = "".join(f'<span class="vs-c{i % 6}">{e(x["label"])} <b class="vs-num">{_nu(x["value"], u)}</b></span>' for i, x in enumerate(b["items"]))
        return f'<div class="vs-share"><div class="vs-share-t">{segs}</div><div class="vs-share-k">{keys}</div></div>'
    if t == "timeline":
        li = "".join(f'<li><div class="vs-tl-w">{e(x["when"])}</div><div>{e(x["text"])}</div></li>' for x in b["items"])
        return f'<ol class="vs-tl">{li}</ol>'
    if t == "table":
        head = "".join(f"<th>{e(c)}</th>" for c in b["columns"])
        body = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in b["rows"])
        return f'<div class="vs-tbl-w"><table class="vs-tbl"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    if t == "list":
        tag = "ol" if b["ordered"] else "ul"
        return f'<{tag} class="vs-list">' + "".join(f"<li>{e(x)}</li>" for x in b["items"]) + f"</{tag}>"
    if t == "takeaway":
        return f'<p class="vs-take">{e(b["text"])}</p>'
    if t == "heading":
        return f'<h2 class="vs-h">{e(b["text"])}</h2>'
    if t == "text":
        return f'<p class="vs-p">{e(b["text"])}</p>'
    if t == "note":
        return f'<p class="vs-note">{e(b["text"])}</p>'
    if t == "image":
        # 图由 render_report 读成 data URI 放进 b["src"]（CSP 是 img-src data:，不放宽）。
        # 没内联成功（没有 src）就不画——宁可不画，也不在交付页里留一张裂图。
        src = str(b.get("src") or "").strip()
        if not src:
            return ""
        cap = f'<p class="vs-cap">{e(b["caption"])}</p>' if b.get("caption") else ""
        return (
            '<figure class="vs-fig"><img class="vs-img"'
            f' src="{e(src, quote=True)}" alt="{e(b["alt"], quote=True)}"'
            ' style="max-width:100%;height:auto;border-radius:10px;display:block">'
            f"{cap}</figure>"
        )
    if t == "sources":
        li = "".join(f'<li><a href="{e(x["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{e(x["title"])}</a></li>' for x in b["items"])
        return f'<ol class="vs-src">{li}</ol>'
    if t == "tabs" and static:
        # 卡片截图（图片没法点）：每页摊开，上面一行页名
        secs = "".join(
            f'<section class="vs-sec"><h4 class="vs-sec-h">{e(tab["label"])}</h4>'
            + "".join(_block_html(x, uid, idx, static) for x in tab["blocks"]) + "</section>"
            for tab in b["tabs"]
        )
        return f'<div class="vs-flat">{secs}</div>'
    if t == "tabs":
        idx[0] += 1
        name = f"{uid}-t{idx[0]}"
        inputs = "".join(
            f'<input type="radio" name="{e(name)}" id="{e(name)}-{i}" aria-label="{e(tab["label"])}"{" checked" if i == 0 else ""}>'
            for i, tab in enumerate(b["tabs"])
        )
        labels = "".join(f'<label for="{e(name)}-{i}">{e(tab["label"])}</label>' for i, tab in enumerate(b["tabs"]))
        panels = "".join(
            '<div class="vs-panel">' + "".join(_block_html(x, uid, idx) for x in tab["blocks"]) + "</div>" for tab in b["tabs"]
        )
        return f'<div class="vs-tabs">{inputs}<div class="vs-tabbar">{labels}</div><div class="vs-panels">{panels}</div></div>'
    return ""  # pragma: no cover


def render(spec: dict, uid: str = "vs", *, static: bool = False) -> str:
    """规整过的规格 → HTML 片段（<div class="vs">…</div>）。uid 用来给标签页起不重复的名字。

    static=True：平铺版（群资讯卡片截图用）——标签页全部摊开，外层多一个 vs-static 类。
    """
    safe_uid = "".join(c for c in str(uid) if c.isalnum() or c in "-_")[:40] or "vs"
    idx = [0]
    head = f'<h3 class="vs-title">{e(spec["title"])}</h3>' if spec.get("title") else ""
    cap = f'<p class="vs-cap">{e(spec["caption"])}</p>' if spec.get("caption") else ""
    body = "".join(_block_html(b, safe_uid, idx, static) for b in spec["blocks"])
    return f'<div class="vs{" vs-static" if static else ""}">{head}{cap}{body}</div>'


# 单独成页（卡片截图 / 交付网页）：补上站点同名颜色变量 + 深色
_PAGE_TOKENS = """
:root{--ink:#111114;--ink-2:#3a3a3c;--gray:#8a8a8e;--line:#ececf0;--fill:#f2f2f5;--fill-2:#f7f7f9;
--blue:#2f6fe8;--green:#34c759;--orange:#ff9f0a;--red:#ff3b30;--bg:#fff;color-scheme:light dark}
@media (prefers-color-scheme: dark){:root{--ink:#f2f2f5;--ink-2:#d1d1d6;--gray:#98989f;--line:#2a2a2e;
--fill:#242427;--fill-2:#1a1a1d;--blue:#5b8cff;--green:#30d158;--orange:#ff9f0a;--red:#ff453a;--bg:#0e0e10}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased}
"""
_REPORT_CSS = """
.rp{max-width:760px;margin:0 auto;padding:28px 18px 48px;display:grid;gap:16px}
.rp-h{margin:0;font-size:26px;line-height:1.25;letter-spacing:-0.02em}
.rp-sub{margin:-6px 0 0;color:var(--gray)}
.rp-foot{color:var(--gray);font-size:12px;text-align:center;margin-top:8px}
.rp .vs{background:transparent;border:0;padding:0}
"""
# 平铺版（卡片截图）：图片没法点也没法滚，所以什么都不许藏——
# 标签页摊开、柱子标签和表格折行（不出省略号、不横向滚动）
STATIC_MARK = '<meta name="maiwork-viz" content="static">'
_STATIC_CSS = """
.vs-flat{display:grid;gap:14px}
.vs-sec{display:grid;gap:10px}
.vs-sec + .vs-sec{padding-top:12px;border-top:1px solid var(--vs-line, var(--line))}
.vs-sec-h{margin:0;font-size:13px;font-weight:650;color:var(--vs-muted, var(--gray))}
.vs-static .vs-bar-l{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.25;word-break:break-all}
.vs-static .vs-bar-v{white-space:normal}
.vs-static .vs-tbl-w{overflow:visible}
.vs-static .vs-tbl{width:100%;table-layout:fixed}
.vs-static .vs-tbl th,.vs-static .vs-tbl td{white-space:normal;overflow-wrap:anywhere;word-break:break-word}
"""
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'"


def page(spec: dict, *, uid: str = "vs", report: bool = False, foot: str = "", static: bool = False) -> str:
    """规格 → 一整页 HTML（不带脚本；CSP 禁一切外部资源）。

    - report=False：资讯图解页（卡片截图、网页 iframe 回落用），只有那块图；
    - report=True：交付报告页，大标题 + 零件 + 页脚。
    """
    frag = render(spec, uid, static=static)
    if report:
        title = e(spec.get("title") or "报告")
        sub = f'<p class="rp-sub">{e(spec["caption"])}</p>' if spec.get("caption") else ""
        inner = render({**spec, "title": "", "caption": ""}, uid)
        body = f'<main class="rp"><h1 class="rp-h">{title}</h1>{sub}{inner}' + (f'<p class="rp-foot">{e(foot)}</p>' if foot else "") + "</main>"
    else:
        title = e(spec.get("title") or "图解")
        body = frag
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{PAGE_CSP}\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        + (STATIC_MARK if static else "")
        + f"<title>{title}</title><style>{_PAGE_TOKENS}{CSS}{_REPORT_CSS if report else ''}{_STATIC_CSS if static else ''}</style></head>"
        f"<body>{body}</body></html>"
    )


# ----------------------------------------------------------------------
# 给模型的说明
# ----------------------------------------------------------------------


def catalog_prompt(profile: str = "news") -> str:
    lim = LIMITS[profile]
    lines = [
        "交一个 JSON 规格：{\"title\":\"标题\",\"caption\":\"一行说明(可省)\",\"blocks\":[零件…]}。",
        "只能用下面这些零件（程序按统一样式画，颜色/字号/深浅色都不用你管；不能写 HTML）：",
    ]
    for k, v in CATALOG.items():
        if profile in v["profiles"]:
            lines.append(f"- {k}：{v['desc']}")
    lines.append(
        f"限制：最多 {lim['blocks']} 个零件；数字字段只写数字（单位放 unit）；文字最长 {lim['text']} 字、标签最长 {lim['label']} 字。"
    )
    # 和 _block 的校验对齐（2026-10-15 巡检 M8：清单里写的是资讯口径，报告页其实宽得多）
    lines.append("标签页的页签名最长 16 字；时间线的时间最长 24 字；单位最长 12 字。")
    if profile == "report":
        lines.append(
            f"报告页比清单里写的宽：表格最多 40 行、要点 1~30 条、小标题最长 {lim['title']} 字、"
            f"来源最多 40 条、标签页最多 {lim['tabs_total']} 组。"
        )
    if profile == "news":
        lines.append("这张图也会画进发到群里的资讯卡片（一张图片，没法点）：那里标签页会全部摊开从上往下排，所以每页都要精简，别为了塞东西才用标签页。")
    return "\n".join(lines)
