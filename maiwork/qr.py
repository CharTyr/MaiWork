#
# MaiWork 自带的二维码编码器（纯 Python，无第三方依赖）。
#
# 移植自 Project Nayuki 的 QR Code generator library（Python 版 qrcodegen），MIT 许可：
#   介绍页 https://www.nayuki.io/page/qr-code-generator-library
#   源码   https://github.com/nayuki/QR-Code-generator/blob/master/python/qrcodegen.py
#
# Copyright (c) Project Nayuki. (MIT License)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
# - The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
# - The Software is provided "as is", without warranty of any kind, express or
#   implied, including but not limited to the warranties of merchantability,
#   fitness for a particular purpose and noninfringement. In no event shall the
#   authors or copyright holders be liable for any claim, damages or other
#   liability, whether in an action of contract, tort or otherwise, arising from,
#   out of or in connection with the Software or the use or other dealings in the
#   Software.
#
# 本文件的改动（MaiWork，2026-10）：
# - 只保留字节模式（byte mode，UTF-8）：删掉数字 / 字母数字 / 汉字 / ECI 分段模式，
#   以及 encode_text / encode_binary / encode_segments / QrSegment 等入口；
# - 固定用户要的纠错等级，不做「不涨版本就自动升级纠错」（原实现 boostecl=True）；
# - 新增对外两个函数 matrix() / svg_data_uri()，输入上限 2000 字节；
# - 类 QrCode 改名 _QrCode 并去掉公开访问器，其余算法（纠错码、掩码选择、惩罚分、
#   矩阵摆放、格式/版本信息、对齐图形位置）与原实现逐行一致。
#
# 为什么自带：线上服务器（MaiBot 的 Python 环境）没有 qrcode / segno 这类库，
# 也不允许往宿主环境装包。资讯卡片要在图里放一个扫码打开的 MaiWork 网页链接
# （https，一般不到 120 字符），于是自带一个只依赖标准库的编码器。
#
"""纯 Python 二维码编码（QR Code Model 2，ISO/IEC 18004）。

只对外提供两个函数：

    matrix(text, ecc="M") -> list[list[bool]]
    svg_data_uri(text, ecc="M", border=2, dark=..., light=...) -> str

只走字节模式（UTF-8），版本和掩码都自动选。
"""

from __future__ import annotations

import base64
import collections
import itertools
from collections.abc import Callable, Sequence
from typing import Union

__all__ = ["matrix", "svg_data_uri"]

# 输入上限：超过就抛 ValueError。二维码本身在 M 等级能装 2331 字节，但 2000 字节的
# 符号已经是 177x177（v40 附近）的怪物，画进卡片再扫基本没意义；MaiWork 只拿它放
# 群网页链接（不到 120 字符）。
MAX_BYTES = 2000

_MODE_BYTE = 0x4  # 字节模式指示符
_BYTE_CHAR_COUNT_BITS = (8, 16, 16)  # 三个版本区间的字符计数字段宽度


def matrix(text: str, *, ecc: str = "M") -> list[list[bool]]:
    """返回二维码模块矩阵（True=黑），不含静区。

    text 为空、不是字符串、或超过 2000 字节（UTF-8）时抛 ValueError；
    ecc 只认 L / M / Q / H（默认 M），版本和掩码自动选。
    """
    return _grid(_encode(_to_bytes(text), _ecc_level(ecc)))


def svg_data_uri(text: str, *, ecc: str = "M", border: int = 2, dark: str = "#1d1d1f",
                 light: str = "#ffffff") -> str:
    """返回 `data:image/svg+xml;base64,...` 的 SVG，可直接放进 HTML 的 <img src>。

    图形是一个浅色底 <rect> 加一个深色 <path>（shape-rendering="crispEdges"，
    坐标按模块走，不糊边）；border 是静区模块数（默认 2），viewBox 按
    「模块数 + 2*border」算。参数校验同 matrix()。
    """
    if not isinstance(border, int) or isinstance(border, bool) or border < 0:
        raise ValueError(f"border 必须是非负整数，收到 {border!r}")
    code = _encode(_to_bytes(text), _ecc_level(ecc))
    size = code.get_size()
    side = size + 2 * border
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}" '
        f'viewBox="0 0 {side} {side}" shape-rendering="crispEdges">',
        f'<rect width="{side}" height="{side}" fill="{light}"/>',
        f'<path fill="{dark}" d="{_path_data(code, border)}"/>',
        "</svg>",
    ]
    payload = "".join(parts).encode("utf-8")
    return "data:image/svg+xml;base64," + base64.b64encode(payload).decode("ascii")


# ---- 输入与输出 ----


def _to_bytes(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError(f"text 必须是字符串，收到 {type(text).__name__}")
    data = text.encode("utf-8")
    if not data:
        raise ValueError("text 不能为空")
    if len(data) > MAX_BYTES:
        raise ValueError(f"text 太长（{len(data)} 字节，上限 {MAX_BYTES} 字节）")
    return data


def _ecc_level(ecc: str):
    if isinstance(ecc, str) and ecc.upper() in _ECC_BY_NAME:
        return _ECC_BY_NAME[ecc.upper()]
    raise ValueError(f"ecc 只认 L / M / Q / H，收到 {ecc!r}")


def _grid(code: "_QrCode") -> list[list[bool]]:
    size = code.get_size()
    return [[code.get_module(x, y) for x in range(size)] for y in range(size)]


def _path_data(code: "_QrCode", border: int) -> str:
    """把每行连续的深色模块合成一条「M x y h宽 v1 h-宽 z」子路径（省体积，好解析）。"""
    strips: list[str] = []
    for y in range(code.get_size()):
        x = 0
        while x < code.get_size():
            if not code.get_module(x, y):
                x += 1
                continue
            run = 1
            while x + run < code.get_size() and code.get_module(x + run, y):
                run += 1
            strips.append(f"M{x + border} {y + border}h{run}v1h-{run}z")
            x += run
    return "".join(strips)


# ---- 字节模式编码 ----


def _encode(data: bytes, ecl: "_Ecc", mask: int = -1) -> "_QrCode":
    """选最小版本 → 拼数据位 → 补终止符/填充 → 交给 _QrCode 画矩阵并选掩码。"""
    version = 0
    datacapacitybits = 0
    ccbits = 0
    for ver in range(_QrCode.MIN_VERSION, _QrCode.MAX_VERSION + 1):
        ccbits = _BYTE_CHAR_COUNT_BITS[(ver + 7) // 17]
        if len(data) >= (1 << ccbits):  # 字符数放不进计数字段，换更大版本
            continue
        capbits = _QrCode._get_num_data_codewords(ver, ecl) * 8
        if 4 + ccbits + len(data) * 8 <= capbits:
            version, datacapacitybits = ver, capbits
            break
    if version == 0:
        raise ValueError(
            f"数据太长：{len(data)} 字节放不进纠错等级 {ecl.name} 的任何一个版本")

    bb = _BitBuffer()
    bb.append_bits(_MODE_BYTE, 4)
    bb.append_bits(len(data), ccbits)
    for b in data:
        bb.append_bits(b, 8)

    # 终止符、补齐到字节边界、再用 0xEC / 0x11 交替填满数据容量
    bb.append_bits(0, min(4, datacapacitybits - len(bb)))
    bb.append_bits(0, -len(bb) % 8)
    for padbyte in itertools.cycle((0xEC, 0x11)):
        if len(bb) >= datacapacitybits:
            break
        bb.append_bits(padbyte, 8)

    # 位流按大端装字节
    datacodewords = bytearray([0] * (len(bb) // 8))
    for (i, bit) in enumerate(bb):
        datacodewords[i >> 3] |= bit << (7 - (i & 7))
    return _QrCode(version, ecl, datacodewords, mask)


# ---- 二维码符号 ----


class _QrCode:
    """一个二维码符号：不可变的模块矩阵。构造时就算纠错码、画功能图形、选掩码。"""

    MIN_VERSION: int = 1
    MAX_VERSION: int = 40

    _PENALTY_N1: int = 3
    _PENALTY_N2: int = 3
    _PENALTY_N3: int = 40
    _PENALTY_N4: int = 10

    def __init__(self, version: int, errcorlvl: "_Ecc",
                 datacodewords: Union[bytes, Sequence[int]], msk: int) -> None:
        if not (self.MIN_VERSION <= version <= self.MAX_VERSION):
            raise ValueError("Version value out of range")
        if not (-1 <= msk <= 7):
            raise ValueError("Mask value out of range")

        self._version = version
        self._size = version * 4 + 17
        self._errcorlvl = errcorlvl
        self._modules = [[False] * self._size for _ in range(self._size)]
        self._isfunction = [[False] * self._size for _ in range(self._size)]

        self._draw_function_patterns()
        allcodewords: bytes = self._add_ecc_and_interleave(bytearray(datacodewords))
        self._draw_codewords(allcodewords)

        if msk == -1:  # 自动选掩码：八种都试一遍，取惩罚分最低的
            minpenalty: int = 1 << 32
            for i in range(8):
                self._apply_mask(i)
                self._draw_format_bits(i)
                penalty = self._get_penalty_score()
                if penalty < minpenalty:
                    msk = i
                    minpenalty = penalty
                self._apply_mask(i)
        assert 0 <= msk <= 7
        self._mask = msk
        self._apply_mask(msk)
        self._draw_format_bits(msk)

        del self._isfunction

    def get_version(self) -> int:
        return self._version

    def get_size(self) -> int:
        return self._size

    def get_mask(self) -> int:
        return self._mask

    def get_module(self, x: int, y: int) -> bool:
        return (0 <= x < self._size) and (0 <= y < self._size) and self._modules[y][x]

    # ---- 画功能图形 ----

    def _draw_function_patterns(self) -> None:
        for i in range(self._size):
            self._set_function_module(6, i, i % 2 == 0)
            self._set_function_module(i, 6, i % 2 == 0)

        self._draw_finder_pattern(3, 3)
        self._draw_finder_pattern(self._size - 4, 3)
        self._draw_finder_pattern(3, self._size - 4)

        alignpatpos: list[int] = self._get_alignment_pattern_positions()
        numalign: int = len(alignpatpos)
        skips: Sequence[tuple[int, int]] = ((0, 0), (0, numalign - 1), (numalign - 1, 0))
        for i in range(numalign):
            for j in range(numalign):
                if (i, j) not in skips:
                    self._draw_alignment_pattern(alignpatpos[i], alignpatpos[j])

        self._draw_format_bits(0)  # 占位；构造末尾会用真正的掩码重画
        self._draw_version()

    def _draw_format_bits(self, mask: int) -> None:
        data: int = self._errcorlvl.formatbits << 3 | mask
        rem: int = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits: int = (data << 10 | rem) ^ 0x5412
        assert bits >> 15 == 0

        for i in range(0, 6):
            self._set_function_module(8, i, _get_bit(bits, i))
        self._set_function_module(8, 7, _get_bit(bits, 6))
        self._set_function_module(8, 8, _get_bit(bits, 7))
        self._set_function_module(7, 8, _get_bit(bits, 8))
        for i in range(9, 15):
            self._set_function_module(14 - i, 8, _get_bit(bits, i))

        for i in range(0, 8):
            self._set_function_module(self._size - 1 - i, 8, _get_bit(bits, i))
        for i in range(8, 15):
            self._set_function_module(8, self._size - 15 + i, _get_bit(bits, i))
        self._set_function_module(8, self._size - 8, True)  # 固定深色模块

    def _draw_version(self) -> None:
        if self._version < 7:
            return
        rem: int = self._version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits: int = self._version << 12 | rem
        assert bits >> 18 == 0

        for i in range(18):
            bit: bool = _get_bit(bits, i)
            a: int = self._size - 11 + i % 3
            b: int = i // 3
            self._set_function_module(a, b, bit)
            self._set_function_module(b, a, bit)

    def _draw_finder_pattern(self, x: int, y: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                xx, yy = x + dx, y + dy
                if (0 <= xx < self._size) and (0 <= yy < self._size):
                    self._set_function_module(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))

    def _draw_alignment_pattern(self, x: int, y: int) -> None:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function_module(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)

    def _set_function_module(self, x: int, y: int, isdark: bool) -> None:
        assert type(isdark) is bool
        self._modules[y][x] = isdark
        self._isfunction[y][x] = True

    # ---- 纠错码与掩码 ----

    def _add_ecc_and_interleave(self, data: bytearray) -> bytes:
        version: int = self._version
        assert len(data) == _QrCode._get_num_data_codewords(version, self._errcorlvl)

        numblocks: int = _QrCode._NUM_ERROR_CORRECTION_BLOCKS[self._errcorlvl.ordinal][version]
        blockecclen: int = _QrCode._ECC_CODEWORDS_PER_BLOCK[self._errcorlvl.ordinal][version]
        rawcodewords: int = _QrCode._get_num_raw_data_modules(version) // 8
        numshortblocks: int = numblocks - rawcodewords % numblocks
        shortblocklen: int = rawcodewords // numblocks

        blocks: list[bytes] = []
        rsdiv: bytes = _QrCode._reed_solomon_compute_divisor(blockecclen)
        k: int = 0
        for i in range(numblocks):
            dat: bytearray = data[k: k + shortblocklen - blockecclen + (0 if i < numshortblocks else 1)]
            k += len(dat)
            ecc: bytes = _QrCode._reed_solomon_compute_remainder(dat, rsdiv)
            if i < numshortblocks:
                dat.append(0)
            blocks.append(dat + ecc)
        assert k == len(data)

        result = bytearray()
        for i in range(len(blocks[0])):
            for (j, blk) in enumerate(blocks):
                if (i != shortblocklen - blockecclen) or (j >= numshortblocks):
                    result.append(blk[i])
        assert len(result) == rawcodewords
        return result

    def _draw_codewords(self, data: bytes) -> None:
        assert len(data) == _QrCode._get_num_raw_data_modules(self._version) // 8

        i: int = 0
        for right in range(self._size - 1, 0, -2):  # 每次一对列，从右往左
            if right <= 6:
                right -= 1
            for vert in range(self._size):
                for j in range(2):
                    x: int = right - j
                    upward: bool = (right + 1) & 2 == 0
                    y: int = (self._size - 1 - vert) if upward else vert
                    if (not self._isfunction[y][x]) and (i < len(data) * 8):
                        self._modules[y][x] = _get_bit(data[i >> 3], 7 - (i & 7))
                        i += 1
        assert i == len(data) * 8

    def _apply_mask(self, mask: int) -> None:
        if not (0 <= mask <= 7):
            raise ValueError("Mask value out of range")
        masker: Callable[[int, int], int] = _QrCode._MASK_PATTERNS[mask]
        for y in range(self._size):
            for x in range(self._size):
                self._modules[y][x] ^= (masker(x, y) == 0) and (not self._isfunction[y][x])

    def _get_penalty_score(self) -> int:
        result: int = 0
        size: int = self._size
        modules: list[list[bool]] = self._modules

        # 行内同色连续块 + 形如定位图形的 1:1:3:1:1
        for y in range(size):
            runcolor: bool = False
            runx: int = 0
            runhistory = collections.deque([0] * 7, 7)
            for x in range(size):
                if modules[y][x] == runcolor:
                    runx += 1
                    if runx == 5:
                        result += _QrCode._PENALTY_N1
                    elif runx > 5:
                        result += 1
                else:
                    self._finder_penalty_add_history(runx, runhistory)
                    if not runcolor:
                        result += self._finder_penalty_count_patterns(runhistory) * _QrCode._PENALTY_N3
                    runcolor = modules[y][x]
                    runx = 1
            result += self._finder_penalty_terminate_and_count(runcolor, runx, runhistory) * _QrCode._PENALTY_N3
        # 列内同理
        for x in range(size):
            runcolor = False
            runy: int = 0
            runhistory = collections.deque([0] * 7, 7)
            for y in range(size):
                if modules[y][x] == runcolor:
                    runy += 1
                    if runy == 5:
                        result += _QrCode._PENALTY_N1
                    elif runy > 5:
                        result += 1
                else:
                    self._finder_penalty_add_history(runy, runhistory)
                    if not runcolor:
                        result += self._finder_penalty_count_patterns(runhistory) * _QrCode._PENALTY_N3
                    runcolor = modules[y][x]
                    runy = 1
            result += self._finder_penalty_terminate_and_count(runcolor, runy, runhistory) * _QrCode._PENALTY_N3

        # 2x2 同色块
        for y in range(size - 1):
            for x in range(size - 1):
                if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    result += _QrCode._PENALTY_N2

        # 黑白比例失衡
        dark: int = sum((1 if cell else 0) for row in modules for cell in row)
        total: int = size ** 2
        k: int = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        assert 0 <= k <= 9
        result += k * _QrCode._PENALTY_N4
        assert 0 <= result <= 2568888
        return result

    # ---- 纯函数与查表 ----

    def _get_alignment_pattern_positions(self) -> list[int]:
        if self._version == 1:
            return []
        numalign: int = self._version // 7 + 2
        step: int = (self._version * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
        result: list[int] = [(self._size - 7 - i * step) for i in range(numalign - 1)] + [6]
        return list(reversed(result))

    @staticmethod
    def _get_num_raw_data_modules(ver: int) -> int:
        if not (_QrCode.MIN_VERSION <= ver <= _QrCode.MAX_VERSION):
            raise ValueError("Version number out of range")
        result: int = (16 * ver + 128) * ver + 64
        if ver >= 2:
            numalign: int = ver // 7 + 2
            result -= (25 * numalign - 10) * numalign - 55
            if ver >= 7:
                result -= 36
        assert 208 <= result <= 29648
        return result

    @staticmethod
    def _get_num_data_codewords(ver: int, ecl: "_Ecc") -> int:
        return _QrCode._get_num_raw_data_modules(ver) // 8 \
            - _QrCode._ECC_CODEWORDS_PER_BLOCK[ecl.ordinal][ver] \
            * _QrCode._NUM_ERROR_CORRECTION_BLOCKS[ecl.ordinal][ver]

    @staticmethod
    def _reed_solomon_compute_divisor(degree: int) -> bytes:
        if not (1 <= degree <= 255):
            raise ValueError("Degree out of range")
        result = bytearray([0] * (degree - 1) + [1])
        root: int = 1
        for _ in range(degree):
            for j in range(degree):
                result[j] = _QrCode._reed_solomon_multiply(result[j], root)
                if j + 1 < degree:
                    result[j] ^= result[j + 1]
            root = _QrCode._reed_solomon_multiply(root, 0x02)
        return bytes(result)

    @staticmethod
    def _reed_solomon_compute_remainder(data: bytes, divisor: bytes) -> bytes:
        result = bytearray([0] * len(divisor))
        for b in data:
            factor: int = b ^ result.pop(0)
            result.append(0)
            for (i, coef) in enumerate(divisor):
                result[i] ^= _QrCode._reed_solomon_multiply(coef, factor)
        return bytes(result)

    @staticmethod
    def _reed_solomon_multiply(x: int, y: int) -> int:
        if (x >> 8 != 0) or (y >> 8 != 0):
            raise ValueError("Byte out of range")
        z: int = 0
        for i in reversed(range(8)):
            z = (z << 1) ^ ((z >> 7) * 0x11D)
            z ^= ((y >> i) & 1) * x
        assert z >> 8 == 0
        return z

    def _finder_penalty_count_patterns(self, runhistory: collections.deque) -> int:
        n: int = runhistory[1]
        assert n <= self._size * 3
        core: bool = n > 0 and (runhistory[2] == runhistory[4] == runhistory[5] == n) and runhistory[3] == n * 3
        return (1 if (core and runhistory[0] >= n * 4 and runhistory[6] >= n) else 0) \
            + (1 if (core and runhistory[6] >= n * 4 and runhistory[0] >= n) else 0)

    def _finder_penalty_terminate_and_count(self, currentruncolor: bool, currentrunlength: int,
                                            runhistory: collections.deque) -> int:
        if currentruncolor:
            self._finder_penalty_add_history(currentrunlength, runhistory)
            currentrunlength = 0
        currentrunlength += self._size  # 边界算浅色
        self._finder_penalty_add_history(currentrunlength, runhistory)
        return self._finder_penalty_count_patterns(runhistory)

    def _finder_penalty_add_history(self, currentrunlength: int, runhistory: collections.deque) -> None:
        if runhistory[0] == 0:
            currentrunlength += self._size
        runhistory.appendleft(currentrunlength)

    # 每个版本每个纠错等级的纠错码字数（index 0 是占位）
    _ECC_CODEWORDS_PER_BLOCK: Sequence[Sequence[int]] = (
        (-1,  7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # Low
        (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),  # Medium
        (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # Quartile
        (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),  # High
    )

    # 每个版本每个纠错等级的纠错块数
    _NUM_ERROR_CORRECTION_BLOCKS: Sequence[Sequence[int]] = (
        (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4,  4,  4,  4,  4,  6,  6,  6,  6,  7,  8,  8,  9,  9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),  # Low
        (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5,  5,  8,  9,  9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),  # Medium
        (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8,  8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),  # Quartile
        (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),  # High
    )

    _MASK_PATTERNS: Sequence[Callable[[int, int], int]] = (
        (lambda x, y: (x + y) % 2),
        (lambda x, y: y % 2),
        (lambda x, y: x % 3),
        (lambda x, y: (x + y) % 3),
        (lambda x, y: (x // 3 + y // 2) % 2),
        (lambda x, y: x * y % 2 + x * y % 3),
        (lambda x, y: (x * y % 2 + x * y % 3) % 2),
        (lambda x, y: ((x + y) % 2 + x * y % 3) % 2),
    )


class _Ecc:
    """纠错等级。ordinal 是查表下标，formatbits 是写进格式信息的两位。"""

    ordinal: int
    formatbits: int

    def __init__(self, name: str, i: int, fb: int) -> None:
        self.name = name
        self.ordinal = i
        self.formatbits = fb


_ECC_LOW = _Ecc("L", 0, 1)
_ECC_MEDIUM = _Ecc("M", 1, 0)
_ECC_QUARTILE = _Ecc("Q", 2, 3)
_ECC_HIGH = _Ecc("H", 3, 2)
_ECC_BY_NAME = {"L": _ECC_LOW, "M": _ECC_MEDIUM, "Q": _ECC_QUARTILE, "H": _ECC_HIGH}


class _BitBuffer(list):
    """可追加位（0/1）的缓冲区。"""

    def append_bits(self, val: int, n: int) -> None:
        if (n < 0) or (val >> n != 0):
            raise ValueError("Value out of range")
        self.extend(((val >> i) & 1) for i in reversed(range(n)))


def _get_bit(x: int, i: int) -> bool:
    """返回整数 x 第 i 位（从低位起）是否为 1。"""
    return (x >> i) & 1 != 0
