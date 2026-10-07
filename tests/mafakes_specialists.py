"""专岗集成测试共用的 Fake 对象（契约 A/B 接口的内存实现）。

不 import A/B 的真模块（这两个文件可能还没就位），只实现契约里 C 侧会调的方法。
C 侧一律按鸭子类型调用；「专岗已挂上」时用 getattr(..., "_agents", None) 拿 Agents
做 profile.enabled 判定 + accepted 后 remember（父会话已同意 `_agents` 当稳定引用）。
"""

from __future__ import annotations

from CharTyr_MaiWork.maiwork.workers import WorkerReport


class FakeAgents:
    """内存版 Agents（契约公开方法）。"""

    def __init__(self, *, enabled: bool = True, enabled_kinds=None):
        self.calls: dict[str, list] = {
            "profile": [], "remember": [], "review": [], "fail": [],
            "begin": [], "returned": [], "set_notes": [],
        }
        self.remembers: list[dict] = []
        self.reviews: list[dict] = []
        self.fails: list[dict] = []
        self._profiles = {}
        read_only = ("web_search", "fetch_page", "read_profile", "search_chat",
                     "read_chat_history", "list_skills", "read_skill", "submit_result")
        for kind, tools, skills in (
            ("news", read_only, ("news-standard",)),
            ("idea", read_only, ()),
            ("goal", read_only, ()),
            ("task", None, ()),
        ):
            self._profiles[kind] = {
                "kind": kind, "title": kind, "instructions": "", "skills": list(skills),
                "enabled": bool(enabled), "tools": (list(tools) if tools is not None else None),
            }
        if enabled_kinds:
            for kind, on in enabled_kinds.items():
                if kind in self._profiles:
                    self._profiles[kind]["enabled"] = bool(on)
        self._counter = 0
        self._handoffs: dict[str, dict] = {}

    # 契约公开方法 -----------------------------------------------------

    def profile(self, kind: str) -> dict:
        self.calls["profile"].append(str(kind))
        p = self._profiles.get(str(kind))
        if p is None:
            raise ValueError(f"不存在的岗位：{kind}")
        return dict(p)

    def remember(self, gid, kind, text, refs=(), source_id="", now=None):
        self.calls["remember"].append((str(gid), str(kind)))
        self.remembers.append({
            "gid": str(gid), "kind": str(kind), "text": str(text),
            "refs": [str(x) for x in refs], "source_id": str(source_id or ""),
        })

    def review(self, gid, id, accepted, summary, refs=(), *, learn=True):
        self.calls["review"].append((str(gid), str(id), bool(accepted)))
        self.reviews.append({
            "gid": str(gid), "id": str(id), "accepted": bool(accepted),
            "summary": str(summary), "refs": [str(x) for x in refs], "learn": bool(learn),
        })
        h = self._handoffs.get(str(id))
        if h is not None:
            h["status"] = "accepted" if accepted else "rejected"

    def begin(self, gid, kind, brief, *, task_id="", phase="", parent_id="",
              tools=(), skills=(), criteria=None):
        self.calls["begin"].append((str(gid), str(kind)))
        self._counter += 1
        hid = f"H-{self._counter}"
        self._handoffs[hid] = {
            "id": hid, "gid": str(gid), "kind": str(kind), "status": "queued",
            "task_id": str(task_id or ""), "phase": str(phase or ""),
        }
        return hid

    def returned(self, gid, id, summary, data=None, evidence=(), *, ok=True, error="", used_tools=None):
        self.calls["returned"].append((str(gid), str(id), bool(ok)))
        h = self._handoffs.get(str(id))
        if h is not None:
            h["status"] = "returned" if ok else "failed"

    def fail(self, gid, id, error, *, state="failed", used_tools=None):
        self.calls["fail"].append((str(gid), str(id), str(state)))
        self.fails.append({"gid": str(gid), "id": str(id), "state": str(state), "error": str(error)})
        h = self._handoffs.get(str(id))
        if h is not None:
            h["status"] = str(state)

    def handoffs(self, gid, kind=None, limit=20):
        items = [dict(h) for h in self._handoffs.values() if h["gid"] == str(gid)]
        if kind:
            items = [h for h in items if h["kind"] == kind]
        return items[:limit]

    def handoff(self, gid, id):
        h = self._handoffs.get(str(id))
        if h is None or h["gid"] != str(gid):
            return None
        return dict(h)

    # 开关辅助 -----------------------------------------------------------

    def set_enabled(self, kind: str, enabled: bool) -> None:
        self._profiles[str(kind)]["enabled"] = bool(enabled)


class FakeSpecialists:
    """契约 B 接口的内存 Specialists：录每一次 run/review，按队列回放 report。"""

    def __init__(self, agents: FakeAgents, results=None, fail_exc=None):
        self._agents = agents
        self.queue = list(results or [])
        self.fail_exc = fail_exc
        self.runs: list[dict] = []
        self.reviews: list[dict] = []

    async def run(self, kind, brief, *, group_id, phase="", task_id="",
                  tools=None, output_schema=None, actor="", deadline_ts=None,
                  parent_id="", workspace=None, max_steps=0, artifact_scope=None, criteria=None,
                  write_scope=None, history=None, escalate=False):
        try:
            profile = self._agents.profile(kind)
        except Exception:
            return WorkerReport(ok=False, summary="", error=f"岗位 {kind} 不存在")
        if not profile.get("enabled"):
            return WorkerReport(ok=False, summary="", error=f"岗位 {kind} 已停用")
        hid = self._agents.begin(
            str(group_id), str(kind), str(brief)[:200],
            task_id=str(task_id or ""), phase=str(phase or ""), parent_id=str(parent_id or ""),
            tools=tuple(tools or ()), skills=(),
        )
        white = profile.get("tools")
        if white is None:
            eff_tools = list(tools or [])
        else:
            eff_tools = [t for t in (list(tools) if tools is not None else list(white))
                         if t in set(white)]
        self.runs.append({
            "kind": str(kind), "brief": str(brief), "group_id": str(group_id),
            "phase": str(phase or ""), "task_id": str(task_id or ""),
            "tools": list(tools) if tools is not None else None,
            "eff_tools": eff_tools, "output_schema": output_schema,
            "actor": str(actor or ""), "workspace": workspace,
            "max_steps": int(max_steps or 0), "artifact_scope": artifact_scope,
            "deadline_ts": deadline_ts, "parent_id": str(parent_id or ""),
        })
        if self.fail_exc is not None:
            self._agents.fail(str(group_id), hid, str(self.fail_exc), state="failed")
            raise self.fail_exc
        if not self.queue:
            report = WorkerReport(ok=True, summary="stub ok")
        else:
            report = self.queue.pop(0)
            if isinstance(report, BaseException):
                self._agents.fail(str(group_id), hid, str(report), state="failed")
                raise report
        if not isinstance(report, WorkerReport):
            report = WorkerReport(ok=False, summary="", error="bad queue item")
        report.handoff_id = hid  # B 契约：交接 ID 回填；绝不 auto accept
        self._agents.returned(
            str(group_id), hid, str(report.summary or ""),
            data=report.data, evidence=list(report.evidence or []),
            ok=bool(report.ok), error=str(report.error or ""))
        return report

    def review(self, gid, report_or_id, accepted, summary, refs=(), *, learn=True):
        if isinstance(report_or_id, WorkerReport):
            hid = str(getattr(report_or_id, "handoff_id", "") or "")
        else:
            hid = str(report_or_id or "")
        self.reviews.append({
            "gid": str(gid), "id": hid, "accepted": bool(accepted),
            "summary": str(summary), "refs": [str(x) for x in refs], "learn": bool(learn),
        })
        if not hid:
            return
        # 契约（父会话确认）：learn=False 只关学习，仍落 accepted/rejected 终态
        self._agents.review(str(gid), hid, bool(accepted), str(summary), refs=refs, learn=learn)

    # 查询辅助 -----------------------------------------------------------

    def runs_of(self, kind: str) -> list[dict]:
        return [r for r in self.runs if r["kind"] == kind]

    def tools_of(self, kind: str):
        runs = self.runs_of(kind)
        if not runs or runs[0]["tools"] is None:
            return None
        return list(runs[0]["tools"])


class FakeWorkersQueue:
    """按队列回放 WorkerReport 的假 Workers；录下调用（老路 / 对比断言用）。"""

    def __init__(self, results=None):
        self.queue = list(results or [])
        self.calls: list[dict] = []

    async def run(self, brief, *, group_id, tools, task_id="", actor="\u5b50 agent #1",
                  max_steps=0, output_schema=None, workspace=None, skills_hint=None,
                  system_extra="", deadline_ts=None, artifact_scope=None, write_scope=None,
                  agent_type="task", allowed_tools=None, allowed_skills=None, used_tools=None):
        self.calls.append({
            "brief": str(brief), "group_id": str(group_id), "tools": list(tools or []),
            "task_id": str(task_id or ""), "actor": str(actor or ""),
            "output_schema": output_schema, "workspace": workspace,
            "artifact_scope": artifact_scope, "deadline_ts": deadline_ts,
            "agent_type": str(agent_type or ""),
        })
        if not self.queue:
            return WorkerReport(ok=True, summary="stub ok")
        r = self.queue.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r
