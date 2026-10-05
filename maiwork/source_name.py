"""来源名统一（docs/10-资讯流水线改进计划.md §九 第一步 1，2026-10-05）。

一个候选的「来源网站」（`sources[].site`）以前有两种写法：搜索候选是代码从 url 算的域名
（`feeds._site_of`），RSS 候选却拿 RSS 源标题（`entry.title`）当 site——于是
news_items 里出现「机核」「游研社」「触乐」这类中文站名：同一网站被拆成两份，
站名也没法拿去 `site` 搜 / 找 RSS。

现在统一走这里一个函数：
- 一律用真实域名（从 url 算、去 `www.`）；
- 大平台按作者 / 仓库 / 子域名算：`github.com/<owner>`、`<name>.substack.com`、
  `medium.com/@<作者>`、`dev.to/<作者>`（不然 github.com 一个平台吃掉整张优质来源名单）；
- YouTube / B 站不做按频道切分：视频地址（`/watch?v=`、`/video/BV…`）里拿不到频道，
  只有分享「频道页」链接时才拿得到，同一频道会一会拆成 `youtube.com/@x`、一会只剩
  `youtube.com`，比不拆更乱。要按频道算得打开页面读，这步不花那个钱。
- 公众号 / 知乎 / X 同理（要登录 / 反爬），先不碰。

用法（写入和统计必须用同一个函数，别各写一份）：
- 写入候选 / 入库：`site_of_url(item["url"])`；
- 读旧数据 / 跟库里存的 site 比：`normalize_site(存的 site)`（是域名标签就直接用，
  是 URL 就走 `site_of_url`）；
- 域名级判断（屏蔽名单、同域名配额）：`domain_of(site)`。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# 合法主机名（至少要有一个点）：中文站名、带空格 / 协议的脏值都会被拒
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

# github.com 自己的栏目页：第一个路径段不是「仓库主人」
_GITHUB_RESERVED = frozenset({
    "about", "apps", "blog", "collections", "contact", "customer-stories", "dashboard", "docs",
    "enterprise", "events", "explore", "features", "issues", "join", "login", "marketplace",
    "new", "notifications", "orgs", "pricing", "pulls", "readme", "resources", "search",
    "security", "settings", "site", "solutions", "sponsors", "topics", "trending",
})
# dev.to 的栏目页（不是作者）
_DEVTO_RESERVED = frozenset({
    "about", "challenges", "dashboard", "dev", "enter", "latest", "list", "new", "notifications",
    "pod", "readinglist", "search", "settings", "shop", "sponsors", "t", "tag", "tags", "top", "videos",
})


def _host_of(value: str) -> str:
    """从 URL / 主机名取主机（小写、去 www.、去端口）；不像域名 → ""。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host if _HOST_RE.match(host) else ""


def _segments(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if "://" not in raw:
        raw = "https://" + raw
    try:
        path = urlsplit(raw).path or ""
    except ValueError:
        return []
    return [s for s in path.split("/") if s]


def domain_of(value: str) -> str:
    """来源名 → 纯域名（去 www.、去作者 / 路径段）。不合法 → ""。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        return _host_of(raw)
    raw = raw.split("/", 1)[0].split("@", 1)[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw if _HOST_RE.match(raw) else ""


def site_of_url(url: str) -> str:
    """候选来源名：真实域名（去 www.）；大平台按作者 / 仓库 / 子域名细分。"""
    host = _host_of(url)
    if not host:
        return ""
    segs = _segments(url)
    if host == "github.com":
        owner = segs[0] if segs else ""
        if owner and owner.lower() not in _GITHUB_RESERVED:
            return f"github.com/{owner}"
        return "github.com"
    if host.endswith(".substack.com"):
        return host  # 作者子域名本身就是一站
    if host == "medium.com":
        first = segs[0] if segs else ""
        return f"medium.com/{first}" if first.startswith("@") else "medium.com"
    if host == "dev.to":
        first = segs[0] if segs else ""
        return f"dev.to/{first}" if first and first.lower() not in _DEVTO_RESERVED else "dev.to"
    return host


def normalize_site(value: str) -> str:
    """把「库里的 site / url_key / 配置里的域名」归一到同一个名字（跟 site_of_url 对得上）。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return site_of_url(raw)
    if "/" in raw or "." in raw:
        one = site_of_url(raw)
        if one:
            return one
    return domain_of(raw)
