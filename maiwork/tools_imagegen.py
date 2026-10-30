"""`generate_image` 工具（2026-10-15 生图第一期）：子 agent 自己画图，成品落进任务目录。

分工（research/imagegen.md §3.4）：
- **照片 / 插画 / 氛围图 / 概念图** → 这个工具；
- **数字 / 图表 / 资讯卡片** → 继续用确定性渲染（数字要能逐个对原文，AI 生图画不准）。

落盘位置固定 `artifacts/<任务ID>/images/<名字>.<ext>`：
- 交付 / 验收 / 网页都按「工作区相对路径」找成品，路径稳定才引用得住；
- 范围检查沿用 tools_exec 的成品目录口径（`ToolContext.artifact_scope` 非空时，
  只能写本任务的目录）；**不套 write_scope**（那是「各步骤各写各的文件夹」用的，
  图不是步骤中间产物，交付步骤要能引用到同一个位置）。

安全 / 成本：
- 没配生图模型（没有 use="image" 的条目 / 端点缺地址密钥）→ 中文报错，**不发请求**；
- 图片 URL 由服务商给，可能指向内网 → 下载走 imagegen 的 SSRF 闸（复用 fetch_page 那套）；
- 密钥只从端点配置读、只进请求头；返回值和 tool_calls 里只有相对路径和尺寸；
- 每次调用记一条 `imagegen.calls`（总次数 / 成功 / 失败 / 张数 / 最后时间），
  管理员能看出花了多少钱（token 口径的用量表算不进生图）。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from . import clock
from . import imagegen as imagegen_mod
from .imagegen import ImageGenError
from .tools import Tool, ToolContext, ToolResult, Tools

logger = logging.getLogger("maiwork.tools_imagegen")

# 一次生图的整体超时（工具层比 imagegen 的默认略宽，留出下载时间）
TOOL_TIMEOUT_S = 300.0
# prompt 上限（够写清主体 / 场景 / 风格；太长纯属浪费 token）
PROMPT_MAX = 2000
# filename 上限（含扩展名）
FILENAME_MAX = 80
# 同目录重名时最多试到 -v20
_MAX_VERSIONS = 20
# 记账用的 kv 键
KV_CALLS = "imagegen.calls"
# kv["imagegen.calls"]["days"] 只留最近多少天（够用量页看「近 7 天」）
_KEEP_DAYS = 31
# 网页选的「用哪个生图模型」（不填 = 模型库里第一个 use="image" 的条目）
KV_MODEL = "imagegen.model"

_NAME_BAD_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff-]")


def image_entries(settings: Any) -> list[Any]:
    """当前配置里能用的生图条目（use="image" 且端点有地址和密钥）。"""
    eps = {str(getattr(e, "id", "") or ""): e for e in (getattr(settings, "endpoints", ()) or ())}
    out: list[Any] = []
    for m in (getattr(settings, "model_list", ()) or ()):
        if str(getattr(m, "use", "chat") or "chat").strip().lower() != "image":
            continue
        ep = eps.get(str(getattr(m, "endpoint", "") or ""))
        if ep is None:
            continue
        if not str(getattr(ep, "base_url", "") or "").strip():
            continue
        if not str(getattr(ep, "api_key", "") or "").strip():
            continue
        out.append(m)
    return out


def pick_image_entry(settings: Any, store: Any = None) -> Any | None:
    """这次用哪个生图模型：网页选的（kv）优先，其次模型库里第一个。

    选中的条目没了 / 变成聊天条目 → 自动回落第一个（不报「配置坏了」，但也不会静默乱用）。
    """
    entries = image_entries(settings)
    if not entries:
        return None
    want = ""
    if store is not None:
        try:
            want = str(store.kv_get(KV_MODEL) or "").strip()
        except Exception:
            want = ""
    if want:
        for m in entries:
            if str(getattr(m, "id", "") or "") == want:
                return m
    return entries[0]


def endpoint_of(settings: Any, entry: Any) -> Any | None:
    eid = str(getattr(entry, "endpoint", "") or "")
    for ep in (getattr(settings, "endpoints", ()) or ()):
        if str(getattr(ep, "id", "") or "") == eid:
            return ep
    return None


def _norm_scope(scope: Any) -> list[str]:
    """scope 规范化成 ["artifacts/T-4", …]（和 tools_exec 同一口径：剥 ./ 和 . 段、去多余斜杠）。"""
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


def _scope_allows(scope: Any, rel: str) -> tuple[bool, str]:
    """rel 在这个成品目录范围里吗？回 (允许?, scope 文本)。范围空 = 不限制。"""
    allowed = _norm_scope(scope)
    if not allowed:
        return True, ""
    parts = [p for p in Path(str(rel or "")).parts if p not in ("", ".")]
    for a in allowed:
        a_parts = a.split("/")
        if parts[: len(a_parts)] == a_parts:
            return True, ""
    return False, "、".join(f"{a}/" for a in allowed)


def _clean_filename(raw: str) -> str:
    """filename → 干净的主文件名（不含扩展名）；不合法抛 ValueError（中文）。

    只收「文件名」：带目录分隔符 / `..` / 以点开头一律拒（路径安全第一）；
    别的怪字符换成 `_`（中文、字母、数字、下划线、点、横线保留）。
    """
    name = str(raw or "").strip()
    if not name:
        return ""
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ValueError("filename 只能是文件名：不能带目录、`..` 或以点开头（图会存进这次任务的 images/ 目录）")
    if len(name) > FILENAME_MAX:
        raise ValueError(f"filename 太长（上限 {FILENAME_MAX} 字）")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = _NAME_BAD_RE.sub("_", stem).strip("_")
    return stem[:60] or "image"


def _slug_from_prompt(prompt: str) -> str:
    text = re.sub(r"\s+", "-", str(prompt or "").strip())[:30]
    slug = _NAME_BAD_RE.sub("", text).strip("-")
    return slug[:30] or "image"


def _write_bytes(path: Path, data: bytes) -> None:
    """安全落盘：O_NOFOLLOW + 独立 inode（和 coordinator 收原图一个口径）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PermissionError(f"目标文件是符号链接，不写：{path.name}")
    fd = os.open(
        str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644,
    )
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _bump(store: Any, *, ok: bool, images: int, model: str, error: str = "") -> None:
    """记一次调用（kv["imagegen.calls"]）：管理员看得到生图花了多少次、出了多少张。

    失败也要记（成本照样发生）；记账本身出问题绝不拖垮这次工具调用。

    记录格式（2026-10-15 起，给用量页用；**向后兼容**老记录）：
    - `total` / `ok` / `failed` / `images`：累计（老键，一个字不改）；
    - `last_ts` / `last_ok` / `last_model` / `last_error`：最近一次调用（老键）；
    - `days`：{北京日期: {"calls","ok","failed","images"}}，只留最近 `_KEEP_DAYS` 天；
      老记录没有这个键 → 用量页把「今天 / 近 7 天」算 0，累计照旧能看；
    - `last_failure_ts` / `last_failure_model` / `last_failure_error`：**最近一次失败**
      （后面成功了也不清），用量页据此显示「最近一次失败原因」。
    """
    if store is None:
        return
    try:
        cur = store.kv_get(KV_CALLS)
        stat = dict(cur) if isinstance(cur, dict) else {}
        stat["total"] = int(stat.get("total") or 0) + 1
        stat["ok" if ok else "failed"] = int(stat.get("ok" if ok else "failed") or 0) + 1
        stat["images"] = int(stat.get("images") or 0) + max(0, int(images))
        now = clock.now()
        stat["last_ts"] = now
        stat["last_ok"] = bool(ok)
        stat["last_model"] = str(model or "")[:80]
        stat["last_error"] = str(error or "")[:200]
        # 按天（北京日期）攒一份：老记录没有 days → 从现在开始记
        days = stat.get("days") if isinstance(stat.get("days"), dict) else {}
        day = clock.day_key(now)
        bucket = days.get(day) if isinstance(days.get(day), dict) else {}
        days[day] = {
            "calls": int(bucket.get("calls") or 0) + 1,
            "ok": int(bucket.get("ok") or 0) + (1 if ok else 0),
            "failed": int(bucket.get("failed") or 0) + (0 if ok else 1),
            "images": int(bucket.get("images") or 0) + max(0, int(images)),
        }
        # 只留最近 _KEEP_DAYS 天（键是 YYYY-MM-DD，按字符串排序就是按日期排序）
        if len(days) > _KEEP_DAYS:
            for stale in sorted(days)[:-_KEEP_DAYS]:
                days.pop(stale, None)
        stat["days"] = days
        if not ok:
            stat["last_failure_ts"] = now
            stat["last_failure_model"] = str(model or "")[:80]
            stat["last_failure_error"] = str(error or "")[:200]
        with store.tx() as conn:
            store.kv_set(conn, KV_CALLS, stat)
    except Exception:
        logger.exception("生图记账失败（不影响这次调用）")


def register_image_tools(
    tools: Tools,
    *,
    get_settings: Callable[[], Any],
    env: Any = None,             # LocalEnv（落盘用它的 resolve 做越界检查）
    store: Any = None,           # Store（记账 / 记住用哪个生图模型）
    http_transport: Any = None,  # 测试注入 httpx.MockTransport
    resolver: Any = None,        # 测试注入 DNS resolver（host -> [ip]）
) -> None:
    """注册 generate_image（roles={"worker"}：只给子 agent；主模型不亲自生图）。"""

    async def generate_image(ctx: ToolContext, args: dict) -> ToolResult:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(ok=False, output="", error="prompt 不能为空：写清想画什么")
        if len(prompt) > PROMPT_MAX:
            return ToolResult(ok=False, output="", error=f"prompt 太长（上限 {PROMPT_MAX} 字），写重点")

        settings = get_settings()
        entry = pick_image_entry(settings, store)
        if entry is None:
            return ToolResult(
                ok=False, output="",
                error=(
                    "没配生图模型：先到网页「设置 → 模型」加一个用途是「生图」的模型"
                    "（端点要填地址和密钥），再来画图"
                ),
            )
        ep = endpoint_of(settings, entry)
        if ep is None:
            return ToolResult(ok=False, output="", error="这个生图模型挂的端点不在了，先到「设置 → 模型」修一下")
        model = str(getattr(entry, "model", "") or "").strip()
        api = str(getattr(entry, "image_api", "openai") or "openai").strip().lower()

        if ctx.workspace is None:
            return ToolResult(ok=False, output="", error="这次任务没带工作区，图没地方存")
        if env is None or not hasattr(env, "resolve"):
            return ToolResult(ok=False, output="", error="这次的执行环境不支持存图，画不了")
        task_id = str(getattr(ctx, "task_id", "") or "").strip()
        if not task_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
            return ToolResult(ok=False, output="", error="这次没有可用的任务号，图不知道该存进哪个任务目录")

        # 尺寸：显式给的优先，其次条目默认，再没有就不发这个字段
        size = str(args.get("size") or "").strip() or str(getattr(entry, "image_default_size", "") or "").strip()
        if len(size) > 40:
            return ToolResult(ok=False, output="", error="size 太长（上限 40 字，例如 1024x1024）")

        # 张数：上限取条目配置（配置层已夹到 1~4）
        max_n = int(getattr(entry, "image_max_n", 1) or 1)
        raw_n = args.get("n")
        if raw_n is None or raw_n == "":
            n = 1
        else:
            try:
                n = int(raw_n)
            except (TypeError, ValueError):
                return ToolResult(ok=False, output="", error=f"n 要是整数（1 到 {max_n}）")
        if n < 1 or n > max_n:
            return ToolResult(
                ok=False, output="",
                error=f"n 只能是 1 到 {max_n}（这个生图模型一次最多 {max_n} 张）；要更多就分几次画",
            )

        try:
            stem = _clean_filename(str(args.get("filename") or "")) or _slug_from_prompt(prompt)
        except ValueError as e:
            return ToolResult(ok=False, output="", error=str(e))

        rel_dir = f"artifacts/{task_id}/images"
        allowed, scope_text = _scope_allows(getattr(ctx, "artifact_scope", None), rel_dir + "/x")
        if not allowed:
            return ToolResult(
                ok=False, output="",
                error=f"这是别的任务的文件，和本任务无关，不能写；你的成品目录是 {scope_text}",
            )

        started = clock.now()
        try:
            results = await imagegen_mod.generate(
                api=api,
                base_url=str(getattr(ep, "base_url", "") or ""),
                api_key=str(getattr(ep, "api_key", "") or ""),
                model=model,
                prompt=prompt,
                size=size,
                n=n,
                extra_headers=dict(getattr(ep, "headers", None) or {}),
                transport=http_transport,
                resolver=resolver,
                timeout=imagegen_mod.DEFAULT_TIMEOUT_S,
            )
        except ImageGenError as e:
            _bump(store, ok=False, images=0, model=model, error=e.message)
            logger.info("生图失败（条目 %s，模型 %s）：%s",
                        str(getattr(entry, "id", "") or ""), model, e.message[:160])
            return ToolResult(ok=False, output="", error=e.message)
        except Exception as e:  # 兜底：意外异常也要中文、也要记账
            _bump(store, ok=False, images=0, model=model, error=str(e))
            logger.exception("生图意外出错（条目 %s）", str(getattr(entry, "id", "") or ""))
            return ToolResult(ok=False, output="", error=f"生图出错：{e}")

        saved: list[str] = []
        for i, img in enumerate(results):
            base_name = stem if len(results) == 1 else f"{stem}-{i + 1}"
            rel, err = _save_one(env, ctx.workspace.name, rel_dir, base_name, img.ext, img.data)
            if err:
                _bump(store, ok=False, images=len(saved), model=model, error=err)
                return ToolResult(ok=False, output="", error=err)
            saved.append(rel)
        if not saved:
            _bump(store, ok=False, images=0, model=model, error="没有图片可存")
            return ToolResult(ok=False, output="", error="生图服务这次一张图都没给，再试一次")

        elapsed_ms = int(max(0.0, clock.now() - started) * 1000)
        _bump(store, ok=True, images=len(saved), model=model)
        logger.info("生图成功（条目 %s，模型 %s，%d 张，%d 毫秒）",
                    str(getattr(entry, "id", "") or ""), model, len(saved), elapsed_ms)
        secs = f"{elapsed_ms / 1000:.1f}"
        size_text = size or "默认尺寸"
        files_text = "、".join(saved)
        return ToolResult(
            ok=True,
            output=(
                f"出了 {len(saved)} 张图（{size_text}，{secs} 秒）：{files_text}。"
                "图已存进这次任务的成品目录，交付说明 / 网页里直接用这个相对路径。"
            ),
            data={
                "files": saved, "count": len(saved), "model": model,
                "size": size, "elapsed_ms": elapsed_ms,
            },
        )

    def _save_one(env_obj: Any, ws_name: str, rel_dir: str, stem: str, ext: str, data: bytes) -> tuple[str, str]:
        """把一张图写进 rel_dir/stem.ext（重名顺延 -v2、-v3…）；回 (相对路径, 错误)。"""
        try:
            for v in range(1, _MAX_VERSIONS + 1):
                name = f"{stem}.{ext}" if v == 1 else f"{stem}-v{v}.{ext}"
                rel = f"{rel_dir}/{name}"
                try:
                    path = env_obj.resolve(ws_name, rel)
                except PermissionError as e:
                    return "", f"不允许访问：{e}"
                except ValueError as e:
                    return "", str(e)
                try:
                    _write_bytes(path, data)
                except FileExistsError:
                    continue
                except PermissionError as e:
                    return "", f"不允许访问：{e}"
                except OSError as e:
                    return "", f"图存不下（{e}），这次结果用不了"
                return rel, ""
            return "", f"同名文件太多了（{stem}），换个 filename 再来"
        except Exception as e:  # 兜底
            return "", f"图存不下：{e}"

    tools.register(
        Tool(
            name="generate_image",
            description=(
                "画一张图（AI 生图），成品存进这次任务的 artifacts/<任务>/images/ 目录，"
                "返回工作区相对路径，交付说明和网页里直接用。"
                "适合：照片、插画、氛围图、封面、概念示意图。"
                "不适合：数字、图表、资讯卡片——那些要用能逐个对原文的确定性做法（数字会被画错）。"
                "没配生图模型时会直接告诉你。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "想画什么。写清主体、场景、风格、构图；不要写文件路径或内部说法。",
                    },
                    "size": {
                        "type": "string",
                        "description": "尺寸（可选），例如 1024x1024、1536x1024；不填用设置里的默认。",
                    },
                    "n": {
                        "type": "integer",
                        "description": "要几张（可选，默认 1，上限由设置里这个模型定）。",
                    },
                    "filename": {
                        "type": "string",
                        "description": "存成什么名字（可选，如 hero 或 hero.png）；不给就按提示词自动起名。",
                    },
                },
                "required": ["prompt"],
            },
            roles=frozenset({"worker"}),
            handler=generate_image,
            summarize=_summarize,
            timeout_s=TOOL_TIMEOUT_S,
            needs_workspace=True,
            exec_class=True,
            task_only=True,
        )
    )


def _summarize(args: Any, result: ToolResult) -> tuple[str, str]:
    """网页任务时间线里的一行：输入 → 输出（不含密钥、不含临时 URL）。"""
    data = result.data if isinstance(result.data, dict) else {}
    prompt = str((args or {}).get("prompt") or "").strip().replace("\n", " ")
    if len(prompt) > 60:
        prompt = prompt[:60] + "…"
    left = f"生图：{prompt or '(没写提示词)'}"
    if not result.ok:
        return left, f"没画成：{str(result.error or '')[:120]}"
    files = [str(x) for x in (data.get("files") or [])]
    count = int(data.get("count") or len(files))
    secs = int(data.get("elapsed_ms") or 0) / 1000
    size = str(data.get("size") or "默认尺寸")
    right = f"出了 {count} 张（{size}，{secs:.1f} 秒）→ {'、'.join(files)}"
    return left, right
