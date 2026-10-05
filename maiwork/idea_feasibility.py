"""idea_feasibility.py（构想可行性把关，docs/18 §八「构想先评估 MaiWork 能否实现」）。

用户拍板（2026-10-05）：
- **1.A 严格**：只留 MaiWork 自己从头做得完、交得出东西的构想；要群友报名 / 参与 / 配合
  才成立的一律不出（「办活动 / 接龙 / 擂台」「约人开黑」这类都不出）。
- **2.A 已有构想不动**：只拦以后新出的，不改数据库已有数据、不加迁移。

怎么把关：拿子 agent **实际能用的工具名单**（`Workers.tool_catalog()` →
`Tools.catalog("worker")`）算一份能力清单，把清单写进构想的提示词，再在入库前用
`check()` 逐条核对模型交回的 feasibility。工具被摘掉时（`app._drop_command_tools_if_stopped`
摘 `run_command` / `start_process` 等）清单自然没有那项能力，模型硬写 uses 也过不了。

工具名以代码里的真实名字为准（2026-10-05 grep 核实）：
- `web_search` / `fetch_page`（tools_builtin.py）→ search
- `read_chat_history` / `search_memory`（tools_exec.py）、`read_profile`（tools_builtin.py）→ chat
  （`search_chat` 只是 chatlog.py 里的 Python 函数，**不是**给子 agent 的工具，不算）
- `write_file`（tools_exec.py）→ write
- `run_command`（tools_exec.py）→ code
- `vm_run`（tools_railway.py）→ vm
- `machine_*` 前缀（tools_ssh.py）→ machine
- `mcp_<扩展名>_<工具名>`（extensions.py）→ `ext:<工具名>`，最多 12 个

群文件 / 公告 / 相册（本群权限，MaiWork 时有时无）**不进清单**：构想不该靠它成立。

模块是纯函数为主：`inventory` / `check` / `prompt_section` 不碰 IO；
`record_blocked` / `blocked_view` 只读写 `store.kv` 的 `ideas.blocked.<群号>` 一个键。
"""

from __future__ import annotations

import logging
from typing import Any

from . import clock

logger = logging.getLogger("maiwork.idea_feasibility")

# 拿不到工具名单时只给这些基本能力（search / chat / write / watch）
_BASIC_CAPS: tuple[str, ...] = ("chat", "search", "watch", "write")

# 能力 → 提供它的真实工具名（任一在场就算有这个能力）
_CAP_TOOLS: dict[str, tuple[str, ...]] = {
    "search": ("web_search", "fetch_page"),
    "chat": ("read_chat_history", "search_memory", "read_profile"),
    "write": ("write_file",),
    "code": ("run_command",),
    "vm": ("vm_run",),
}
# 能力 → 工具名前缀（machine_*）
_CAP_PREFIXES: dict[str, str] = {"machine": "machine_"}
# 扩展工具名前缀（extensions.py：mcp_<扩展名>_<工具名>）
_EXT_PREFIX = "mcp_"
_EXT_MAX = 12

# 群文件 / 公告 / 相册：看本群权限，不算能力（本来也匹配不上上面几条，列出来是为了可读、可测）
_NOT_CAPS = frozenset({
    "files_list", "files_manage", "file_list", "file_upload", "group_file_list",
    "send_notice", "notice_send", "album_upload", "album_list",
})

# 能力的中文说明（提示词里讲给模型听）
_CAP_LABEL: dict[str, str] = {
    "search": "联网搜索、打开网页",
    "chat": "读本群聊天记录 / 群记忆 / 群画像",
    "write": "在工作区写文件",
    "watch": "长期盯着一个目标慢慢推进",
    "code": "在本机跑命令 / 脚本",
    "vm": "起一台一次性云 VM 跑",
    "machine": "连你自己的服务器跑",
}

# 做不到清单（提示词里逐条讲给模型听；check() 不做文本匹配，靠 level / needs_members / uses
# / deliver 四个字段硬拦——这里的句子是给模型的「别往这些方向想」提示）
CANNOT: tuple[str, ...] = (
    "约人组队、凑局、开黑、拉群友一起做某件事（不能替人约、也不能替人答应）",
    "组织活动、接龙、报名、投票、擂台、比赛（要群友参与 / 报名才成立，MaiWork 只能做个模板）",
    "替人联系某个人、私聊、加好友、传话（不能以任何人的身份和别人说话）",
    "要登录 / 注册 / 实名 / 付费下单 / 填验证码才能用的站和服务",
    "实际去玩游戏、代打、线下跑腿、买卖实物",
    "实时盯盘、抢购、抢票、秒级行情（没有常驻实时通道）",
    "生成图片 / 视频 / 音频（除非清单里有对应的 ext: 扩展）",
    "保证某个结果一定发生（只能做事、交东西，不能保证群友会响应）",
)

# 交付形式：key → {label 讲给模型听, needs 需要的任一能力（空 = 只要工作区和网页，不额外要）}
# 说明：按设计草案只给 tool / report 挂能力要求；page / doc 靠工作区 + 网页（写文件工具在
# 「受限」模式下也保留，见 app._drop_command_tools_if_stopped），所以不额外卡。
DELIVER: dict[str, dict[str, Any]] = {
    "page": {"label": "网页（在 MaiWork 网页上给一页）", "needs": ()},
    "doc": {"label": "文档 / 表格 / 清单（一个文件）", "needs": ()},
    "tool": {"label": "能跑的小工具 / 脚本 / 自动化", "needs": ("code", "vm", "machine")},
    "report": {"label": "定期汇报 / 长期盯着一件事", "needs": ("watch",)},
}


# ----------------------------------------------------------------------
# 能力清单
# ----------------------------------------------------------------------


def _catalog_names(tool_catalog: Any) -> list[str]:
    """把 [(名字, 描述)]（也容 dict / 裸字符串）洗成不重复的工具名表；拿不到 → []。"""
    if not tool_catalog:
        return []
    names: list[str] = []
    try:
        entries = list(tool_catalog)
    except TypeError:
        return []
    for entry in entries:
        name = ""
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "")
        elif isinstance(entry, (tuple, list)) and entry:
            name = str(entry[0] or "")
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


def _names_to_caps(names: list[str]) -> tuple[list[str], list[str]]:
    """工具名 → (能力名表, 扩展名表)，两表都排序稳定。watch（长期目标）总是有。"""
    caps: set[str] = {"watch"}
    ext: list[str] = []
    for name in names:
        if name in _NOT_CAPS:
            continue
        if name.startswith(_CAP_PREFIXES["machine"]):
            caps.add("machine")
            continue
        if name.startswith(_EXT_PREFIX):
            if len(ext) < _EXT_MAX:
                ext.append(f"ext:{name}")
            continue
        for cap, tools in _CAP_TOOLS.items():
            if name in tools:
                caps.add(cap)
                break
    return sorted(caps), sorted(ext)


def inventory(tool_catalog: Any = None) -> dict:
    """按子 agent 实际能用的工具名单算能力清单（纯函数）。

    tool_catalog：[(工具名, 描述)]；None / 空 / 认不出来 → 只给基本能力。
    返回 {"caps": [能力名…], "ext": ["ext:<mcp 工具名>"…], "tools": [看到的工具名…]}。
    """
    names = _catalog_names(tool_catalog)
    if not names:
        return {"caps": list(_BASIC_CAPS), "ext": [], "tools": []}
    caps, ext = _names_to_caps(names)
    return {"caps": caps, "ext": ext, "tools": sorted(names)}


# ----------------------------------------------------------------------
# 提示词段落（群向 / 个人向共用）
# ----------------------------------------------------------------------


def _inv_caps(inv: Any) -> list[str]:
    if not isinstance(inv, dict):
        return list(_BASIC_CAPS)
    caps = [str(c) for c in (inv.get("caps") or []) if str(c or "").strip()]
    return caps or list(_BASIC_CAPS)


def _inv_ext(inv: Any) -> list[str]:
    if not isinstance(inv, dict):
        return []
    return [str(e) for e in (inv.get("ext") or []) if str(e or "").strip()]


def prompt_section(inv: Any) -> str:
    """把能力清单、做不到清单、交付形式、feasibility 字段格式讲给模型（中文一段）。

    群向 feeds.make_idea 和个人向 personal._plan_focus 共用同一段，两边口径一致。
    """
    caps = _inv_caps(inv)
    ext = _inv_ext(inv)
    lines: list[str] = ["【MaiWork 现在真有这些本事（只有这些，别想当然）】"]
    for cap in caps:
        lines.append(f"- {cap}：{_CAP_LABEL.get(cap, '（清单里列的本事）')}")
    for name in ext:
        lines.append(f"- {name}：管理员接的外部扩展工具（清单里有就能用）")
    lines.append("")
    lines.append("【这些它做不到——凡是靠这些才成立的构想，一律不要提】")
    lines.extend(f"- {x}" for x in CANNOT)
    lines.append("")
    lines.append("【交付形式 deliver 只能从这四种里挑一个，而且要和上面本事对得上】")
    for key, meta in DELIVER.items():
        needs = meta.get("needs") or ()
        hint = f"（清单里要有 {' / '.join(needs)}）" if needs else ""
        lines.append(f"- {key}：{meta['label']}{hint}")
    lines.append("")
    lines.append("【feasibility 就这么写】")
    lines.append(
        '{"level": "ok", "note": "一句话：为什么真做得到", '
        '"uses": ["用到的本事名，至少 1 个，只能从上面清单里挑"], '
        '"deliver": "page|doc|tool|report", "needs_members": false}'
    )
    lines.append('- level 必须是 "ok"；maybe（可能能做）/ need（还要群友提供什么）一律不出。')
    lines.append(
        "- needs_members 必须是 false：要群友报名 / 参与 / 配合才成立的（约人开黑、组织活动、"
        "接龙、投票……）一律不要提。"
    )
    lines.append("- uses 里的本事名要在上面清单里真实存在、至少 1 个，用了清单上没有的本事这条不会被采用。")
    lines.append(
        "- deliver 认不出、或和本事对不上的（例如 deliver=tool 却没有 code / vm / machine），"
        "这条也不会被采用。"
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 逐条核对
# ----------------------------------------------------------------------


def _norm_uses(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for x in raw:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _is_false(v: Any) -> bool:
    """「明确是 false」：布尔 False，或字符串 false / no / 0（大小写无所谓）。"""
    if v is False:
        return True
    if isinstance(v, str):
        return v.strip().lower() in ("false", "no", "0")
    return False


def check(raw_feasibility: Any, inv: Any) -> tuple[bool, str, dict]:
    """核对模型交回的可行性评估。返回 (ok, 中文原因, normalized)。

    规则（用户拍板 1.A 严格）：level 必须 "ok"；needs_members 必须明确 false；
    uses 非空且全在清单里；deliver 认得出且与能力对得上；缺字段一律不过。

    normalized 一定带这五个键：level / note / uses / deliver / needs_members
    （入库前调用方直接 json.dumps(normalized)；card_push.py / feeds.ideas_view 只认
    level / note，这两个键保持原意不动）。
    """
    d: dict = raw_feasibility if isinstance(raw_feasibility, dict) else {}
    caps = set(_inv_caps(inv))
    ext = set(_inv_ext(inv))
    uses = _norm_uses(d.get("uses"))
    deliver = str(d.get("deliver") or "").strip().lower()
    normalized = {
        "level": str(d.get("level") or "").strip().lower(),
        "note": str(d.get("note") or "").strip()[:120],
        "uses": uses,
        "deliver": deliver,
        "needs_members": False if _is_false(d.get("needs_members")) else True,
    }
    if not isinstance(raw_feasibility, dict) or not d:
        return False, "没给可行性评估（feasibility 缺字段一律不出）", normalized
    level = normalized["level"]
    if not level:
        return False, '可行性评估缺 level（必须 "ok"）', normalized
    if level != "ok":
        return (
            False,
            f'level={level} 不是 "ok"（maybe=可能能做、need=还要群友提供什么，按拍板一律不出）',
            normalized,
        )
    if "needs_members" not in d or not _is_false(d.get("needs_members")):
        return (
            False,
            "needs_members 必须明确是 false（要群友报名 / 参与 / 配合才成立的构想一律不出）",
            normalized,
        )
    if not uses:
        return False, "uses 是空的：得写清用哪些本事（至少 1 个，只能从清单里挑）", normalized
    unknown = [u for u in uses if u not in caps and u not in ext]
    if unknown:
        return (
            False,
            f'MaiWork 没有这个本事：{"、".join(unknown)}（清单里没有，做不了）',
            normalized,
        )
    if deliver not in DELIVER:
        return (
            False,
            f'deliver 认不出：{deliver or "（空）"}（只能 page / doc / tool / report）',
            normalized,
        )
    needs = DELIVER[deliver].get("needs") or ()
    if needs and not (set(needs) & (caps | ext)):
        return (
            False,
            f'deliver={deliver} 需要 {" / ".join(needs)} 里的能力，但清单里没有',
            normalized,
        )
    return True, "", normalized


# ----------------------------------------------------------------------
# 拦下来的记录（kv ideas.blocked.<群号>）
# ----------------------------------------------------------------------

_BLOCKED_PREFIX = "ideas.blocked."
_BLOCKED_KEEP_DAYS = 14.0
_BLOCKED_MAX = 30
_BLOCKED_RECENT = 5
_TITLE_MAX = 120
_REASON_MAX = 200


def _blocked_key(gid: Any) -> str:
    return f"{_BLOCKED_PREFIX}{gid}"


def _clean_entries(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        try:
            ts = float(e.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        out.append({
            "ts": ts,
            "kind": "personal" if str(e.get("kind") or "") == "personal" else "group",
            "title": str(e.get("title") or "")[:_TITLE_MAX],
            "reason": str(e.get("reason") or "")[:_REASON_MAX],
        })
    return out


def record_blocked(store: Any, gid: Any, kind: str, title: str, reason: str) -> None:
    """记一条被拦下的构想（kv `ideas.blocked.<群号>`；保留近 14 天、最多 30 条）。

    每条 {ts, kind("group"|"personal"), title, reason}；按时间从旧到新存，读取时倒过来。
    任何异常都只记日志、绝不抛（拦构想是顺手的事，不能拖垮出构想这轮）。
    """
    key = _blocked_key(str(gid))
    now = float(clock.now())
    try:
        entries = _clean_entries(store.kv_get(key, []))
    except Exception:
        logger.exception("读构想拦截记录出错（群 %s），这次从空的记起", gid)
        entries = []
    entries.append({
        "ts": now,
        "kind": "personal" if str(kind) == "personal" else "group",
        "title": str(title or "").strip()[:_TITLE_MAX],
        "reason": str(reason or "").strip()[:_REASON_MAX],
    })
    cutoff = now - _BLOCKED_KEEP_DAYS * 86400.0
    entries = [e for e in entries if e["ts"] >= cutoff][-_BLOCKED_MAX:]
    try:
        with store.tx() as conn:
            store.kv_set(conn, key, entries)
    except Exception:
        logger.exception("写构想拦截记录出错（群 %s），这条没记上", gid)


def blocked_view(store: Any, gid: Any, days: int = 7) -> dict:
    """近 days 天拦下的构想：{"count": 条数, "recent": 最近最多 5 条（新的在前）}。

    给网页管理员视图用（views.group_view → ideas_blocked）。读不到 / 结构不对 → 全 0，不抛。
    """
    empty = {"count": 0, "recent": []}
    try:
        raw = store.kv_get(_blocked_key(str(gid)), [])
    except Exception:
        logger.exception("读构想拦截记录出错（群 %s）", gid)
        return {"count": 0, "recent": []}
    entries = _clean_entries(raw)
    if not entries:
        return empty
    try:
        span = max(0.0, float(days)) * 86400.0
    except (TypeError, ValueError):
        span = 7 * 86400.0
    cutoff = float(clock.now()) - span
    recent = [e for e in entries if e["ts"] >= cutoff]
    if not recent:
        return empty
    recent.reverse()  # 新的在前
    return {"count": len(recent), "recent": recent[:_BLOCKED_RECENT]}
