"""首次安装引导的状态（网页 /api/onboarding）。

记在 kv["onboarding"]：{state, step, ts}。state 只有这几种：
- ""            从没开始过；
- "in_progress" 开始了还没走完（每一步保存后前端发 progress 记下当前步）——刷新 / 关页重进
                都接着弹，回到记下的那一步（docs/13 A04：不能靠「模型已配」推断装完了）；
- "later"       点了完成 / 进入，但必要条件（模型、服务群）还缺——只是「先存下，稍后继续」，
                不算可用（docs/13 A08）；不再硬弹，设置概况里给继续入口；
- "done"        必要条件都满足后完成；
- "skipped"     主动跳过（尊重，不再弹）。

要不要弹：in_progress 一定弹；从没开始过且模型没配好也弹（全新安装）；
没有记录但模型已经配好 = 已经在用的老安装，不打扰。

checks（老字段，布尔）保留给旧前端；items 是给完成页 / 设置页的能力清单（docs/13 G01/G02）：
模型、群、搜索、执行、交付、第一件小事，各自写清楚「能用 / 受限 / 在等 / 没开」和下一步。
全部从现有设置和运行状态里读，不另存、不联网、不调模型。
"""

from __future__ import annotations

import json
from typing import Any

from . import clock

KV_KEY = "onboarding"
ACTIONS = ("done", "skip", "reset", "progress")
# 和前端 ONB_STEPS 同一份名单（前端改步骤要一起改）
STEPS = ("hello", "models", "groups", "keys", "search", "admins", "look", "done")
# 必要条件：缺了就不算「可用」；其余（Jev、头像、管理员、搜索）可选
REQUIRED = ("models", "groups")


def _checks(svc: Any) -> dict[str, bool]:
    settings = svc.get_settings()
    out = {"models": False, "groups": False, "jev": False, "search": False, "admins": False}
    try:
        out["models"] = bool(svc.models.settings().ready())
    except Exception:
        out["models"] = False
    if settings is not None:
        out["groups"] = bool(getattr(settings, "groups", None))
        out["admins"] = bool(tuple(getattr(settings.approval, "admins", ()) or ()))
    # 搜索（2026-10）：有效搜索链可用就算配好（绑定存数据库 kv["extensions.search"]）
    try:
        search = getattr(svc, "search", None)
        out["search"] = bool(search is not None and search.available())
    except Exception:
        out["search"] = False
    try:
        from .jev import _resolve_key

        out["jev"] = bool(_resolve_key(settings))
    except Exception:
        out["jev"] = False
    return out


# ----------------------------------------------------------------------
# 能力清单（items）
# ----------------------------------------------------------------------


def _item(key: str, state: str, title: str, text: str, step: str = "", sub: str = "") -> dict[str, Any]:
    """state：ok 能用 / warn 能用但受限 / wait 在等（不用你做什么）/ off 没开。
    step：引导里对应哪一步（前端「去补」跳过去）；sub：设置里对应哪个子页。"""
    return {"key": key, "state": state, "title": title, "text": text, "step": step, "sub": sub}


def _verified_for(svc: Any, service_model: str) -> dict[str, Any] | None:
    """kv["models.verified.<条目id>"] 里找服务端模型名对得上的那条（最新的）。"""
    if not service_model:
        return None
    try:
        rows = svc.store.read().execute(
            "SELECT value FROM kv WHERE key LIKE 'models.verified.%' ORDER BY updated DESC"
        ).fetchall()
    except Exception:
        return None
    for row in rows:
        try:
            rec = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict) and str(rec.get("model") or "") == service_model:
            return rec
    return None


def _models_item(svc: Any) -> dict[str, Any]:
    try:
        s = svc.models.settings()
        ready = bool(s.ready())
        main = str(getattr(s, "main_label", "") or getattr(s, "main", "") or "")
        service = str(getattr(s, "main", "") or "")
    except Exception:
        ready, main, service = False, "", ""
    if not ready:
        return _item("models", "off", "模型", "还没配：MaiWork 现在不会做任何要模型的事", step="models", sub="models")
    rec = _verified_for(svc, service)
    if rec is None:
        return _item(
            "models", "warn", "模型",
            f"主模型 {main} 已选好，但还没验证过能不能正常回答和调用工具（未验证）",
            step="models", sub="models",
        )
    if not rec.get("ok"):
        why = str(rec.get("error") or "没通过")
        return _item("models", "off", "模型", f"主模型 {main} 验证没通过：{why}", step="models", sub="models")
    if not rec.get("tools_ok"):
        note = str(rec.get("note") or "工具调用没走通")
        return _item("models", "warn", "模型", f"主模型 {main} 已验证能回答；{note}", step="models", sub="models")
    return _item("models", "ok", "模型", f"主模型 {main} 已验证：能回答、能调用工具；其他专岗没单独选就跟它一样")


def _group_rows(svc: Any, gids: list[str]) -> list[dict[str, Any]]:
    out = []
    for gid in gids:
        try:
            row = svc.store.read().execute(
                "SELECT group_id, last_msg_ts, profile_ready_ts FROM groups WHERE group_id = ?", (gid,)
            ).fetchone()
        except Exception:
            row = None
        out.append(dict(row) if row is not None else {"group_id": gid, "last_msg_ts": 0, "profile_ready_ts": 0})
    return out


def _groups_item(svc: Any, gids: list[str]) -> dict[str, Any]:
    if not gids:
        return _item("groups", "off", "服务的群", "还没有：MaiWork 只在你列出的群里工作", step="groups", sub="config")
    rows = _group_rows(svc, gids)
    heard = [r for r in rows if float(r.get("last_msg_ts") or 0) > 0]
    if len(heard) == len(rows):
        return _item("groups", "ok", "服务的群", f"{len(rows)} 个群，都已经读到聊天")
    if not heard:
        return _item(
            "groups", "wait", "服务的群",
            f"{len(rows)} 个群，还没读到消息：等群里有人说话；一直没有的话，检查群号是不是填对了",
            step="groups",
        )
    return _item(
        "groups", "warn", "服务的群",
        f"{len(rows)} 个群里 {len(heard)} 个已读到聊天；其余还没收到消息（群号可能填错，或群里还没人说话）",
        step="groups",
    )


def _rss_count(svc: Any, gids: list[str]) -> int:
    try:
        from . import rss as _rss

        return sum(
            1 for gid in gids for e in _rss.list_feeds(svc.store, gid) if e.get("enabled", True)
        )
    except Exception:
        return 0


def _search_item(svc: Any, gids: list[str]) -> dict[str, Any]:
    search = getattr(svc, "search", None)
    ok, text = False, "还没选"
    if search is not None:
        try:
            ok, text = search.status()
        except Exception:
            ok, text = False, "搜索状态读不出来"
    if ok:
        return _item("search", "ok", "联网搜索", str(text))
    rss_n = _rss_count(svc, gids)
    if rss_n:
        return _item(
            "search", "warn", "联网搜索",
            f"现在不能联网搜（{text}）；资讯先只用已订的 {rss_n} 个 RSS 源，任务里查资料会受限",
            step="search", sub="extensions",
        )
    return _item(
        "search", "warn", "联网搜索",
        f"现在不能联网搜（{text}）：资讯出不来、查资料受限；读群、构想、只动文件的活照常",
        step="search", sub="extensions",
    )


def _exec_item(svc: Any) -> dict[str, Any]:
    try:
        from .console.views import _localenv_health, _railway_health

        local = _localenv_health(svc)
        railway = _railway_health(svc)
    except Exception:
        local, railway = {"state": "off", "text": "读不出来"}, None
    if local.get("state") == "ok":
        return _item("exec", "ok", "干活", f"能查资料、写文件、跑命令（{local.get('text')}）")
    if railway is not None and railway.get("state") == "ok":
        return _item("exec", "ok", "干活", "本机不能跑命令；能查资料、写文件，要跑命令的活交给一次性机器")
    return _item(
        "exec", "warn", "干活",
        "能查资料、写文件；这台机器不能隔离跑命令，要跑命令的活做不了"
        "（需要的话去「设置 → 执行环境」开一次性机器）",
        sub="environments",
    )


def _delivery_item(svc: Any) -> dict[str, Any]:
    settings = svc.get_settings()
    public_url = ""
    try:
        public_url = str(settings.console.public_url or "").rstrip("/") if settings is not None else ""
    except Exception:
        public_url = ""
    if public_url:
        out = _item(
            "delivery", "ok", "交付",
            f"群友能打开网页：{public_url}；在群里发「/mw 网页」可以拿到本群链接试一下",
        )
        out["copy"] = public_url
        return out
    return _item(
        "delivery", "warn", "交付",
        "网页现在只有你（管理员）在这台机器上能打开；群友拿不到链接——要给群友用，"
        "需要你自己给网页配一个公网地址（设置 → 全部配置 → 网页对外地址），MaiWork 不会替你公开",
        sub="config",
    )


def _first_item(svc: Any, gids: list[str], models_ready: bool) -> dict[str, Any]:
    if not gids:
        return _item("first", "off", "第一件小事", "先加一个服务群", step="groups")
    if not models_ready:
        return _item("first", "off", "第一件小事", "先配好模型", step="models")
    rows = _group_rows(svc, gids)
    if any(float(r.get("profile_ready_ts") or 0) > 0 for r in rows):
        return _item(
            "first", "ok", "第一件小事",
            "群画像已成形：去群的「群」页看看 MaiWork 对这个群的理解，没问题就在资讯页点「现在就备一批」",
        )
    return _item(
        "first", "wait", "第一件小事",
        "MaiWork 正在读群聊，读够后整理出群画像（新群一般要等一段时间的聊天）；"
        "成形后先去「群」页看看理解得对不对，资讯会在之后的时段自动备",
    )


def items(svc: Any, checks: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    checks = checks if checks is not None else _checks(svc)
    settings = svc.get_settings()
    try:
        gids = list(settings.groups.keys()) if settings is not None else []
    except Exception:
        gids = []
    return [
        _models_item(svc),
        _groups_item(svc, gids),
        _search_item(svc, gids),
        _exec_item(svc),
        _delivery_item(svc),
        _first_item(svc, gids, bool(checks.get("models"))),
        _item("jev", "ok" if checks.get("jev") else "off", "Jev 快速判断",
              "密钥已填" if checks.get("jev") else "没填（可选）：判断会走主模型，慢一些、多花一点", step="keys"),
        _item("admins", "ok" if checks.get("admins") else "warn", "管理员",
              "已填" if checks.get("admins") else "还没填：群友派的活没人能批准（可选，之后再填也行）", step="admins"),
    ]


# ----------------------------------------------------------------------
# 读 / 写
# ----------------------------------------------------------------------


def _record(svc: Any) -> dict[str, Any]:
    rec = svc.store.kv_get(KV_KEY, None)
    return rec if isinstance(rec, dict) else {}


def view(svc: Any) -> dict[str, Any]:
    rec = _record(svc)
    state = str(rec.get("state") or "")
    step = str(rec.get("step") or "")
    if step not in STEPS:
        step = ""
    checks = _checks(svc)
    missing = [k for k in REQUIRED if not checks.get(k)]
    if state == "in_progress":
        show = True
    elif state == "":
        show = not checks["models"]
    else:
        show = False
    return {
        "show": show,
        "state": state,
        "step": step,
        "ts": float(rec.get("ts") or 0),
        "checks": checks,
        "usable": not missing,
        "missing": missing,
        # 下一步：缺的第一个必要条件所在的步；都齐了就是记下的步
        "next_step": missing[0] if missing else step,
        "items": items(svc, checks),
    }


def act(svc: Any, action: str, step: str = "") -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError("action 只能是 done / skip / reset / progress")
    now = clock.now()
    if action == "reset":
        value: Any = {}
    elif action == "skip":
        value = {"state": "skipped", "step": str(_record(svc).get("step") or ""), "ts": now}
    elif action == "progress":
        step_s = str(step or "").strip()
        if step_s not in STEPS:
            raise ValueError(f"step 只能是 {' / '.join(STEPS)}")
        value = {"state": "in_progress", "step": step_s, "ts": now}
    else:  # done：必要条件齐了才算完成，否则只是「先存下」
        checks = _checks(svc)
        usable = all(checks.get(k) for k in REQUIRED)
        value = {"state": "done" if usable else "later", "step": "done", "ts": now}
    with svc.store.tx() as conn:
        svc.store.kv_set(conn, KV_KEY, value)
    return view(svc)
