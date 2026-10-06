"""生成 tests/fixtures/qr_known.json（二维码已知答案矩阵）。

**这个脚本不在仓库测试里跑**，也不参与 CI：它需要临时虚拟环境里的第三方库
（`qrcode`、`zxing-cpp`、`Pillow`）和一份上游 Nayuki qrcodegen.py。仓库测试
（tests/test_qr.py）只读生成的 JSON，不依赖任何第三方库。

用法（本机临时环境，2026-10 实测）：

    python3 -m venv /tmp/qrtest
    /tmp/qrtest/bin/pip install qrcode zxing-cpp pillow
    curl -sSLo /tmp/qrcodegen.py \
        https://raw.githubusercontent.com/nayuki/QR-Code-generator/master/python/qrcodegen.py
    cd plugin/CharTyr_MaiWork
    /tmp/qrtest/bin/python tests/fixtures/gen_qr_known.py

每个用例做三件事，缺一不可：
1. 用**上游 Nayuki qrcodegen.py**（未改动）按字节模式、指定纠错等级、不提升纠错，
   自己选版本和 mask —— 这是 mask 的取值来源；
2. 用**另一个独立实现 `qrcode`（PyPI，ISO/IEC 18004）**在同一版本、同一 mask、
   强制字节模式下生成矩阵，断言和上游 Nayuki 逐模块一致（不一致直接报错）；
3. 把矩阵画成 PNG 交给 **zxing-cpp**（真解码器）解一遍，断言解回原文。
这样写进 JSON 的矩阵同时被两个编码器和一个独立解码器验证过。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import qrcode
import qrcode.util as qu
import zxingcpp
from PIL import Image

HERE = Path(__file__).resolve().parent
NAYUKI_DIR = Path("/tmp")  # 放上游 qrcodegen.py 的目录
sys.path.insert(0, str(NAYUKI_DIR))
import qrcodegen as qg  # noqa: E402  （上游文件，路径由上面决定）

ENDOM = "https://mw.example.com/#/"

# (名字, 文本, 纠错等级)
CASES = [
    ("ascii_words", "hello world", "M"),
    ("chinese_short", "你好，这是 MaiWork 资讯卡片", "M"),
    ("group_link", ENDOM + "g/900000001", "M"),
    ("news_link", ENDOM + "AbC123xyz/news", "Q"),
    ("news_link_m", ENDOM + "AbC123xyz/news", "M"),
    ("long_link_120", ENDOM + "AbC123xyz/news?g=900000001&t=" + "AbC123xyz" * 8, "M"),
    ("long_link_120_q", ENDOM + "AbC123xyz/news?g=900000001&t=" + "AbC123xyz" * 8, "Q"),
    ("digits_100", "0123456789" * 10, "M"),
    ("chinese_long", "群聊工作台：资讯、构想、目标。" * 2, "M"),
    ("chinese_long_q", "群聊工作台：资讯、构想、目标。" * 2, "Q"),
    ("single_char", "A", "M"),
    ("link_300", ENDOM + "news/" + "aB3" * 90, "M"),
    ("link_1050", ENDOM + "news/" + "aB3xY9" * 170, "M"),
    ("digits_2000", "7" * 2000, "M"),
]

ECC = {"L": qg.QrCode.Ecc.LOW, "M": qg.QrCode.Ecc.MEDIUM,
       "Q": qg.QrCode.Ecc.QUARTILE, "H": qg.QrCode.Ecc.HIGH}
QRCODE_ECC = {"L": qrcode.constants.ERROR_CORRECT_L,
              "M": qrcode.constants.ERROR_CORRECT_M,
              "Q": qrcode.constants.ERROR_CORRECT_Q,
              "H": qrcode.constants.ERROR_CORRECT_H}


def upstream_matrix(data: bytes, ecc: str, mask: int = -1):
    """上游 Nayuki 实现：字节模式、不提升纠错、自动选版本和 mask。"""
    code = qg.QrCode.encode_segments(
        [qg.QrSegment.make_bytes(data)], ECC[ecc], mask=mask, boostecl=False)
    size = code.get_size()
    grid = [[code.get_module(x, y) for x in range(size)] for y in range(size)]
    return grid, code.get_version(), code.get_mask()


def qrcode_matrix(data: bytes, ecc: str, version: int, mask: int):
    """独立实现 `qrcode`：固定版本、固定 mask、强制字节模式。"""
    qr = qrcode.QRCode(version=version, error_correction=QRCODE_ECC[ecc],
                       box_size=1, border=0, mask_pattern=mask)
    qr.add_data(qu.QRData(data, mode=qu.MODE_8BIT_BYTE))
    qr.make(fit=False)
    assert qr.mask_pattern == mask, (qr.mask_pattern, mask)
    return [[bool(v) for v in row] for row in qr.get_matrix()]


def decode(grid) -> str:
    """把矩阵画成 PNG，交给 zxing-cpp 真解码器解回文本。"""
    border, scale = 4, 8
    size = (len(grid) + 2 * border) * scale
    img = Image.new("L", (size, size), 255)
    px = img.load()
    for y, row in enumerate(grid):
        for x, dark in enumerate(row):
            if dark:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(x + border) * scale + dx, (y + border) * scale + dy] = 0
    result = zxingcpp.read_barcode(img)
    assert result is not None, "zxing 解不出来"
    return result.text


def main() -> None:
    cases = []
    for name, text, ecc in CASES:
        data = text.encode("utf-8")
        ref, version, mask = upstream_matrix(data, ecc)
        other = qrcode_matrix(data, ecc, version, mask)
        if ref != other:
            raise SystemExit(
                f"{name}: 上游 Nayuki 与 qrcode 库在 v{version} mask{mask} 下不一致")
        got = decode(ref)
        if got != text:
            raise SystemExit(f"{name}: zxing 解出 {got[:40]!r} != 原文")
        size = len(ref)
        assert size == version * 4 + 17
        cases.append({
            "name": name,
            "ecc": ecc,
            "version": version,
            "mask": mask,
            "bytes": len(data),
            "text": text,
            "text_sha256": hashlib.sha256(data).hexdigest(),
            "rows": ["".join("1" if b else "0" for b in row) for row in ref],
        })
        print(f"{name}: v{version} mask{mask} {size}x{size} {len(data)} 字节"
              f" 两实现一致 + zxing 解回原文")

    payload = {
        "note": "由 tests/fixtures/gen_qr_known.py 生成，勿手改。矩阵经 qrcode(PyPI)、"
                "上游 Nayuki qrcodegen 两个编码器互校，并经 zxing-cpp 解码验证。",
        "cases": cases,
    }
    out = HERE / "qr_known.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print(f"写入 {out}（{out.stat().st_size} 字节，{len(cases)} 个用例）")


if __name__ == "__main__":
    main()
