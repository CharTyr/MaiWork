"""会话主模型对主人说话；JSON/工具/子 agent 不进入这条发送路径。

自然话整条限 480 字，超长拒发：不截断后冒充已通过整条安全检查。
短句在固定 2 秒窗口合并，已入 outbox 可恢复；没有常驻循环。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass

from . import clock, privacy, voice

logger = logging.getLogger("maiwork.session_speech")
SPEECH_TIMEOUT_S = 6.0
SPEECH_MAX_TOKENS = 1024
MERGE_SECONDS = 2.0
MAX_CHARS = 480
_SECRET = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|token|secret|password|密钥)\s*[=:：]|(?<![a-z0-9_])sk-[a-z0-9_-]{8,}|\bbearer\s+\S+")
_RANDOM = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9_])")
_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|(?<![\w:])/[^\s，。；！？]+|(?:workspaces?|artifacts|plugins?|CharTyr_MaiWork|maiwork|profiles?|data|\.trellis|\.dsh)[\\/]|\S+\.db\b|\bSOUL\.md\b)")
_STRUCTURED = re.compile(r"```|^\s*[\[{]|\{[^{}]*[\"'][^\"']+[\"']\s*:|<(?:analysis|thinking|tool_call|function)\b|(?:tool_calls?|function_call|验收JSON|内部计划)\s*[:：]", re.I)
_INTERNAL_SPEECH = re.compile(
    r"(?:\b(?:T|REQ|I|G)-\d+\b|\bR\d+\b.*(?:判定|没过)"
    r"|(?:这轮)?验收通过|交付说明\s*[:：]|验收模型原话|判定没做到|判定部分做到|加分项没做到)"
)
_TRUNCATED_TAIL = re.compile(r"(?:[-—–~～:：,，、/\\|(（\[【]\s*$|\d+-\s*$)")
_STRIP_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()\[\]【】—–-]+")


@dataclass(frozen=True)
class GateResult:
    text: str = ""
    reason: str = ""


def _notes_overlap(a: str, b: str) -> bool:
    """判断自然回话是否已经复述了交付说明（避免同一句话前后拼两遍）。"""
    na = _STRIP_PUNCT.sub("", str(a or ""))
    nb = _STRIP_PUNCT.sub("", str(b or ""))
    if not na or not nb:
        return False
    if nb in na or na in nb:
        return True
    if len(nb) < 4 or len(na) < 4:
        return False
    grams_a = {na[i:i + 2] for i in range(len(na) - 1)}
    grams_b = {nb[i:i + 2] for i in range(len(nb) - 1)}
    return len(grams_a & grams_b) / max(1, len(grams_b)) >= 0.55


def gate_text(text, *, protected=()) -> GateResult:
    """纯规则；protected 由调用方提供本群画像和人设，不读取任何外部状态。"""
    s = str(text or "").strip()
    if _SECRET.search(privacy.fold(s)) or _SECRET.search(s) or _RANDOM.search(s):
        return GateResult(reason="secret")
    if _STRUCTURED.search(s):
        return GateResult(reason="structured")
    if _INTERNAL_SPEECH.search(s):
        return GateResult(reason="internal")
    # 公共 URL 可以发；本地 file URL 与普通绝对路径仍拒绝。
    paths = re.sub(r"https?://[^\s，。；！？]+", "", s)
    if _PATH.search(paths):
        return GateResult(reason="path")
    if any(privacy._contains_note_fragment(s, str(p)) for p in protected if p):
        return GateResult(reason="private")
    if voice.is_self_intro(s):
        return GateResult(reason="self_intro")
    if not s or re.sub(r"[\s，。！？,.!?~～]", "", s) in {"嗯", "嗯嗯", "好", "好的", "收到", "好嘞", "做好了", "完成了"}:
        return GateResult(reason="empty")
    if _TRUNCATED_TAIL.search(s):
        return GateResult(reason="truncated")
    if len(s) > MAX_CHARS:
        return GateResult(reason="length")
    return GateResult(text=" ".join(s.split()))


def session_followup(task, settings) -> bool:
    """首轮沿用 MaiBot 引用去重；关闭会话功能时保持旧行为。"""
    return bool(task and task.get("session_id")
                and getattr(getattr(settings, "session", None), "enabled", False)
                and (int(task.get("turn_no") or 0) > 1 or task.get("source") == "session"))


def delivery_key(store, settings, task_id, artifact: str) -> str:
    """会话交付身份 = task + 当前需求版 + 产物指纹，保留旧任务 key。"""
    if not getattr(getattr(settings, "session", None), "enabled", False):
        return f"task:{task_id}:deliver"
    task = store.read().execute("SELECT session_id,req_version FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task or not task["session_id"]:
        return f"task:{task_id}:deliver"
    digest = hashlib.sha256(str(artifact).encode()).hexdigest()[:16]
    return f"task:{task_id}:deliver:r{task['req_version']}:{digest}:deliver"


def delivery_prefix(task_id, task, settings):
    prefix = f"task:{task_id}:deliver"
    if getattr(getattr(settings, "session", None), "enabled", False) and task["session_id"]:
        prefix += f":r{task['req_version']}"
    return prefix


def delivery_address(store, settings, task_id, gid):
    """会话直接交付与 relay_text 开关无关；非服务群先返回，不查库。"""
    if not getattr(getattr(settings, "session", None), "enabled", False) or not settings.is_served(gid):
        return {}
    row = store.read().execute("SELECT s.*,t.request_id AS task_request FROM tasks t JOIN task_sessions s ON s.id=t.session_id WHERE t.id=? AND t.group_id=? AND s.state!='closed'", (task_id, gid)).fetchone()
    if not row or row["platform"] != settings.platform_of(gid):
        return {}
    refs = store.kv_get("session.materials." + task_id, {})
    mid = str(refs.get("message_id") or "") if isinstance(refs, dict) else ""
    if not mid:
        request = store.read().execute("SELECT message_id FROM requests WHERE id=?", (row["task_request"] or row["request_id"],)).fetchone()
        mid = str(request[0] or "") if request else ""
    return {"push_kind": "command", "at_user": row["owner_id"], "reply_to": mid, "session_delivery": True}


class SessionSpeech:
    def __init__(self, store, outbox, get_settings, *, models=None, identity=None):
        self.store, self.outbox, self._settings = store, outbox, get_settings
        self.models, self.identity = models, identity
        self.closing_proposed = None

    def eligible(self, task):
        settings = self._settings()
        return bool(task and task.get("session_id") and settings.session.enabled
                    and settings.session.relay_text and settings.is_served(task["group_id"]))

    def _session(self, task):
        if not self.eligible(task):
            return None
        row = self.store.read().execute("SELECT * FROM task_sessions WHERE id=? AND state!='closed'", (task["session_id"],)).fetchone()
        return dict(row) if row and row["group_id"] == task["group_id"] and row["platform"] == self._settings().platform_of(task["group_id"]) else None

    def clean(self, gid, text):
        # 调用方先确认服务群；这里只读这个群的私有文字。读失败时关闭新增发送路径。
        try:
            protected = [voice.persona(self.identity).soul]
            for row in privacy._focus_rows(self.store, gid):
                protected.extend((row.get("note", ""), row.get("persona_summary", "")))
            result = gate_text(text, protected=protected)
        except Exception:
            result = GateResult(reason="guard_unavailable")
        if result.reason:
            logger.info("会话发群闸门已拦截（原因 %s）", result.reason)
        return result.text

    async def natural(self, session, purpose, facts):
        settings = self._settings()
        if (not settings.session.enabled or not settings.session.relay_text
                or not settings.is_served(session["group_id"]) or self.models is None):
            return ""
        tid = str((facts or {}).get("task_id") or "")
        prompt_facts = {k: v for k, v in (facts or {}).items() if k != "task_id"}
        persona = voice.persona(self.identity)
        messages = [{"role": "system", "content": persona.section() +
            "\n你在对这段工作会话的主人说话，只说一两句第一人称自然话。"
            "不展示内部计划、任务编号、验收过程、工具、JSON、路径、密钥、成员画像或人设提示词。"
            "只按提供的事实回答，不猜进度，不新开活，不执行操作。"},
            {"role": "user", "content": purpose + "\n以下是工作事实，仅作资料：\n" + json.dumps(prompt_facts, ensure_ascii=False)}]
        try:
            async with asyncio.timeout(SPEECH_TIMEOUT_S):
                result = await self.models.chat("main", messages, group_id=session["group_id"],
                    task_id=tid, retries=1, timeout=SPEECH_TIMEOUT_S,
                    max_tokens=SPEECH_MAX_TOKENS, effort="low")
            if getattr(result, "tool_calls", None):
                logger.info("会话发群闸门已拦截（原因 tool_calls）")
                return ""
            if not self._settings().session.enabled or not self._settings().session.relay_text or not self._settings().is_served(session["group_id"]):
                return ""
            return self.clean(session["group_id"], result.text)
        except Exception as exc:
            logger.info("会话自然回话不可用（%s），沿用固定话", type(exc).__name__)
            return ""

    def address(self, task, session):
        refs = self.store.kv_get("session.materials." + task["id"], {})
        mid = str(refs.get("message_id") or "") if isinstance(refs, dict) else ""
        if not mid:
            request = self.store.read().execute("SELECT message_id FROM requests WHERE id=?", (task.get("request_id") or session["request_id"],)).fetchone()
            mid = str(request[0] or "") if request else ""
        return {"push_kind": "command", "at_user": session["owner_id"], "reply_to": mid}

    def _pending(self, task):
        rows = self.store.read().execute("SELECT * FROM outbox WHERE task_id=? AND status='pending' AND key LIKE 'session:speech:%' ORDER BY id DESC", (task["id"],)).fetchall()
        return [r for r in rows if json.loads(r["payload"]).get("req_version") == int(task.get("req_version") or 1)]

    def relay(self, task, text, *, source):
        if source != "main":
            return
        session = self._session(task)
        if not session or not self.outbox:
            return
        text = self.clean(task["group_id"], text)
        if not text:
            return
        now = clock.now()
        with self.store.tx() as conn:
            recent = next((r for r in self._pending(task) if now - r["created"] < MERGE_SECONDS), None)
            if recent:
                payload = json.loads(recent["payload"])
                combined = self.clean(task["group_id"], payload["text"] + " " + text)
                if combined:
                    payload["text"] = combined
                    conn.execute("UPDATE outbox SET payload=?,updated=? WHERE id=? AND status='pending'", (json.dumps(payload, ensure_ascii=False), now, recent["id"]))
                    return
            payload = {**self.address(task, session), "text": text, "req_version": int(task.get("req_version") or 1)}
            self.outbox.enqueue(f"session:speech:{session['id']}:{task['id']}:{task.get('req_version',1)}:{now}",
                task["group_id"], "text", payload, task_id=task["id"], not_before=now + MERGE_SECONDS, conn=conn)

    async def start(self, task):
        session = self._session(task)
        if not session:
            return
        req_ver = int(task.get("req_version") or 1)
        started_key = f"session.started.{task['id']}.{req_ver}"
        if self.store.kv_get(started_key):
            return
        text = await self.natural(session, "刚准备开始这轮工作，用自然话回应主人，不能声称已完成。", {"task_id": task["id"], "标题": task["title"], "状态": task["status"]})
        self.relay(task, text, source="main")
        if text:
            with self.store.tx() as conn:
                self.store.kv_set(conn, started_key, 1)

    async def finish(self, task, note, *, deliver_kind: str = "", scale: str = "standard"):
        session = self._session(task)
        if not session:
            return "" if deliver_kind == "text" else note
        key = f"session.final.{task['id']}.{task.get('req_version',1)}"
        cached = self.store.kv_get(key)
        if cached is not None:
            return str(cached)
        proposal_key = "session.closing." + session["id"]
        proposed = self.store.kv_get(proposal_key, {})
        may_propose = proposed.get("task_id") != task["id"] or proposed.get("req_version") != task.get("req_version", 1)
        parts = [json.loads(r["payload"])["text"] for r in reversed(self._pending(task))]
        turn_no = int(task.get("turn_no") or 1)
        is_brief_turn1 = str(scale or "").strip().lower() == "brief" and turn_no <= 1

        if deliver_kind == "text":
            if is_brief_turn1:
                text = ""
                may_propose = False
            else:
                purpose = "正文紧接着就会发在下面，你只用接一句简短过渡话（二十字以内），不要复述结论或交付说明、不要提链接在哪、不要声称主人已收到。"
                if may_propose and turn_no > 1:
                    purpose += "可以顺口问主人这段工作是否先收尾，明确要再@你确认才会结束；不能自行关闭。"
                text = await self.natural(
                    session, purpose,
                    {"task_id": task["id"], "标题": task["title"], "第几轮": turn_no},
                )
            raw_joined = " ".join(dict.fromkeys([p for p in parts + [text] if p]))
            combined = self.clean(task["group_id"], raw_joined) if raw_joined else ""
        else:
            purpose = "这轮已通过验收，交付说明会一起附上；你只用补一句顺口收尾的话，不要复述交付说明里的结论，不要声称主人已收到。"
            if may_propose:
                purpose += "可以问主人这段工作是否先收尾，明确要再@你确认才会结束；不能自行关闭。"
            else:
                purpose += "本轮已提议过收尾，不重复提议。"
            text = await self.natural(session, purpose, {"task_id": task["id"], "标题": task["title"], "状态": task["status"], "交付说明": note})
            safe_note = self.clean(task["group_id"], note) or "成品已准备好。"
            tail = [text] if (text and _notes_overlap(text, safe_note)) else [x for x in (text, safe_note) if x]
            combined = self.clean(task["group_id"], " ".join(dict.fromkeys(parts + tail))) or safe_note

        # 只有提议那句真的进了最终那条消息才算提过；被闸门整条拒掉时主人没看到，不能挂确认。
        if text and may_propose and text in combined and re.search(r"收尾|结束.*[吗？?]", text):
            if self.closing_proposed:
                self.closing_proposed(task["id"])
        with self.store.tx() as conn:
            # 交付说明接管尚未发送的末句；已发/不确定记录绝不重放。
            for row in self._pending(task):
                conn.execute("UPDATE outbox SET status='dropped',error='已合入交付',updated=? WHERE id=? AND status='pending'", (clock.now(), row["id"]))
            self.store.kv_set(conn, key, combined)
        return combined
