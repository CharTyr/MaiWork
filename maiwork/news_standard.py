"""资讯标准：读内置 skill `maiwork/builtin_skills/news-standard/`，按环节取对应段落注入提示词。

资讯的「什么收、什么不收、怎么打分、怎么找」只写在那个 skill 里（Agent Skills 格式：
SKILL.md + references/）。资讯流水线的四个环节是固定的，所以不靠模型自己 read_skill，
而是程序按环节把对应的那份交给模型（渐进披露由程序做）：

- 定关注点（主模型）      ← finding.md「定关注点」「跳一步」
- 找候选（子 agent）      ← criteria.md（资讯 / 文章 / 拓展 / 同一件事）+ finding.md「跳一步」「找候选」
- 打分（主模型）          ← criteria.md 全文 + scoring.md 全文

程序硬判的数字（7 天、180 天、3.8 …）仍是 feeds.py 的常量；skill 文本里写的数字由
tests/test_news_standard.py 对照常量，改一边忘了另一边会红。
skill 同时挂进 skills 列表（内置、只读、roles=worker），任务子 agent 可以 read_skill 读全文；
不给主模型（给了会让排计划回合每次都带 skill 工具、多一轮调用）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("maiwork.news_standard")

SKILL_NAME = "news-standard"
SKILL_DIR = Path(__file__).resolve().parent / "builtin_skills" / SKILL_NAME

_cache: dict[str, str] = {}


def _read(ref: str) -> str:
    """references/<ref>.md 全文（去掉第一行「# 标题」）；读不到记 error 回空串（不拖垮备料）。"""
    if ref in _cache:
        return _cache[ref]
    path = SKILL_DIR / "references" / f"{ref}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.error("资讯标准文件读不到：%s（这轮提示词里缺这一段）", path)
        return ""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    out = "\n".join(lines).strip()
    _cache[ref] = out
    return out


def section(ref: str, heading: str) -> str:
    """references/<ref>.md 里「## heading」这一节（含标题行，到下一个 ## 为止）；没有 → ""。"""
    text = _read(ref)
    m = re.search(rf"^## {re.escape(heading)}\s*$", text, flags=re.M)
    if not m:
        logger.error("资讯标准 %s.md 里没有「%s」这一节", ref, heading)
        return ""
    rest = text[m.start():]
    nxt = re.search(r"^## ", rest[3:], flags=re.M)
    return (rest[: nxt.start() + 3] if nxt else rest).strip()


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


def for_focus() -> str:
    """定关注点用。"""
    return _join(section("finding", "定关注点"), section("finding", "跳一步"))


def for_collect(guides: bool = True) -> str:
    """找候选的子 agent 用；guides=False（[feeds] guides 关）就不给「文章」那节。"""
    return _join(
        section("criteria", "资讯"),
        section("criteria", "文章") if guides else "",
        section("criteria", "拓展（让群友眼前一亮）"),
        section("criteria", "同一件事"),
        section("finding", "跳一步"),
        section("finding", "找候选"),
    )


def for_scoring() -> str:
    """打分用：收录标准 + 打分分档全文。"""
    return _join(_read("criteria"), _read("scoring"))


def clear_cache() -> None:
    _cache.clear()
