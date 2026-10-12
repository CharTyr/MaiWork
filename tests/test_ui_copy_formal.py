"""网页文案保持简洁正式（design.md §11，2026-10-12 用户定）。

扫网页源码（js / index.html，去掉注释）里常见的口语说法。只拦明显的口语词，
不替代人工审稿；群里发的话不在网页源码里，不受这条约束。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "maiwork/console/static"
assert STATIC.is_dir(), f"找不到网页静态目录：{STATIC}"

# 口语说法 → 建议的正式说法（只用于报错提示）
BANNED = {
    "存好了": "已保存", "保存好了": "已保存", "换好了": "已切换", "删掉了": "已删除", "改好了": "已修改",
    "装好了": "已安装", "加好了": "已添加", "弄好了": "已完成", "搞定": "已完成",
    "没通": "连接失败", "通了": "连接成功", "真问一次": "实际调用一次", "接上": "添加 / 连接",
    "没连上": "连接失败", "没填": "未填写", "还没有": "暂无", "没人用": "未被使用",
    "读取中": "加载中", "试试": "测试", "不对。": "不正确。", "太多次了": "次数过多",
    "啦": "（删去语气词）", "呀": "（删去语气词）", "哦": "（删去语气词）", "嘛": "（删去语气词）",
    "咱": "（改为具体对象）", "啥": "什么", "咋": "怎么",
    "一下": "（删去）", "去选": "前往设置", "看一眼": "查看",
}


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    # 行注释：// 前面不是 : （避开 https://）
    return re.sub(r"(?<![:\"'`])//[^\n]*", "", text)


def _sources():
    files = sorted((STATIC / "js").rglob("*.js")) + [STATIC / "index.html"]
    assert files, "没找到网页源码"
    return files


def test_no_colloquial_words_in_web_copy():
    hits = []
    for f in _sources():
        lines = _strip_comments(f.read_text(encoding="utf-8")).splitlines()
        for i, line in enumerate(lines, 1):
            line = re.sub(r"「[^」]*」", "「」", line)  # 「」里是引用群友原话的例子，不算界面文案
            for word, better in BANNED.items():
                if word in line:
                    hits.append(f"{f.relative_to(STATIC)}:{i}「{word}」→ {better}：{line.strip()[:80]}")
    assert not hits, "网页文案里还有口语说法：\n" + "\n".join(hits[:60]) + (f"\n…共 {len(hits)} 处" if len(hits) > 60 else "")


def test_page_subtitles_are_not_instructions():
    """h-meta 副标题不再写长句说明（≥ 24 个字的说明请收进 tip）。"""
    long = []
    pat = re.compile(r'class="h-meta"[^>]*>([^<$]{24,})<')
    for f in _sources():
        for m in pat.finditer(_strip_comments(f.read_text(encoding="utf-8"))):
            long.append(f"{f.relative_to(STATIC)}：{m.group(1)[:60]}")
    assert not long, "这些副标题写成了说明，请收进「ⓘ」：\n" + "\n".join(long)
