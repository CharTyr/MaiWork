"""deliverable_check.py：最终成品文本的纯文本扫描（线上 T-11）。

验收模型看不出两种「成品本身有问题」的情况，代码再扫一遍**交给群友看的最终成品文本**：

- **留空标记**：成品里只写「这块没去做」的说法——`不另扩搜` / `未覆盖该范围` / `此处不写` /
  `待补充` / `另开一次调研` 等（`DELIVERABLE_MARKERS`，比验收证据用的
  `requirements.PLACEHOLDER_MARKERS` 窄：单独的「未覆盖 / 未收录 / 占位 / 不展开」在正文里
  多半是正常内容，不拦）。「查过之后如实写清查过哪里、没查到」是合法的，不算留空。
- **内部用语**：`job1` / `job 2`（1–2 位数字）、`research.md`、`artifacts/T-数字`、`steps/<数字>/`、
  **本任务的任务号**（按传入的 `task_id` 精确匹配）。「主模型 / 子 agent」在 AI 资讯里是正常
  用词，只写进提示词，不在这道硬闸里拦。

只有纯函数和只读文件：不调模型、不碰宿主、不发消息。读文件限 `MAX_BYTES`（默认 200KB）。
`.md` / `.txt` 等文本直接解码；`.html` 去标签（script / style 内容不算成品文字）；
`.docx` 读 `word/document.xml`（zipfile + 去标签，**不新增 python-docx 依赖**）。
"""

from __future__ import annotations

import html as _html
import io
import re
import zipfile
from pathlib import Path
from typing import Any


MAX_BYTES = 200_000   # 成品文本最多读这么多字节（超出部分不扫）
DOCX_MAX_BYTES = 4_000_000  # .docx 是压缩包，截断就读不开；整体读取也留一个硬上限
DOCX_XML_MAX_BYTES = 2_000_000  # 解压 word/document.xml 时最多读这么多字节
MAX_HITS = 5          # 每一类最多返回几条
HIT_LEN = 80          # 每条片段截到多少字
SIDE = 24             # 命中处两边各带上这么多字，便于返工意见里看清楚

_HTML_STRIP = (
    (re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>"), " "),
    (re.compile(r"(?s)<[^>]+>"), " "),
)
# 成品闸是硬闸（命中就改判不通过、返工），只收「明摆着是内部说法」的写法；
# 「主模型 / 子 agent / artifacts/」在 AI 资讯、CI 文档里是正常用词，只写进提示词，不硬拦。
_INTERNAL_FIXED: tuple[tuple[str, str], ...] = (
    ("job 编号", r"(?<![A-Za-z])job\s?\d{1,2}(?!\d)"),
    ("内部文件名", r"research\.md"),
    ("工作区路径", r"artifacts/T-\d+"),
    ("步骤目录", r"steps/\d+/"),
)
# 成品里「这块没去做」的写法（比 requirements.PLACEHOLDER_MARKERS 窄：那份查的是验收证据，
# 这份查的是给群友看的正文——「未覆盖 / 未收录 / 占位 / 不展开」单独出现在正文里多半是正常内容）
DELIVERABLE_MARKERS = (
    "不另扩搜", "不扩搜", "未覆盖该范围", "未覆盖此范围", "本次未覆盖", "此处不写",
    "另开一次调研", "需另开调研", "需另开一次", "留待后续调研", "待补充",
)


def _fold(text: Any) -> str:
    """折叠空白：片段和匹配都在压平后的文本上做，日志/意见里不会带换行。"""
    return re.sub(r"\s+", " ", str(text or ""))


def _snippet(folded: str, start: int, end: int) -> str:
    piece = folded[max(0, start - SIDE): min(len(folded), end + SIDE)].strip()
    if len(piece) > HIT_LEN:
        piece = piece[:HIT_LEN]
    return piece


def _collect(folded: str, pattern: re.Pattern[str], *, cap: int = MAX_HITS) -> list[str]:
    out: list[str] = []
    for m in pattern.finditer(folded):
        piece = _snippet(folded, m.start(), m.end())
        if piece and piece not in out:
            out.append(piece)
        if len(out) >= cap:
            break
    return out


def _internal_patterns(task_id: Any = "") -> list[tuple[str, re.Pattern[str]]]:
    patterns = [(why, re.compile(pat)) for why, pat in _INTERNAL_FIXED]
    tid = str(task_id or "").strip()
    if tid:
        # 只认这一个任务号，且后面不许再跟数字（T-11 不匹到 T-110）
        patterns.append(("本任务任务号", re.compile(re.escape(tid) + r"(?!\d)")))
    return patterns


def scan_text(text: Any, *, task_id: Any = "") -> dict:
    """扫一段成品文本，返回 `{"placeholder": [片段…], "internal": [片段…]}`。

    去重、每类最多 `MAX_HITS` 条、每条截到 `HIT_LEN` 字。任何输入都不抛。
    """
    folded = _fold(text)
    placeholder: list[str] = []
    seen_ranges: list[tuple[int, int]] = []
    if folded:
        for marker in DELIVERABLE_MARKERS:
            idx = folded.find(marker)
            if idx < 0:
                continue
            end = idx + len(marker)
            # 标记词互相嵌套（「需另开」⊂「需另开一次」）：同一处只报一条，返工意见别啰嗦
            if any(s <= idx < e or s < end <= e for s, e in seen_ranges):
                continue
            seen_ranges.append((idx, end))
            piece = _snippet(folded, idx, end)
            if piece and piece not in placeholder:
                placeholder.append(piece)
            if len(placeholder) >= MAX_HITS:
                break
    internal: list[str] = []
    if folded:
        for _why, pattern in _internal_patterns(task_id):
            for piece in _collect(folded, pattern):
                if piece not in internal:
                    internal.append(piece)
                if len(internal) >= MAX_HITS:
                    break
            if len(internal) >= MAX_HITS:
                break
    return {"placeholder": placeholder, "internal": internal}


def has_issues(scan: Any) -> bool:
    """扫描结果里有没有命中（留空标记或内部用语）。"""
    if not isinstance(scan, dict):
        return False
    return bool(scan.get("placeholder") or scan.get("internal"))


def _html_text(raw: str) -> str:
    text = raw
    for pattern, repl in _HTML_STRIP:
        text = pattern.sub(repl, text)
    return _html.unescape(text)


def _docx_text(data: bytes) -> str:
    """`.docx` → 正文文字：直接读 `word/document.xml` 去标签（不依赖 python-docx）。

    document.xml 只读到 `DOCX_XML_MAX_BYTES`（成品正文远小于它；压缩包解压可能很大，
    这里给解压留一个硬上限）。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with zf.open("word/document.xml") as fh:
                xml = fh.read(DOCX_XML_MAX_BYTES).decode("utf-8", "replace")
    except (KeyError, ValueError, zipfile.BadZipFile, OSError):
        return ""
    xml = re.sub(r"(?i)</w:p\s*>", "\n", xml)
    xml = re.sub(r"(?s)<[^>]+>", "", xml)
    return _html.unescape(xml)


def text_from_bytes(data: bytes, suffix: Any = "") -> str:
    """按后缀把成品字节转成可扫的文字（纯函数）。"""
    sfx = str(suffix or "").lower()
    if sfx in (".html", ".htm"):
        return _html_text(bytes(data or b"").decode("utf-8", "replace"))
    if sfx == ".docx":
        return _docx_text(bytes(data or b""))
    return bytes(data or b"").decode("utf-8", "replace")


def read_deliverable_text(path: Any, *, max_bytes: int = MAX_BYTES) -> str:
    """读一个成品文件的可读文字（最多 `max_bytes` 个字的成品文本）；读不了由调用方处理异常。

    `.docx` 是压缩包，按字节截断会读不开：整体读到 `DOCX_MAX_BYTES` 上限，再按
    `max_bytes` 截**解出来的正文**。
    """
    p = Path(str(path))
    limit = max(1, int(max_bytes))
    if p.suffix.lower() == ".docx":
        with p.open("rb") as fh:
            data = fh.read(DOCX_MAX_BYTES)
        return text_from_bytes(data, p.suffix)[:limit]
    with p.open("rb") as fh:
        data = fh.read(limit)
    return text_from_bytes(data, p.suffix)[:limit]
