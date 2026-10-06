"""封面图过滤（maiwork/cover_filter.py）：识别站点 logo / 带字横幅，别让它们当资讯封面。

所有测试图都用 Pillow 现场生成，不联网、不读外部文件。
"""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from maiwork import cover_filter


def _font(size: int) -> ImageFont.ImageFont:
    """默认字体放大（Pillow ≥ 10.1 支持 size 参数）。"""
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------- 测试图生成


def _logo_like(size: tuple[int, int] = (512, 512)) -> Image.Image:
    """白底 + 几个纯色几何块 + 粗体字：站点 logo 的典型长相。"""
    im = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 180, 180), fill=(219, 68, 55))
    d.ellipse((210, 60, 300, 150), fill=(66, 133, 244))
    d.polygon([(360, 60), (460, 150), (360, 160)], fill=(15, 157, 88))
    d.text((60, 280), "EXAMPLY", font=_font(72), fill=(32, 32, 32))
    d.text((60, 380), "Open Knowledge", font=_font(36), fill=(110, 110, 110))
    return im


def _flat_icon(size: tuple[int, int] = (256, 256)) -> Image.Image:
    """纯色底 + 一个图形：最朴素的图标。"""
    im = Image.new("RGB", size, (240, 244, 248))
    d = ImageDraw.Draw(im)
    d.ellipse((48, 48, 208, 208), fill=(0, 122, 255))
    d.rectangle((104, 104, 152, 200), fill=(255, 255, 255))
    return im


def _banner(size: tuple[int, int] = (1200, 630)) -> Image.Image:
    """浅色底 + 一行大号文字的 1200×630 宣传横幅（og:image 常见）。"""
    im = Image.new("RGB", size, (245, 240, 232))
    d = ImageDraw.Draw(im)
    d.text((90, 250), "Gleam v1.9 release", font=_font(96), fill=(38, 34, 30))
    return im


def _banner_two_lines(size: tuple[int, int] = (1200, 630)) -> Image.Image:
    """米色底 + 大字母（ElevenLabs 那种）。"""
    im = Image.new("RGB", size, (238, 232, 220))
    d = ImageDraw.Draw(im)
    d.text((120, 200), "ELEVEN", font=_font(140), fill=(20, 20, 20))
    d.text((120, 380), "LABS", font=_font(140), fill=(20, 20, 20))
    return im


def _photo(seed: int, size: tuple[int, int] = (1024, 640)) -> Image.Image:
    """模拟自然照片：平滑渐变 + 多个随机彩色模糊块 + 高斯噪声。只用 Pillow。"""
    rnd = random.Random(seed)
    w, h = size

    grad = Image.linear_gradient("L")
    if rnd.random() < 0.5:
        grad = grad.transpose(Image.Transpose.ROTATE_90)
    grad = grad.resize((w, h), Image.BILINEAR)
    dark = (rnd.randrange(20, 90), rnd.randrange(20, 90), rnd.randrange(20, 90))
    light = (rnd.randrange(150, 255), rnd.randrange(150, 255), rnd.randrange(150, 255))
    base = ImageOps.colorize(grad, black=dark, white=light)

    # 彩色模糊块：模拟物体 / 人物 / 景物块面
    mask = Image.new("L", (w, h), 0)
    blobs = Image.new("RGB", (w, h), (0, 0, 0))
    md = ImageDraw.Draw(mask)
    bd = ImageDraw.Draw(blobs)
    for _ in range(rnd.randrange(3, 7)):
        cx, cy = rnd.randrange(0, w), rnd.randrange(0, h)
        rx, ry = rnd.randrange(w // 8, w // 3), rnd.randrange(h // 8, h // 3)
        color = (rnd.randrange(0, 256), rnd.randrange(0, 256), rnd.randrange(0, 256))
        box = (cx - rx, cy - ry, cx + rx, cy + ry)
        if rnd.random() < 0.5:
            md.ellipse(box, fill=255)
            bd.ellipse(box, fill=color)
        else:
            md.rectangle(box, fill=255)
            bd.rectangle(box, fill=color)
    base = Image.composite(
        blobs.filter(ImageFilter.GaussianBlur(28)), base, mask.filter(ImageFilter.GaussianBlur(24))
    )

    noise = Image.effect_noise((w, h), 26.0)
    out = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.12)
    return out.filter(ImageFilter.GaussianBlur(0.6)).convert("RGB")


def _screenshot_photo(seed: int) -> Image.Image:
    """更像「游戏截图」：噪声底 + 大块高饱和色域 + 斜向渐变。"""
    rnd = random.Random(seed)
    w, h = 1280, 720
    grad = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_270).resize((w, h), Image.BILINEAR)
    tint = Image.new("RGB", (w, h), (rnd.randrange(0, 200), rnd.randrange(0, 200), rnd.randrange(0, 200)))
    base = ImageChops.screen(ImageOps.colorize(grad, black=(5, 5, 25), white=(230, 210, 170)), tint)
    d = ImageDraw.Draw(base)
    for _ in range(rnd.randrange(6, 12)):
        x, y = rnd.randrange(0, w), rnd.randrange(0, h)
        d.polygon(
            [
                (x, y),
                (x + rnd.randrange(60, 400), y + rnd.randrange(-200, 200)),
                (x + rnd.randrange(-100, 300), y + rnd.randrange(60, 400)),
            ],
            fill=(rnd.randrange(0, 256), rnd.randrange(0, 256), rnd.randrange(0, 256)),
        )
    base = base.filter(ImageFilter.GaussianBlur(3))
    noise = Image.effect_noise((w, h), 20.0)
    return Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.10).convert("RGB")


def _poster(size: tuple[int, int] = (1200, 630)) -> Image.Image:
    """平底 + 纯色块 + 三行大字的宣传图：底色没到一半（< 45%），只有「带字横幅」那条规则管得住。"""
    im = Image.new("RGB", size, (246, 242, 234))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 380, size[0], size[1]), fill=(38, 62, 122))
    d.ellipse((60, 60, 290, 290), fill=(232, 92, 80))
    d.polygon([(920, 50), (1140, 50), (1030, 300)], fill=(22, 150, 122))
    f = _font(80)
    for i, line in enumerate(["A fast compiler", "for the Erlang VM", "and the JS target"]):
        d.text((320, 70 + i * 96), line, font=f, fill=(30, 28, 26))
    return im


def _dark_logo(size: tuple[int, int] = (1000, 500)) -> Image.Image:
    """深色底 + 白字：暗色主题站点常见的 og:image。"""
    im = Image.new("RGB", size, (18, 22, 34))
    d = ImageDraw.Draw(im)
    d.text((80, 180), "ACME", font=_font(120), fill=(245, 245, 245))
    d.rectangle((80, 320, 320, 340), fill=(80, 140, 255))
    return im


def _transparent_logo(size: tuple[int, int] = (400, 400)) -> Image.Image:
    """透明底 PNG logo：白色图案 + 透明背景（要按白底算，不能当照片）。"""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((60, 60, 340, 340), fill=(255, 255, 255, 255))
    d.rectangle((150, 150, 250, 330), fill=(255, 255, 255, 255))
    return im


def _soft_gradient_photo(seed: int = 3, size: tuple[int, int] = (1200, 800)) -> Image.Image:
    """大面积同一色调的柔和渐变（雾/雪/白墙那类照片）+ 几个模糊深色物体 + 颗粒。"""
    rnd = random.Random(seed)
    w, h = size
    grad = Image.linear_gradient("L")
    if rnd.random() < 0.5:
        grad = grad.transpose(Image.Transpose.ROTATE_90)
    im = ImageOps.colorize(grad.resize((w, h), Image.BILINEAR), black=(206, 204, 200), white=(252, 251, 250))
    d = ImageDraw.Draw(im)
    for _ in range(4):
        x, y = rnd.randrange(0, w), rnd.randrange(0, h)
        r = rnd.randrange(120, 420)
        d.ellipse((x, y, x + r, y + r), fill=(96, 92, 88))
    im = im.filter(ImageFilter.GaussianBlur(22))
    noise = Image.effect_noise((w, h), 20.0)
    return Image.blend(im, Image.merge("RGB", (noise, noise, noise)), 0.10)


# ---------------------------------------------------------------- 1. logo / 平涂


def test_logo_like_is_rejected():
    reason = cover_filter.judge(_logo_like())
    assert reason, "白底几何块 + 粗体字的 logo 应该被拒"
    assert isinstance(reason, str)


def test_flat_icon_is_rejected():
    assert cover_filter.judge(_flat_icon()), "纯色底图标应该被拒"


def test_solid_color_is_rejected():
    assert cover_filter.judge(Image.new("RGB", (600, 400), (250, 250, 250))), "纯色图应该被拒"


# ---------------------------------------------------------------- 2. 带字横幅


def test_text_banner_is_rejected():
    assert cover_filter.judge(_banner()), "浅色底 + 一行大字的 1200×630 横幅应该被拒"


def test_big_letter_banner_is_rejected():
    assert cover_filter.judge(_banner_two_lines()), "米色底 + 大字母应该被拒"


def test_poster_with_big_text_is_rejected():
    """底色占比不到一半、只有大字 + 色块的宣传图：靠「带字横幅」那条规则拦住。"""
    assert cover_filter.judge(_poster()), "平底 + 大字的海报应该被拒"


def test_dark_logo_is_rejected():
    assert cover_filter.judge(_dark_logo()), "深色底 + 白字的 logo 应该被拒"


def test_transparent_logo_is_rejected():
    assert cover_filter.judge(_transparent_logo()), "透明底 logo 应该被拒"


def test_palette_mode_logo_is_rejected():
    assert cover_filter.judge(_logo_like().convert("P")), "调色板模式的 logo 也应该被拒"


# ---------------------------------------------------------------- 3. 照片必须放行


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11])
def test_photo_is_allowed(seed: int):
    assert cover_filter.judge(_photo(seed)) == "", f"自然照片（seed={seed}）不能被误杀"


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_screenshot_is_allowed(seed: int):
    assert cover_filter.judge(_screenshot_photo(seed)) == "", f"截图（seed={seed}）不能被误杀"


@pytest.mark.parametrize("seed", [3, 5])
def test_soft_gradient_photo_is_allowed(seed: int):
    assert cover_filter.judge(_soft_gradient_photo(seed)) == "", f"柔和渐变照片（seed={seed}）不能被误杀"

# ---------------------------------------------------------------- 4. url 关键词


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/static/browse/0.3.4/images/arxiv-logo-fb.png",
        "https://example.com/favicon.ico",
        "https://cdn.example.com/assets/site_logo.png",
        "https://example.com/img/og-default.png",
        "https://example.com/static/share-default.jpg",
        "https://example.com/brand/banner.png",
        "https://example.com/a/icon.png",
        "https://example.com/u/avatar.png",
        "https://example.com/img/placeholder.png",
        "https://example.com/img/default-avatar-meetup-2026.jpg",
    ],
)
def test_logo_url_is_rejected(url: str):
    # 图本身是照片，只有地址露了 logo 味，也算拒
    assert cover_filter.judge(_photo(5), url) != "", url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/2026/05/12/photo-1234.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a7/ant.jpg/640px-ant.jpg",
        "https://cdn.example.com/images/iconic-moment-in-rome.jpg",
        "https://example.com/posts/silicon-valley-report.jpg",
        "https://example.com/logo-contest/winners/painting-2019.jpg",  # 关键词只算文件名 + 末两段路径
    ],
)
def test_normal_url_does_not_get_rejected_by_url(url: str):
    # 照片 + 干净地址 → 必须放行（不因为地址被拒）
    assert cover_filter.judge(_photo(6), url) == "", url


# ---------------------------------------------------------------- 5. 坏输入不许抛


@pytest.mark.parametrize(
    "im",
    [
        Image.new("RGB", (1, 1), (255, 255, 255)),
        Image.new("RGB", (3, 3), (0, 0, 0)),
        Image.new("P", (64, 64)),
        Image.new("RGBA", (400, 300), (255, 0, 0, 128)),
        Image.new("L", (400, 300), 128),
        Image.new("LA", (400, 300), 128),
        Image.new("CMYK", (400, 300)),
        Image.new("1", (400, 300), 1),
        Image.new("I", (400, 300), 700),
        Image.new("F", (400, 300), 0.5),
        Image.new("RGBX", (400, 300), (10, 20, 30, 255)),
        Image.new("I;16", (400, 300), 4000),
        Image.new("RGB", (1, 4000), (10, 20, 30)),
        Image.new("RGB", (4000, 1), (10, 20, 30)),
        _logo_like().convert("P", palette=Image.ADAPTIVE, colors=8),
    ],
)
def test_bad_input_never_raises(im: Image.Image):
    out = cover_filter.judge(im, "https://example.com/x.jpg")
    assert isinstance(out, str)


def test_none_url_never_raises():
    assert isinstance(cover_filter.judge(_photo(9), None), str)
