"""自带二维码编码器（maiwork/qr.py）——正确性靠「已知答案矩阵」+ 结构检查。

已知答案 `tests/fixtures/qr_known.json` 由 `tests/fixtures/gen_qr_known.py` 在**临时虚拟
环境**里生成（见那个脚本的头注释）：矩阵同时被两个独立编码器（PyPI `qrcode` 和
上游 Nayuki qrcodegen）逐模块核对过，又经 zxing-cpp 真解码器解回原文。仓库测试
本身不依赖任何第三方库，只比矩阵：本机 python 没有 qrcode / segno / zxing，
不许 skip 造成假绿（缺 fixture 直接报错）。

对比的是**整个矩阵**（含格式信息、功能图形、掩码），所以版本、纠错等级、掩码
选择（自动选 mask）、数据摆放、纠错码生成全都在验证范围内；空文本和超长文本
按契约抛 ValueError。
"""

from __future__ import annotations

import base64
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork import qr

SVG_NS = "{http://www.w3.org/2000/svg}"

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "qr_known.json"
if not FIXTURE_PATH.is_file():
    raise RuntimeError(f"缺少已知答案矩阵文件（不许 skip）: {FIXTURE_PATH}")
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]
if not FIXTURES:
    raise RuntimeError(f"已知答案矩阵文件是空的: {FIXTURE_PATH}")


def _expected(case) -> list[list[bool]]:
    return [[ch == "1" for ch in row] for row in case["rows"]]


# ---- 已知答案矩阵（真解码器验证过的） ----


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_matrix_matches_known_answer(case) -> None:
    got = qr.matrix(case["text"], ecc=case["ecc"])
    size = case["version"] * 4 + 17
    assert len(got) == size, f"{case['name']}: 版本应当是 v{case['version']}"
    assert all(len(row) == size for row in got)
    assert all(isinstance(v, bool) for row in got for v in row)
    assert got == _expected(case), f"{case['name']}: 矩阵与已知答案不一致"


RELATIONS = [
    # 同一段文本、换纠错等级 → 换版本/换矩阵
    (FIXTURES[3], FIXTURES[4]),      # news_link(Q) vs news_link_m(M)
    (FIXTURES[5], FIXTURES[6]),      # long_link_120(M) vs _q(Q)
    (FIXTURES[8], FIXTURES[9]),      # chinese_long(M) vs _q(Q)
]


@pytest.mark.parametrize("a,b", RELATIONS)
def test_higher_ecc_needs_more_room(a, b) -> None:
    assert a["text"] == b["text"]
    size_m = len(qr.matrix(a["text"], ecc="M"))
    size_q = len(qr.matrix(a["text"], ecc="Q"))
    assert size_q >= size_m
    assert qr.matrix(a["text"], ecc="M") != qr.matrix(a["text"], ecc="Q")


def test_finder_patterns_and_timing() -> None:
    m = qr.matrix("https://mw.example.com/#/g/900000001")
    size = len(m)
    for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
        assert m[oy + 0][ox + 0] and m[oy + 0][ox + 6]
        assert m[oy + 3][ox + 3]  # 中心实心 3x3
        assert not m[oy + 5][ox + 5]  # 内圈留白
        assert m[oy + 6][ox + 6]
    # 定位图形右侧/下方的分隔带必须是浅色
    for i in range(8):
        assert not m[7][i] and not m[i][7]
        assert not m[7][size - 1 - i] and not m[i][size - 8]
        assert not m[size - 8][i] and not m[size - 1 - i][7]
    # 定时图形（第 6 行/列）黑白相间
    for i in range(8, size - 8):
        assert m[6][i] == (i % 2 == 0)
        assert m[i][6] == (i % 2 == 0)
    # 右下角固定深色模块
    assert m[size - 8][8]


# ---- 契约：空 / 超长 ----


@pytest.mark.parametrize("fn", [qr.matrix, qr.svg_data_uri])
def test_empty_text_raises(fn) -> None:
    with pytest.raises(ValueError):
        fn("")


@pytest.mark.parametrize("fn", [qr.matrix, qr.svg_data_uri])
def test_too_long_text_raises(fn) -> None:
    with pytest.raises(ValueError):
        fn("x" * 2001)
    with pytest.raises(ValueError):
        fn("中" * 2001)


def test_two_thousand_bytes_is_allowed_but_bigger_is_not() -> None:
    assert len(qr.matrix("7" * 2000)) == 169  # v38，边界之内
    with pytest.raises(ValueError):
        qr.matrix("7" * 2000 + "8")


def test_bad_ecc_raises() -> None:
    with pytest.raises(ValueError):
        qr.matrix("hello", ecc="Z")


# ---- SVG data URI ----


def _svg_text(uri: str) -> str:
    assert uri.startswith("data:image/svg+xml;base64,"), uri[:40]
    raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
    return raw.decode("utf-8")


def _decode_svg(uri: str) -> ET.Element:
    return ET.fromstring(_svg_text(uri))


def _path_modules(elem: ET.Element) -> list[tuple[int, int]]:
    """把 <path> 里「M x y h宽 v1 h-宽 z」的横条展开成一个个模块坐标。"""
    out: list[tuple[int, int]] = []
    for x, y, width in re.findall(r"M(\d+) (\d+)h(\d+)v1h-\d+z", elem.attrib["d"]):
        out.extend((int(x) + i, int(y)) for i in range(int(width)))
    return out


def test_svg_data_uri_shape() -> None:
    text = "https://mw.example.com/#/AbC123xyz/news"
    uri = qr.svg_data_uri(text)
    root = _decode_svg(uri)
    assert root.tag == SVG_NS + "svg"
    m = qr.matrix(text)
    size = len(m) + 4  # border 默认 2
    assert root.attrib["viewBox"] == f"0 0 {size} {size}"
    assert root.attrib["width"] == str(size) and root.attrib["height"] == str(size)
    assert root.attrib["shape-rendering"] == "crispEdges"
    # ElementTree 会把 xmlns 折进命名空间，所以查原文
    assert 'xmlns="http://www.w3.org/2000/svg"' in _svg_text(uri)

    paths = [e for e in root if e.tag == SVG_NS + "path"]
    rects = [e for e in root if e.tag == SVG_NS + "rect"]
    assert len(paths) == 1, "只该有一个 <path>"
    assert len(rects) == 1, "浅色底该用一个 <rect>"
    assert rects[0].attrib["fill"] == "#ffffff"
    assert paths[0].attrib["fill"] == "#1d1d1f"

    # path 里的方块必须和矩阵里的深色模块完全一一对应（含 2 模块静区偏移）
    dark = {(x + 2, y + 2) for y, row in enumerate(m) for x, v in enumerate(row) if v}
    drawn = _path_modules(paths[0])
    assert len(drawn) == len(set(drawn)) == len(dark)
    assert set(drawn) == dark


def test_svg_border_and_colors() -> None:
    text = "hello world"
    uri = qr.svg_data_uri(text, border=0, dark="#000000", light="#ffeecc")
    root = _decode_svg(uri)
    m = qr.matrix(text)
    size = len(m)
    assert root.attrib["viewBox"] == f"0 0 {size} {size}"
    dark = {(x, y) for y, row in enumerate(m) for x, v in enumerate(row) if v}
    assert set(_path_modules(root.find(SVG_NS + "path"))) == dark
    assert root.find(SVG_NS + "path").attrib["fill"] == "#000000"
    assert root.find(SVG_NS + "rect").attrib["fill"] == "#ffeecc"


def test_svg_respects_ecc_and_border_3() -> None:
    text = "你好，这是 MaiWork 资讯卡片"
    uri = qr.svg_data_uri(text, ecc="Q", border=3)
    root = _decode_svg(uri)
    m = qr.matrix(text, ecc="Q")
    size = len(m) + 6
    assert root.attrib["viewBox"] == f"0 0 {size} {size}"
    dark = {(x + 3, y + 3) for y, row in enumerate(m) for x, v in enumerate(row) if v}
    assert set(_path_modules(root.find(SVG_NS + "path"))) == dark


def test_svg_is_ascii_and_reasonably_small() -> None:
    uri = qr.svg_data_uri("https://mw.example.com/#/AbC123xyz/news")
    assert uri.isascii()
    assert len(uri) < 6000  # 39 字节链接（默认 M 等级 v3，33x33），base64 后不该失控
