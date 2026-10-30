"""settings_ops.py：网页设置页和管理员对话**共用**的「改设置」逻辑（docs/02 §18）。

为什么单独一个模块：网页的 /api/settings/endpoints*、/api/settings/model-list*、
/api/agents* 和管理员对话里的同名能力（tools_admin 的 endpoint_save / model_save /
agent_create …）必须是**同一套校验和写入**——不能网页一套规则、对话另一套。
网页接口（console/server.py）只管鉴权、读请求体、把 OpError 翻成 HTTP 状态码；
管理员对话工具只管确认门和「密钥不经过对话」，真正的校验和落盘都在这里。

对外接口：
- 端点：endpoint_entries / endpoint_public / endpoints_view / validate_endpoint_headers /
  validate_endpoint_payload / save_endpoint / check_endpoint_delete / delete_endpoint
- 模型库：model_public / validate_model_payload / save_model / check_model_delete / delete_model
- 专岗：agent_create_custom / agent_delete_custom
- OpError(status, message)：校验 / 找不到 / 冲突；status 就是网页接口回的 HTTP 状态码。

密钥口径（docs/02 §18、AGENTS.md 线上红线 6）：endpoint_public / model_public / endpoints_view
只回 key_set 布尔和请求头**名字**，绝不回 api_key 或头的值（只进不出）。
"""

from __future__ import annotations

import logging
from typing import Any

from . import config as _cfg
from . import config_file as _cf

logger = logging.getLogger("maiwork.settings_ops")


class OpError(Exception):
    """改设置失败：status = 网页接口该回的 HTTP 状态码，message = 中文原因。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


# ----------------------------------------------------------------------
# 读：只进不出的视图
# ----------------------------------------------------------------------


def endpoint_entries(settings: Any) -> list[dict[str, Any]]:
    """当前全部端点的完整字典（**含 api_key / headers 值，只给写路径用，不许出接口**）。"""
    return [
        dict(
            id=str(ep.id), name=str(ep.name), protocol=str(ep.protocol), base_url=str(ep.base_url),
            api_key=str(getattr(ep, "api_key", "") or ""), retries=int(ep.retries),
            retry_delay_s=int(ep.retry_delay_s), max_concurrency=int(ep.max_concurrency),
            max_rpm=int(ep.max_rpm),
            headers=dict(getattr(ep, "headers", None) or {}),
        )
        for ep in (getattr(settings, "endpoints", ()) or ())
    ]


def endpoint_public(svc: Any, ep: Any) -> dict[str, Any]:
    """一个端点的对外视图：api_key 只给 key_set 布尔，请求头只给名字。"""
    d: dict[str, Any] = {
        "id": str(ep.id), "name": str(ep.name), "protocol": str(ep.protocol),
        "base_url": str(ep.base_url), "key_set": bool(str(getattr(ep, "api_key", "") or "")),
        "retries": int(ep.retries), "retry_delay_s": int(ep.retry_delay_s),
        "max_concurrency": int(ep.max_concurrency), "max_rpm": int(ep.max_rpm),
        # 网页「按行加头」用：只回名字，绝不回值（值是凭据）
        "header_names": [
            str(k) for k in (getattr(ep, "headers", None) or {}).keys() if str(k or "").strip()
        ],
    }
    store = getattr(svc, "store", None)
    checked = store.kv_get(f"endpoints.checked.{ep.id}") if store is not None else None
    if isinstance(checked, dict):
        try:
            d["checked_at"] = float(checked.get("checked_at") or 0.0)
        except (TypeError, ValueError):
            d["checked_at"] = 0.0
        raw = checked.get("available")
        d["available"] = [str(x) for x in raw] if isinstance(raw, list) else []
    return d


def model_public(entry: Any) -> dict[str, Any]:
    """模型库一条的对外视图（模型库条目本身没有密钥）。"""
    return {
        "id": str(entry.id), "endpoint": str(entry.endpoint), "model": str(entry.model),
        "name": str(entry.name), "efforts": [str(x) for x in (entry.efforts or ())],
        "vision": bool(entry.vision),
        "context_window": int(entry.context_window), "max_tokens": int(entry.max_tokens),
        # 2026-10-15 生图第一期：用途 + 生图参数（密钥照旧只给 key_set 布尔）
        "use": str(getattr(entry, "use", "chat") or "chat"),
        "image_api": str(getattr(entry, "image_api", "openai") or "openai"),
        "image_default_size": str(getattr(entry, "image_default_size", "") or ""),
        "image_max_n": int(getattr(entry, "image_max_n", 1) or 1),
    }


def endpoints_view(svc: Any) -> dict[str, Any]:
    """GET /api/settings/endpoints 的形状：{"endpoints": [...], "models": [...]}（不含密钥）。"""
    settings = svc.get_settings()
    return {
        "endpoints": [endpoint_public(svc, ep) for ep in (getattr(settings, "endpoints", ()) or ())],
        "models": [model_public(m) for m in (getattr(settings, "model_list", ()) or ())],
    }


# ----------------------------------------------------------------------
# 端点：校验 + 写 config.toml
# ----------------------------------------------------------------------


def validate_endpoint_headers(raw: Any, saved: Any) -> dict[str, str]:
    """保存与试连共用：完整名称集合；已存同名空值保留，其余删除。"""
    if not isinstance(raw, dict):
        raise ValueError("headers 要是对象（名字→值）")
    if len(raw) > 32:
        raise ValueError("请求头个数超了（最多 32 个）")
    old_ci = {name.lower(): value for name, value in dict(saved or {}).items()}
    out: dict[str, str] = {}
    seen: set[str] = set()
    for raw_name, value in raw.items():
        error, _ = _cfg.validate_endpoint_header_name(raw_name)
        if error:
            raise ValueError(error)
        name = raw_name.strip()
        low = name.lower()
        if low in seen:
            raise ValueError("请求头同名不同大小写重复（名字大小写不敏感）")
        seen.add(low)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("新请求头的值要填非空字符串")
            error = _cfg.validate_endpoint_header_value(value)
            if error:
                raise ValueError(error)
        # 先校验再判断空值，换行/控制/超长空白不能被当成「保留」。
        if value is None or not value.strip():
            if low not in old_ci:
                raise ValueError("新请求头的值要填非空字符串")
            out[name] = old_ci[low]
        else:
            out[name] = value.strip()
    return out


def validate_endpoint_payload(
    body: dict[str, Any], path_id: str, existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 body 合出一份端点字典；格式错误直接抛 ValueError(中文)。
    api_key：空串/没给 = 保持 old 值（新建=空），非空串 = 覆盖。

    headers：没给「headers」键 → 原样保留；给了（对象）= 完整名单覆盖（规则见
    validate_endpoint_headers；错误文案带中文原因，绝不透传值本身）。
    """
    raw_patch: dict[str, Any] = {}
    for key in ("name", "protocol", "base_url"):
        if key in body:
            raw_patch[key] = body[key]
    for key in ("retries", "retry_delay_s", "max_concurrency", "max_rpm"):
        if key in body and body[key] is not None:
            raw_patch[key] = body[key]
    cand: dict[str, Any] = dict(existing or {})
    cand.update(raw_patch)
    cand["id"] = path_id  # id 以路由为准（body.id 一致才放行，不然误导）
    if "id" in body and str(body.get("id") or "").strip() != path_id:
        raise ValueError("地址里的端点 id 和 body.id 不一致")
    # api_key：只进；空串/没给 = 不改
    if "api_key" in body:
        key_v = body.get("api_key")
        if key_v is not None and str(key_v).strip():
            cand["api_key"] = str(key_v)
    elif "api_key" not in cand:
        cand["api_key"] = ""

    saved_headers = dict(cand.get("headers") or {})
    cand["headers"] = (
        validate_endpoint_headers(body["headers"], saved_headers)
        if "headers" in body else saved_headers
    )

    problems: list[str] = []
    parsed = _cfg._parse_endpoints([cand], problems)
    if problems:
        raise ValueError(problems[0])
    if not parsed:
        raise ValueError("端点参数不合法")
    e = parsed[0]
    return {
        "id": e.id, "name": e.name, "protocol": e.protocol, "base_url": e.base_url,
        "api_key": e.api_key, "retries": e.retries, "retry_delay_s": e.retry_delay_s,
        "max_concurrency": e.max_concurrency, "max_rpm": e.max_rpm,
        "headers": dict(e.headers or {}),
    }


async def apply_config_after_file_write(svc: Any, new_text: str) -> None:
    """写完文件立刻在本进程应用；失败只记日志（宿主文件监控会补一次）。"""
    try:
        await svc.apply_config_text(new_text)
    except Exception:
        logger.exception("写后应用出错（文件已写，宿主文件监控会补一次）")


def _write_section(svc: Any, section: str, entries: list[dict[str, Any]]) -> str:
    try:
        path, example = svc.config_file_ops()
        return _cf.write_aot_section(path, example, section, entries)
    except _cf.ConfigFileError as e:
        raise OpError(500, str(e)) from None


def find_endpoint(settings: Any, endpoint_id: str) -> dict[str, Any] | None:
    """按 id 取端点的完整字典（含密钥，内部用）；没有 → None。"""
    eid = str(endpoint_id or "").strip()
    return next((e for e in endpoint_entries(settings) if e["id"] == eid), None)


def preview_endpoint(svc: Any, path_id: str, body: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """不写盘，先把这次保存会得到的端点算出来：(旧值或 None, 新值)。

    和 save_endpoint 走同一套校验（新建的 id 规则、字段合法性）；不合法抛 OpError(400)。
    管理员对话用它判断「是不是新建 / 改了地址」再决定要不要先等确认。
    """
    settings = svc.get_settings()
    path_id = str(path_id or "").strip()
    old = find_endpoint(settings, path_id)
    if old is None:
        # 新建再走一遍 id 规则（路径 id 本身就得合法，不然 table 会跳过）
        problems: list[str] = []
        _cfg._parse_endpoints([{"id": path_id, "base_url": "https://x.test"}], problems)
        if problems:
            raise OpError(400, problems[0])
    try:
        cand = validate_endpoint_payload(body, path_id, old)
    except ValueError as e:
        raise OpError(400, str(e)) from None
    return old, cand


async def save_endpoint(svc: Any, path_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """新建或修改一个端点（PUT /api/settings/endpoints/{id} 的落点），写 config.toml 并热应用。

    返回保存后的端点字典（**含密钥，调用方不许原样回给外面**）。OpError：400 校验 / 500 写文件。
    """
    settings = svc.get_settings()
    entries = endpoint_entries(settings)
    old, cand = preview_endpoint(svc, path_id, body)
    # 新建不许和已有 id 撞
    if old is None and any(e["id"] == cand["id"] for e in entries):
        raise OpError(400, f"端点 id「{cand['id']}」已经存在")
    entries = [cand if e["id"] == cand["id"] else e for e in entries] if old else entries + [cand]
    new_text = _write_section(svc, "endpoints", entries)
    await apply_config_after_file_write(svc, new_text)
    logger.info("端点「%s」已保存（key_set=%s）", cand["id"], bool(cand.get("api_key")))
    return cand


def check_endpoint_delete(svc: Any, path_id: str) -> None:
    """删端点前的检查：没有 → OpError(404)；模型库还有条目挂在上面 → OpError(400)。"""
    settings = svc.get_settings()
    path_id = str(path_id or "").strip()
    if find_endpoint(settings, path_id) is None:
        raise OpError(404, "没有这个端点")
    users = [m for m in (getattr(settings, "model_list", ()) or ()) if str(getattr(m, "endpoint", "")) == path_id]
    if users:
        raise OpError(400, f"模型库里还有 {len(users)} 条模型挂在这个端点上，先把它们删掉或换到别的端点")


async def delete_endpoint(svc: Any, path_id: str) -> None:
    """删一个端点（DELETE /api/settings/endpoints/{id} 的落点）。"""
    path_id = str(path_id or "").strip()
    check_endpoint_delete(svc, path_id)
    entries = [e for e in endpoint_entries(svc.get_settings()) if e["id"] != path_id]
    new_text = _write_section(svc, "endpoints", entries)
    await apply_config_after_file_write(svc, new_text)
    try:
        store = getattr(svc, "store", None)
        if store is not None:
            with store.tx() as conn:
                conn.execute("DELETE FROM kv WHERE key=?", (f"endpoints.checked.{path_id}",))
    except Exception:
        pass
    logger.info("端点「%s」已删除", path_id)


# ----------------------------------------------------------------------
# 模型库（[[model_list]]）
# ----------------------------------------------------------------------


def validate_model_payload(
    body: dict[str, Any], path_id: str, existing: dict[str, Any] | None, endpoints: tuple,
) -> dict[str, Any]:
    raw_patch: dict[str, Any] = {}
    for key in ("endpoint", "model", "name"):
        if key in body:
            raw_patch[key] = body[key]
    if "efforts" in body and body["efforts"] is not None:
        raw_patch["efforts"] = body["efforts"]
    if "vision" in body and body["vision"] is not None:
        raw_patch["vision"] = body["vision"]
    for key in ("context_window", "max_tokens"):
        if key in body and body[key] is not None:
            raw_patch[key] = body[key]
    # 2026-10-15 生图第一期：用途 + 生图参数（不认识的值由 _parse_model_list 记问题 → 400）
    for key in ("use", "image_api", "image_default_size", "image_max_n"):
        if key in body and body[key] is not None:
            raw_patch[key] = body[key]
    cand: dict[str, Any] = dict(existing or {})
    cand.update(raw_patch)
    cand["id"] = path_id
    if "id" in body and str(body.get("id") or "").strip() != path_id:
        raise ValueError("地址里的 id 和 body.id 不一致")
    problems: list[str] = []
    parsed = _cfg._parse_model_list([cand], endpoints, problems)
    if problems:
        raise ValueError(problems[0])
    if not parsed:
        raise ValueError("模型条目参数不合法")
    m = parsed[0]
    return {
        "id": m.id, "endpoint": m.endpoint, "model": m.model, "name": m.name,
        "efforts": list(m.efforts or ()), "vision": bool(m.vision),
        "context_window": int(m.context_window), "max_tokens": int(m.max_tokens),
        "use": m.use, "image_api": m.image_api,
        "image_default_size": m.image_default_size, "image_max_n": int(m.image_max_n),
    }


async def save_model(svc: Any, path_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """新建或修改模型库一条（PUT /api/settings/model-list/{id} 的落点）。返回保存后的条目。

    body.if_fingerprint 给了就先核对「验证期间配置没变」，对不上 → OpError(409)。
    OpError：400 校验 / 409 指纹 / 500 写文件。
    """
    settings = svc.get_settings()
    endpoints = tuple(getattr(settings, "endpoints", ()) or ())
    entries = [model_public(m) for m in (getattr(settings, "model_list", ()) or ())]
    path_id = str(path_id or "").strip()
    old = next((m for m in entries if m["id"] == path_id), None)
    expected = body.get("if_fingerprint")
    if expected is not None:
        if not isinstance(expected, str) or not expected or svc.models.verification_stamp(path_id) != expected:
            raise OpError(409, "验证期间模型配置已经改变：请重新验证后再保存建议，未覆盖你的新配置")
    if old is None:
        problems: list[str] = []
        _cfg._parse_model_list([{"id": path_id, "endpoint": "__x__", "model": "y"}], endpoints, problems)
        if problems and "不合法" in problems[0]:
            raise OpError(400, problems[0])
    try:
        cand = validate_model_payload(body, path_id, old, endpoints)
    except ValueError as e:
        raise OpError(400, str(e)) from None
    if old is None and any(m["id"] == cand["id"] for m in entries):
        raise OpError(400, f"模型条目 id「{cand['id']}」已经存在")
    entries = [cand if m["id"] == cand["id"] else m for m in entries] if old else entries + [cand]
    new_text = _write_section(svc, "model_list", entries)
    await apply_config_after_file_write(svc, new_text)
    logger.info("模型条目「%s」已保存（服务端模型名 %s）", cand["id"], cand.get("model", ""))
    return cand


def check_model_delete(svc: Any, path_id: str) -> None:
    """删模型库条目前的检查：没有 → OpError(404)；还有专岗在用 → OpError(400，点名岗位)。"""
    settings = svc.get_settings()
    path_id = str(path_id or "").strip()
    entries = [model_public(m) for m in (getattr(settings, "model_list", ()) or ())]
    if not any(m["id"] == path_id for m in entries):
        raise OpError(404, "没有这个模型条目")
    users: list[str] = []
    agents_mod = getattr(svc, "agents", None)
    if agents_mod is not None:
        try:
            for p in agents_mod.profiles():
                if str(p.get("model") or "") == path_id or str(p.get("backup") or "") == path_id:
                    users.append(str(p.get("title") or p.get("kind") or "?"))
        except Exception:
            pass
    if users:
        raise OpError(400, f"还有专岗在用这个模型（{'、'.join(users)}），先到「专岗」页改掉再删")


async def delete_model(svc: Any, path_id: str) -> None:
    """删模型库一条（DELETE /api/settings/model-list/{id} 的落点）。"""
    path_id = str(path_id or "").strip()
    check_model_delete(svc, path_id)
    settings = svc.get_settings()
    entries = [model_public(m) for m in (getattr(settings, "model_list", ()) or ()) if str(m.id) != path_id]
    new_text = _write_section(svc, "model_list", entries)
    await apply_config_after_file_write(svc, new_text)
    logger.info("模型条目「%s」已删除", path_id)


# ----------------------------------------------------------------------
# 专岗（自定义专岗的建 / 删；改岗位直接调 svc.agents.update_profile）
# ----------------------------------------------------------------------


def _agents_mod(svc: Any) -> Any:
    mod = getattr(svc, "agents", None)
    if mod is None:
        raise OpError(503, "这个功能还没开")
    return mod


async def agent_create_custom(svc: Any, title: str) -> dict[str, Any]:
    """新建自定义专岗（POST /api/agents 的落点）：建 kv + 落身份文档目录。返回 profile。"""
    mod = _agents_mod(svc)
    try:
        profile = mod.create_custom(str(title or ""))
    except ValueError as e:
        raise OpError(400, str(e)) from None
    # 建 kv 成功后落 identity 文档（SOUL 从 MaiBot 同步或空 + AGENTS=custom 预设
    # + main AGENTS.md 自动加一行 stub）。identity 缺位 → 只警告，kv 那份还在
    # （下轮启动/修复 identity 再补建也来得及——kind 已经有了）。
    ident = getattr(svc, "identity", None)
    if ident is None:
        logger.warning("identity 没就位：自定义专岗 %s 只建了 kv，文档目录跳过", profile.get("kind"))
    else:
        try:
            await ident.agent_init_custom(str(profile["kind"]), str(profile["title"]))
        except Exception:
            logger.exception("自定义专岗 %s 建文档目录出错（kv 那份在）", profile.get("kind"))
    return profile


def check_agent_delete(svc: Any, kind: str) -> dict[str, Any]:
    """删自定义专岗前的检查（不动手）：内置 → 400；不存在 → 404；有未结交接单 → 409。
    返回这个岗位的 profile（管理员对话写小票时用来说清删的是谁）。"""
    from .agents import PROFILES_KINDS

    mod = _agents_mod(svc)
    k = str(kind or "").strip()
    if k in PROFILES_KINDS:
        raise OpError(400, "内置岗位不能删")
    if not mod.is_custom_kind(k):
        raise OpError(404, "没有这个专岗")
    if mod._has_unsettled_handoffs(k):  # noqa: SLF001  和 delete_custom 同一个判断
        raise OpError(409, "这个专岗还有进行中的交接单，等它收尾了再删")
    return mod.profile(k)


def agent_delete_custom(svc: Any, kind: str) -> str:
    """删自定义专岗（DELETE /api/agents/{kind} 的落点）：清 kv，文档目录挪到 .trash。返回被删的 kind。"""
    mod = _agents_mod(svc)
    try:
        deleted = mod.delete_custom(str(kind or ""))
    except ValueError as e:
        msg = str(e)
        if "内置" in msg or "不能删" in msg:
            raise OpError(400, msg) from None
        if "交接" in msg or "进行中" in msg:
            raise OpError(409, msg) from None
        raise OpError(404, msg or "没有这个专岗") from None
    # kv 删了；文档目录挪到 .trash（挪不动也只是警告，kv 那份已经删了）
    ident = getattr(svc, "identity", None)
    if ident is None:
        logger.warning("identity 没就位：删专岗 %s 只清了 kv，文档目录留着", deleted)
    else:
        try:
            ident.agent_trash_custom(deleted)
        except Exception:
            logger.exception("删专岗 %s 挪文档到 .trash 出错（kv 已删）", deleted)
    return deleted
