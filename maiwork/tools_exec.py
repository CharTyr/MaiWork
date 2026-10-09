"""M3 子 agent 工具（docs/02-设计 §10、docs/07 §10.2 + §11.1）：

- 子 agent（worker）：read_file / write_file / list_files / run_command /
  start_process / check_process / stop_process / read_chat_history / search_memory。
- 主模型（main）：inspect_file / inspect_files（只读，用来验收子 agent 的成品）。
- read_file / inspect_file **分页读**（线上 T-10 整改）：offset / limit 单位都是字符
  （offset 0 = 文件第一个字），返回里带「共多少字 / 本页范围 / 下一页 offset」；
  单页正文 + 元信息卡在单条 tool 消息 6000 字以内，所以一页页读能一字不差地重建
  20KB~50KB 的中文文件，不再需要把资料拆成小文件。整份文件不超过一页时保持老行为，
  原样返回（没有元信息）。

安全要点：
- 一切路径过 env.resolve：越界（绝对路径、..、符号链接逃逸）→ PermissionError
  → Tools.call 记一条失败调用，子 agent 看到的是中文「不允许访问…」。
- 成品目录隔离（`artifacts/`）之外，`tool_spill/<任务ID>/`（工具输出归档，docs/27 §8 P1）
  也只许**那个任务自己**读 / 列 / 写：跨任务归档一律中文拒绝，列目录时按条目滤掉。
- 工作区名取自 ctx.workspace.name；不传 workspace 一律报错，不让模型自己填
  workspace 参数（填了它也不会经过群授权检查）。
- run_command timeout_s 用 [environments] command_timeout_s 夹住（配置里写死
  默认 300 秒），配置之外模型自己说了不算。
- read_chat_history 只读本群：session 从外部 session_of(group_id) 回调拿
  （由 tasks.py/wiring 层去查 store.groups.session_id），本模块不依赖 store。
- search_memory 只传 group_id 不传 person_id（docs/06：person_id 过滤是假的）。
- EXEC 环境 runner 由 LocalEnv 内部注入；tools 层只看到语义化的 RunResult。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from . import clock
from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_exec")

# ---------------------------------------------------------------------------
# 分页读（线上 T-10）：read_file / inspect_file 的 offset / limit
#
# 线上教训：以前 read_file 只有 path，同一个文件反复读也只能拿开头；子 agent 那侧
# 单条 tool 消息截 6000 字，于是再长的资料实际只到前 6000 字，下游错过 research.md
# 后半，被迫把资料拆成许多 ≤3KB 的小文件。
#
# 这里的规矩：
# - offset / limit 的单位都是**字符**（不是字节）；offset 从 0 数，0 = 文件第一个字。
# - 单页「正文 + 元信息」必须 ≤ _TOOL_MSG_BUDGET，否则 workers / coordinator 那层
#   会再截一刀、正文就丢了（分页等于白分）。
# - 单次读取的**字节**上限沿用 env.read_file 的默认（20 万字节），分页不扩权限。
# ---------------------------------------------------------------------------

_READ_BYTES_CAP = 200_000        # env.read_file 单次最多读多少字节（LocalEnv 的默认值）
# 顶到上限时的说明文案：普通文件说「20 万字节」，自己任务的归档说「扫描上限」
# （归档走流式窗口读，字节上限随 offset 增长，不是一个固定数字）
_READ_CAP_HEAD = f"文件达到单次字节上限 {_READ_BYTES_CAP // 10000} 万字节，后面若还有内容读不到"
_READ_CAP_FOOT = (
    f"文件达到单次字节上限（{_READ_BYTES_CAP // 10000} 万字节），后面若还有内容本工具读不到"
)
_ARCHIVE_CAP_HEAD = "这个归档文件太长，读到本次扫描上限了，后面若还有内容读不到"
_ARCHIVE_CAP_FOOT = "这个归档文件太长，读到本次扫描上限了，后面若还有内容本工具读不到"
_PAGE_CHARS_DEFAULT = 5_000      # 默认每页多少字
_PAGE_CHARS_MAX = 5_000          # 每页上限；再大就顶到单条 tool 消息的预算了
_TOOL_MSG_BUDGET = 6_000         # workers._TOOL_MSG_MAX / coordinator 的工具消息上限
_PATH_SHOWN_MAX = 80             # 元信息里路径最多显示多少字（防超长路径挤掉正文）
_BODY_START = "----- 正文开始（原样，未删改）-----"
_BODY_END = "----- 正文结束 -----"
_SPILL_DIR = "tool_spill"        # 工具输出归档根目录（workers.py / coordinator.py 同一套口径）


def _parse_offset(args: dict) -> tuple[int, str]:
    """offset：从文件开头数第几个字开始读。

    契约：**只有「没给 offset」或显式 null 才用默认 0**；其余一律必须是 JSON 整数——
    字符串（含空串 / 空白串）、布尔、小数、列表 / 字典都直接报错，不悄悄当默认，
    也不做 "100" 这种字符串转数字。
    """
    raw = args.get("offset")
    if raw is None:
        return 0, ""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0, f"offset 只能是整数（单位：字符，从文件开头数，0 = 第一个字）；你给的是 {raw!r}"
    if raw < 0:
        return 0, f"offset 不能是负数（0 = 文件第一个字，单位：字符）；你给的是 {raw}"
    return raw, ""


def _parse_limit(args: dict) -> tuple[int, str]:
    """limit：本页最多返回多少字。

    契约：**只有「没给 limit」或显式 null 才用默认 5000**；其余一律必须是 1–5000 的
    JSON 整数——字符串（含空串 / 空白串）、布尔、小数、列表 / 字典都直接报错，
    越界也不悄悄改小。
    """
    raw = args.get("limit")
    if raw is None:
        return _PAGE_CHARS_DEFAULT, ""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0, f"limit 只能是整数（单位：字符，本页最多读多少字）；你给的是 {raw!r}"
    if raw < 1 or raw > _PAGE_CHARS_MAX:
        return 0, (
            f"limit 只能是 1–{_PAGE_CHARS_MAX} 之间的整数"
            f"（本页最多读多少字，默认 {_PAGE_CHARS_DEFAULT}）；你给的是 {raw}"
        )
    return raw, ""


def _format_page(
    *,
    tool: str,
    rel: str,
    offset: int,
    body: str,
    total: int,
    byte_capped: bool,
    cap_head: str = _READ_CAP_HEAD,
    cap_foot: str = _READ_CAP_FOOT,
    total_known: bool = True,
) -> str:
    """拼一页的返回：元信息 + 原样正文 + 元信息。正文一个字都不改动（不加空行、不 strip）。

    `byte_capped` 时 `total` 只是「本次读得到的**前缀**长度」（源文件可能更长），所以
    表头和末页说明都只能说「本次可读 / 可读前缀」，不能说成文件总字数或文件读完。
    `total_known=False` 是流式读（工具输出归档）提前收手的情况：只扫到「窗口够用」为止，
    文件总字数**还不知道**，所以只说「本次读到 N 字」，下页提示照给（不能假装读完了）。
    """
    shown = rel if len(rel) <= _PATH_SHOWN_MAX else rel[:_PATH_SHOWN_MAX] + "…"
    end = offset + len(body)
    if byte_capped:
        head = f"【{tool}】{shown} · 本次可读 {total} 字（{cap_head}）"
    elif not total_known:
        head = f"【{tool}】{shown} · 本次读到 {total} 字（还没到文件末尾，总字数暂时不知道）"
    else:
        head = f"【{tool}】{shown} · 共 {total} 字"
    head += f" · 本页第 {offset + 1}–{end} 字（从文件开头数，offset={offset}，单位：字符）"
    if end < total:
        foot = f"还有 {total - end} 字没读：下一页 {tool}(path 照旧, offset={end})"
    elif byte_capped:
        foot = (
            f"已到本次可读前缀末尾：{cap_foot}"
            "（要看更远的地方得换办法，例如 run_command 里用 sed / head / tail）"
        )
    elif not total_known:
        foot = f"后面还有没读的（本次只读到 {total} 字）：下一页 {tool}(path 照旧, offset={end})"
    else:
        foot = f"已到文件末尾：共 {total} 字，全读完了"
    return "\n".join([head, _BODY_START, body, _BODY_END, foot])


def register_exec_tools(
    tools: Tools,
    *,
    env: Any,  # LocalEnv（environments/local.py）
    host: Any,  # Host（host.py），用它的 messages/knowledge
    get_settings: Callable[[], Any],
    session_of: Callable[[str], str] | None = None,
) -> None:
    """把 M3 工具注册进 tools。

    - session_of(group_id) -> session_id：由接线层提供（去 store.groups 查）；
      没给或查不到时 read_chat_history 直接报「没找到本群会话」。
    - env 的 local_mode / 各种上限从 get_settings() 现读（配置改了下次生效）。
    """
    session_of = session_of or (lambda _gid: "")

    # ------------------------------------------------------------------
    # 共用：resolve 包成中文 ToolResult
    # ------------------------------------------------------------------

    def _bound(ctx: ToolContext, rel: str) -> tuple[Path | None, ToolResult | None]:
        """返回 (path, None) 或 (None, 错误 ToolResult)。"""
        if ctx.workspace is None:
            return None, ToolResult(ok=False, output="", error="这次任务没带工作区，不能操作文件")
        try:
            return env.resolve(ctx.workspace.name, str(rel or "")), None
        except PermissionError as e:
            return None, ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        except ValueError as e:
            return None, ToolResult(ok=False, output="", error=str(e))

    # ------------------------------------------------------------------
    # 成品目录隔离（2026-10，线上 T-4 整改）：有 artifact_scope 时，
    # artifacts/ 下但不在 scope 里的路径一律拒绝；list 时 scope 外的目录滤掉不报错。
    # ------------------------------------------------------------------

    def _norm_scope(scope: Any) -> list[str]:
        """scope 规范化成 ["artifacts/T-4", …]（剥 ./ 和 . 段、去多余斜杠；无效项丢弃）。"""
        out: list[str] = []
        for item in scope or ():
            text = str(item or "").strip()
            cand = Path(text)
            if cand.is_absolute() or ".." in cand.parts:
                continue
            parts = [p for p in cand.parts if p not in ("", ".")]
            if len(parts) >= 2 and parts[0].lower() == "artifacts":
                out.append("/".join(parts))
        return out

    def _scope_other_artifacts(ctx: ToolContext, rel: str) -> tuple[bool, str]:
        """返回 (拒?, scope 文本)。rel 在 artifacts/ 下、不在 scope 任何一个目录里 → 拒。

        artifacts/ 本身（0 段目录名）不算「别的任务的」：列它时过滤即可，不报错。
        路径先按 env.resolve 的同款剥段规范化（./artifacts/../artifacts/T-2 这类
        绕法在 resolve 那层已经会按越界拒掉，这里是双保险：即使解析器哪天变宽松，
        ./ 打头的也一样按规范路径判）。
        """
        scope = _norm_scope(getattr(ctx, "artifact_scope", None))
        if not scope:
            return False, ""
        cand = Path(str(rel or ""))
        if cand.is_absolute():
            return False, ""  # 绝对路径交给 env.resolve 去拒
        parts = [p for p in cand.parts if p not in ("", ".")]
        if not parts:
            return False, ""
        if parts[0].lower() != "artifacts" or len(parts) < 2:
            return False, ""  # artifacts/ 之外（PROFILE-*.md、tools/、tasks/…）不受影响
        for allowed in scope:
            a_parts = allowed.split("/")
            if parts[: len(a_parts)] == a_parts:
                return False, ""
        return True, "、".join(f"{a}/" for a in scope)

    def _scope_error(scope_text: str) -> ToolResult:
        return ToolResult(
            ok=False, output="",
            error=f"这是别的任务的文件，和本任务无关，不能读写；你的成品目录是 {scope_text}",
        )

    # ------------------------------------------------------------------
    # 工具输出归档目录的隔离（2026-10，docs/27 §8 P1）：`tool_spill/<任务ID>/` 只许
    # **那个任务自己**读——归档里是别的任务的完整工具输出，跨任务看一眼就把隔离破了
    # （成品目录那套只管 `artifacts/`，管不到这里）。读 / 检查 / 列目录 / 写都过这道闸。
    # ------------------------------------------------------------------

    def _own_spill_task(ctx: ToolContext) -> str:
        """本轮任务的归档目录名（= task_id）；没有任务就是 ""。"""
        return str(getattr(ctx, "task_id", "") or "").strip()

    def _scope_other_task_spill(ctx: ToolContext, rel: str) -> tuple[bool, str]:
        """rel 在 tool_spill/ 下、不属于本轮任务 → (True, 自己任务名)。"""
        parts = [p for p in Path(str(rel or "")).parts if p not in ("", ".")]
        if not parts or parts[0].lower() != _SPILL_DIR or len(parts) < 2:
            return False, ""      # tool_spill/ 这一层本身由列目录时的条目过滤处理
        mine = _own_spill_task(ctx)
        if mine and parts[1] == mine:
            return False, ""
        return True, mine

    def _is_own_task_spill(ctx: ToolContext, rel: str) -> bool:
        """rel 是不是**本轮任务自己**的工具输出归档（`tool_spill/<本轮任务ID>/…`）。"""
        mine = _own_spill_task(ctx)
        if not mine:
            return False
        parts = [p for p in Path(str(rel or "")).parts if p not in ("", ".")]
        return len(parts) >= 2 and parts[0].lower() == _SPILL_DIR and parts[1] == mine

    def _spill_scope_error(mine: str) -> ToolResult:
        if mine:
            return ToolResult(
                ok=False, output="",
                error=(
                    "这是别的任务的工具输出归档，和本任务无关，不能读写；"
                    f"你自己的归档目录是 {_SPILL_DIR}/{mine}/"
                ),
            )
        return ToolResult(
            ok=False, output="",
            error=(
                f"{_SPILL_DIR}/ 下面是各个任务的工具输出归档，只有那个任务自己能读写；"
                "本轮没有任务（没有 task_id），不能读写。"
            ),
        )

    def _entry_out_of_task_spill(ctx: ToolContext, path: str, listed_under: str) -> bool:
        """列目录时这一条要不要滤掉：tool_spill 下不属于本轮任务的一律不给看。"""
        parts = [p for p in Path(str(path or "")).parts if p not in ("", ".")]
        listed = [p for p in Path(str(listed_under or "")).parts if p not in ("", ".")]
        if listed and listed[0].lower() == _SPILL_DIR and parts[: len(listed)] != listed:
            # env.list_files 给的条目一般带被列目录的前缀，万一不带就补上再判
            parts = listed + parts
        if not parts or parts[0].lower() != _SPILL_DIR or len(parts) < 2:
            return False
        mine = _own_spill_task(ctx)
        if len(listed) >= 2 and listed[1] == mine and mine:
            return False          # 列的就是自己的归档目录：里面的都算我的
        return parts[1] != mine

    # ------------------------------------------------------------------
    # 各步骤分文件夹（docs/22 §4 C）：write_scope 非空时，artifacts/ 下的写必须落在
    # 写范围内；`artifacts/<任务>/steps/<步号>/` 只许对应那一步写。读不受它管。
    # ------------------------------------------------------------------

    def _write_denied(ctx: ToolContext, rel: str) -> str:
        """返回拒绝原因（"" = 允许写）。只管道 artifacts/ 下的路径。"""
        scope = _norm_scope(getattr(ctx, "write_scope", None))
        if not scope:
            return ""
        cand = Path(str(rel or ""))
        if cand.is_absolute():
            return ""  # 绝对路径交给 env.resolve 去拒
        parts = [p for p in cand.parts if p not in ("", ".")]
        if not parts or parts[0].lower() != "artifacts" or len(parts) < 2:
            return ""
        allowed = [s.split("/") for s in scope]
        if not any(parts[: len(a)] == a for a in allowed):
            dirs = "、".join(f"{s}/" for s in scope)
            return (
                f"这一步只能写 {dirs}（中间步骤各写各的文件夹，"
                "不能写交付成品的位置）"
            )
        if len(parts) >= 4 and parts[2] == "steps":
            # steps/<步号>/ 只许对应那一步写：交付步骤（写范围是任务目录）写不了别人的步骤目录
            own = "/".join(parts[:4])
            if not any("/".join(a) == own or "/".join(a).startswith(own + "/") for a in allowed):
                return (
                    f"artifacts/{parts[1]}/steps/{parts[3]}/ 是别的步骤的文件夹，不能写"
                    "（各步骤各写各的）"
                )
        return ""

    def _write_scope_error(reason: str) -> ToolResult:
        return ToolResult(ok=False, output="", error=reason)

    def _entry_out_of_scope(ctx: ToolContext, path: str, listed_under: str) -> bool:
        """目录清单里这一条要不要滤掉（不报错，只是不给看别的任务的目录）。"""
        scope = _norm_scope(getattr(ctx, "artifact_scope", None))
        if not scope:
            return False
        listed = str(listed_under or "")
        path_parts = [p for p in Path(str(path or "")).parts if p not in ("", ".")]
        listed_parts = [p for p in Path(listed).parts if p not in ("", ".")] if listed else []
        # env.list_files 给的条目路径带被列目录的前缀（列 "artifacts" 出 "artifacts/T-4"）；
        # 万一不带（别的 env 实现），就把前缀拼上再判。
        if listed_parts and path_parts[: len(listed_parts)] != listed_parts:
            parts = listed_parts + path_parts
        else:
            parts = path_parts
        if not parts:
            return False
        if parts[0].lower() != "artifacts" or len(parts) < 2:
            return False
        sub = "/".join(parts[:2])
        depth_below = len(parts) - 2
        for allowed in scope:
            a0 = str(allowed.split("/")[1]) if len(allowed.split("/")) >= 2 else ""
            if sub == allowed:
                return False  # 在放行目录里
            if depth_below == 0 and a0.startswith(sub + "/"):
                return False  # 列 artifacts/ 时：放行目录的父目录项要留着，不然进不去
        return True

    # ------------------------------------------------------------------
    # 文件工具
    # ------------------------------------------------------------------

    async def _read_page(ctx: ToolContext, args: dict, *, tool: str) -> ToolResult:
        """read_file / inspect_file 的公共实现（分页读，见模块顶部注释）。

        顺序：成品目录隔离 → env.resolve 越界检查 → 页参数校验 → 读文件 →
        offset 与文件长度对账 → 拼页。隔离 / 越界 / 字节上限都和以前一模一样，
        分页不放松任何一道闸。

        返回 data 的字段（`total` 一律指「本次读得到的可读前缀长度」）：
        - `total_chars`：可读前缀有多少字符。
        - `total_chars_is_lower_bound`：True = 源文件可能更长（顶到了单次字节上限）。
        - `truncated_by_byte_limit`：True = 这次读顶到了单次读取上限。
        - `eof`：True = **本工具能给的都给了**（可读前缀读完了）。**不代表整份源文件结束**；
          整份源文件是否读完看 `source_complete`。
        - `source_complete`：True = 整份源文件已完整读到（`eof` 且没顶到上限）。
        - `next_offset`：下一页的 offset；None = 没有下一页。

        读法按路径分两种（**权限闸一样，只有读法不同**）：
        - **自己任务的工具输出归档**（`tool_spill/<本轮任务ID>/`）：走 `env.read_file_page`
          流式按字符读窗口——归档可能几 MB，老的「整份读 + 20 万字节上限」会让 offset 一深
          就读不动（500k 汉字 = 1.5MB，只能看到前面 6.6 万字）；
        - 其他文件：照旧整份读前缀（本地 20 万字节上限 + 顶上限时如实说「本次可读前缀」）。
        """
        rel = str(args.get("path") or "")
        refused, scope_text = _scope_other_artifacts(ctx, rel)
        if refused:
            return _scope_error(scope_text)
        spilled, mine = _scope_other_task_spill(ctx, rel)
        if spilled:
            return _spill_scope_error(mine)
        _path, err = _bound(ctx, rel)
        if err:
            return err
        offset, offset_err = _parse_offset(args)
        if offset_err:
            return ToolResult(ok=False, output="", error=offset_err)
        limit, limit_err = _parse_limit(args)
        if limit_err:
            return ToolResult(ok=False, output="", error=limit_err)
        window_start = 0        # 手里的 window 从文件的第几个字开始
        total_known = True      # total 是准数（普通文件 / 归档扫到了末尾）还是下限
        cap_head, cap_foot = _READ_CAP_HEAD, _READ_CAP_FOOT
        own_archive = _is_own_task_spill(ctx, rel)
        try:
            if own_archive:
                size = max(1, min(limit, _PAGE_CHARS_MAX))
                page = await env.read_file_page(
                    ctx.workspace.name, rel, offset=offset, limit=size
                )
                window = page.text
                window_start = offset
                total = int(page.total_chars)
                # 流式读「扫到窗口够用就收手」：没扫到文件末尾时 total 只是下限，
                # 只能说「本次读到 N 字」+ 给下一页，不能假装是文件总长 / 读完了。
                total_known = bool(page.complete)
                byte_capped = bool(page.hit_scan_cap)
                cap_head, cap_foot = _ARCHIVE_CAP_HEAD, _ARCHIVE_CAP_FOOT
            else:
                window = await env.read_file(ctx.workspace.name, rel)
                total = len(window)
                # 读回来的字节数顶到单次上限：这份文件后面可能还有内容，本工具读不到（如实说）。
                # 保守判法：正好 20 万字节的文件也会被判成 capped，所以文案一律用「若还有内容」兜住。
                byte_capped = len(window.encode("utf-8")) >= _READ_BYTES_CAP
        except FileNotFoundError:
            return ToolResult(ok=False, output="", error=f"工作区内没有这个文件：{rel}")
        except IsADirectoryError:
            return ToolResult(ok=False, output="", error=f"{rel} 是目录，不是文件")
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        lower_bound = byte_capped or not total_known
        if offset > total:
            if lower_bound:
                return ToolResult(
                    ok=False, output="",
                    error=(
                        f"offset={offset} 超过了本次可读范围：{rel} 本次可读 {total} 字"
                        f"（{cap_head}），offset 最多给 {total}"
                    ),
                )
            return ToolResult(
                ok=False, output="",
                error=f"offset={offset} 超过了文件长度：{rel} 共 {total} 字，offset 最多给 {total}",
            )
        if offset == total and total > 0:
            if byte_capped:
                # 顶到上限时不能说「文件末尾 / 没有更多内容」：这只是本次可读前缀的末尾
                notice = (
                    f"（已到本次可读前缀末尾：{rel} {cap_foot}，本工具只读得到前 {total} 字）"
                )
            else:
                notice = f"（已经在文件末尾：{rel} 共 {total} 字，没有更多内容了）"
            return ToolResult(
                ok=True, output=notice,
                data={
                    "path": rel, "chars": 0, "total_chars": total,
                    "total_chars_is_lower_bound": lower_bound, "offset": offset,
                    "next_offset": None, "eof": True, "paged": False,
                    "truncated_by_byte_limit": byte_capped,
                    "source_complete": not lower_bound,
                },
            )
        # 整份文件都在这一页里、也没顶到上限 → 保持老行为：原样返回，不加元信息
        if offset == 0 and total_known and total <= limit and not byte_capped:
            return ToolResult(
                ok=True, output=window,
                data={
                    "path": rel, "chars": total, "total_chars": total,
                    "total_chars_is_lower_bound": False, "offset": 0,
                    "next_offset": None, "eof": True, "paged": False,
                    "truncated_by_byte_limit": False,
                    "source_complete": True,
                },
            )
        # 分页：正文按 limit 切，元信息挤到预算之外就缩正文（游标跟着真实页长重算，不跳字）
        # 归档那份 window 只装了这一页（window_start = offset），缩页就在内存里缩，不再读盘。
        size = max(1, min(limit, _PAGE_CHARS_MAX))
        while True:
            body = window[offset - window_start: offset - window_start + size]
            out = _format_page(
                tool=tool, rel=rel, offset=offset, body=body, total=total,
                byte_capped=byte_capped, cap_head=cap_head, cap_foot=cap_foot,
                total_known=total_known,
            )
            overflow = len(out) - _TOOL_MSG_BUDGET
            if overflow <= 0 or size <= 1:
                break
            size = max(1, size - overflow)
        end = offset + len(body)
        # eof = 「本工具能给的都给了」：普通文件看到可读前缀末尾就算；归档要真扫到文件末尾
        eof = end >= total and total_known
        return ToolResult(
            ok=True, output=out,
            data={
                "path": rel, "chars": len(body), "total_chars": total,
                "total_chars_is_lower_bound": lower_bound, "offset": offset,
                "next_offset": None if eof else end, "eof": eof, "paged": True,
                "truncated_by_byte_limit": byte_capped,
                "source_complete": bool(eof and not byte_capped),
            },
        )

    async def read_file(ctx: ToolContext, args: dict) -> ToolResult:
        return await _read_page(ctx, args, tool="read_file")

    async def inspect_file(ctx: ToolContext, args: dict) -> ToolResult:
        return await _read_page(ctx, args, tool="inspect_file")

    async def write_file(ctx: ToolContext, args: dict) -> ToolResult:
        rel = str(args.get("path") or "").strip()
        if not rel:
            return ToolResult(ok=False, output="", error="path 不能为空")
        content = args.get("content")
        if content is None:
            return ToolResult(ok=False, output="", error="content 不能为空")
        append = bool(args.get("append"))
        refused, scope_text = _scope_other_artifacts(ctx, rel)
        if refused:
            return _scope_error(scope_text)
        spilled, mine = _scope_other_task_spill(ctx, rel)
        if spilled:
            return _spill_scope_error(mine)
        denied = _write_denied(ctx, rel)
        if denied:
            return _write_scope_error(denied)
        try:
            await env.write_file(ctx.workspace.name, rel, str(content), append=append)
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        verb = "追加" if append else "写入"
        return ToolResult(ok=True, output=f"已{verb} {rel}（{len(str(content))} 字）", data={"path": rel})

    async def list_files(ctx: ToolContext, args: dict) -> ToolResult:
        listed_rel = str(args.get("path") or "")
        refused, scope_text = _scope_other_artifacts(ctx, listed_rel)
        if refused:
            return _scope_error(scope_text)
        spilled, mine = _scope_other_task_spill(ctx, listed_rel)
        if spilled:
            return _spill_scope_error(mine)
        path, err = _bound(ctx, listed_rel)
        if err:
            return err
        try:
            depth = int(args.get("depth") or 2)
        except (TypeError, ValueError):
            depth = 2
        try:
            limit = max(1, min(500, int(args.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        try:
            entries = await env.list_files(
                ctx.workspace.name, str(args.get("path") or ""), depth=depth, limit=limit
            )
        except FileNotFoundError:
            return ToolResult(ok=False, output="", error=f"工作区内没有这个目录：{args.get('path')}")
        except IsADirectoryError:
            return ToolResult(ok=False, output="", error=f"{args.get('path')} 是文件，不是目录")
        entries = [
            e for e in entries
            if isinstance(e, dict)
            and not _entry_out_of_scope(ctx, str(e.get("path") or ""), listed_rel)
            and not _entry_out_of_task_spill(ctx, str(e.get("path") or ""), listed_rel)
        ]
        lines = []
        for e in entries:
            suffix = "/" if e["is_dir"] else f"（{e['size']} 字节）"
            lines.append(f"{e['path']}{suffix}")
        if not lines:
            return ToolResult(ok=True, output="（空的）", data=[])
        return ToolResult(ok=True, output="\n".join(lines), data=entries)

    # ------------------------------------------------------------------
    # 命令工具
    # ------------------------------------------------------------------

    async def run_command(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能跑命令")
        command = str(args.get("command") or "").strip()
        if not command:
            return ToolResult(ok=False, output="", error="command 不能为空")
        cfg_timeout = max(1, int(get_settings().environments.command_timeout_s))
        try:
            timeout_s = int(args.get("timeout_s") or cfg_timeout)
        except (TypeError, ValueError):
            timeout_s = cfg_timeout
        timeout_s = max(1, min(timeout_s, cfg_timeout))  # 配置为准，模型说了不算
        try:
            result = await env.run(ctx.workspace.name, command, timeout_s=timeout_s)
        except PermissionError as e:
            return ToolResult(ok=False, output="", error=f"不允许访问：{e}")
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        head = f"退出码 {result.exit_code}"
        if result.timed_out:
            head += " · 超时被强制停止"
        if result.oom:
            head += " · 内存超限被杀（推断）"
        head += f" · 耗时 {result.ms / 1000:.1f} 秒"
        parts = [head, "--- 输出（stdout） ---"]
        parts.append(result.stdout or "（空）")
        if result.stderr:
            parts.append("--- 错误输出（stderr） ---")
            parts.append(result.stderr)
        return ToolResult(
            ok=True,
            output="\n".join(parts),
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "ms": result.ms,
                "timed_out": result.timed_out,
                "oom": result.oom,
            },
        )

    # ------------------------------------------------------------------
    # 后台进程
    # ------------------------------------------------------------------

    async def start_process(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能起进程")
        command = str(args.get("command") or "").strip()
        label = str(args.get("label") or "").strip()
        if not command:
            return ToolResult(ok=False, output="", error="command 不能为空")
        if not label:
            return ToolResult(ok=False, output="", error="label 不能为空（起个名字方便回头查）")
        try:
            timeout_s = int(args.get("timeout_s") or get_settings().environments.runtime_max_sec)
        except (TypeError, ValueError):
            timeout_s = int(get_settings().environments.runtime_max_sec)
        try:
            unit = await env.start(ctx.workspace.name, command, label=label, timeout_s=timeout_s)
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        return ToolResult(
            ok=True,
            output=f"已在后台跑起来（{label}），单元名 {unit}",
            data={"unit": unit, "label": label},
        )

    async def check_process(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能查进程")
        label = str(args.get("label") or "").strip()
        if not label:
            return ToolResult(ok=False, output="", error="label 不能为空")
        try:
            label = env._check_label(label)  # noqa: SLF001 — 同一条防线，和 start_process 共用
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        unit = f"maiwork-{ctx.workspace.name}-{label}"
        st = await env.status(unit)
        logs = await env.logs(ctx.workspace.name, unit, tail=50)
        if st["active"]:
            status_line = f"{label} 还在跑"
        else:
            code_text = f"，退出码 {st['exit_code']}" if st["exit_code"] is not None else ""
            status_line = f"{label} 没在跑了{code_text}"
        parts = [status_line]
        if logs:
            parts.append("--- 最近日志 ---")
            parts.append(logs)
        return ToolResult(ok=True, output="\n".join(parts), data={"active": st["active"], "exit_code": st["exit_code"], "logs": logs})

    async def stop_process(ctx: ToolContext, args: dict) -> ToolResult:
        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，不能停进程")
        label = str(args.get("label") or "").strip()
        if not label:
            return ToolResult(ok=False, output="", error="label 不能为空")
        try:
            label = env._check_label(label)  # noqa: SLF001 — 同一条防线，和 start_process 共用
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))
        unit = f"maiwork-{ctx.workspace.name}-{label}"
        await env.stop(unit)
        return ToolResult(ok=True, output=f"{label} 已停（如果它本来就没在跑，现在也没在跑了）")

    # ------------------------------------------------------------------
    # 群聊和记忆
    # ------------------------------------------------------------------

    async def read_chat_history(ctx: ToolContext, args: dict) -> ToolResult:
        group_id = str(ctx.group_id or "").strip()
        if not group_id:
            return ToolResult(ok=False, output="", error="拿不到当前群号")
        session_id = str(session_of(group_id) or "").strip()
        if not session_id:
            return ToolResult(ok=False, output="", error="没找到本群的会话")
        try:
            hours = float(args.get("hours") or 24)
        except (TypeError, ValueError):
            hours = 24.0
        hours = max(0.1, min(hours, 24 * 30))  # 最多往回翻 30 天
        try:
            limit = max(1, min(500, int(args.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        keyword = str(args.get("keyword") or "").strip().lower()
        end = clock.now()
        start = end - hours * 3600
        try:
            msgs = await host.messages(session_id, start, end, limit)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"读群消息失败：{e}")
        lines: list[str] = []
        for m in msgs:
            if keyword and keyword not in str(getattr(m, "text", "") or "").lower():
                continue
            stamp = clock.bj(float(getattr(m, "ts", 0.0))).strftime("%H:%M")
            name = str(getattr(m, "user_name", "") or "") or str(getattr(m, "user_id", "") or "")
            text = str(getattr(m, "text", "") or "")
            lines.append(f"[{stamp}] {name}: {text}")
        if not lines:
            hint = f"（带「{args.get('keyword')}」的）" if keyword else ""
            return ToolResult(ok=True, output=f"最近 {hours:g} 小时没有{hint}消息", data=[])
        if len(lines) > limit:
            lines = lines[-limit:]
        return ToolResult(ok=True, output="\n".join(lines), data={"count": len(lines)})

    async def search_memory(ctx: ToolContext, args: dict) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="", error="query 不能为空")
        group_id = str(ctx.group_id or "").strip()
        if not group_id:
            return ToolResult(ok=False, output="", error="拿不到当前群号")
        try:
            text = await host.knowledge(query, group_id=group_id)
        except Exception as e:
            return ToolResult(ok=False, output="", error=f"查记忆失败：{e}")
        if not str(text or "").strip():
            return ToolResult(ok=True, output="记忆里没留下这方面的印象", data={"found": False})
        return ToolResult(ok=True, output=str(text), data={"found": True})

    # ------------------------------------------------------------------
    # 注册（角色严格区分）
    # ------------------------------------------------------------------

    tools.register(
        Tool(
            name="read_file",
            description=(
                "读工作区内一个文件（utf-8 文本）。一页最多 5000 字：返回里带「共多少字 / 本页范围 / "
                "下一页 offset」，照着一页页读就能拿全，别自己拆文件。offset / limit 的单位都是字符，"
                "offset 从 0 数（0 = 文件第一个字）。整份文件不超过一页时原样返回全部内容（没有页码信息）。"
                f"单次最多读文件前 {_READ_BYTES_CAP // 10000} 万字节，更大的文件更远的内容读不到。"
                "只能在当前任务的工作区内读。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径，如 tasks/t1/out.md"},
                    "offset": {
                        "type": "integer",
                        "description": "从文件开头数第几个字开始读，0 = 第一个字（默认 0；单位：字符，不是字节）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"本页最多读多少字，1–{_PAGE_CHARS_MAX}（默认 {_PAGE_CHARS_DEFAULT}）；接着读下一页用返回里的 offset",
                    },
                },
                "required": ["path"],
            },
            roles=frozenset({"worker"}),
            handler=read_file,
            summarize=lambda args, res: (
                str(args.get("path", "")),
                (res.output[:200] + "…") if res.ok and len(res.output) > 200 else (res.output if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="write_file",
            description="写工作区内一个文件（单文件最多 5MB；append=true 追加；自动建父目录）。工作区外一律写不进去。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径"},
                    "content": {"type": "string", "description": "完整内容（文本）"},
                    "append": {"type": "boolean", "description": "true = 追加到文件末尾；false/缺省 = 整文件覆盖"},
                },
                "required": ["path", "content"],
            },
            roles=frozenset({"worker"}),
            handler=write_file,
            summarize=lambda args, res: (
                str(args.get("path", "")),
                f"{'追加' if args.get('append') else '写入'} {len(str(args.get('content', '')))} 字" if res.ok else res.error,
            ),
            timeout_s=15.0,
        )
    )
    tools.register(
        Tool(
            name="list_files",
            description="列工作区内一个目录下的文件和子目录（默认两层深、200 条），看看里面有什么。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径；空 = 工作区根"},
                    "depth": {"type": "integer", "description": "往下钻几层，默认 2"},
                    "limit": {"type": "integer", "description": "最多列多少条，默认 200，上限 500"},
                },
            },
            roles=frozenset({"worker"}),
            handler=list_files,
            summarize=lambda args, res: (
                f"列目录：{args.get('path', '') or '（根）'}",
                (f"{len(res.data or [])} 个条目" if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="run_command",
            description=f"在工作区里跑一条 bash 命令，拿回退出码、stdout、stderr、耗时。默认超时和上限以配置为准（单条命令最长 [environments] runtime_max_sec，再长的活要拆着跑）。命令里写 ~ 指工作区，不继承插件进程的环境变量。",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "bash 命令（会按 bash -lc 跑）"},
                    "timeout_s": {"type": "integer", "description": "这条命令最多跑多少秒；不给 = 配置里的默认上限；给得比配置大也会被夹住"},
                },
                "required": ["command"],
            },
            roles=frozenset({"worker"}),
            handler=run_command,
            summarize=lambda args, res: (
                f"输入「{str(args.get('command', '')).splitlines()[0] if args.get('command') else ''}」",
                (
                    (f"退出码 {res.data['exit_code']} · 超时" if res.data and res.data.get("timed_out") else f"退出码 {res.data['exit_code']}")
                    + (f" · 内存超限" if res.data and res.data.get("oom") else "")
                    + (f" · {res.data['ms'] / 1000:.1f} 秒" if res.data else "")
                )
                if res.ok and isinstance(res.data, dict)
                else (res.error or "出错"),
            ),
            timeout_s=3600.0,  # 工具自身的兜底：比 runtime_max_sec + 兜底 15s 多一档
        )
    )
    tools.register(
        Tool(
            name="start_process",
            description="在工作区里起一个后台进程（不等你），起个 label 名字。之后用 check_process 看它跑得怎么样、stop_process 停它。输出会一直写到 runtime/logs/<label>.log。",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "bash 命令"},
                    "label": {"type": "string", "description": "进程名，只能用字母数字下划线横线"},
                    "timeout_s": {"type": "integer", "description": "到点强杀；默认 = 配置里的 runtime_max_sec"},
                },
                "required": ["command", "label"],
            },
            roles=frozenset({"worker"}),
            handler=start_process,
            summarize=lambda args, res: (
                f"起进程 {args.get('label', '')}",
                res.output if res.ok else res.error,
            ),
            timeout_s=30.0,
        )
    )
    tools.register(
        Tool(
            name="check_process",
            description="看后台进程还在不在跑、退出码多少，顺带把最近几行日志捞出来。",
            parameters={
                "type": "object",
                "properties": {"label": {"type": "string", "description": "start_process 起的那个名字"}},
                "required": ["label"],
            },
            roles=frozenset({"worker"}),
            handler=check_process,
            summarize=lambda args, res: (
                f"查进程 {args.get('label', '')}",
                (res.output.splitlines()[0] if res.output else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="stop_process",
            description="停掉一个后台进程（SIGKILL）。名字写错了/本就没在跑不会报错。",
            parameters={
                "type": "object",
                "properties": {"label": {"type": "string", "description": "start_process 起的那个名字"}},
                "required": ["label"],
            },
            roles=frozenset({"worker"}),
            handler=stop_process,
            summarize=lambda args, res: (
                f"停进程 {args.get('label', '')}",
                res.output if res.ok else res.error,
            ),
            timeout_s=15.0,
        )
    )
    tools.register(
        Tool(
            name="read_chat_history",
            description="读本群最近一段时间的聊天记录（只会给你本群的，别群不给）。输出一行一条，「[HH:MM] 名字: 文本」。超过需求再带 keyword 筛。",
            parameters={
                "type": "object",
                "properties": {
                    "hours": {"type": "number", "description": "往回翻多少小时，默认 24；最多 30 天"},
                    "keyword": {"type": "string", "description": "只留文本里带这个词的消息（可选）"},
                    "limit": {"type": "integer", "description": "最多多少条，默认 200，上限 500"},
                },
            },
            roles=frozenset({"worker"}),
            handler=read_chat_history,
            summarize=lambda args, res: (
                f"读本群最近 {args.get('hours', 24)} 小时" + (f"（筛 {args.get('keyword')}）" if args.get("keyword") else ""),
                f"{(res.data or {}).get('count', 0)} 条" if res.ok else res.error,
            ),
            timeout_s=20.0,
        )
    )
    tools.register(
        Tool(
            name="search_memory",
            description="查 MaiBot 的长期记忆（按本群过滤；不是按人）。想起来要引用旧事时再用。",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "想找的内容，用人话说"}},
                "required": ["query"],
            },
            roles=frozenset({"worker"}),
            handler=search_memory,
            summarize=lambda args, res: (
                f"查记忆：{args.get('query', '')}",
                ("找到了" if (res.data or {}).get("found") else "没印象") if res.ok else res.error,
            ),
            timeout_s=20.0,
        )
    )
    tools.register(
        Tool(
            name="inspect_file",
            description=(
                "（主模型验收用）读工作区内一个文件，和子 agent 的 read_file 是同一个东西的只读版："
                "同样支持 offset / limit 分页（单位：字符，offset 0 = 第一个字，单页最多 "
                f"{_PAGE_CHARS_MAX} 字），返回里带文件总字数与下一页 offset，大文件要一页页读完再下结论。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径"},
                    "offset": {
                        "type": "integer",
                        "description": "从文件开头数第几个字开始读，0 = 第一个字（默认 0；单位：字符）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"本页最多读多少字，1–{_PAGE_CHARS_MAX}（默认 {_PAGE_CHARS_DEFAULT}）",
                    },
                },
                "required": ["path"],
            },
            roles=frozenset({"main"}),
            handler=inspect_file,
            summarize=lambda args, res: (
                f"（验收）{args.get('path', '')}",
                (res.output[:120] + "…") if res.ok and len(res.output) > 120 else (res.output if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )
    tools.register(
        Tool(
            name="inspect_files",
            description="（主模型验收用）列工作区内一个目录下的文件，和子 agent 的 list_files 是同一个东西的只读版。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区内的相对路径；空 = 根"},
                    "depth": {"type": "integer", "description": "往下钻几层，默认 2"},
                    "limit": {"type": "integer", "description": "最多多少条，默认 200"},
                },
            },
            roles=frozenset({"main"}),
            handler=list_files,
            summarize=lambda args, res: (
                f"（验收）{args.get('path', '') or '根目录'}",
                (f"{len(res.data or [])} 个条目" if res.ok else res.error),
            ),
            timeout_s=10.0,
        )
    )


__all__ = ["register_exec_tools"]
