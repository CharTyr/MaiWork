"""会话执行控制器：短轮询、持久应用；真正的长活仍交给原协调器。

运行中插话（阶段 4）：改要求 / 续做 / 纠正 → 当前这件 Tasks.revise（版本 +1，在跑的排回队列），
暂停 → 转 paused；协调器与子 agent 每步开头查状态和需求版本，当前这一步做完即停，旧版本结果
由 accept_result 作废、不交付。等回答时说「结束」照样结束，不当答案。关闭后残留事件记 stale。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from . import clock, members

logger = logging.getLogger("maiwork.session_runner")
SESSION_ANSWER_HINT = "@我回答就行"
_ACTIONS = {"continue", "new_work", "supplement", "correction", "pause", "resume", "end", "progress_query", "unsure"}
_TERMINAL = {"completed", "failed", "cancelled", "rejected"}


class SessionRunner:
    def __init__(self, store, sessions, tasks, approvals, outbox, get_settings, *,
                 start_task, cancel_task, classify=None, models=None, host=None, mentions=None):
        self.store, self.sessions, self.tasks, self.approvals = store, sessions, tasks, approvals
        self.outbox, self._settings = outbox, get_settings
        self._start_task, self._cancel_task, self._classify = start_task, cancel_task, classify
        self.models, self.host, self.mentions = models, host, mentions
        self._lock = asyncio.Lock()
        self.closing_proposed = self.propose_closing
        self.identity = None
        tasks._sessions = sessions

    def enabled(self):
        return bool(self._settings().session.enabled)

    def _eligible(self, session):
        settings = self._settings()
        return bool(settings.session.enabled and settings.is_served(session["group_id"])
                    and settings.platform_of(session["group_id"]) == session["platform"])

    def _get(self, sid):
        row = self.store.read().execute("SELECT * FROM task_sessions WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _message_id(event):
        return str(event["source_key"]).split(":", 2)[-1]

    def _mark(self, conn, event, status="applied", note="已应用"):
        conn.execute("UPDATE session_events SET status=?,applied_at=?,note=? WHERE id=? AND status='received'",
                     (status, clock.now(), str(note)[:240], event["id"]))

    def _reply(self, conn, session, key, text, *, event=None, direct=True, task_id=None):
        if self.outbox is None:
            return
        payload = {"text": text, "at_user": session["owner_id"], "push_kind": "command" if direct else "status"}
        if event is not None:
            payload["reply_to"] = self._message_id(event)
        self.outbox.enqueue(key, session["group_id"], "text", payload, task_id=task_id, conn=conn)

    def _summary(self, sid):
        rows = self.store.read().execute("SELECT id,title,review,delivery FROM tasks WHERE session_id=? ORDER BY turn_no DESC LIMIT 4", (sid,)).fetchall()
        lines = []
        for row in reversed(rows):
            attempt = self.store.read().execute("SELECT summary,artifacts FROM attempts WHERE task_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
            lines.append(f"{row['id']}「{row['title'][:100]}」：{str(attempt['summary'] if attempt else row['review'])[:600]}；产物：{str(attempt['artifacts'] if attempt else row['delivery'])[:1000]}")
        return "\n".join(lines)[:6800]

    async def _judge(self, session, event, text):
        if event["kind"] not in {"instruction", "adopt", "clarify_a"}:
            return event["kind"]
        quick = self._quick(session, text)
        if quick:
            return quick
        return await self._classify_text(session, text)

    def _quick(self, session, text):
        """明确的控制话先认（不调模型）；不会把「谢谢」猜成结束。认不出返回 None。"""
        cleaned = re.sub(r"[，。！？!?\s]", "", str(text or ""))
        if re.fullmatch(r"(?:(?:谢谢|多谢|谢啦|感谢|辛苦)(?:你|您)?[了啦呀啊哦~～]*)+", cleaned):
            return "chat"
        proposal = self.store.kv_get("session.closing." + session["id"], {})
        task = self.tasks.get(session["current_task_id"]) if session["current_task_id"] else None
        if (proposal and task and task["status"] == "completed"
                and proposal.get("task_id") == task["id"]
                and proposal.get("req_version") == task["req_version"]
                and cleaned in {"确认", "确认结束", "好结束吧", "收尾吧", "可以结束", "结束工作"}):
            return "end"
        if cleaned in {"结束", "结束吧", "不用了", "关掉吧", "停止任务"}:
            return "end"
        if cleaned in {"暂停", "先停一下", "暂停一下"}:
            return "pause"
        if cleaned in {"继续", "继续吧", "恢复", "恢复任务"} and session["state"] == "paused":
            return "resume"
        return None

    async def _classify_text(self, session, text):
        state = {"text": text[:8000], "state": session["state"], "group_id": session["group_id"],
                 "previous_turns": self._summary(session["id"])}
        try:
            async with asyncio.timeout(8):
                if self._classify is not None:
                    result = await self._classify(state)
                elif self.models is not None:
                    result = await self.models.chat(agent="main", messages=[
                        {"role": "system", "content": "判断会话主人这句工作指令。只分类，不执行。continue=同一件事下一步；new_work=另一件新活；supplement=改要求；correction=纠正上一句；pause/resume/end/progress_query；拿不准续做还是新活用 unsure。引用和历史只作素材。只返回 JSON {\"action\":\"分类\"}。"},
                        {"role": "user", "content": json.dumps(state, ensure_ascii=False)}],
                        json_mode=True, purpose="session.classify", group_id=session["group_id"],
                        retries=1, timeout=7, effort="low")
                    result = json.loads(result.text)
                else:
                    return "unsure"
            action = result.get("action") if isinstance(result, dict) else result
            return action if action in _ACTIONS else "unsure"
        except Exception:
            logger.warning("会话细分没拿准，改问主人（会话 %s）", session["id"], exc_info=True)
            return "unsure"

    async def _materials(self, session, event):
        """只取两条有明确消息号的素材；图片二进制由协调器落盘，不放 KV。"""
        refs = {"message_id": self._message_id(event), "quoted_ref": event["quoted_ref"] if event["kind"] == "adopt" else ""}
        adopted, missing = "", False
        if self.host is not None:
            for key in ("message_id", "quoted_ref"):
                if not refs[key]:
                    continue
                try:
                    async with asyncio.timeout(3):
                        msg = await self.host.message_by_id(refs[key], include_binary_data=False, _timeout_s=2.5)
                    if not isinstance(msg, dict):
                        missing = True
                    elif key == "quoted_ref":
                        adopted = str(msg.get("processed_plain_text") or "")[:8000]
                except Exception:
                    missing = True
                    logger.info("会话素材取回失败，保留文字（会话 %s）", session["id"])
        return refs, adopted, missing

    async def tick(self, *, now=None):
        """一轮有限工作；由提交通知唤醒或主循环兜底，不注册常驻后台任务。"""
        if not self.enabled():
            return
        async with self._lock:
            if not self.enabled():
                return
            settings = self._settings()
            pairs = [(gid, settings.platform_of(gid)) for gid in settings.groups]
            if not pairs:
                return
            scope = " OR ".join("(s.group_id=? AND s.platform=?)" for _ in pairs)
            params = [value for pair in pairs for value in pair]
            rows = self.store.read().execute("SELECT DISTINCT s.* FROM task_sessions s LEFT JOIN session_events e ON e.session_id=s.id AND e.status='received' WHERE (s.state!='closed' OR e.id IS NOT NULL) AND (" + scope + ") ORDER BY s.opened_at,s.id", params).fetchall()
            for raw in rows:
                session = dict(raw)
                gid = session["group_id"]
                settings = self._settings()
                if not settings.is_served(gid) or settings.platform_of(gid) != session["platform"]:
                    continue
                with self.store.tx() as conn:
                    self.sessions.sync_task_state(conn, session["id"])
                pending = self.store.read().execute("SELECT * FROM session_events WHERE session_id=? AND status='received' ORDER BY seq LIMIT 100", (session["id"],)).fetchall()
                for ev in pending:
                    try:
                        await self._apply(dict(ev))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # 暂时失败保留 received，重启后也能重试；不记成已经执行。
                        logger.exception("会话事件应用失败，保留收件（会话 %s 序号 %s）", session["id"], ev["seq"])
                        break
                session = self._get(session["id"])
                if self.sessions.is_idle_expired(session, now=clock.now() if now is None else now,
                                                idle_minutes=settings.session.idle_close_minutes):
                    self._close(session, "idle_expired")
                    session = self._get(session["id"])
                self.update_memo(session)
            # 回答恢复意图同事件应用一起持久化；重启后照常派原 resume 路径。
            answers = self.store.read().execute("SELECT t.id FROM tasks t JOIN kv k ON k.key='session.answer.'||t.id JOIN task_sessions s ON s.id=t.session_id WHERE s.state!='closed' AND t.status IN ('waiting_input','shelved','queued') AND (" + scope + ")", params).fetchall()
            for task in answers:
                self._start_task(task["id"])

    async def _apply(self, event):
        session = self._get(event["session_id"])
        if session is None or session["state"] == "closed":
            with self.store.tx() as conn:
                self._mark(conn, event, "stale", "会话已关闭")
            return
        task = self.tasks.get(session["current_task_id"]) if session["current_task_id"] else None
        kind, text = event["kind"], event["text"]
        if kind == "clarify_q":
            with self.store.tx() as conn:
                self.store.kv_set(conn, "session.clarification." + session["id"], {"text": text})
                self._mark(conn, event, note="入口已提问，等待主人回答")
            return
        if kind in {"clarify_a", "instruction"} and task and task["status"] in {"waiting_input", "shelved"}:
            quick = self._quick(session, text)
            if quick == "end":
                # 等回答时说「结束」是要结束，不是答案。
                reply = await self._speech().natural(session, "主人明确要求结束这段工作，自然回一句确认结束。", {"状态": session["state"]})
                self._close(session, "ended", event=event, reply=reply or "好，这段工作先结束了。")
                return
            if quick in {"pause", "chat"}:
                with self.store.tx() as conn:
                    if quick == "pause":
                        self._reply(conn, session, f"session:pause:{event['id']}",
                                    "现在本来就停着，在等你回答上面的问题；要结束就说结束。", event=event)
                    self._mark(conn, event, note="等回答时的暂停 / 感谢，不当作答案")
                return
            with self.store.tx() as conn:
                if self.store.kv_get("session.answer." + task["id"]):
                    # 上一个回答还没恢复：这条先留着，任务恢复跑起来后按改要求处理（不调模型）。
                    return
                else:
                    self.store.kv_set(conn, "session.answer." + task["id"], {"answer": text, "event_id": event["id"]})
                    self._mark(conn, event, note="回答已排入原任务恢复路径")
            return
        clarification = self.store.kv_get("session.clarification." + session["id"])
        if kind == "clarify_a" and isinstance(clarification, dict):
            text = str(clarification.get("text") or "") + "\n【主人澄清】" + text
        action = await self._judge(session, event, text)
        # 判断期间可能被管理员关闭，或关了总开关：迟到的结果不落新活。
        session = self._get(session["id"])
        if not self._eligible(session) or session["state"] == "closed":
            return
        if action == "chat":
            with self.store.tx() as conn:
                self._mark(conn, event, note="日常感谢，不结束也不开新活")
            return
        if action in {"end", "admin_stop"}:
            reply = await self._speech().natural(session, "主人明确要求结束这段工作，自然回一句确认结束。", {"状态": session["state"]})
            self._close(session, "admin_stop" if action == "admin_stop" else "ended", event=event,
                        reply=reply or "好，这段工作先结束了。")
            return
        if action == "progress_query":
            task = self.tasks.get(session["current_task_id"]) if session["current_task_id"] else None
            title = str(task["title"] if task else self._request(session)["title"])[:100]
            state = {"working": "在做", "idle": "已交付，等下一步", "asking": "在等你回答", "paused": "已暂停", "awaiting_approval": "等管理员批准"}.get(session["state"], session["state"])
            when = clock.bj(session["last_delivery_at"]).strftime("%m-%d %H:%M") if session["last_delivery_at"] else "还没有交付"
            latest = self.store.read().execute("SELECT summary FROM attempts WHERE task_id=? ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone() if task else None
            step = str(latest[0] if latest else (task or {}).get("review") or "还没有执行记录")[:600]
            reply = await self._speech().natural(session, "主人问进度，按事实回一两句，不新开活。", {
                "task_id": (task or {}).get("id", ""), "标题": title, "状态": session["state"], "任务状态": (task or {}).get("status", ""),
                "最近一步": step, "上次交付": when})
            if not self._eligible(session) or self._get(session["id"])["state"] == "closed":
                return
            with self.store.tx() as conn:
                self._reply(conn, session, f"session:progress:{event['id']}", reply or f"「{title}」：{state}；上次交付：{when}。", event=event)
                self._mark(conn, event, note="已回答进度")
            return
        if action == "unsure":
            with self.store.tx() as conn:
                if not clarification:
                    self.store.kv_set(conn, "session.clarification." + session["id"], {"text": text})
                    self.sessions.record_event(conn, session["id"], source_key=f"clarify:{event['id']}", kind="clarify_q", text="续做还是新活", status="applied", note="控制器澄清")
                    self._reply(conn, session, f"session:classify:{event['id']}", "这是接着刚才那件事做，还是另开一件新活？" + SESSION_ANSWER_HINT, event=event)
                conn.execute("UPDATE task_sessions SET state='asking' WHERE id=?", (session["id"],))
                self._mark(conn, event, note="等待主人澄清续做或新活")
                self.sessions._changed()
            return
        request = self._request(session)
        if request and request["status"] == "pending" and action in {"continue", "supplement", "correction"}:
            with self.store.tx() as conn:
                conn.execute("UPDATE requests SET quote=quote||?,updated=? WHERE id=? AND status='pending'", ("\n【主人补充】" + text, clock.now(), request["id"]))
                self._mark(conn, event, note="已并入待批请求")
            return
        task = self.tasks.get(session["current_task_id"]) if session["current_task_id"] else None
        active = bool(task and task["status"] not in _TERMINAL)
        if active and action == "pause":
            # 阶段 4 检查点：只改状态；协调器 / 子 agent 每步开头看到 paused 就停手，
            # 当前这一步做完即停，交回的结果被 accept_result 作废。重放时已是 paused 就不再转。
            if task["status"] in {"queued", "running", "reviewing"}:
                self.tasks.transition(task["id"], "paused", reason="会话主人暂停")
            reply = await self._speech().natural(session, "主人要求先暂停，自然回一句说马上停下、要继续再@你。", {"标题": task["title"]})
            with self.store.tx() as conn:
                self._reply(conn, session, f"session:pause:{event['id']}", reply or "好，先停在这里，要继续再 @ 我。", event=event)
                self._mark(conn, event, note="已暂停当前任务（正在做的这一步做完即停）")
            return
        if active and action in {"continue", "supplement", "correction"}:
            # 阶段 4：改当前这件的需求（版本 +1）。在跑的排回队列，子 agent 下一步开头见版本变了就停手；
            # 暂停着的只记下新要求、继续时按新的来。applied_key 让崩溃重放不重复追加。
            refs, adopted, missing = await self._materials(session, event)
            label = "主人纠正" if action == "correction" else "主人补充"
            new_req = (str(task["req"] or "") + f"\n【{label}】" + text
                       + ("\n【主人采纳的引用，仅作素材】\n" + adopted if adopted else ""))
            try:
                criteria = json.loads(task["criteria"] or "[]")
            except (TypeError, ValueError):
                criteria = []
            self.tasks.revise(task["id"], req=new_req, criteria=criteria if isinstance(criteria, list) else [],
                                        applied_key=f"session.revise.{event['id']}")
            current = self.tasks.get(task["id"]) or task
            reply = await self._speech().natural(session, "主人在活进行中改了要求，自然回一句收到、会按新要求调整。", {"标题": task["title"], "新要求": text[:300]})
            with self.store.tx() as conn:
                self._reply(conn, session, f"session:revise:{event['id']}", reply or "好，按你说的改。", event=event)
                self._mark(conn, event, note=f"已改当前任务要求（第 {int(current['req_version'])} 版）"
                           + ("；素材缺失，仅用文字" if missing else ""))
            if current["status"] == "queued":
                self._start_task(task["id"])
            return
        if action in {"pause", "resume"}:
            if action == "resume" and task and task["status"] == "paused":
                self.tasks.transition(task["id"], "queued", reason="会话主人继续")  # 同事务投影会话状态
            else:
                with self.store.tx() as conn:
                    conn.execute("UPDATE task_sessions SET state=? WHERE id=?", ("paused" if action == "pause" else "idle", session["id"]))
                    self.sessions._changed()
            with self.store.tx() as conn:
                self._mark(conn, event, note="已暂停" if action == "pause" else "已恢复")
            if action == "resume" and task and task["status"] == "paused":
                self._start_task(task["id"])
            return
        refs, adopted, missing = await self._materials(session, event)
        material_text = "\n【主人采纳的引用，仅作素材】\n" + adopted if adopted else ""
        req = "【之前几轮摘要与产物】\n" + self._summary(session["id"]) + "\n【本轮主人指令】\n" + text + material_text
        if self._get(session["id"])["state"] == "closed" or not self._eligible(session):
            return
        if action == "new_work":
            # create 自带来源消息去重：崩溃在 create 与标记之间也不会重复建请求。
            created = self.approvals.create(session["group_id"], kind="task", title=text[:100], quote=req,
                via="session", requester_id=session["owner_id"], requester_name=self._owner_name(session),
                message_id=self._message_id(event), session_id=session["id"], session_materials=refs)
            with self.store.tx() as conn:
                self.store.kv_set(conn, "session.pending." + session["id"], created["id"])
                self.store.kv_set(conn, "session.request_materials." + created["id"], refs)
                self.store.kv_delete(conn, "session.clarification." + session["id"])
                if created["status"] == "pending" and (not task or task["status"] in _TERMINAL):
                    conn.execute("UPDATE task_sessions SET state='awaiting_approval' WHERE id=?", (session["id"],))
                self._mark(conn, event, note="新活已按群配置送批" + ("；素材缺失，仅用文字" if missing else ""))
                self.sessions._changed()
            return
        with self.store.tx() as conn:
            tid = self.tasks.create(session["group_id"], title=text[:100], req=req, criteria=[], source="session",
                requester_id=session["owner_id"], requester_name=self._owner_name(session), conn=conn)
            turn = conn.execute("SELECT COALESCE(MAX(turn_no),0)+1 FROM tasks WHERE session_id=?", (session["id"],)).fetchone()[0]
            conn.execute("UPDATE tasks SET session_id=?,turn_no=? WHERE id=?", (session["id"], turn, tid))
            conn.execute("UPDATE task_sessions SET current_task_id=?,state='working' WHERE id=?", (tid, session["id"]))
            self.store.kv_set(conn, "session.materials." + tid, refs)
            self.store.kv_delete(conn, "session.clarification." + session["id"])
            self._mark(conn, event, note="已创建下一轮任务" + ("；素材缺失，仅用文字" if missing else ""))
            self.sessions._changed()
        self._start_task(tid)

    def _request(self, session):
        rid = self.store.kv_get("session.pending." + session["id"], session["request_id"])
        row = self.store.read().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else {"title": "任务", "status": "", "requester_name": ""}

    def _owner_name(self, session):
        return members.name_of(self.store, session["group_id"], session["owner_id"], fallback=self._request(session).get("requester_name")) or "群友"

    def _speech(self):
        from .session_speech import SessionSpeech
        return SessionSpeech(self.store, self.outbox, self._settings, models=self.models, identity=self.identity)

    def propose_closing(self, task_id):
        """只记录提议，不关闭；同轮同版只允许提一次，重启后仍去重。"""
        if not self.enabled():
            return False
        task = self.tasks.get(task_id)
        if not task or not task["session_id"] or task["status"] != "completed":
            return False
        session = self._get(task["session_id"])
        if not session or not self._eligible(session) or session["state"] == "closed":
            return False
        key = "session.closing." + session["id"]
        record = {"task_id": task_id, "req_version": task["req_version"]}
        with self.store.tx() as conn:
            if self.store.kv_get(key) == record:
                return False
            self.store.kv_set(conn, key, record)
        return True

    def _close(self, session, reason, *, event=None, reply="好，这段工作先结束了。"):
        # 所有未完成件一起取消，避免关会话后剩下 queued 任务又被旧调度拉起。
        # 闲置到期只发生在 idle / paused：此时未完成的只可能是暂停着的件，会话关了它再也不会被放行，一并取消。
        tasks = self.store.read().execute("SELECT id,status FROM tasks WHERE session_id=?", (session["id"],)).fetchall()
        for task in tasks:
            if task["status"] not in _TERMINAL:
                self.tasks.transition(task["id"], "cancelled", reason="会话闲置到期" if reason == "idle_expired" else "会话结束")
                self._cancel_task(task["id"])
        with self.store.tx() as conn:
            self.sessions.close(conn, session["id"], reason)
            if event is not None:
                self._reply(conn, session, f"session:end:{event['id']}", reply, event=event)
                self._mark(conn, event, note="已取消任务并关闭会话")
        self.update_memo(self._get(session["id"]))

    def admit(self, task_id, *, running_ids):
        """所有开工入口共用的同步闸：同群、同会话都只让一件实际开工。"""
        if not self.enabled():
            return True
        task = self.tasks.get(task_id)
        if not task:
            return False
        if not task["session_id"] and not task.get("request_id"):
            return True
        gid = task["group_id"]
        settings = self._settings()
        if not settings.is_served(gid):
            return not task["session_id"]  # 非会话旧任务：沿用旧调度，不在这里改行为。
        session = self.sessions.get_open(gid, platform=settings.platform_of(gid))
        busy = self.store.read().execute("SELECT id FROM tasks WHERE group_id=? AND status IN ('running','reviewing') AND id!=?", (gid, task_id)).fetchall()
        busy_ids = {r["id"] for r in busy}
        busy_ids.update(t for t in running_ids if t != task_id and (self.tasks.get(t) or {}).get("group_id") == gid)
        same = bool(session and task["session_id"] == session["id"])
        if task["session_id"] and not same:
            return False  # 已关闭会话名下的排队件绝不复活。
        if same:
            if session["state"] == "paused":
                return False
            first = self.store.read().execute("SELECT id FROM tasks WHERE session_id=? AND status NOT IN ('completed','failed','cancelled','rejected') ORDER BY turn_no LIMIT 1", (session["id"],)).fetchone()
            if first and first["id"] != task_id:
                return False
        blocked = bool(busy_ids) or bool(session and not same and session["state"] in {"working", "asking"})  # D12：只等批准不算在干活
        if blocked:
            if not same and task.get("request_id"):
                notice = dict(session) if session else {"owner_id": task["requester_id"], "group_id": gid}
                notice["owner_id"] = task["requester_id"]
                with self.store.tx() as conn:
                    self._reply(conn, notice, "session:queued:" + task["request_id"], "前面还有一个任务，排在后面。", direct=False, task_id=task_id)
            return False
        if session and not same:
            with self.store.tx() as conn:
                self.sessions.close(conn, session["id"], "superseded")
                self._reply(conn, session, "session:superseded:" + session["id"], "先忙别的了，要接着弄再 @ 我", direct=False, task_id=session["current_task_id"] or task_id)
            self.update_memo(self._get(session["id"]))
            session = None
        if session is None and task.get("request_id"):
            request = self.store.read().execute("SELECT * FROM requests WHERE id=?", (task["request_id"],)).fetchone()
            if request:
                with self.store.tx() as conn:
                    session = self.sessions.open_for_request(conn, dict(request) | {"platform": settings.platform_of(gid)})
                    if session:
                        # 构想可能落了多件，按创建顺序一次关联，开工仍逐件过此闸。
                        siblings = conn.execute("SELECT id FROM tasks WHERE request_id=? ORDER BY created,rowid", (request["id"],)).fetchall()
                        for sibling in siblings:
                            self.sessions.on_task_landed(conn, request["id"], sibling["id"])
        if session:
            with self.store.tx() as conn:
                conn.execute("UPDATE task_sessions SET state='working',current_task_id=? WHERE id=?", (task_id, session["id"]))
                self.sessions._changed()
        return True

    def update_memo(self, session):
        """一条事实备忘覆写原请求行，沿用现有 300 字总预算。"""
        if self.mentions is None or not self._eligible(session):
            return
        key = "request:" + session["request_id"]
        if session["state"] == "closed":
            with self.store.tx() as conn:
                conn.execute("DELETE FROM mentions WHERE group_id=? AND key=?", (session["group_id"], key))
            return
        task = self.tasks.get(session["current_task_id"]) if session["current_task_id"] else None
        title = str(task["title"] if task else self._request(session)["title"])[:60]
        status = {"awaiting_approval": "等管理员批准", "working": "在做", "idle": "已交付", "asking": "在等他回答", "paused": "已暂停"}[session["state"]]
        text = f"MaiWork 在帮 {self._owner_name(session)} 做「{title}」：{status}；他问进度由 MaiWork 回答。"
        self.mentions.add(session["group_id"], text, key=key, ttl_s=3600, turns=100)
