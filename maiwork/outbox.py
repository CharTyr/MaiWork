"""outbox.py（M3 交付层）：发件箱 Outbox、任务交付 Delivery、故障报错 report_error。

设计依据 docs/02-设计.md §6：
- 每条通知记 待发(pending)/发送中(sending)/已发(sent)/不确定(uncertain)/失败(failed)，
  按 key（对象+版本+事件类型+目标群）去重。发送超时标 uncertain 不盲目重发；
  群文件上传不幂等，绝不自动重试；文本 / 图片这类「重发一次也不会更糟」的推送，
  非超时的安全失败自动重试一次（attempts 与下次时间都落库，重启后接着算，不重复发）。
- 四种载荷：text（可选 reply_to / at_user / at_name）、file（群文件）、image（PNG 文件，
  严格路径闸：非符号链接、resolve 后在工作区根下、大小与类型按内容判）、herenow（网页）。
- 发送前再查一遍：这个群还在服务名单里吗、这条推送的每群开关还开着吗（关了直接作废，
  不发陈旧的）、睡觉时段与每日总上限（group_push 一份数据，**按这个群**那份算；
  读不到就只延 5 分钟再试，绝不按默认钟点误停到明天）。失败不占额度、不算已发；
  结果不明（uncertain）安全保留额度，网页视图也绝不写成「已发」。
- 推迟时**一次算到「下一次能发」的时刻**（2026-10-06 本地修复，未部署）：额度用完 =
  次日 00:00，若那一刻仍落在本群 quiet 里就接着移到那段 quiet 结束（跨夜 / 同日区间
  都对）——线上 #58 / #59 是「19 点排到次日 00:00 → 又撞静默 → 再排 07:00」两趟，
  而卡片 TTL 06:59，要拖到 07:00 才被发现过期。算出的发送时刻**严格晚于**
  payload.expires_ts（与发送前判定同口径：now > expires 才算过期）→ 当场作废
  （dropped）并如实通知生产者：**不延长 TTL、不提高额度、不改旧记录**；没有有效期的
  载荷（任务交付等）不受影响。读不到那份设置 / 认不出原因 → 只延 5 分钟再试，
  绝不用猜出来的次日钟点提前作废；这 5 分钟只做**有界检查**（下一次 flush 最早也在
  之后，那时已经过期才作废）。延期 / 作废都留一条不含载荷内容的日志 + events。
- 自动消息（开场白 / 资讯卡片 / 构想提一嘴）的载荷带 `expires_ts`：过了期限还没发出去
  的那条直接作废，不把陈旧内容发进群。生产者还能挂 `add_preflight_hook`（同步、纯代码、
  generic 发件箱不替它读群 / 不调模型）做发送前新鲜度复核：开场白排队 / 重试期间群里又
  有人说话了，就不再当成冷场开口（Topics.on_before_send）。
- **个人提一嘴的强制复核**（2026-10）：`key` 是 `idea_mention:<id>`、`push_kind=idea_mention`
  且 `at_user` 非空的载荷（@ 某人的提议），发送前必须过两道：(a) **载荷契约**——必须带生产者
  留下的复核材料 `payload["guard"]`（uid / checked_ts / 材料指纹 / 依据都得像样），这里只做
  **结构性**检查，不读群、不调模型；(b) **生产者必须登记复核者**（`set_personal_guard`，
  由 card_push.IdeaMention 的 on_before_send 承担，同步、纯代码）。**没登记复核者 → 一律
  作废**：别的 preflight hook（比如冷场开场白的）冒充不了，重启 / 旧队列 / 生产者没起来
  都不会把个人提议放出去。群向提一嘴（`at_user` 为空）不受这一条约束。
- 发送结果统一走结果 hook（add_result_hook）：生产者（冷场开场白 / 资讯卡片 / 构想提一嘴）
  只负责 enqueue，真正发出去（或失败 / 不确定 / 作废）之后才回写自己的表。
- 普通交付也受日限额/睡觉时段约束；群友以 /mw 领取 <任务号> 明确索取时，
  仅把待发的本任务成品标 awaited_delivery，不受两项节制、不占额度；
  error / command / admin 即时反馈也不受限。
- file 传完补一条说明消息（MaiBot 不知道文件是谁发的）；herenow 成功发链接说明。
- 交付说明消息（note / note+链接 / 只发文字的交付）带上**发起人**：QQ 走真 at 段，
  Telegram 由 host 退成正文「@名字 」（docs/06）；发起人缺失保持老行为。
- 交付成功都把链接/文件名放进可提起清单（ttl 6 小时）。
- 首选渠道失败（failed，不含 uncertain）自动回落备选；两条都失败 → 兜底说明
  「做好了，但发群文件和网页都没成功，成品在 MaiWork 网页里」。
- file 回落 here.now 的页面必须**手机可读**（线上 T-11 的教训：老页面只有一句
  「附件：」+ 下载链接，点开什么都看不到）：同目录有同名主干的 .html（X.docx →
  X.html）或成品目录根有 index.html 就用那份网页当主体，原文件同批发布并保留下载
  链接；都没有就生成一个内联 CSS、转义过的手机友好页（标题 / 说明 / 文件名+大小 /
  醒目下载按钮）。页面里绝不写工作区路径或 artifacts/ 这类内部路径。
- 插件重启 recover()：sending → uncertain（不重放）。
Fallback 的临时目录放在 settings.workspace_root 下的 .web/.fallback，不进群友可见工作区。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import zipfile
from datetime import timedelta
from html import escape as _html_escape
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote as _url_quote

from . import clock, group_push, members
from .delivery import UNREADABLE_REASON, Mentions, Pushes, task_quota_free_kind
from .host import HostError
from .models import _redact
from .store import Store

logger = logging.getLogger("maiwork.outbox")

_DELIVER_MENTION_TTL_S = 6 * 3600  # 交付备忘在可提起清单里留 6 小时
_ERR_MAX = 300
_DEDUP_WINDOW_S = 600  # report_error 同群同指纹 10 分钟一次
_DELIVERY_NOTE_SUFFIXES = (":note", ":webonly")

# 失败的发送：能安全重发的 kind + 有界重试（首发 + 一次）
_RETRY_SAFE_KINDS = frozenset(("text", "image"))
_MAX_ATTEMPTS = 2
_RETRY_DELAY_S = 300.0
_DROP_REASON = "开关已关"
# Pushes.can_push 给的两个推迟原因（额度按北京日期次日 00:00 恢复；睡觉时段按本群 quiet）
_QUOTA_REASON = "今天推够了"
_QUIET_REASON = "睡觉时段"
# 延期 / 作废的安全留痕（events.kind）：只有原因和目标时刻，绝不含载荷内容
_EVENT_POSTPONE = "outbox.postpone"
_EVENT_TTL_DROP = "outbox.ttl_drop"
# 读不到本群那份设置时的推迟步长（秒）：只延 5 分钟再试，绝不按默认钟点误停到明天
_POSTPONE_RETRY_S = 300.0
# 自动消息过了 payload.expires_ts 还没发出去 → 作废（不发陈旧内容）
_TTL_EXPIRED_REASON = "超过有效期限，作废"

# 图片载荷的严格闸（docs/02 §6.5：发进群的东西必须在工作区里、是真 PNG）
_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 交付说明消息：只有这两种 push_kind 才算「交付」（提问 / 报错 / 卡片不受 @ 发起人影响）
_DELIVERY_PUSH_KINDS = frozenset(("delivery", "awaited_delivery"))
# 回落页的候选网页后缀（X.docx → X.html）
_PAGE_SUFFIXES = (".html", ".htm")


def _row_field(row: Any, name: str) -> str:
    """行里某一列（取不到 → ""）：_due_rows 的行有 task_id，别处不一定。"""
    try:
        return str(row[name] or "")
    except Exception:
        return ""


def _human_size(size: Optional[int]) -> str:
    """字节数 → 人看的「10 B / 2.0 KB / 3.5 MB」；没有 / 坏值 → ""。"""
    try:
        n = int(size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if n < 0:
        return ""
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} B"


def _download_button_html(name: str, href: str) -> str:
    """一个醒目的下载按钮（全内联样式，不引外部资源）。"""
    label = _html_escape(str(name or "原文件"), quote=True)
    url = _html_escape(str(href or ""), quote=True)
    return (
        '<div style="margin:24px 16px 32px;padding:14px 16px;border-radius:14px;'
        'background:#f2f3f5;text-align:center;font-size:17px;">'
        '<a style="display:inline-block;padding:14px 26px;border-radius:999px;'
        'background:#07c160;color:#fff;text-decoration:none;font-weight:600;"'
        f' href="{url}" download>{label}</a></div>'
    )


def _append_download_button(page: str, name: str) -> str:
    """同名网页当页面主体时，补一个原文件下载条（页面自己没链它的话）。"""
    bar = _download_button_html(name, _url_quote(str(name)))
    idx = page.lower().rfind("</body>")
    if idx < 0:
        return page + bar
    return page[:idx] + bar + page[idx:]


# 页面里声明的字符集（<meta charset=...> / <meta http-equiv=... charset=...>）
_PAGE_CHARSET_RE = re.compile(r"(?i)<meta[^>]*?charset\s*=\s*[\"']?[A-Za-z0-9_\-]+[\"']?")


def _read_page_text(path: Path) -> str:
    """读回落页正文：优先 utf-8，读不动就试 gb18030（老工具产的中文网页）。

    非 utf-8 的页面按 utf-8 重写索引页，所以顺手把页面里声明的字符集也改成 utf-8
    （否则浏览器按老声明解新文件，中文全是乱码）。
    """
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "utf-8":
            return text
        return _PAGE_CHARSET_RE.sub('<meta charset="utf-8"', text)
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 交付说明的兜底措辞（2026-10 docs/26 问题 C，线上 T-10 / T-11）：
# 和 coordinator.delivery_note_fallback 同一套口径——`<任务标题>弄好了，点开就能看`
# （file 说「文件在链接里」、text 说「就这几句」），标题里的内部词先刮掉、整句 ≤60 字。
# 这里是**本地副本**：coordinator import 了 outbox，反向 import 会成环；改措辞时两边一起改。
# ---------------------------------------------------------------------------
_NOTE_MAX = 60
_NOTE_INTERNAL_RE = re.compile(
    r"T-\d+|job\d|research\.md|steps/|artifacts/|\.py(?![A-Za-z0-9_])", re.IGNORECASE
)
_NOTE_SENTENCE_ENDS = "。！？!?；;"
_NOTE_SOFT_ENDS = "，,、：:"
_NOTE_BREAKS = _NOTE_SENTENCE_ENDS + _NOTE_SOFT_ENDS
_NOTE_FALLBACK_SUFFIX = {"file": "弄好了，文件在链接里", "text": "弄好了，就这几句"}
_NOTE_FALLBACK_SUFFIX_DEFAULT = "弄好了，点开就能看"


def _cut_note_at_punctuation(text: str, limit: int = _NOTE_MAX) -> str:
    """把 text 截到 ≤limit 字：能在句读处收尾就在句读处收（不硬截半句）。"""
    head = text[:limit]
    cut = -1
    for i in range(len(head) - 1, -1, -1):
        if head[i] in _NOTE_BREAKS:
            if i + 1 >= limit // 2:  # 太靠前的句读不用，免得只剩半句
                cut = i
            break
    if cut >= 0:
        if head[cut] in _NOTE_SOFT_ENDS:
            return head[:cut].strip()
        return head[:cut + 1].strip()
    return head.strip()


def _note_fallback(task_title: Any, deliver_kind: Any = "view") -> str:
    """note 空 / 不合格时的人话兜底：`<任务标题>弄好了，点开就能看`（file 说文件在链接里）。

    标题里混进来的内部词（任务号 / 内部文件名）先刮掉，剩下的按句读截；整句 ≤60 字。
    """
    kind = str(deliver_kind or "").strip()
    suffix = _NOTE_FALLBACK_SUFFIX.get(kind, _NOTE_FALLBACK_SUFFIX_DEFAULT)
    title = " ".join(
        _NOTE_INTERNAL_RE.sub(" ", " ".join(str(task_title or "").split())).split()
    )
    room = _NOTE_MAX - len(suffix)
    if not title or room <= 0:
        return suffix
    if len(title) > room:
        title = _cut_note_at_punctuation(title, room)
    return f"{title}{suffix}"


_TEXT_REPLY_NAME = "reply.md"
# 和 coordinator 的 TEXT_REPLY_MAX 同一套口径（本地重复一份，不 import coordinator）：
# 超过这个字数的回复原文当群文件发，不当聊天消息发。
_TEXT_REPLY_MAX = 1500


def _read_text_reply(task: Any, tid: str, env: Any, settings: Any) -> str:
    """text 活的回复原文（`artifacts/<任务>/reply.md`，线上 T-13）。

    读不出 / env 拿不到 / 空 / 超 1500 字 → 返回 ""（调用方照旧发兜底说明）。
    路径闸和下面 file / view 一样的口径：只在**本任务成品目录**里，符号链接不算。
    """
    if env is None:
        return ""
    try:
        ws_name = str(task["workspace"] or settings.workspace_of(str(task["group_id"])))
        ws = env.workspace(ws_name)
        path = env.resolve(ws_name, f"artifacts/{tid}/{_TEXT_REPLY_NAME}")
    except (KeyError, ValueError, OSError):
        return ""
    base = ws / "artifacts" / tid
    try:
        real = Path(path).resolve()
        real_base = Path(base).resolve()
    except OSError:
        return ""
    if real != real_base and real_base not in real.parents:
        return ""
    try:
        if Path(path).is_symlink() or not Path(path).is_file():
            return ""
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text if 0 < len(text) <= _TEXT_REPLY_MAX else ""


def _mobile_delivery_page(*, title: str, note: str, name: str,
                          size: Optional[int], href: str) -> str:
    """现生成的手机友好回落页：标题 / 说明 / 文件名+大小 / 醒目下载按钮。

    全内联 CSS、不引外部资源；标题、说明、文件名全部按 HTML 转义（防注入）；
    页面里只有文件名，不写工作区路径、artifacts/ 这类内部路径。
    """
    safe_title = _html_escape(str(title or name or "成品"), quote=True)
    safe_name = _html_escape(str(name or "附件"), quote=True)
    safe_note = _html_escape(str(note or ""), quote=True)
    size_text = _human_size(size)
    note_html = f'\n  <p class="note">{safe_note}</p>' if safe_note else ""
    meta = f"文件名：{safe_name}"
    if size_text:
        meta += f" · 大小：{_html_escape(size_text, quote=True)}"
    button = ""
    tip = ""
    if href:
        url = _html_escape(str(href), quote=True)
        button = f'\n    <a class="btn" href="{url}" download>下载 {safe_name}</a>'
        tip = '\n  <p class="tip">这个链接 24 小时后过期，请及时保存。</p>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{safe_title}</title>
<style>
  body {{ margin:0; padding:0; background:#f7f7f8; color:#1a1a1a;
         font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }}
  main {{ max-width:640px; margin:0 auto; padding:28px 18px 40px; }}
  h1 {{ font-size:22px; line-height:1.4; margin:0 0 12px; word-break:break-word; }}
  .note {{ font-size:16px; line-height:1.7; margin:0 0 20px; white-space:pre-wrap; word-break:break-word; }}
  .card {{ background:#fff; border-radius:14px; padding:18px 16px 22px; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  .meta {{ font-size:14px; color:#666; margin:0 0 16px; word-break:break-all; }}
  .btn {{ display:block; text-align:center; padding:16px 20px; border-radius:999px;
          background:#07c160; color:#fff; font-size:17px; font-weight:600; text-decoration:none; }}
  .tip {{ font-size:13px; color:#888; margin:18px 0 0; text-align:center; }}
</style>
</head>
<body>
<main>
  <h1>{safe_title}</h1>{note_html}
  <div class="card">
    <p class="meta">{meta}</p>{button}
  </div>{tip}
</main>
</body>
</html>
"""


class OutboxPayloadError(HostError):
    """这条载荷本身不合法（路径 / 类型 / 大小）：重试多少次都一样，直接判失败。"""


def _is_artifact_outbox_row(kind: str, key: str) -> bool:
    """真实交付项；失败兜底告知和后续说明都不算成品。"""
    return ((kind in ("file", "herenow") and not key.endswith(_DELIVERY_NOTE_SUFFIXES))
            or (kind == "text" and key.endswith(":deliver:text")))


def _expires_ts(payload: dict) -> Optional[float]:
    """payload.expires_ts 的有效值；没有 / 坏值 / 非正数 → None（= 没有期限）。"""
    raw = (payload or {}).get("expires_ts")
    if raw is None:
        return None
    try:
        expires = float(raw)
    except (TypeError, ValueError):
        return None
    return expires if expires > 0 else None


def _ttl_expired(payload: dict, now: float) -> str:
    """自动消息的 payload.expires_ts 过了吗；过了返回作废原因（没有期限 → ""）。

    口径不变：`now > expires` 才算过期（取等号仍算有效）。
    """
    expires = _expires_ts(payload)
    if expires is None:
        return ""
    return _TTL_EXPIRED_REASON if float(now) > expires else ""


def _quiet_window(quiet: Any) -> Optional[tuple[int, int]]:
    """这个群的 quiet 时段：「23:00-08:00」→ (1380, 480)，单位分钟。

    空 / 配错 / 起止相同（= 不限制，跟 Pushes.in_quiet 同一口径）→ None（没有静默窗口）。
    """
    text = str(quiet or "").strip()
    if not text:
        return None
    try:
        start, end = clock.parse_hhmm_range(text)
    except (ValueError, AttributeError):
        return None
    if not (0 <= start < 1440 and 0 <= end < 1440) or start == end:
        return None
    return start, end


def _quiet_end_after(ts: float, window: tuple[int, int]) -> float:
    """ts 落在这段静默里时，这段静默结束的时刻（跨夜 / 同日区间都对）。"""
    t = clock.bj(float(ts))
    end = window[1]
    out = t.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if out.timestamp() <= float(ts):
        out = out + timedelta(days=1)
    return out.timestamp()


def _fmt_bj(ts: float) -> str:
    """epoch → "YYYY-MM-DD HH:MM"（北京时间）：日志 / 事件里说清目标时刻。"""
    return clock.bj(float(ts)).strftime("%Y-%m-%d %H:%M")


_PERSONAL_MENTION_KEY = "idea_mention:"


def _is_personal_mention(key: str, payload: dict) -> bool:
    """这条推送是不是「个人提一嘴」（@ 某人的 idea_mention）。

    三样都占才算：key=idea_mention:<id>、push_kind=idea_mention、at_user 非空。
    群向提一嘴（at_user 为空）和别的推送（哪怕带 at_user）都不算。
    """
    body = payload or {}
    return (str(key or "").startswith(_PERSONAL_MENTION_KEY)
            and str(body.get("push_kind") or "") == "idea_mention"
            and bool(str(body.get("at_user") or "").strip()))


def _personal_mention_unreviewed(payload: dict) -> str:
    """个人提一嘴的载荷契约：必须带生产者留下的复核材料；缺了返回作废原因。

    这里**只做结构检查**（不读群、不调模型）：`guard` 得是 dict、uid 与 at_user 一致、
    `checked_ts` 是正数、`material`（材料指纹）是 dict、`evidence` 是非空列表。
    生产者（card_push.IdeaMention）入队时把发送前复核的快照放进 `payload["guard"]`；
    真正的复核由它登记的复核者做，这一条只保证「材料没丢」。
    """
    body = payload or {}
    uid = str(body.get("at_user") or "").strip()
    guard = body.get("guard")
    if not isinstance(guard, dict) or str(guard.get("uid") or "").strip() != uid:
        return "个人提一嘴没有发送前复核材料"
    try:
        checked = float(guard.get("checked_ts") or 0.0)
    except (TypeError, ValueError):
        return "个人提一嘴的复核材料不对"
    if checked <= 0 or not isinstance(guard.get("material"), dict):
        return "个人提一嘴的复核材料不对"
    evidence = guard.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return "个人提一嘴的复核材料不对"
    return ""

_API_KEY_RE = re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+")
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


class Outbox:
    """发件箱：enqueue 入库去重；flush 逐条发；retry 手动重发；recover 启动恢复。"""

    def __init__(
        self,
        store: Store,
        host: Any,
        pushes: Pushes,
        mentions: Mentions,
        get_settings: Callable[[], Any],
        herenow: Any = None,
    ) -> None:
        self._store = store
        self._host = host
        self._pushes = pushes
        self._mentions = mentions
        self._get_settings = get_settings
        self._herenow = herenow
        # 群空间（docs/02 §10）：上传成功时登记 group_files_owned 的回调
        # （app 启动时挂 GroupSpace.register_owned；没挂就跳过）
        self._group_file_hook: Any = None
        # 提问回执（docs/02 §7.2）：key 以 "ask:" 开头的 text 发送成功后，
        # 把 QQ 消息 ID 交给 app 回写任务的 question_msg_id（回复那条提问 → 恢复任务）
        self._ask_hook: Any = None
        # 发送结果回调（0.8.0 归一）：生产者自己挂上来，真发出去之后才回写自己的表。
        # 见 add_result_hook；出错的 hook 只记日志，绝不影响发送。
        self._result_hooks: list[Any] = []
        # 发送前新鲜度检查（0.8.0 收口）：生产者自己挂上来，同步、纯代码；
        # 见 add_preflight_hook。generic 发件箱不读群消息、不调模型。
        self._preflight_hooks: list[Any] = []
        # 个人提一嘴的复核者（2026-10）：只有 card_push.IdeaMention 会登记；**没登记就不发**
        # 个人提一嘴（见 set_personal_guard / _is_personal_mention），别的 hook 冒充不了。
        self._personal_guard: Any = None
        # 并发收口（2026-10 复审）：同一实例的 flush 串行（asyncio 锁）；
        # 不同实例 / 跨进程靠 `_claim` 的数据库 CAS（status='pending' 条件）兜底。
        self._flush_lock: Optional[asyncio.Lock] = None
        self._flush_lock_loop: Any = None

    def add_result_hook(self, hook: Any) -> None:
        """挂发送结果回调（可挂多个）：fn(info) -> None。

        info = {outbox_id, key, group_id, kind, push_kind, payload, task_id,
                outcome, error, result, ts}（ts = 这一轮 flush 的时间）
        outcome ∈ sent / uncertain / failed / retrying / dropped（retrying = 已排一次自动重试）。
        生产者据此把「真的发出去了」写回自己的表——只 enqueue 不算发过。
        """
        if callable(hook):
            self._result_hooks.append(hook)

    def add_preflight_hook(self, hook: Any) -> None:
        """挂发送前的**新鲜度**检查（可挂多个）：fn(info) -> Optional[str]。

        在真正发送之前（还没抢 sending）同步调用一次；`info` 和 add_result_hook 的
        形状一样，外加 `now`（这一轮 flush 的时间）。返回空（None / ""）= 放行；
        返回原因字符串 = 这条已经不再新鲜，作废（dropped），绝不发陈旧内容。

        约定：同步、纯代码——生产者可以在里面读**自己**那份数据（比如本群最后消息
        时刻），但 generic 发件箱不替它读群消息、不调模型。hook 抛异常 → 失败关闭
        （作废），宁可少发一条，不可放任陈旧内容发进群。
        """
        if callable(hook):
            self._preflight_hooks.append(hook)

    def set_personal_guard(self, hook: Any) -> None:
        """登记「个人提一嘴的发送前复核」（生产者 card_push.IdeaMention 自己挂）。

        同步、纯代码、由它自己读群 / 不调模型；签名同 add_preflight_hook 的 hook：
        fn(info) -> Optional[str]，返回非空原因 = 作废。

        **个人提一嘴只有在登记了复核者时才会发出去**（载荷契约见 _personal_mention_unreviewed）：
        没登记（生产者没起来、重启后没接上、旧队列）→ 一律作废，宁可少发一条。
        别的 preflight hook 不能冒充这一条。
        """
        self._personal_guard = hook if callable(hook) else None

    def set_group_file_hook(self, hook: Any) -> None:
        """挂群文件上传成功登记回调（fn(group_id, file_id, name, task_id)）。"""
        self._group_file_hook = hook

    def set_ask_hook(self, hook: Any) -> None:
        """挂「提问发出」回调：fn(key, group_id, task_id, message_id)。只在 text 发送成功、
        key 以 "ask:" 开头时调；出错只记日志，不影响发送。"""
        self._ask_hook = hook

    # ------------------------------------------------------------------
    # enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        key: str,
        group_id: str,
        kind: str,
        payload: dict,
        *,
        task_id: Optional[str] = None,
        not_before: float = 0,
    ) -> int:
        """入队；同 key 已存在 → 返回旧 id，不重复。"""
        key = str(key)
        row = self._store.read().execute(
            "SELECT id FROM outbox WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        now = clock.now()
        with self._store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO outbox (key, group_id, kind, payload, status, attempts,"
                " result, error, task_id, not_before, created, updated)"
                " VALUES (?, ?, ?, ?, 'pending', 0, '{}', '', ?, ?, ?, ?)",
                (
                    key,
                    str(group_id),
                    str(kind),
                    json.dumps(payload or {}, ensure_ascii=False),
                    str(task_id) if task_id is not None else None,
                    float(not_before or 0),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    def claim_delivery(self, group_id: str, task_id: str) -> str:
        """群友当场索取本群已完成任务：仅提升尚未发出的交付，不重传已发/不确定的原件。

        返回 queued/sent/sending/uncertain/failed/missing/not_ready/unserved/broken。
        在一次事务里复核归属与状态、改待发载荷并清掉旧的推迟时间。
        """
        gid, tid = str(group_id), str(task_id)
        if not self._get_settings().is_served(gid):
            return "unserved"  # 非服务群零库读取
        prefix = f"task:{tid}:deliver"
        with self._store.tx() as conn:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id=? AND group_id=?", (tid, gid)
            ).fetchone()
            if task is None:
                return "missing"  # 不透露别的群是否存在同 ID
            if task["status"] != "completed":
                return "not_ready"
            rows = conn.execute(
                "SELECT id, key, kind, status, payload FROM outbox"
                " WHERE group_id=? AND task_id=? AND (key=? OR key LIKE ?)",
                (gid, tid, prefix, f"{prefix}:%"),
            ).fetchall()
            artifact = [r for r in rows if _is_artifact_outbox_row(r["kind"], str(r["key"]))]
            # 兜底的「网页里还有副本」不是成品；只有已发布成品的说明可单独补发。
            pending = [r for r in artifact if r["status"] == "pending"]
            if any(r["status"] == "sent" for r in artifact):
                pending.extend(r for r in rows if r["status"] == "pending"
                               and str(r["key"]).endswith(":note"))
            if pending:
                payloads = []
                for row in pending:
                    try:
                        payload = json.loads(row["payload"] or "{}")
                    except (TypeError, ValueError):
                        return "broken"
                    if not isinstance(payload, dict):
                        return "broken"
                    payload["push_kind"] = "awaited_delivery"
                    payloads.append((row["id"], payload))
                now = clock.now()
                for oid, payload in payloads:
                    conn.execute(
                        "UPDATE outbox SET payload=?, not_before=0, error='', updated=?"
                        " WHERE id=? AND status='pending'",
                        (json.dumps(payload, ensure_ascii=False), now, int(oid)),
                    )
                return "queued"
            if any(r["status"] == "sent" for r in artifact):
                return "sent"
            if any(r["status"] == "sending" for r in artifact):
                return "sending"
            if any(r["status"] == "uncertain" for r in artifact):
                return "uncertain"
            if any(r["status"] == "failed" for r in artifact):
                return "failed"
            return "missing"

    # ------------------------------------------------------------------
    # flush
    # ------------------------------------------------------------------

    def _due_rows(self, now: float) -> list[Any]:
        return self._store.read().execute(
            "SELECT id, key, group_id, kind, payload, task_id, not_before FROM outbox"
            " WHERE status='pending' AND not_before<=? ORDER BY id",
            (float(now),),
        ).fetchall()

    def _set(self, oid: int, *, status: str, error: str = "", result: Optional[dict] = None,
             not_before: Optional[float] = None, moment: Optional[float] = None) -> None:
        with self._store.tx() as conn:
            fields = ["status=?", "error=?", "updated=?"]
            params: list[Any] = [status, error, float(moment) if moment is not None else clock.now()]
            if result is not None:
                fields.append("result=?")
                params.append(json.dumps(result, ensure_ascii=False))
            if not_before is not None:
                fields.append("not_before=?")
                params.append(float(not_before))
            params.append(int(oid))
            conn.execute(f"UPDATE outbox SET {', '.join(fields)} WHERE id=?", params)

    def _claim(self, oid: int, *, moment: Optional[float] = None) -> int:
        """pending → sending 的数据库 CAS：抢到返回累计尝试次数，抢不到返回 0。

        `WHERE id=? AND status='pending' AND not_before<=?`：另一个 flush（同一／
        另一个实例、另一个进程）已经把它改成 sending / sent / uncertain，或还没到点，
        都命中 0 行——调用方据此**明确跳过**，不发、不记账、不触发 hook。

        先落库再执行（崩了 recover 能捡到）；attempts 与状态一次事务写完，重启后接着算，
        不会出现「已经试过两次却还当成首发」这种重复发。
        """
        now = float(moment) if moment is not None else clock.now()
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE outbox SET status='sending', attempts=attempts+1, updated=?"
                " WHERE id=? AND status='pending' AND not_before<=?",
                (now, int(oid), now),
            )
            if int(cur.rowcount or 0) != 1:
                return 0
            row = conn.execute("SELECT attempts FROM outbox WHERE id=?", (int(oid),)).fetchone()
        return int(row["attempts"]) if row is not None else 0

    def _fire_result(self, row: Any, payload: dict, *, outcome: str,
                     error: str = "", result: Optional[dict] = None,
                     now: Optional[float] = None) -> None:
        """把发送结果交给生产者（挂上来的 hook）；任何 hook 出错都只记日志。"""
        if not self._result_hooks or row is None:
            return
        try:
            info = {
                "outbox_id": int(row["id"]),
                "key": str(row["key"]),
                "group_id": str(row["group_id"]),
                "kind": str(row["kind"]),
                "push_kind": str((payload or {}).get("push_kind") or ""),
                "payload": dict(payload or {}),
                "task_id": row["task_id"],
                "outcome": str(outcome),
                "error": str(error or ""),
                "result": dict(result or {}),
                # 这一轮 flush 的时间：生产者回写时间戳用它，跟发件箱记额度算的是同一天
                "ts": float(now) if now is not None else clock.now(),
            }
        except Exception:
            logger.exception("组装发送结果失败（发件 %s）", row["id"] if "id" in row.keys() else "?")
            return
        for hook in list(self._result_hooks):
            try:
                hook(info)
            except Exception:
                logger.exception("发送结果 hook 出错（key=%s，outcome=%s）", info["key"], outcome)

    def _group_quiet(self, group_id: str) -> tuple[Optional[str], str]:
        """这个群那份睡觉时段（group_push 真源）+ 读不出来的原因。

        返回 (quiet, why)：
        - 读到了可信的一份 → (quiet 字符串（可能是 ""，= 这个群没有静默窗口）, "")；
        - 读不到（配置口炸了 / get_config 炸了 / 判定拿不到证据）→ (None, 中文原因)，
          调用方只延 5 分钟再试，绝不拿猜出来的钟点当提前作废的依据。
        """
        try:
            settings = self._get_settings()
        except Exception:
            logger.debug("读配置失败（推迟时间按 5 分钟后重试，群 %s）", group_id, exc_info=True)
            return None, "现在读不出来"
        if settings is None:
            return None, "现在读不出来"
        try:
            cfg = group_push.get_config(self._store, str(group_id), settings)
        except Exception:
            logger.debug("读每群推送设置失败（群 %s），推迟时间按 5 分钟后重试", group_id,
                         exc_info=True)
            return None, "读不到设置"
        return str((cfg or {}).get("quiet_hours") or ""), ""

    def _postpone_soon(self, oid: int, reason: str, now: float, why: str) -> None:
        """读不到可信设置：只延 5 分钟再试，不猜钟点（绝不停到明天）。"""
        self._set(oid, status="pending", error=f"推迟：{reason}（{why}，5 分钟后再试）",
                  not_before=float(now) + _POSTPONE_RETRY_S, moment=now)

    def _hook_info(self, row: Any, payload: dict, now: float) -> dict:
        """给发送前 hook 的材料（形状同 add_result_hook 的 info，外加 now）。"""
        return {
            "outbox_id": int(row["id"]),
            "key": str(row["key"]),
            "group_id": str(row["group_id"]),
            "kind": str(row["kind"]),
            "push_kind": str((payload or {}).get("push_kind") or ""),
            "payload": dict(payload or {}),
            "task_id": row["task_id"],
            "now": float(now),
        }

    def _personal_review_reason(self, row: Any, payload: dict, now: float) -> str:
        """个人提一嘴发送前的**强制**复核：载荷契约 + 生产者登记的复核者。

        返回非空原因 = 作废；缺材料 / 没复核者 / 复核出错都失败关闭。
        """
        unreviewed = _personal_mention_unreviewed(payload)
        if unreviewed:
            return unreviewed
        hook = self._personal_guard
        if hook is None:
            return "个人提一嘴没有发送前复核者（生产者没接上）"
        try:
            info = self._hook_info(row, payload, now)
        except Exception:
            logger.exception("组装个人提一嘴复核材料失败（发件 %s）",
                             row["id"] if "id" in row.keys() else "?")
            return "个人提一嘴复核材料拼不出来，作废"
        try:
            reason = hook(info)
        except Exception:
            logger.exception("个人提一嘴发送前复核出错（key=%s），按不新鲜作废", info["key"])
            return "个人提一嘴发送前复核出错，作废"
        return str(reason) if reason else ""

    def _preflight(self, row: Any, payload: dict, now: float) -> str:
        """发送前新鲜度检查：返回非空原因 = 作废。hook 出错 → 失败关闭（作废）。"""
        if not self._preflight_hooks or row is None:
            return ""
        try:
            info = self._hook_info(row, payload, now)
        except Exception:
            logger.exception("组装发送前检查材料失败（发件 %s），按不新鲜作废",
                             row["id"] if "id" in row.keys() else "?")
            return "发送前检查材料拼不出来，作废"
        for hook in list(self._preflight_hooks):
            try:
                reason = hook(info)
            except Exception:
                logger.exception("发送前检查出错（key=%s），按不新鲜作废", info["key"])
                return "发送前检查出错，作废"
            if reason:
                return str(reason)
        return ""

    def _next_send_time(self, group_id: str, reason: str, now: float) -> tuple[Optional[float], str]:
        """推迟后「下一次能发」的时刻（**一次算到位**）+ 算不出来的原因。

        - 睡觉时段：本群那段 quiet 结束的钟点（跨夜 / 同日区间都按北京时间那一天算；
          每个群的醒来钟点可以不一样，绝不能拿全局 `delivery.quiet_hours` 去算）。
        - 每日额度用完：次日 00:00（额度按北京日期恢复）；那一刻若仍落在本群 quiet 里，
          就一次接着移到那段 quiet 结束（线上 #58：19 点额度用满 → 00:00 撞上
          00:00-07:00 的静默 → 直接到 07:00，不再排第二趟）。
        - 读不到那份设置 / 认不出原因 /（睡觉时段但 quiet 配错）→ (None, 中文原因)：
          调用方只延 5 分钟再试，绝不用猜出来的次日钟点提前作废。
        """
        reason_s = str(reason or "推送节制")
        now = float(now)
        if reason_s == _QUIET_REASON:
            quiet, why = self._group_quiet(group_id)
            if quiet is None:
                return None, why
            window = _quiet_window(quiet)
            if window is None:
                return None, "睡觉时段配错"
            return _quiet_end_after(now, window), ""
        if reason_s == _QUOTA_REASON:
            quiet, why = self._group_quiet(group_id)
            if quiet is None:
                return None, why
            t = clock.bj(now)
            target = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400.0
            window = _quiet_window(quiet)
            if window is not None and clock.in_range(target, window):
                target = _quiet_end_after(target, window)
            return target, ""
        if reason_s == UNREADABLE_REASON:
            return None, "现在读不出来"
        return None, "认不出这个原因"

    def _payload_for(self, oid: int, payload: Optional[dict]) -> dict:
        """拿这条的载荷：调用方给了就用（flush 已经解过一次）；没给才读一次库。"""
        if isinstance(payload, dict):
            return payload
        try:
            row = self._store.read().execute(
                "SELECT payload FROM outbox WHERE id=?", (int(oid),)
            ).fetchone()
            data = json.loads((row["payload"] if row is not None else "") or "{}")
        except Exception:
            logger.debug("读发件载荷失败（发件 %s）", int(oid), exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def _event(self, kind: str, oid: int, group_id: str, body: dict) -> None:
        """延期 / 作废留一条 events：只有原因和目标时刻，**不含**载荷 / 链接 / token / 正文。

        纯留痕：写不进去只记日志，绝不影响推迟 / 作废本身。
        """
        try:
            with self._store.tx() as conn:
                self._store.event(conn, kind, group_id=str(group_id),
                                  entity="outbox", entity_id=str(int(oid)), payload=dict(body))
        except Exception:
            logger.debug("写发件事件失败（%s，发件 %s）", kind, int(oid), exc_info=True)

    def _drop_for_ttl(self, oid: int, group_id: str, reason: str, now: float, body: dict, *,
                      expires: float, target: Optional[float], why: str = "") -> None:
        """推迟后的发送时刻已经晚于期限 → 提前作废，如实通知生产者（每次只通知一次）。

        不进发送闸、不占额度、不改 TTL：只是把「反正发不出去」提前说清楚，免得陈旧内容
        压在队里等到过了点才被发现（线上 #58 就拖到了次日 07:00）。
        """
        if target is not None:
            detail = f"{_TTL_EXPIRED_REASON}（{reason}推迟到 {_fmt_bj(target)} 也晚于期限）"
        else:
            detail = f"{_TTL_EXPIRED_REASON}（{reason}：{why}，等到下一次 flush 也来不及）"
        logger.info("发件 %s 提前作废（群 %s）：%s；期限 %s", int(oid), group_id, detail,
                    _fmt_bj(expires))
        self._set(oid, status="dropped", error=f"作废：{detail}", moment=now)
        self._event(_EVENT_TTL_DROP, oid, group_id,
                    {"reason": _TTL_EXPIRED_REASON, "postpone_reason": str(reason),
                     "target_ts": float(target) if target is not None else None,
                     "expires_ts": float(expires)})
        self._fire_result(_row_after(self._store, oid), dict(body or {}),
                          outcome="dropped", error=detail, now=now)

    def _postpone(self, oid: int, group_id: str, reason: str, now: float, *,
                  payload: Optional[dict] = None) -> None:
        """推到「下一次能发」的时刻；顺带盯住有效期，别把陈旧内容留在队里。

        目标时刻一次算到位（见 _next_send_time），原因写 error，状态仍 pending。
        算出的发送时刻**严格晚于** payload.expires_ts（跟发送前判定同一口径：
        now > expires 才算过期，取等号仍算有效）→ 这条已经没法新鲜地发出去，当场作废
        （dropped）并把结果交给生产者（_fire_result）：**不延长 TTL、不提高额度、
        不改旧记录**。没有有效期的载荷（任务交付等）不受这一条影响。

        读不到那份设置 / 认不出原因 → 只延 5 分钟再试，绝不用猜出来的次日钟点提前作废；
        这条 5 分钟只做**有界检查**：下一次 flush 最早也在 now + 5 分钟，那时已经过期
        才当场作废，不白等一趟。
        """
        now = float(now)
        reason_s = str(reason or "推送节制")
        body = self._payload_for(oid, payload)
        expires = _expires_ts(body)
        target, why = self._next_send_time(group_id, reason_s, now)
        if target is None:
            if expires is not None and now + _POSTPONE_RETRY_S > expires:
                self._drop_for_ttl(oid, group_id, reason_s, now, body,
                                   expires=expires, target=None, why=why)
                return
            self._postpone_soon(oid, reason_s, now, why)
            return
        if expires is not None and target > expires:
            self._drop_for_ttl(oid, group_id, reason_s, now, body,
                               expires=expires, target=target)
            return
        logger.info("发件 %s 推迟（群 %s）：%s → %s", int(oid), group_id, reason_s,
                    _fmt_bj(target))
        self._event(_EVENT_POSTPONE, oid, group_id,
                    {"reason": reason_s, "target_ts": float(target)})
        self._set(oid, status="pending", error=f"推迟：{reason_s}",
                  not_before=float(target), moment=now)

    def _session_for_group(self, group_id: str) -> str:
        row = self._store.read().execute(
            "SELECT session_id FROM groups WHERE group_id=? LIMIT 1", (str(group_id),)
        ).fetchone()
        if row is None or not row["session_id"]:
            raise HostError(f"groups 表里没有 {group_id} 的 session_id")
        return str(row["session_id"])

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        if isinstance(exc, asyncio.TimeoutError):
            return True
        return "超时" in str(exc)

    def _check_upload_path(self, raw_path: str) -> Path:
        """群文件上传前检查（S3）：
        - 不能是符号链接（lstat 判断，线上 root 跟随链接会把工作区外文件传进群）；
        - resolve 后必须在 workspace_root 下。
        不合法抛 HostError（中文），调用方按普通失败走 failed + 回落。
        """
        p = Path(raw_path)
        if p.is_symlink():
            raise HostError(f"群文件路径是符号链接，不传：{raw_path}")
        try:
            resolved = p.resolve()
        except OSError as e:
            raise HostError(f"群文件路径解析失败：{raw_path}（{e}）") from None
        try:
            root = Path(getattr(self._get_settings(), "workspace_root", "")).resolve()
        except Exception:
            root = Path("").resolve()
        if resolved != root and root not in resolved.parents:
            raise HostError(f"群文件路径不在工作区根目录下，不传：{raw_path}")
        return resolved

    def _check_image_path(self, raw_path: str) -> Path:
        """图片发送前检查：不能是符号链接、resolve 后必须在 workspace_root 下、是普通文件、
        大小在 1 字节 ~ 8MB 之间、按内容（PNG 魔数）判类型。

        不合法抛 OutboxPayloadError（中文）：这种错误重试多少次都一样，直接判失败，
        不浪费那次自动重试。
        """
        text = str(raw_path or "").strip()
        if not text:
            raise OutboxPayloadError("图片条目没写路径，不发")
        p = Path(text)
        if p.is_symlink():
            raise OutboxPayloadError(f"图片路径是符号链接，不发：{text}")
        try:
            resolved = p.resolve()
        except OSError as e:
            raise OutboxPayloadError(f"图片路径解析失败：{text}（{e}）") from None
        try:
            root = Path(getattr(self._get_settings(), "workspace_root", "")).resolve()
        except Exception:
            root = Path("").resolve()
        if resolved != root and root not in resolved.parents:
            raise OutboxPayloadError(f"图片路径不在工作区根目录下，不发：{text}")
        if not resolved.is_file():
            raise OutboxPayloadError(f"图片不是普通文件（或不存在），不发：{text}")
        try:
            size = resolved.stat().st_size
        except OSError as e:
            raise OutboxPayloadError(f"图片读不到大小：{text}（{e}）") from None
        if size <= 0 or size > _IMAGE_MAX_BYTES:
            raise OutboxPayloadError(f"图片大小不合适（{size} 字节，上限 {_IMAGE_MAX_BYTES}），不发")
        try:
            with resolved.open("rb") as fh:
                head = fh.read(len(_PNG_MAGIC))
        except OSError as e:
            raise OutboxPayloadError(f"图片读不出来：{text}（{e}）") from None
        if head != _PNG_MAGIC:
            raise OutboxPayloadError(f"图片不是 PNG（按内容判），不发：{text}")
        return resolved

    async def _execute(self, row: Any) -> dict:
        """执行一条；成功返回 result dict；失败抛异常。"""
        kind = row["kind"]
        payload = json.loads(row["payload"] or "{}")
        gid = row["group_id"]
        if kind == "text":
            session_id = self._session_for_group(gid)
            # at 透传：群里有真正的 @（Telegram 由 host 退成正文「@名字 」）。
            # 空的时候不传这两个参数，保持老调用形状（只想发一条普通文字时不多给参数）。
            extra = {}
            at_user = str(payload.get("at_user") or "")
            at_name = str(payload.get("at_name") or "")
            if not at_user and not at_name and self._is_delivery_text(payload):
                # 交付说明消息带上发起人（只 @ 本人、不加别的信息）；缺失 → 保持老行为
                who = self._requester_at(gid, _row_field(row, "task_id"))
                at_user = str(who.get("at_user") or "")
                at_name = str(who.get("at_name") or "")
            if at_user or at_name:
                extra = {"at_user": at_user, "at_name": at_name}
            res = await self._host.send_text(
                session_id,
                str(payload.get("text") or ""),
                reply_to=str(payload.get("reply_to") or ""),
                **extra,
            )
            return {"message_id": str(getattr(res, "message_id", "") or "")}
        if kind == "image":
            safe = self._check_image_path(str(payload.get("path") or ""))
            try:
                data = safe.read_bytes()
            except OSError as e:
                raise OutboxPayloadError(f"图片读不出来：{safe}（{e}）") from None
            session_id = self._session_for_group(gid)
            res = await self._host.send_image(
                session_id, data, text=str(payload.get("text") or "")
            )
            return {"message_id": str(getattr(res, "message_id", "") or "")}
        if kind == "file":
            safe_path = self._check_upload_path(str(payload.get("path") or ""))
            file_id = await self._host.upload_group_file(
                gid, str(safe_path), str(payload.get("name") or "")
            )
            return {"file_id": str(file_id)}
        if kind == "herenow":
            if self._herenow is None:
                raise HostError("没配 here.now，发不了网页链接")
            pub = await self._herenow.publish(Path(str(payload.get("dir") or "")))
            return {"url": str(pub.get("url") or ""), "slug": str(pub.get("slug") or "")}
        raise HostError(f"未知的发件类型：{kind}")

    def _after_sent(self, row: Any, payload_like: Optional[dict] = None) -> None:
        """发送成功后的连贯动作：群文件补说明 / here.now 发链接 / 记备忘。"""
        kind = row["kind"]
        try:
            payload = payload_like if payload_like is not None else json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        gid = row["group_id"]
        key = row["key"]
        tid = row["task_id"]
        follow_kind = ("awaited_delivery" if payload.get("push_kind") == "awaited_delivery"
                       else "delivery")
        if kind == "file":
            note = str(payload.get("note") or "").strip()
            name = str(payload.get("name") or "")
            if note:
                self.enqueue(
                    f"{key}:note",
                    gid,
                    "text",
                    # follow_up_of：这条是同一件交付自动补的说明，不是新的一次主动推送
                    # （不占第二份额度、也不再过一遍推送闸）
                    {"text": note, "push_kind": follow_kind, "follow_up_of": int(row["id"])},
                    task_id=tid,
                )
            text = f"刚在群里发了文件「{name}」，有人问起可以告诉他：{note}" if note \
                else f"刚在群里发了文件「{name}」，有人问起可以告诉他在群文件里找"
            self._mentions.add(gid, text, key=f"deliver:{int(row['id'])}", ttl_s=_DELIVER_MENTION_TTL_S)
        elif kind == "herenow":
            url = ""
            try:
                url = str(json.loads(self._get_result(row["id"])).get("url") or "")
            except Exception:
                url = ""
            note = str(payload.get("note") or "").strip()
            if url:
                text_out = f"{note}\n{url}" if note else url
                self.enqueue(
                    f"{key}:note",
                    gid,
                    "text",
                    {"text": text_out, "push_kind": follow_kind, "follow_up_of": int(row["id"])},
                    task_id=tid,
                )
            memo = f"刚在群里发了网页链接 {url}"
            if note:
                memo += f"（{note}）"
            memo += "，有人问起可以把链接再给他；链接 24 小时后过期，MaiWork 网页里还有副本"
            self._mentions.add(gid, memo, key=f"deliver:{int(row['id'])}", ttl_s=_DELIVER_MENTION_TTL_S)
        elif kind == "text":
            text = str(payload.get("text") or "")
            if text:
                self._mentions.add(
                    gid,
                    f"刚在群里说了：{text[:80]}",
                    key=f"deliver:{int(row['id'])}",
                    ttl_s=_DELIVER_MENTION_TTL_S,
                )

    def _get_result(self, oid: int) -> str:
        row = self._store.read().execute(
            "SELECT result FROM outbox WHERE id=?", (int(oid),)
        ).fetchone()
        return str(row["result"]) if row is not None else "{}"

    # ------------------------------------------------------------------
    # 交付说明 @ 发起人（docs/06：QQ 走真 at 段，Telegram 退成正文「@名字 」）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_delivery_text(payload: dict) -> bool:
        """这条文字是不是「交付说明」（只发文字的交付 / note / note+链接）。"""
        return str((payload or {}).get("push_kind") or "") in _DELIVERY_PUSH_KINDS

    def _requester_at(self, group_id: str, task_id: Any) -> dict:
        """任务发起人的 at 参数：{"at_user", "at_name"}。

        只按**本群**这个任务查（不跨群）；发起人 id 缺失 / 查不到 / 读库失败 → {}，
        调用方保持老行为（不 @）。显示名优先用名册当前名，查不到回落任务里的老快照。
        """
        gid = str(group_id or "").strip()
        tid = str(task_id or "").strip()
        if not gid or not tid:
            return {}
        try:
            row = self._store.read().execute(
                "SELECT requester_id, requester_name FROM tasks WHERE id=? AND group_id=?",
                (tid, gid),
            ).fetchone()
        except Exception:
            logger.debug("读任务发起人失败（任务 %s），这条不 @", tid, exc_info=True)
            return {}
        if row is None:
            return {}
        uid = str(row["requester_id"] or "").strip()
        if not uid:
            return {}
        name = str(row["requester_name"] or "").strip()
        try:
            name = str(members.name_of(self._store, gid, uid, fallback=name) or "").strip()
        except Exception:
            logger.debug("读名册名字失败（任务 %s），用老快照", tid, exc_info=True)
        return {"at_user": uid, "at_name": name}

    def _task_title(self, task_id: Any) -> str:
        """任务标题（回落页的 h1）；查不到 → ""。"""
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        try:
            row = self._store.read().execute(
                "SELECT title FROM tasks WHERE id=?", (tid,)
            ).fetchone()
        except Exception:
            return ""
        return str(row["title"] or "").strip() if row is not None else ""

    def _task_artifact_root(self, row: Any) -> Optional[Path]:
        """这个任务的成品目录 <工作区>/artifacts/<任务号>；查不到 → None。"""
        tid = _row_field(row, "task_id").strip()
        if not tid:
            return None
        try:
            task = self._store.read().execute(
                "SELECT workspace FROM tasks WHERE id=?", (tid,)
            ).fetchone()
        except Exception:
            return None
        if task is None:
            return None
        ws_name = str(task["workspace"] or "").strip()
        if not ws_name:
            return None
        root = Path(getattr(self._get_settings(), "workspace_root", Path("data/workspaces")))
        try:
            return (root / ws_name / "artifacts" / tid).resolve()
        except OSError:
            return None

    @staticmethod
    def _pick_fallback_page(src: Path, artifact_root: Optional[Path]) -> Optional[Path]:
        """回落页主体：成品自己的 .html → 同名主干的 .html（X.docx → X.html）→ 成品目录根 index.html。

        只认普通文件、不认符号链接（和工作区其它闸同一个口径）；都没有 → None（现生成页面）。
        """
        def ok(p: Optional[Path]) -> bool:
            if p is None:
                return False
            try:
                return p.is_file() and not p.is_symlink()
            except OSError:
                return False

        if ok(src) and src.suffix.lower() in _PAGE_SUFFIXES:
            return src
        if src.suffix:
            for suf in _PAGE_SUFFIXES:
                cand = src.with_suffix(suf)
                if ok(cand):
                    return cand
        if artifact_root is not None:
            for suf in _PAGE_SUFFIXES:
                cand = artifact_root / f"index{suf}"
                if ok(cand):
                    return cand
        return None

    def _fallback_dir(self) -> Path:
        """回落材料（zip、附件页）的临时存放处：workspace_root/.web/.fallback。"""
        settings = self._get_settings()
        root = Path(getattr(settings, "workspace_root", Path("data/workspaces")))
        d = root / ".web" / ".fallback"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _enqueue_fallback(self, row: Any) -> None:
        """首选 file/herenow 失败 → 自动入队回落项；回落描述不全 → 兜底说明。"""
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        fb = payload.get("fallback") if isinstance(payload.get("fallback"), dict) else None
        gid = row["group_id"]
        tid = row["task_id"]
        if not fb:
            self.enqueue(
                f"{row['key']}:webonly",
                gid,
                "text",
                {
                    "text": "做好了，但发群文件和网页都没成功，成品在 MaiWork 网页里",
                    "push_kind": str(payload.get("push_kind") or "delivery"),
                },
                task_id=tid,
            )
            return
        fb_kind = str(fb.get("kind") or "")
        fb_note = str(fb.get("note") or "")
        push_kind = str(payload.get("push_kind") or "delivery")
        try:
            if fb_kind == "file":
                fb_path = str(fb.get("path") or "")
                fb_name = str(fb.get("name") or "")
                # 目录 → 打 zip（比如 view 回落群文件时给的是目录）
                if Path(fb_path).is_dir():
                    zdir = self._fallback_dir()
                    zpath = zdir / f"delivery-{int(row['id'])}.zip"
                    src_root = Path(fb_path).resolve()
                    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                        for p in sorted(Path(fb_path).rglob("*")):
                            # 符号链接一律跳过（S3：线上是 root，跟随链接会把工作区外的
                            # 文件发进群文件）；resolve 后必须在打包目录内
                            if p.is_symlink():
                                logger.warning("打 zip 跳过符号链接：%s", p)
                                continue
                            if p.is_file():
                                try:
                                    resolved = p.resolve()
                                except OSError:
                                    continue
                                if resolved != src_root and src_root not in resolved.parents:
                                    logger.warning("打 zip 跳过解析到目录外的文件：%s → %s", p, resolved)
                                    continue
                                zf.write(p, p.relative_to(fb_path))
                    fb_path = str(zpath)
                    if not fb_name.endswith(".zip"):
                        fb_name = (fb_name or "成品") + ".zip"
                self.enqueue(
                    f"{row['key']}:fallback",
                    gid,
                    "file",
                    {"path": fb_path, "name": fb_name, "note": fb_note, "push_kind": push_kind},
                    task_id=tid,
                )
            elif fb_kind == "herenow":
                # 群文件发不出去 → here.now 回落页：优先用同目录的手机版网页当主体
                # （T-11 的教训），没有就现生成一个手机友好页；原文件同批发布并保留
                # 下载链接（页面里绝不放工作区路径 / artifacts/ 这类内部路径）。
                src = Path(str(fb.get("path") or ""))
                name = str(fb.get("name") or src.name or "附件")
                d = self._fallback_dir() / f"hn-{int(row['id'])}"
                if d.exists():
                    shutil.rmtree(d)
                d.mkdir(parents=True, exist_ok=True)
                copied = False
                size: Optional[int] = None
                if src.is_file() and not src.is_symlink():
                    # 符号链接不复制（S3：会跟着链接读到工作区外的文件）
                    shutil.copyfile(src, d / name)
                    copied = True
                    try:
                        size = int(src.stat().st_size)
                    except OSError:
                        size = None
                page_src = self._pick_fallback_page(src, self._task_artifact_root(row))
                page = ""
                if page_src is not None:
                    try:
                        page = _read_page_text(page_src)
                    except OSError as e:
                        logger.warning("回落页读不出来，改用生成的页面：%s（%s）", page_src, e)
                        page = ""
                href = _url_quote(name) if copied else ""
                if not page.strip():
                    # 没有可用网页 → 现生成一个手机能看的页面（标题 / 说明 / 文件名+大小 / 下载按钮）
                    page = _mobile_delivery_page(
                        title=self._task_title(_row_field(row, "task_id")) or name,
                        note=fb_note,
                        name=name,
                        size=size,
                        href=href,
                    )
                elif copied and name and name not in page:
                    # 那份网页自己没链原文件 → 补一个下载条，别让下载入口丢了
                    page = _append_download_button(page, name)
                (d / "index.html").write_text(page, encoding="utf-8")
                self.enqueue(
                    f"{row['key']}:fallback",
                    gid,
                    "herenow",
                    {"dir": str(d), "note": fb_note, "push_kind": push_kind},
                    task_id=tid,
                )
            else:
                raise HostError(f"未知的回落类型：{fb_kind}")
        except Exception as e:  # 回落准备本身就坏了 → 兜底说明
            logger.warning("准备回落失败: %s", e)
            self.enqueue(
                f"{row['key']}:webonly",
                gid,
                "text",
                {
                    "text": "做好了，但发群文件和网页都没成功，成品在 MaiWork 网页里",
                    "push_kind": push_kind,
                },
                task_id=tid,
            )

    def _repair_sent_followups(self, served: set[str]) -> None:
        """发送已成功却在补说明/备忘前崩溃：只补幂等后续，不重发原件。"""
        rows = []
        for gid in served:
            rows.extend(self._store.read().execute(
                "SELECT id, key, group_id, kind, payload, task_id, status, result, updated FROM outbox"
                " WHERE group_id=? AND status='sent' AND kind IN ('file', 'herenow', 'text') ORDER BY id",
                (gid,),
            ).fetchall())
        for row in rows:
            gid = str(row["group_id"])
            kind = str(row["kind"])
            key = str(row["key"])
            if gid not in served or (kind == "text" and not key.endswith(":deliver:text")):
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
                result = json.loads(row["result"] or "{}")
                need_note = ((kind == "file" and bool(str(payload.get("note") or "").strip()))
                             or (kind == "herenow" and bool(result.get("url"))))
                note_exists = self._store.read().execute(
                    "SELECT 1 FROM outbox WHERE key=?", (f"{key}:note",)
                ).fetchone() is not None
                recent = clock.now() - float(row["updated"] or 0) <= _DELIVER_MENTION_TTL_S
                memo_exists = self._store.read().execute(
                    "SELECT 1 FROM mentions WHERE group_id=? AND key=?",
                    (gid, f"deliver:{int(row['id'])}"),
                ).fetchone() is not None
                if (need_note and not note_exists) or (recent and not memo_exists):
                    self._after_sent(row, payload)
            except Exception:
                logger.exception("补交付说明/备忘失败（发件 %s），下轮再试", row["id"])

    def _lock(self) -> asyncio.Lock:
        """本实例的 flush 锁：按当前事件循环懒建（换循环就换一把，避免跨循环复用）。"""
        loop = asyncio.get_running_loop()
        if self._flush_lock is None or self._flush_lock_loop is not loop:
            self._flush_lock = asyncio.Lock()
            self._flush_lock_loop = loop
        return self._flush_lock

    async def flush(self, now: float, *, allowed_groups: Any = None) -> None:
        """把到期（not_before≤now）的 pending 逐条处理。后台循环调。

        allowed_groups：只发这些群的（None = 现查 settings.is_served）。
        非服务群的 pending 留在原地（由 app 的回收逻辑标 cancelled，不删数据）——
        非服务群零发送是红线。

        并发（2026-10 复审）：同一实例的 flush 串行（asyncio 锁）；跨实例 / 跨进程
        由 `_claim` 的数据库 CAS 兜底——同一个 key 全场只发一次。
        """
        async with self._lock():
            await self._flush_locked(float(now), allowed_groups=allowed_groups)

    async def _flush_locked(self, now: float, *, allowed_groups: Any = None) -> None:
        if allowed_groups is None:
            try:
                settings = self._get_settings()
                allowed_groups = set(getattr(settings, "groups", {}) or {})
            except Exception:
                # 读配置失败：一个群都不发（也不 seed），不猜服务群
                logger.debug("读配置失败（flush 本轮不发任何群）", exc_info=True)
                allowed_groups = set()
        served = {str(g) for g in allowed_groups}
        self._repair_sent_followups(served)
        # 抢不到（CAS 0 行）的行：这一轮不再碰，避免反复重试同一行
        skipped: set[int] = set()
        while True:
            rows = [r for r in self._due_rows(now)
                    if str(r["group_id"]) in served and int(r["id"]) not in skipped]
            if not rows:
                return
            row = rows[0]
            oid = int(row["id"])
            gid = str(row["group_id"])
            kind = str(row["kind"])
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                payload = {}
            push_kind = str(payload.get("push_kind") or "delivery")
            # 任务自己的消息（task_id 非空 + push_kind ∈ delivery/status，含交付自动补的
            # 说明行）在闸门与留痕里换成 quota-free kind：不吃每日额度、不占额度名额
            # （用户 2026-10-10 定的口径，见 delivery.TASK_QUOTA_FREE_KINDS）。
            # 载荷里的 push_kind 一个字不改，下游（@ 发起人 / 说明行）照旧认它。
            gate_kind = task_quota_free_kind(row["task_id"], push_kind)
            # 同一件交付自动补的说明（file → note）：跟着已经出去的那条走，
            # 不占第二份额度、也不再过一遍闸（不然说明会被推到第二天）。
            follow_up = bool(payload.get("follow_up_of"))

            # 自动消息的期限：到点还没发出去（睡觉时段 / 每日上限推迟了几天）就作废，
            # 绝不把陈旧的开场白 / 资讯卡片 / 提一嘴发进群。
            ttl_reason = _ttl_expired(payload, now)
            if ttl_reason:
                self._set(oid, status="dropped", error=f"作废：{ttl_reason}", moment=now)
                self._fire_result(_row_after(self._store, oid), payload,
                                  outcome="dropped", error=ttl_reason, now=now)
                continue

            # 个人提一嘴的强制复核（载荷契约 + 生产者登记的复核者）：没材料 / 没复核者 /
            # 复核不过 → 作废。纯代码、不调模型，放在通用新鲜度检查之前。
            if _is_personal_mention(str(row["key"]), payload):
                personal_reason = self._personal_review_reason(row, payload, now)
                if personal_reason:
                    self._set(oid, status="dropped", error=f"作废：{personal_reason}", moment=now)
                    self._fire_result(_row_after(self._store, oid), payload,
                                      outcome="dropped", error=personal_reason, now=now)
                    continue

            # 生产者自己的新鲜度检查（打开场白：排队期间群里又有人说话 → 作废）。
            stale_reason = self._preflight(row, payload, now)
            if stale_reason:
                self._set(oid, status="dropped", error=f"作废：{stale_reason}", moment=now)
                self._fire_result(_row_after(self._store, oid), payload,
                                  outcome="dropped", error=stale_reason, now=now)
                continue

            # 单一节制入口：明确领取与故障/指令的豁免由 Pushes 决定；
            # 每群开关也在这里查（待发期间关开关 → 直接作废，不发陈旧的）。
            # 任务自己的消息用 quota-free kind 问闸门（只看服务群 + 睡觉时段）。
            if not follow_up:
                ok_push, reason = self._pushes.can_push(gid, gate_kind, now)
                if not ok_push:
                    if reason == _DROP_REASON:
                        self._set(oid, status="dropped", error=f"作废：{reason}", moment=now)
                        self._fire_result(_row_after(self._store, oid), payload,
                                          outcome="dropped", error=reason, now=now)
                        continue
                    self._postpone(oid, gid, reason or "推送节制", now, payload=payload)
                    continue

            # 先落库 sending（CAS：只有仍是 pending 且到点的行才抢得到），再执行（崩了 recover 能捡到）
            attempts = self._claim(oid, moment=now)
            if attempts <= 0:
                # 另一个 flush / 另一个发件箱实例已经把它拿走或改过状态：明确跳过，
                # 不发、不记账、不触发结果 hook（同一 key 全场只发一次）
                logger.debug("发件 %s 已被其它 flush 抢先处理，本轮跳过", oid)
                skipped.add(oid)
                continue
            try:
                result = await self._execute(row)
            except Exception as exc:
                err = _redact(str(exc), [])
                if self._is_timeout(exc):
                    # 超时：可能已经发出去了，绝不重发
                    self._set(oid, status="uncertain", error=err, moment=now)
                    self._fire_result(_row_after(self._store, oid), payload,
                                      outcome="uncertain", error=err, now=now)
                    continue
                if (kind in _RETRY_SAFE_KINDS and not isinstance(exc, OutboxPayloadError)
                        and attempts < _MAX_ATTEMPTS):
                    # 安全失败（不是超时、载荷也没毛病）：有界地重试一次
                    self._set(oid, status="pending", error=f"发送失败，稍后重试一次：{err}",
                              not_before=now + _RETRY_DELAY_S, moment=now)
                    self._fire_result(_row_after(self._store, oid), payload,
                                      outcome="retrying", error=err, now=now)
                    continue
                self._set(oid, status="failed", error=err, moment=now)
                if kind in ("file", "herenow"):
                    self._enqueue_fallback(_row_after(self._store, oid))
                self._fire_result(_row_after(self._store, oid), payload,
                                  outcome="failed", error=err, now=now)
                continue
            self._set(oid, status="sent", result=result, moment=now)
            fresh = _row_after(self._store, oid)
            # 提问回执：ask:{task_id}:{attempt} 的提问发出去了，把 QQ 消息 ID 交回
            # （回复那条提问 → 恢复 waiting_input / shelved 任务；app 挂了钩子才有动作）
            if kind == "text" and str(row["key"]).startswith("ask:") and self._ask_hook is not None:
                try:
                    mid = str(result.get("message_id") or "").strip()
                    if mid:
                        self._ask_hook(str(row["key"]), gid, row["task_id"], mid)
                except Exception:
                    logger.exception("提问回执 hook 出错（群 %s），不影响发送", gid)
            # 群空间：群文件传成功了，登记进 group_files_owned（防手滑只动自己传的）
            if kind == "file" and self._group_file_hook is not None:
                try:
                    fid = str(result.get("file_id") or "").strip()
                    if fid:
                        self._group_file_hook(
                            gid, fid, str(payload.get("name") or ""), row["task_id"]
                        )
                except Exception:
                    logger.exception("群文件登记 hook 出错（群 %s），不影响发送", gid)
            if not follow_up:
                try:
                    # 留痕按闸门口径记：任务自己的消息记 task_delivery / task_status，
                    # used_today 不会把它算进群额度（载荷里的 push_kind 不动）。
                    self._pushes.record(
                        gid, gate_kind,
                        str(payload.get("text") or payload.get("note") or kind), now,
                    )
                except Exception:
                    logger.exception("pushes.record 失败")
            try:
                self._after_sent(_RowWithResult(fresh, result), payload)
            except Exception:
                logger.exception("交付后续动作失败（说明/备忘）")
            # 真的发出去了：生产者（开场白 / 资讯卡片 / 构想提一嘴）在这里回写自己的表
            self._fire_result(_RowWithResult(fresh, result), payload, outcome="sent",
                              result=result, now=now)

    # ------------------------------------------------------------------
    # retry / recover / cancel_group_pending
    # ------------------------------------------------------------------

    def cancel_group_pending(self, group_id: str, *, reason: str) -> int:
        """这个群不再服务：pending 的仍标 cancelled（不删数据）。返回改了几条。

        只动 pending——sending 的上次中断由 recover 管，sent/failed/uncertain 是历史。
        """
        gid = str(group_id)
        now = clock.now()
        reason_s = str(reason or "")[:_ERR_MAX]
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE outbox SET status='cancelled', error=?, updated=?"
                " WHERE group_id=? AND status='pending'",
                (reason_s, now, gid),
            )
            n = int(cur.rowcount or 0)
            rows = conn.execute(
                "SELECT key FROM outbox WHERE group_id=? AND status='cancelled' AND updated=?",
                (gid, now),
            ).fetchall()
            for r in rows:
                self._store.event(
                    conn, "outbox.cancelled", group_id=gid,
                    entity="outbox", entity_id=str(r["key"]),
                    payload={"reason": reason_s},
                )
        return n

    def retry(self, outbox_id: int, *, force: bool = False) -> None:
        """网页手动重发：只允许 failed / uncertain → pending。

        kind=file 且 status=uncertain 的：上传可能已经成功（群文件上传不幂等，
        自动/手动盲目重发都可能再传一份）。默认拒绝；force=True（网页管理员明确
        确认过「群里其实没有」）才允许。
        """
        row = self._store.read().execute(
            "SELECT status, kind FROM outbox WHERE id=?", (int(outbox_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"发件箱里没有这条：{outbox_id}")
        if row["status"] not in ("failed", "uncertain"):
            raise ValueError(f"只有失败或不确定的才能重发（现在是 {row['status']}）")
        if str(row["kind"]) == "file" and str(row["status"]) == "uncertain" and not force:
            raise ValueError("群文件可能已发出，请先到群里确认；确认没发出来才能强制重发")
            raise ValueError(f"只有失败或不确定的才能重发（现在是 {row['status']}）")
        with self._store.tx() as conn:
            conn.execute(
                "UPDATE outbox SET status='pending', attempts=attempts+1,"
                " not_before=0, updated=? WHERE id=?",
                (clock.now(), int(outbox_id)),
            )

    def recover(self) -> int:
        """插件重启：sending（上次中断的）→ uncertain，不重放。返回改了几条。"""
        with self._store.tx() as conn:
            cur = conn.execute(
                "UPDATE outbox SET status='uncertain',"
                " error='插件重启时发送中断，标为不确定，不自动重发', updated=?"
                " WHERE status='sending'",
                (clock.now(),),
            )
            return int(cur.rowcount or 0)


# ----------------------------------------------------------------------
# 行包装小工具
# ----------------------------------------------------------------------


def _row_after(store: Store, oid: int) -> Any:
    return store.read().execute(
        "SELECT id, key, group_id, kind, payload, task_id, status, result FROM outbox WHERE id=?",
        (int(oid),),
    ).fetchone()


class _RowWithResult:
    """把刚拿到的 result dict 贴到行上，给 _after_sent 用。"""

    def __init__(self, row: Any, result: dict) -> None:
        self._row = row
        self._result = result

    def __getattr__(self, name: str) -> Any:
        if name == "result":
            return json.dumps(self._result, ensure_ascii=False)
        return self._row[name] if name in self._row.keys() else getattr(self._row, name)

    def __getitem__(self, name: str) -> Any:
        if name == "result":
            return json.dumps(self._result, ensure_ascii=False)
        return self._row[name]


# ----------------------------------------------------------------------
# Delivery：任务成品交付
# ----------------------------------------------------------------------


class Delivery:
    """按成品类型挑渠道：view → here.now 先、群文件回落；file → 群文件先、here.now 回落。

    本类只入队首选 + 拼回落描述；真正发和自动回落在 Outbox.flush 里。
    tasks 表的 delivery/undelivered 字段由调用方（coordinator/网页）用
    delivery_records / undelivered 的结果更新。
    """

    def __init__(self, store: Store, outbox: Outbox, tasks_getter: Any = None) -> None:
        self._store = store
        self._outbox = outbox
        self._tasks_getter = tasks_getter

    def _group_of_task(self, task_id: str) -> str:
        if self._tasks_getter is not None:
            try:
                t = self._tasks_getter.get(task_id)
                if isinstance(t, dict) and t.get("group_id"):
                    return str(t["group_id"])
            except Exception:
                pass
        row = self._store.read().execute(
            "SELECT group_id FROM tasks WHERE id=?", (str(task_id),)
        ).fetchone()
        return str(row["group_id"]) if row is not None else ""

    def _staging_dir(self) -> Path:
        settings = self._outbox._get_settings()
        root = Path(getattr(settings, "workspace_root", Path("data/workspaces")))
        d = root / ".web" / ".staging"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _workspace_name_of(self, task_id: str, gid: str) -> str:
        """任务钉的工作区名；查不到回落 settings.workspace_of(gid)（再不行 "g<群号>"）。"""
        try:
            row = self._store.read().execute(
                "SELECT workspace FROM tasks WHERE id=?", (str(task_id),)
            ).fetchone()
            if row is not None and str(row["workspace"] or "").strip():
                return str(row["workspace"]).strip()
        except Exception:
            pass
        try:
            fn = getattr(self._outbox._get_settings(), "workspace_of", None)
            if callable(fn):
                name = str(fn(gid) or "").strip()
                if name:
                    return name
        except Exception:
            pass
        return f"g{gid}"

    def _task_artifact_dir(self, task_id: str, gid: str) -> Path:
        """这个任务的成品目录 <工作区>/artifacts/<task_id>（resolve 后的绝对路径）。"""
        settings = self._outbox._get_settings()
        root = Path(getattr(settings, "workspace_root", Path("data/workspaces")))
        return (root / self._workspace_name_of(task_id, gid) / "artifacts" / str(task_id)).resolve()

    def _check_deliver_path(self, task_id: str, gid: str, path: Path) -> Path:
        """交付路径第二道闸（插件中心审核整改 6）：path 必须等于或位于本任务的成品目录里。

        用 resolve() 后的真实路径比较（跟符号链接、消 `..`），所以 `artifacts/别的任务/x`、
        `tasks/...`、`.`、指到外面的符号链接都过不去。不满足抛 ValueError（中文），
        调用方记日志、任务照常结束。
        """
        base = self._task_artifact_dir(task_id, gid)
        try:
            real = Path(path).resolve()
        except OSError as e:
            raise ValueError(f"交付路径解析失败：{path}（{e}）") from e
        if real != base and base not in real.parents:
            raise ValueError(
                f"交付路径不在本任务的成品目录 artifacts/{task_id}/ 里，不交付：{path}"
            )
        return real

    async def deliver_task(
        self,
        task_id: str,
        *,
        kind: str,
        path: Path,
        name: str,
        note: str,
    ) -> int:
        """把成品交付出去（只入队首选渠道）。返回首选的 outbox id。"""
        path = Path(path)
        gid = self._group_of_task(task_id)
        if not gid:
            raise ValueError(f"找不到任务或任务没有群：{task_id}")
        path = self._check_deliver_path(task_id, gid, path)
        name = str(name or path.name or "成品")
        note = str(note or "")

        if kind == "view":
            # 首选 here.now：目录原样发；单个 html 文件放进临时目录当 index.html
            if path.is_dir():
                pub_dir = path
            else:
                pub_dir = self._staging_dir() / f"view-{task_id}"
                if pub_dir.exists():
                    shutil.rmtree(pub_dir)
                pub_dir.mkdir(parents=True, exist_ok=True)
                target_name = "index.html" if path.suffix.lower() in (".html", ".htm") else path.name
                shutil.copyfile(path, pub_dir / target_name)
            payload = {
                "dir": str(pub_dir),
                "note": note,
                "title": name,
                "push_kind": "delivery",
                "fallback": {
                    "kind": "file",
                    "path": str(path),
                    "name": name,
                    "note": note,
                },
            }
            return self._outbox.enqueue(
                f"task:{task_id}:deliver", gid, "herenow", payload, task_id=task_id
            )
        # kind == "file"：首选群文件，回落 herenow 附件页
        payload = {
            "path": str(path),
            "name": name,
            "note": note,
            "push_kind": "delivery",
            "fallback": {
                "kind": "herenow",
                "path": str(path),
                "name": name,
                "note": note,
            },
        }
        return self._outbox.enqueue(
            f"task:{task_id}:deliver", gid, "file", payload, task_id=task_id
        )

    async def reenqueue_missing(self, task_id: str, env: Any) -> bool:
        """管理员重发：无成品发件行时重建入队；已入队/已发/不确定的成品不碰。"""
        tid = str(task_id)
        task = self._store.read().execute(
            "SELECT group_id, workspace, title, status, delivery_kind FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        if task is None or task["status"] != "completed":
            raise ValueError("只有已完成任务能补建交付记录")
        gid = str(task["group_id"])
        settings = self._outbox._get_settings()
        if not settings.is_served(gid):
            raise ValueError("这个群已不在服务列表，不能交付")
        existing = self._store.read().execute(
            "SELECT key, kind FROM outbox WHERE task_id=?", (tid,)
        ).fetchall()
        if any(_is_artifact_outbox_row(r["kind"], str(r["key"])) for r in existing):
            return False
        kind = str(task["delivery_kind"] or "")
        # 兜底措辞和 coordinator 的交付说明同一套（本地 _note_fallback，不 import coordinator）
        note = _note_fallback(task["title"], kind)
        if kind == "text":
            # 线上 T-13（2026-10-10）：直接回文字的活，成品是 artifacts/<任务>/reply.md
            # 里的回复原文（coordinator 的 TEXT_REPLY_NAME / TEXT_REPLY_MAX 同一套口径，
            # 这里按老规矩本地重复一份常量，不 import coordinator）。补发时优先发原文；
            # 读不到 / 空的 / 超 1500 字 / 路径不对 → 照旧只发兜底说明，绝不报错。
            reply = _read_text_reply(task, tid, env, settings)
            self._outbox.enqueue(
                f"task:{tid}:deliver:text", gid, "text",
                {"text": reply or note, "push_kind": "delivery"}, task_id=tid,
            )
            return True
        if kind not in ("file", "view"):
            raise ValueError("任务缺少可恢复的交付方式，请先核对成品")
        attempt = self._store.read().execute(
            "SELECT artifacts FROM attempts WHERE task_id=? AND status='passed' ORDER BY n DESC LIMIT 1",
            (tid,),
        ).fetchone()
        try:
            saved = json.loads(attempt["artifacts"] or "[]") if attempt else []
        except (TypeError, ValueError):
            saved = []
        rel = str(saved[0] or "") if isinstance(saved, list) and saved else ""
        if not rel:
            raise ValueError("找不到验收通过时的成品路径，请先人工核对")
        ws_name = str(task["workspace"] or settings.workspace_of(gid))
        try:
            ws = env.workspace(ws_name)
            path = env.resolve(ws_name, rel)
        except (ValueError, OSError) as e:
            raise ValueError("成品路径已失效或越过工作区，不能重发") from e
        base = ws / "artifacts" / tid
        if (path != base and base not in path.parents) or not path.exists():
            raise ValueError("成品不在本任务专属目录内或已不存在，不能重发")
        await self.deliver_task(tid, kind=kind, path=path, name=path.name or tid, note=note)
        return True

    # ------------------------------------------------------------------
    # 网页视图
    # ------------------------------------------------------------------

    _KIND_CN = {"herenow": "here.now", "file": "群文件", "text": "网页副本"}
    _STATE_CN = {
        "pending": "待发",
        "sending": "发送中",
        "sent": "已发",
        "uncertain": "不确定",
        "failed": "失败",
        "dropped": "已作废",
        "cancelled": "已取消",
    }
    def delivery_records(self, task_id: str) -> list[dict]:
        """§9.3 任务详情的 delivery：kind「here.now/群文件/网页副本」、text、state 中文、url。"""
        rows = self._store.read().execute(
            "SELECT id, key, kind, payload, status, result, error, created"
            " FROM outbox WHERE task_id=? ORDER BY id",
            (str(task_id),),
        ).fetchall()
        records: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {}
            try:
                result = json.loads(r["result"] or "{}")
            except Exception:
                result = {}
            url = result.get("url")
            if not url and r["kind"] == "text":
                m = re.search(r"https?://\S+", str(payload.get("text") or ""))
                url = m.group(0) if m else None
            if r["kind"] == "file" and result.get("file_id"):
                url = None  # 群文件没有稳定外链，前端展示文件名
            error_text = str(r["error"] or "")
            # M1：群文件发送超时（uncertain）可能其实已经传上去了——详情里提示先去群里确认
            if str(r["kind"]) == "file" and str(r["status"]) == "uncertain":
                hint = "群文件可能已发出，请先到群里确认"
                error_text = f"{error_text}；{hint}" if error_text else hint
            records.append(
                {
                    "id": int(r["id"]),
                    "key": str(r["key"]),
                    "kind": self._KIND_CN.get(str(r["kind"]), str(r["kind"])),
                    "raw_kind": str(r["kind"]),
                    "text": str(payload.get("note") or payload.get("text") or payload.get("name") or ""),
                    "state": self._STATE_CN.get(str(r["status"]), str(r["status"])),
                    "raw_state": str(r["status"]),
                    "url": url or None,
                    "error": error_text,
                }
            )
        return records

    def undelivered(self, task_id: str) -> bool:
        """已完成但没有成品发件、或成品发送失败：网页必须显眼标未交付。"""
        task = self._store.read().execute(
            "SELECT status, delivery_kind, undelivered FROM tasks WHERE id=?", (str(task_id),)
        ).fetchone()
        if task is None or str(task["status"]) != "completed":
            return False
        rows = self._store.read().execute(
            "SELECT key, kind, status FROM outbox WHERE task_id=?",
            (str(task_id),),
        ).fetchall()
        artifact = [r for r in rows if _is_artifact_outbox_row(r["kind"], str(r["key"]))]
        if not artifact:
            return True
        if any(r["status"] == "sent" for r in artifact):
            return False
        if any(r["status"] in ("failed", "uncertain", "cancelled") for r in artifact):
            return True
        return bool(task["undelivered"])


# ----------------------------------------------------------------------
# report_error：故障报错（同群同指纹 10 分钟一次）
# ----------------------------------------------------------------------


def _redact_report(text: str) -> str:
    """报错文本去密钥：已知形式 + api_key= + 明显的长 token。"""
    out = _redact(text, [])
    out = _API_KEY_RE.sub(lambda m: m.group(1) + "***", out)
    out = _TOKEN_RE.sub(lambda m: m.group(0)[:6] + "***", out)
    return out[:_ERR_MAX]


def _fingerprint(text: str) -> str:
    """指纹 = sha1(去掉数字后的 text)：超时 12 秒和超时 37 秒算同一个错误。"""
    digits_free = re.sub(r"\d+", "", text)
    return hashlib.sha1(digits_free.encode("utf-8")).hexdigest()


def report_error(store: Store, outbox: Outbox, group_id: str, text: str, now: float) -> bool:
    """同群同指纹 10 分钟内已报过 → False；否则写 error_reports 并入队（push_kind=error）→ True。"""
    now = float(now)
    clean = _redact_report(text)
    fp = _fingerprint(clean)
    row = store.read().execute(
        "SELECT ts FROM error_reports WHERE group_id=? AND fingerprint=?",
        (str(group_id), fp),
    ).fetchone()
    if row is not None and now - float(row["ts"]) < _DEDUP_WINDOW_S:
        return False
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO error_reports (group_id, fingerprint, ts) VALUES (?, ?, ?)"
            " ON CONFLICT(group_id, fingerprint) DO UPDATE SET ts=excluded.ts",
            (str(group_id), fp, now),
        )
    bucket = int(now // _DEDUP_WINDOW_S)
    outbox.enqueue(
        f"error:{fp}:{bucket}",
        group_id,
        "text",
        {"text": f"【故障】{clean}", "push_kind": "error"},
    )
    return True
