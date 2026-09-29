"""outbox.py（M3 交付层）：发件箱 Outbox、任务交付 Delivery、故障报错 report_error。

设计依据 docs/02-设计.md §6：
- 每条通知记 待发(pending)/发送中(sending)/已发(sent)/不确定(uncertain)/失败(failed)，
  按 key（对象+版本+事件类型+目标群）去重。发送超时标 uncertain 不盲目重发；
  群文件上传不幂等，绝不自动重试。
- 普通交付也受日限额/睡觉时段约束；群友以 /mw 领取 <任务号> 明确索取时，
  仅把待发的本任务成品标 awaited_delivery，不受两项节制、不占额度；
  error / command / admin 即时反馈也不受限。
- file 传完补一条说明消息（MaiBot 不知道文件是谁发的）；herenow 成功发链接说明。
- 交付成功都把链接/文件名放进可提起清单（ttl 6 小时）。
- 首选渠道失败（failed，不含 uncertain）自动回落备选；两条都失败 → 兜底说明
  「做好了，但发群文件和网页都没成功，成品在 MaiWork 网页里」。
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
from pathlib import Path
from typing import Any, Callable, Optional

from . import clock
from .delivery import Mentions, Pushes
from .host import HostError
from .models import _redact
from .store import Store

logger = logging.getLogger("maiwork.outbox")

_DELIVER_MENTION_TTL_S = 6 * 3600  # 交付备忘在可提起清单里留 6 小时
_ERR_MAX = 300
_DEDUP_WINDOW_S = 600  # report_error 同群同指纹 10 分钟一次
_DELIVERY_NOTE_SUFFIXES = (":note", ":webonly")


def _is_artifact_outbox_row(kind: str, key: str) -> bool:
    """真实交付项；失败兜底告知和后续说明都不算成品。"""
    return ((kind in ("file", "herenow") and not key.endswith(_DELIVERY_NOTE_SUFFIXES))
            or (kind == "text" and key.endswith(":deliver:text")))

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
             not_before: Optional[float] = None) -> None:
        with self._store.tx() as conn:
            fields = ["status=?", "error=?", "updated=?"]
            params: list[Any] = [status, error, clock.now()]
            if result is not None:
                fields.append("result=?")
                params.append(json.dumps(result, ensure_ascii=False))
            if not_before is not None:
                fields.append("not_before=?")
                params.append(float(not_before))
            params.append(int(oid))
            conn.execute(f"UPDATE outbox SET {', '.join(fields)} WHERE id=?", params)

    def _postpone(self, oid: int, group_id: str, reason: str, now: float) -> None:
        """推到睡觉时段结束 / 明天 00:00（取更近的），原因写 error，状态仍 pending。"""
        settings = self._get_settings()
        quiet = getattr(settings.delivery, "quiet_hours", "") or "23:00-08:00"
        try:
            s, e = clock.parse_hhmm_range(quiet)
        except (ValueError, AttributeError):
            s, e = 0, 0
        nb: float
        if reason == "睡觉时段" and s != e:
            t = clock.bj(now)
            end = t.replace(hour=e // 60, minute=e % 60, second=0, microsecond=0)
            if end.timestamp() <= now:
                end = end + timedelta(days=1)
            nb = end.timestamp()
        else:
            t = clock.bj(now)
            nb = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400.0
        self._set(oid, status="pending", error=f"推迟：{reason}", not_before=nb)

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

    async def _execute(self, row: Any) -> dict:
        """执行一条；成功返回 result dict；失败抛异常。"""
        kind = row["kind"]
        payload = json.loads(row["payload"] or "{}")
        gid = row["group_id"]
        if kind == "text":
            session_id = self._session_for_group(gid)
            res = await self._host.send_text(
                session_id,
                str(payload.get("text") or ""),
                reply_to=str(payload.get("reply_to") or ""),
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
                    {"text": note, "push_kind": follow_kind},
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
                    {"text": text_out, "push_kind": follow_kind},
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
                # 群文件发不出去 → herenow 附件页：临时目录放 index.html + 原文件一起发布
                src = Path(str(fb.get("path") or ""))
                name = str(fb.get("name") or src.name or "附件")
                d = self._fallback_dir() / f"hn-{int(row['id'])}"
                if d.exists():
                    shutil.rmtree(d)
                d.mkdir(parents=True, exist_ok=True)
                if src.is_file() and not src.is_symlink():
                    # 符号链接不复制（S3：会跟着链接读到工作区外的文件）
                    target = d / name
                    shutil.copyfile(src, target)
                    page = (
                        "<!doctype html><html><head><meta charset='utf-8'>"
                        f"<title>{name}</title></head><body>"
                        f"<p>附件：</p><p><a href='{name}' download>{name}</a></p>"
                        "</body></html>"
                    )
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

    async def flush(self, now: float, *, allowed_groups: Any = None) -> None:
        """把到期（not_before≤now）的 pending 逐条处理。后台循环调。

        allowed_groups：只发这些群的（None = 现查 settings.is_served）。
        非服务群的 pending 留在原地（由 app 的回收逻辑标 cancelled，不删数据）——
        非服务群零发送是红线。
        """
        now = float(now)
        if allowed_groups is None:
            try:
                settings = self._get_settings()
                allowed_groups = set(getattr(settings, "groups", {}) or {})
            except Exception:
                allowed_groups = set()
        served = {str(g) for g in allowed_groups}
        self._repair_sent_followups(served)
        while True:
            rows = [r for r in self._due_rows(now) if str(r["group_id"]) in served]
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

            # 单一节制入口：明确领取与故障/指令的豁免由 Pushes 决定。
            ok_push, reason = self._pushes.can_push(gid, push_kind, now)
            if not ok_push:
                self._postpone(oid, gid, reason or "推送节制", now)
                continue

            # 先落库 sending，再执行（崩了 recover 能捡到）
            self._set(oid, status="sending")
            try:
                result = await self._execute(row)
            except Exception as exc:
                if self._is_timeout(exc):
                    self._set(oid, status="uncertain", error=_redact(str(exc), []))
                else:
                    self._set(oid, status="failed", error=_redact(str(exc), []))
                    if kind in ("file", "herenow"):
                        self._enqueue_fallback(_row_after(self._store, oid))
                continue
            self._set(oid, status="sent", result=result)
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
            try:
                self._pushes.record(gid, push_kind, str(payload.get("text") or payload.get("note") or kind), now)
            except Exception:
                logger.exception("pushes.record 失败")
            try:
                self._after_sent(_RowWithResult(fresh, result), payload)
            except Exception:
                logger.exception("交付后续动作失败（说明/备忘）")

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
        note = f"做好了：{str(task['title'] or '请查看成品')}"[:300]
        if kind == "text":
            self._outbox.enqueue(
                f"task:{tid}:deliver:text", gid, "text",
                {"text": note, "push_kind": "delivery"}, task_id=tid,
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
