"""identity.py（身份与工作记忆，2026-10 新增）。

MaiWork 自己的「长期记忆」分三层，都在 <data_dir>/identity/ 下（目录 0700，周边文件 0600；
exFAT 不支持 chmod 就静默放行）：

- SOUL.md      —— MaiWork 说话的口吻和性格。首次启动从 MaiBot（host.config 读
                 bot.nickname / personality.personality / personality.reply_style 以及
                 其他能读到的 personality.* 文字字段）自动生成一次；之后只有管理员在
                 网页上点「从 MaiBot 同步」才会覆盖，覆盖前旧版存 SOUL.md.bak。
- AGENTS.md    —— 给主模型和子 agent 的做事规矩（管理员维护；默认给一份简短模板）。
- MEMORY.md    —— 全局工作记忆：**只放和具体群、具体人无关的经验**（管理员偏好、
                 工具/来源好坏、做事方法的教训）。
- memory/<群号>.md —— 每群工作记忆：主模型自动记本群相关的经验（这个群喜欢什么交付、
                 哪类资讯被点没用、约定俗成的做法）。**只注入本群的提示词，绝不跨群**。

注入由 prompt_block(kind, group_id) 统一出，分块标题固定（前端/测试都认这几行）：
「## MaiWork 的身份」「## 做事规矩」「## 工作记忆（全局）」「## 这个群的工作记忆」；
各块内容按 UTF-8 编码截到上限（默认 16KB）。

自动记忆入口 remember_sync（工具 remember 的 handler 也走它）：
- 追加格式「- YYYY-MM-DD 文本（原因）」；同一句（去空白后相同，不看日期和原因）不重复记；
- 写满上限时从最旧的一条开始删，删到放得下；
- scope=group：过本群 privacy.scrub（关注成员 note/persona 片段拒）；
- scope=global：① 不许写 ≥5 位连续数字（群号/QQ 号）；② 不许点名——所有服务群
  关注成员名字、以及「这个群名字数 > 3」的成员名单里的名字；③ 过完名单闸还要把
  每个服务群的 privacy.scrub 各过一遍。全局被拦的话术固定带「全局记忆」四个字，
  提醒模型「改记到本群记忆」；
- 每次真正写进去都落 events 事件 memory.write（scope、群号、文本前 40 字），网页可追溯。

note_useless_feedback(gid, item_id)：资讯被标「没用」后由 console/feeds 调一次（纯代码、
不调模型）：同一 topic 里 down>up 累计 ≥3 条 → 记一条「XX 类资讯本群不感兴趣」；
同一来源（sources[0].site，回落 url_key 主机）有 ≥3 条各自被标过没用 → 记一条
「来自 XX 的资讯本群不感兴趣」。两类都只在累计确实够 3 的那个时刻落一条（当时不够以后
不再翻旧账），且和 remember 一样按句去重。
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Optional

from . import clock
from .privacy import scrub as _privacy_scrub

logger = logging.getLogger("maiwork.identity")

# 设计红线（SOUL 边界一节，固定三条口径；「不点名暴露私下画像」原文）
_BOUNDARY_LINES = (
    "- 不点名暴露私下画像：关心成员的小档案只有管理员能看，群里说起谁都不泄露。",
    "- 不刷屏：不在群里一直说自己的事；一天主动开口有节制，没人接就停。",
    "- 不冒充人：机器就是机器，被问到不装人。",
)

_AGENTS_DEFAULT = """# MaiWork 做事规矩

- 交付前自查：完成标准一条条对得上，才算做完。
- 不编造：查不到就说查不到，做不到就说做不到。
- 成品放 artifacts/：成品放在工作区 artifacts/<任务ID>/ 下才算数。
"""

# 同步 SOUL 时除了 nickname/personality/reply_style，还试着读这些 personality.* 文字字段
_EXTRA_PERSONALITY_KEYS = (
    "personality.interests",
    "personality.values",
    "personality.speaking_style",
    "personality.humor",
    "personality.taboos",
    "personality.extra",
)

_REMEMBER_TEXT_MAX = 200
_REMEMBER_REASON_MAX = 60
_GLOBAL_NUMBER_RUN = 5  # ≥N 位连续数字当群号/QQ 号
_MEMBER_NAMES_MIN_GROUP = 3  # 成员名单超过这个数才启用「点这个名字」闸（小群误伤太大）
_USELESS_DOWN_MIN = 3  # 被标没用累计几次才自动记
_FEEDBACK_LOOKBACK_DAYS = 90

_SOUL_KINDS = ("soul", "agents", "memory")


class Identity:
    """身份与工作记忆。data_dir 是插件数据目录（identity/ 建在下面）。"""

    def __init__(
        self,
        data_dir: Any,
        store: Any,
        get_settings: Callable[[], Any],
        *,
        host: Any = None,
    ) -> None:
        self._root = Path(data_dir) / "identity"
        self._store = store
        self._get_settings = get_settings
        self._host = host

    # ------------------------------------------------------------------
    # 路径 / 限额
    # ------------------------------------------------------------------

    @property
    def limits(self) -> dict[str, int]:
        return {"soul": 16384, "agents": 16384, "memory": 16384, "group_memory": 16384}

    def _limit_of(self, kind: str) -> int:
        return self.limits["group_memory" if kind == "group" else kind]

    def _path_of(self, kind: str, group_id: Optional[str] = None) -> Path:
        if kind == "soul":
            return self._root / "SOUL.md"
        if kind == "agents":
            return self._root / "AGENTS.md"
        if kind == "memory":
            return self._root / "MEMORY.md"
        if kind == "group":
            assert group_id is not None
            return self._root / "memory" / f"{group_id}.md"
        raise ValueError(f"不认识的身份文件种类：{kind!r}")

    # ------------------------------------------------------------------
    # 小工具（目录权限 / 落盘 / 截断 / 去重）
    # ------------------------------------------------------------------

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, stat.S_IMODE(mode))
        except OSError:
            pass  # exFAT 等不支持 chmod 的文件系统：静默放行

    def _ensure_dirs(self) -> None:
        memory_dir = self._root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        # 权限要收紧到 0700（memory/ 跟着 root 走）
        self._chmod(self._root, 0o700)
        self._chmod(memory_dir, 0o700)

    def _write_file(self, path: Path, text: str) -> None:
        """新建文件先 touch 再写（exFAT 红线）；写完尽量 0600。"""
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass
        path.write_text(str(text), encoding="utf-8")
        self._chmod(path, 0o600)

    @staticmethod
    def _cut_utf8(text: str, limit: int) -> str:
        """按 UTF-8 编码截到 limit 字节内，不劈半个字符。"""
        raw = str(text or "").encode("utf-8")
        if len(raw) <= limit:
            return str(text or "")
        return raw[:limit].decode("utf-8", errors="ignore")

    @staticmethod
    def _normalize(text: str) -> str:
        """去重比较用：所有空白折叠成无。同一句换行/空格写法不同也算同一句。"""
        return re.sub(r"\s+", "", str(text or ""))

    @staticmethod
    def _mem_lines(content: str) -> list[str]:
        """记忆文件的非空行（约定每条一行「- 日期 文本（原因）」），保持原序。"""
        return [ln for ln in str(content or "").splitlines() if ln.strip()]

    @classmethod
    def _line_key(cls, line: str) -> str:
        """单条记忆的去重键：去「- 」前缀、去 YYYY-MM-DD、去结尾（原因），再折叠空白。"""
        s = str(line or "").strip()
        s = re.sub(r"^-\s*", "", s)
        s = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", s)
        s = re.sub(r"（[^（）]*）\s*$", "", s)
        return cls._normalize(s)

    def _served_groups(self) -> list[str]:
        try:
            settings = self._get_settings()
        except Exception:
            return []
        groups = getattr(settings, "groups", None) or {}
        try:
            return [str(g) for g in groups.keys()]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 读 / 写
    # ------------------------------------------------------------------

    def read(self, kind: str) -> dict:
        if kind not in _SOUL_KINDS:
            raise ValueError(f"read 只认 {('/'.join(_SOUL_KINDS))}，收到 {kind!r}")
        path = self._path_of(kind)
        out: dict[str, Any] = {"text": "", "updated_ts": 0.0}
        try:
            st = path.stat()
            out["text"] = path.read_text(encoding="utf-8")
            out["updated_ts"] = float(st.st_mtime)
        except OSError:
            pass
        if kind == "soul":
            out["synced_from_maibot"] = self._soul_synced_flag(path)
        return out

    def _soul_synced_flag(self, soul_path: Path) -> bool:
        """SOUL 当前内容是不是「从 MaiBot 同步来的」。用一个哨兵文件 .soul_synced
        记下最近一次写 SOUL 是不是同步（mtime 精度在快速连续写下不可靠）。"""
        del soul_path
        try:
            return (self._root / ".soul_synced").read_text(encoding="utf-8").strip() == "1"
        except OSError:
            return False

    def _set_soul_synced(self, flag: bool) -> None:
        try:
            self._write_file(self._root / ".soul_synced", "1" if flag else "0")
        except OSError:
            pass

    def write(self, kind: str, text: str) -> dict:
        if kind not in _SOUL_KINDS:
            raise ValueError(f"write 只认 {('/'.join(_SOUL_KINDS))}，收到 {kind!r}")
        text = str(text if text is not None else "")
        limit = self._limit_of(kind)
        if len(text.encode("utf-8")) > limit:
            raise ValueError(f"超过单个文件上限（{limit} 字节）：请删减到 {limit // 1024}KB 以内")
        self._ensure_dirs()
        self._write_file(self._path_of(kind), text)
        if kind == "soul":
            self._set_soul_synced(False)  # 手动改过的不再是「从 MaiBot 同步」
        return self.read(kind)

    def group_read(self, group_id: str) -> dict:
        gid = str(group_id)
        if gid not in self._served_groups():
            raise KeyError(f"非服务群：{gid}")
        path = self._path_of("group", gid)
        out: dict[str, Any] = {"text": "", "updated_ts": 0.0}
        try:
            st = path.stat()
            out["text"] = path.read_text(encoding="utf-8")
            out["updated_ts"] = float(st.st_mtime)
        except OSError:
            pass
        return out

    def group_write(self, group_id: str, text: str) -> dict:
        gid = str(group_id)
        if gid not in self._served_groups():
            raise KeyError(f"非服务群：{gid}")
        text = str(text if text is not None else "")
        limit = self._limit_of("group")
        if len(text.encode("utf-8")) > limit:
            raise ValueError(f"超过单个文件上限（{limit} 字节）：请删减到 {limit // 1024}KB 以内")
        self._ensure_dirs()
        self._write_file(self._path_of("group", gid), text)
        return self.group_read(gid)

    def group_memory_map(self) -> dict[str, dict]:
        """GET /api/identity 的 group_memory：只列服务群（没写过的群给空文本）。"""
        return {gid: self.group_read(gid) for gid in self._served_groups()}

    # ------------------------------------------------------------------
    # prompt_block（注入）
    # ------------------------------------------------------------------

    def prompt_block(self, kind: str, group_id: Optional[str] = None) -> str:
        """注入提示词的统一出口。空文件不出块；各块按 UTF-8 截到上限；块与块空行隔开。"""
        parts: list[str] = []
        if kind == "soul":
            text = self._cut_utf8(self.read("soul")["text"], self._limit_of("soul")).strip()
            if text:
                parts.append(f"## MaiWork 的身份\n{text}\n\n")
        elif kind == "agents":
            text = self._cut_utf8(self.read("agents")["text"], self._limit_of("agents")).strip()
            if text:
                parts.append(f"## 做事规矩\n{text}\n\n")
        elif kind == "memory":
            g_text = self._cut_utf8(self.read("memory")["text"], self._limit_of("memory")).strip()
            if g_text:
                parts.append(f"## 工作记忆（全局）\n{g_text}\n\n")
            gid = str(group_id or "").strip()
            if gid and gid in self._served_groups():
                l_text = self._cut_utf8(self.group_read(gid)["text"], self._limit_of("group")).strip()
                if l_text:
                    parts.append(f"## 这个群的工作记忆\n{l_text}\n\n")
        return "".join(parts)

    # ------------------------------------------------------------------
    # 首次启动 + 从 MaiBot 同步
    # ------------------------------------------------------------------

    async def ensure_started(self) -> None:
        """插件启动时调一次：建目录；缺的文件补默认；SOUL 不存在则从 MaiBot 同步生成一次。"""
        self._ensure_dirs()
        agents = self._path_of("agents")
        if not agents.exists():
            self._write_file(agents, _AGENTS_DEFAULT)
        memory = self._path_of("memory")
        if not memory.exists():
            self._write_file(memory, "")
        soul = self._path_of("soul")
        if not soul.exists():
            try:
                await self.sync_soul_from_maibot()
            except Exception:
                logger.exception("首次从 MaiBot 同步 SOUL 出错，用兜底版")
                self._write_file(soul, self._render_soul({}, {}))
                self._set_soul_synced(True)  # 兜底版也算「来自 MaiBot 同步流程」的默认份
        else:
            # 兜底：老部署可能只有空文件/半截文件——不自动覆盖，只保证文件存在
            self._chmod(soul, 0o600)

    async def sync_soul_from_maibot(self) -> dict:
        """从 MaiBot 重新生成 SOUL。覆盖前旧版存 SOUL.md.bak；生成内容没变就不覆盖。

        返回单项 {"text","updated_ts","synced_from_maibot":True,"preview_changed":bool}。
        """
        got: dict[str, str] = {}
        host = self._host
        if host is not None:
            keys = ["bot.nickname", "personality.personality", "personality.reply_style", *_EXTRA_PERSONALITY_KEYS]
            for key in keys:
                try:
                    val = await host.config(key)
                except Exception:
                    val = None
                if val is not None and str(val).strip():
                    got[key] = str(val).strip()
        new_text = self._render_soul(got, {})
        soul_path = self._path_of("soul")
        old_text = ""
        try:
            old_text = soul_path.read_text(encoding="utf-8")
        except OSError:
            old_text = ""
        self._ensure_dirs()
        if self._normalize(old_text) != self._normalize(new_text):
            bak = self._root / "SOUL.md.bak"
            self._write_file(bak, old_text)  # 覆盖前旧版存 .bak（首次为空也留档）
            self._write_file(soul_path, new_text)
            self._set_soul_synced(True)
            out = self.read("soul")
            out["preview_changed"] = True
            return out
        # 没变：不覆盖，也不动 .bak；但若从来没同步过（手动建的文件），标记位不动
        if not (self._root / ".soul_synced").exists():
            self._set_soul_synced(True)
        out = self.read("soul")
        out["preview_changed"] = False
        return out

    @staticmethod
    def _render_soul(got: dict[str, str], extra: dict[str, str]) -> str:
        del extra  # 预留：以后把 personality.* 的 list 字段也展成文字
        nickname = got.get("bot.nickname", "")
        personality = got.get("personality.personality", "")
        reply_style = got.get("personality.reply_style", "")
        other_lines = [
            f"- {key.split('.', 1)[1]}：{got[key]}"
            for key in _EXTRA_PERSONALITY_KEYS
            if got.get(key)
        ]
        lines = [
            "# 我是谁",
            "",
            f"我是 {nickname or 'MaiBot'}（MaiWork 是它的后台搭档）。",
        ]
        if personality:
            lines.append(f"性格：{personality}")
        if other_lines:
            lines.extend(other_lines)
        lines.extend(["", "# 说话方式", ""])
        if reply_style:
            lines.append(str(reply_style))
        else:
            lines.append("口语、像跟熟人讲，别端着；别写播报腔。")
        lines.extend(["", "# 边界", ""])
        lines.extend(_BOUNDARY_LINES)
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # remember（主模型写，受控）
    # ------------------------------------------------------------------

    def remember_sync(
        self,
        *,
        scope: str,
        text: str,
        reason: str,
        group_id: str = "",
    ) -> dict:
        """主模型记经验（工具 remember 的落点）。返回 {"ok": bool, ...}；拒绝带中文 error。"""
        scope = str(scope or "").strip()
        text = str(text or "").strip()
        reason = str(reason or "").strip()
        gid = str(group_id or "").strip()
        if scope not in ("group", "global"):
            return {"ok": False, "error": "scope 只认 group / global"}
        if not text:
            return {"ok": False, "error": "text 不能是空的"}
        if len(text) > _REMEMBER_TEXT_MAX:
            return {"ok": False, "error": f"text 最多 {_REMEMBER_TEXT_MAX} 字，请精简后再记"}
        if len(reason) > _REMEMBER_REASON_MAX:
            return {"ok": False, "error": f"reason 最多 {_REMEMBER_REASON_MAX} 字，写简短点"}
        if scope == "group":
            if not gid:
                return {"ok": False, "error": "scope=group 要带 group_id"}
            if gid not in self._served_groups():
                return {"ok": False, "error": f"群 {gid} 不是服务群，不能记"}
        # 写入前过闸
        err = self._remember_gate(scope, gid, text)
        if err is not None:
            return {"ok": False, "error": err}
        # 追加
        kind = "group" if scope == "group" else "memory"
        path = self._path_of("group", gid) if scope == "group" else self._path_of("memory")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        lines = self._mem_lines(content)
        key = self._normalize(text)
        for ln in lines:
            if self._line_key(ln) == key:
                return {"ok": True, "deduped": True, "text": text}
        day = clock.bj(clock.now()).strftime("%Y-%m-%d")
        suffix = f"（{reason}）" if reason else ""
        lines.append(f"- {day} {text}{suffix}")
        limit = self._limit_of(kind)
        self._ensure_dirs()
        content = "\n".join(lines) + "\n"
        while lines and len(content.encode("utf-8")) > limit:
            lines.pop(0)  # 满了从最旧的删
            content = "\n".join(lines) + ("\n" if lines else "")
        self._write_file(path, content)
        # 事件落库（网页可追溯）
        try:
            with self._store.tx() as conn:
                self._store.event(
                    conn,
                    "memory.write",
                    group_id=gid if scope == "group" else "",
                    entity="memory",
                    entity_id=scope,
                    payload={"scope": scope, "group_id": gid, "text": text[:40]},
                )
        except Exception:
            logger.exception("memory.write 事件落库失败")
        return {"ok": True, "deduped": False, "text": text}

    def _remember_gate(self, scope: str, gid: str, text: str) -> Optional[str]:
        """写入前的闸；要拦就回中文原因，放行回 None。"""
        if scope == "group":
            cleaned = _privacy_scrub(gid, text, self._store)
            if cleaned is None:
                return "这段含私下画像的细节，不宜记下来（连本群记忆也不写）"
            return None
        # global：一、不许写长数字（群号/QQ 号）
        if re.search(rf"\d{{{_GLOBAL_NUMBER_RUN},}}", text):
            return "全局记忆里不能写具体的群和人（不许带 QQ 号 / 群号），改记到本群记忆"
        # 二、不许点名：全部服务群关注成员 + 「名字数>3」的群成员名单
        names = self._global_forbidden_names()
        for name in names:
            if name and len(name) >= 2 and name in text:
                return f"全局记忆里不能写具体的群和人（「{name}」是某个群的人），改记到本群记忆"
        # 三、全部服务群的 privacy.scrub 各过一遍（别的群的画像细节也不能写进全局）
        for g in self._served_groups():
            if _privacy_scrub(g, text, self._store) is None:
                return "这段含私下画像的细节，不宜写进全局记忆"
        return None

    def _global_forbidden_names(self) -> list[str]:
        """全局记忆不许出现的名字：所有服务群关注成员 + 大名单群的成员名字。"""
        out: list[str] = []
        try:
            groups = self._served_groups()
        except Exception:
            groups = []
        for gid in groups:
            try:
                rows = self._store.read().execute(
                    "SELECT name FROM focus_members WHERE group_id=? AND removed=0",
                    (gid,),
                ).fetchall()
                out.extend(str(r["name"] or "") for r in rows if str(r["name"] or "").strip())
            except Exception:
                logger.debug("读关注成员名单失败（群 %s），记得按没有处理", gid, exc_info=True)
            try:
                rows = self._store.read().execute(
                    "SELECT DISTINCT name FROM member_activity WHERE group_id=? AND name!=''",
                    (gid,),
                ).fetchall()
            except Exception:
                rows = []
            # 小群（名字太少）不做这道闸：撞名误伤太大
            if len(rows) > _MEMBER_NAMES_MIN_GROUP:
                out.extend(str(r["name"] or "") for r in rows if str(r["name"] or "").strip())
        # 去重，名字按长度倒序（长名先拦，避免短名子串抢闸误伤）
        uniq = sorted({n for n in out if len(n) >= 2}, key=len, reverse=True)
        return uniq

    # ------------------------------------------------------------------
    # 反馈自动记（纯代码；console 的反馈接口触发）
    # ------------------------------------------------------------------

    def note_useless_feedback(self, group_id: str, item_id: int) -> None:
        """资讯被标「没用」后调一次：累计够 3 就记进本群记忆（不重复记、绝不跨群）。"""
        gid = str(group_id)
        if gid not in self._served_groups():
            return
        try:
            iid = int(item_id)
        except (TypeError, ValueError):
            return
        try:
            row = self._store.read().execute(
                "SELECT id, group_id, topic, sources, url_key FROM news_items WHERE id=?",
                (iid,),
            ).fetchone()
        except Exception:
            return
        if row is None or str(row["group_id"]) != gid:
            return
        since = clock.now() - _FEEDBACK_LOOKBACK_DAYS * 86400.0
        try:
            rows = self._store.read().execute(
                "SELECT topic, sources, url_key, up, down FROM news_items"
                " WHERE group_id=? AND created>=? AND rejected=0 AND (down>0 OR up>0)",
                (gid, since),
            ).fetchall()
        except Exception:
            return

        # topic 级：被标没用的条数（down>up）≥3
        topic = str(row["topic"] or "").strip()
        if topic:
            n = sum(
                1 for r in rows
                if str(r["topic"] or "").strip() == topic and int(r["down"] or 0) > int(r["up"] or 0)
            )
            if n >= _USELESS_DOWN_MIN:
                self._remember_direct(gid, f"{topic} 类资讯本群不感兴趣", "被标没用累计 3 次")

        # 来源级：同一来源有 ≥3 条各自被标没用
        site = self._site_of_row(row)
        if site:
            def _same_site(r: Any) -> bool:
                return self._site_of_row(r) == site

            n = sum(
                1 for r in rows
                if _same_site(r) and int(r["down"] or 0) > int(r["up"] or 0)
            )
            if n >= _USELESS_DOWN_MIN:
                self._remember_direct(gid, f"来自 {site} 的资讯本群不感兴趣", "被标没用累计 3 次")

    @staticmethod
    def _site_of_row(row: Any) -> str:
        try:
            src = json.loads(row["sources"] or "[]")
            if isinstance(src, list) and src and isinstance(src[0], dict):
                site = str(src[0].get("site") or "").strip()
                if site:
                    return site
        except (ValueError, TypeError):
            pass
        url_key = str(row["url_key"] or "")
        return url_key.split("/", 1)[0].strip() if url_key else ""

    def _remember_direct(self, gid: str, text: str, reason: str) -> None:
        """代码直写本群记忆：和 remember 一样的追加/淘汰/去重，但不走模型闸、
        字段以代码拼好的为准（内容不含人和隐私片段，不必再过名单闸；仍过本群 scrub 兜底）。"""
        text = str(text or "").strip()[:_REMEMBER_TEXT_MAX]
        reason = str(reason or "").strip()[:_REMEMBER_REASON_MAX]
        if not text:
            return
        if _privacy_scrub(gid, text, self._store) is None:
            return  # 兜底：万一文本凑巧撞上画像细节就不记
        path = self._path_of("group", gid)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        lines = self._mem_lines(content)
        key = self._normalize(text)
        for ln in lines:
            if self._line_key(ln) == key:
                return  # 已有同一句
        day = clock.bj(clock.now()).strftime("%Y-%m-%d")
        lines.append(f"- {day} {text}（{reason}）" if reason else f"- {day} {text}")
        self._ensure_dirs()
        content = "\n".join(lines) + "\n"
        limit = self._limit_of("group")
        while lines and len(content.encode("utf-8")) > limit:
            lines.pop(0)
            content = "\n".join(lines) + ("\n" if lines else "")
        self._write_file(path, content)
        try:
            with self._store.tx() as conn:
                self._store.event(
                    conn,
                    "memory.write",
                    group_id=gid,
                    entity="memory",
                    entity_id="feedback",
                    payload={"scope": "group", "group_id": gid, "text": text[:40], "via": "feedback"},
                )
        except Exception:
            logger.exception("feedback→memory.write 事件落库失败")


def register_remember_tool(tools: Any, identity: Identity) -> None:
    """把 remember 工具注册进 Tools（roles={"main"}；子 agent 不能记）。"""
    from .tools import Tool, ToolResult

    def _summarize(args: dict, result: ToolResult) -> tuple[str, str]:
        scope = str(args.get("scope") or "")
        text = str(args.get("text") or "")
        return f"{scope}: {text[:40]}", result.output[:120]

    async def _handler(ctx: Any, args: dict) -> ToolResult:
        out = identity.remember_sync(
            scope=str(args.get("scope") or ""),
            text=str(args.get("text") or ""),
            reason=str(args.get("reason") or ""),
            group_id=str(getattr(ctx, "group_id", "") or ""),
        )
        if out.get("ok"):
            if out.get("deduped"):
                return ToolResult(ok=True, output="这条已经记过了，不重复记", data=out)
            return ToolResult(ok=True, output="记好了", data=out)
        return ToolResult(ok=False, output="", error=str(out.get("error") or "没记成"))

    tools.register(
        Tool(
            name="remember",
            description=(
                "记一条干活学到的经验。scope=group 记本群相关的（这个群喜欢什么交付、"
                "哪类资讯不受欢迎，最多 200 字）；scope=global 只记和具体群、具体人无关的"
                "通用经验（不许写群号、QQ 号、任何人的名字——那种请记本群记忆）。"
                "reason 一句话说为什么值得记（最多 60 字）。没有值得记的就别调。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["group", "global"]},
                    "text": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["scope", "text", "reason"],
            },
            roles=frozenset({"main"}),
            handler=_handler,
            summarize=_summarize,
        )
    )


__all__ = ["Identity", "register_remember_tool"]
