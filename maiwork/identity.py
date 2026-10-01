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

from . import clock, privacy
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

## 专用机器（配了才写；名字要和设置里「专用 SSH 机器」的名字一样）

<!-- 例：
- 小黑：4 核 8G，装好了 Docker 和 Node 20，编译、跑服务用它。
- 显卡机：有一张 4090，只在要跑模型、处理图片视频时用。
-->
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

# 专岗改版 3/4：每个专岗自己的 SOUL.md / AGENTS.md。
# 存储在 <data_dir>/identity/agents/<kind>/ 下；内建 five 个（main/news/idea/goal/task）
# + 自定义（kind 以 c_ 开头）。旧的全局 SOUL.md / AGENTS.md / MEMORY.md / memory/<gid>.md
# 留在原处不动：全局 MEMORY 和每群记忆 API 不变；旧的全局 SOUL/AGENTS 首启动时拷给 main
# （不删，不丢数据）。agent_presets/<kind>.md 是「恢复默认 / 首次内建」的模板；
# 自定义专岗用 agent_presets/custom.md（preset 找不到的 kind 也回落到它）。
_BUILTIN_AGENT_KINDS: tuple[str, ...] = ("main", "news", "idea", "goal", "task")
_AGENT_KIND_RE = re.compile(r"^(?:main|news|idea|goal|task|c_[0-9a-z]{6,32})$")
_PRESET_DIR = Path(__file__).resolve().parent / "agent_presets"
_MIGRATION_MARK = "agents/.migrated"  # 相对 identity 根；存在 = 已经迁过
_CUSTOM_STUB_HEADING = "## 新建的专岗"  # 主模型 AGENTS.md 里登记新建专岗的位置
_CUSTOM_TRASH_DIRNAME = ".trash"       # 删除专岗时身份文件挪这里（相对 agents/）


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
        """读全局身份文件（旧 API）。kind="soul"/"agents"：专岗改版 3/4 起真源是 main 的
        专岗文档（identity/agents/main/{SOUL,AGENTS}.md）；它还不在/是空 → 回旧全局文件
        （很老的部署 ensure_started 刚跑完迁移那一会儿就两边都齐了）。"""
        if kind not in _SOUL_KINDS:
            raise ValueError(f"read 只认 {('/'.join(_SOUL_KINDS))}，收到 {kind!r}")
        if kind in ("soul", "agents"):
            try:
                main_doc = self.agent_read("main", kind)
            except KeyError:
                main_doc = None
            legacy_text = self._read_text_or_empty(self._path_of(kind))
            if main_doc is not None and (
                str(main_doc.get("text") or "").strip()
                or not legacy_text.strip()
            ):
                return main_doc
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
        """写全局身份文件（旧 API）。kind="soul"/"agents"：旧文件（identity/{SOUL,AGENTS}.md）
        和 main 的专岗文档一起写——AGENTS.md 的真源已经是 main 那一份，旧 API 改全局也要让
        专岗文档跟着变（不然前端改完看不见）。"""
        if kind not in _SOUL_KINDS:
            raise ValueError(f"write 只认 {('/'.join(_SOUL_KINDS))}，收到 {kind!r}")
        text = str(text if text is not None else "")
        limit = self._limit_of(kind)
        if len(text.encode("utf-8")) > limit:
            raise ValueError(f"超过单个文件上限（{limit} 字节）：请删减到 {limit // 1024}KB 以内")
        self._ensure_dirs()
        self._write_file(self._path_of(kind), text)
        if kind in ("soul", "agents"):
            try:
                self.agent_write("main", kind, text)
            except KeyError:
                pass  # 迁移还没跑（极端老部署启动早期）：只落旧全局
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
        """注入提示词的统一出口（旧路径：全局 SOUL/AGENTS 等价于 main 的专岗文档）。

        kind = "soul" / "agents"：等价于 agent_prompt_block("main", kind)——主模型
        （含调用方没分专岗的老代码，比如 feeds 写帖子）读的就是 main 的 SOUL/AGENTS。
        kind = "memory"：全局 MEMORY.md + 可选本群记忆（这块不移到 agents/ 下，
        「记忆」页还在用全局路径）。
        """
        parts: list[str] = []
        if kind == "soul":
            text = self._cut_utf8(self.agent_read("main", "soul")["text"], self._limit_of("soul")).strip()
            if text:
                parts.append(f"## MaiWork 的身份\n{text}\n\n")
        elif kind == "agents":
            text = self._cut_utf8(self.agent_read("main", "agents")["text"], self._limit_of("agents")).strip()
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
    # 专岗 SOUL / AGENTS（阶段 3：identity/agents/<kind>/）
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_kind_ok(kind: Any) -> bool:
        """专岗 kind 合法：内建 five 个或 c_<2–32 位小写字母数字>；绝不许路径穿越。"""
        k = str(kind or "").strip()
        return bool(_AGENT_KIND_RE.match(k))

    def _agent_dir(self, kind: str) -> Path:
        """这个 kind 的专岗文档目录 identity/agents/<kind>/；不认识的 kind → KeyError。"""
        k = str(kind or "").strip()
        if not self._agent_kind_ok(k):
            raise KeyError(f"没有这个专岗：{k or '(空)'}")
        return self._root / "agents" / k

    def _agent_path(self, kind: str, which: str) -> Path:
        w = str(which or "").strip()
        if w not in ("soul", "agents"):
            raise KeyError(f"不认识的专岗文档：{w or '(空)'}")
        return self._agent_dir(kind) / ("SOUL.md" if w == "soul" else "AGENTS.md")

    @staticmethod
    def _agent_preset_path(kind: str) -> Path:
        """岗位预设：agent_presets/<kind>.md；自定义 kind（或预设文件没有）回落 custom.md。"""
        p = _PRESET_DIR / f"{kind}.md"
        if p.is_file():
            return p
        return _PRESET_DIR / "custom.md"

    @staticmethod
    def _agent_preset_text(kind: str) -> str:
        try:
            return Identity._agent_preset_path(kind).read_text(encoding="utf-8")
        except OSError:
            return ""

    def agent_read(self, kind: str, which: str) -> dict[str, Any]:
        """这一个专岗的 SOUL.md 或 AGENTS.md：{"text","updated_ts"}；soul 多带 synced_from_maibot。"""
        path = self._agent_path(kind, which)
        out: dict[str, Any] = {"text": "", "updated_ts": 0.0}
        try:
            st = path.stat()
            out["text"] = path.read_text(encoding="utf-8")
            out["updated_ts"] = float(st.st_mtime)
        except OSError:
            pass
        if str(which) == "soul":
            out["synced_from_maibot"] = self._agent_soul_synced_flag(kind)
        return out

    def agent_read_all(self, kind: str) -> dict[str, Any]:
        """GET /api/agents/{kind}/docs 要的形状：{"soul":…,"agents":…,"limits":{"soul":…,"agents":…}}。"""
        k = str(kind or "").strip()
        if not self._agent_kind_ok(k):
            raise KeyError(f"没有这个专岗：{k or '(空)'}")
        return {
            "soul": self.agent_read(k, "soul"),
            "agents": self.agent_read(k, "agents"),
            "limits": {"soul": int(self.limits["soul"]), "agents": int(self.limits["agents"])},
        }

    def agent_write(self, kind: str, which: str, text: str) -> dict[str, Any]:
        """写这一份专岗文档；超 16KB ValueError；手动改 SOUL 之后 synced_from_maibot 落 False。"""
        path = self._agent_path(kind, which)
        text_s = str(text if text is not None else "")
        limit = self._limit_of(str(which))
        if len(text_s.encode("utf-8")) > limit:
            raise ValueError(f"超过单个文件上限（{limit} 字节）：请删减到 {limit // 1024}KB 以内")
        self._ensure_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(path.parent, 0o700)
        self._write_file(path, text_s)
        if str(which) == "soul":
            self._agent_set_soul_synced(kind, False)
        return self.agent_read(kind, which)

    async def agent_sync_soul(self, kind: str) -> dict[str, Any]:
        """从 MaiBot 同步生成这一份专岗 SOUL（协程）；规则见 _sync_soul_file。
        专岗的兜底是空串（别给子 agent 灌「# 我是谁 / 边界」的占位模板）。
        返回 {"text","updated_ts","synced_from_maibot","preview_changed"[, "persona_missing"]}。"""
        path = self._agent_path(kind, "soul")
        kind_s = str(kind).strip()
        self._ensure_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(path.parent, 0o700)
        changed, missing = await self._sync_soul_file(
            path, fallback="", set_flag=lambda f: self._agent_set_soul_synced(kind_s, f),
        )
        if changed and kind_s == "main":
            # 主模型那一份也镜像回旧全局 SOUL.md：admin_chat 等还在 prompt_block("soul")
            # / read("soul") 的调用方走的就是 main 的专岗文档，两边本来就是一回事
            try:
                self._overwrite_with_bak(self._path_of("soul"), path.read_text(encoding="utf-8"))
                self._set_soul_synced(True)
            except OSError:
                pass
        out = self.agent_read(kind, "soul")
        out["preview_changed"] = changed
        if missing:
            out["persona_missing"] = True
        return out

    async def _maibot_persona(self) -> dict[str, str]:
        """读 MaiBot 的昵称 / 人格文字字段；读不到的键跳过，一个都没有就是空 dict。"""
        got: dict[str, str] = {}
        host = self._host
        if host is None:
            return got
        keys = ["bot.nickname", "personality.personality", "personality.reply_style", *_EXTRA_PERSONALITY_KEYS]
        for key in keys:
            try:
                val = await host.config(key)
            except Exception:
                val = None
            if val is not None and str(val).strip():
                got[key] = str(val).strip()
        return got

    def _overwrite_with_bak(self, path: Path, new_text: str) -> None:
        """写新内容；旧内容非空才先存 <name>.bak（被覆盖的是空文件就没有可备份的）。"""
        old_text = self._read_text_or_empty(path)
        if old_text.strip():
            self._write_file(path.with_suffix(".md.bak"), old_text)
        self._write_file(path, new_text)

    async def _sync_soul_file(
        self, path: Path, *, fallback: str, set_flag: Callable[[bool], None],
    ) -> tuple[bool, bool]:
        """全局 SOUL 和专岗 SOUL 共用的「从 MaiBot 同步」规则，返回 (改没改, 没读到人格)。

        - 读到人格：渲染出新内容，和现在不同才覆盖（旧内容非空才存 .bak），标「已同步」；
        - 没读到人格：绝不覆盖已有内容；文件不存在或是空的才写兜底（全局 = 带边界三条的
          模板，专岗 = 空），标「未同步」——不把兜底冒充成同步来的（外部审查 2026-10-02）。
        """
        got = await self._maibot_persona()
        old_text = self._read_text_or_empty(path)
        if not got:
            changed = False
            if not path.exists() or (not old_text.strip() and fallback.strip()):
                self._write_file(path, fallback)
                changed = bool(fallback.strip())
                set_flag(False)
            return changed, True
        new_text = self._render_soul(got, {})
        if not path.exists() or self._normalize(old_text) != self._normalize(new_text):
            self._overwrite_with_bak(path, new_text)
            set_flag(True)
            return True, False
        set_flag(True)  # 内容和 MaiBot 现在的人格一致：就是同步的
        return False, False

    def agent_reset_agents(self, kind: str) -> dict[str, Any]:
        """把 AGENTS.md 换回 agent_presets/<kind>.md 的内容（自定义专岗回落 custom.md）。
        预设文件本身读不到 → ValueError（别默默写空）。返回 {"text","updated_ts"}。"""
        path = self._agent_path(kind, "agents")
        preset = self._agent_preset_text(str(kind).strip())
        if not preset:
            raise ValueError(f"找不倒这个专岗的预设：{kind}")
        self._ensure_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(path.parent, 0o700)
        self._write_file(path, preset)
        return self.agent_read(kind, "agents")

    # ------------------------------------------------------------------
    # 自定义专岗：建 / 删（专岗改版 4/4）
    # ------------------------------------------------------------------

    async def agent_init_custom(self, kind: str, title: str) -> None:
        """新建自定义专岗（POST /api/agents 的落点）：identity/agents/<kind>/ 就位。
        - SOUL 从 MaiBot 同步（没人格就空，和 _migrate_agent_docs_once 的规矩一致）；
        - AGENTS 用 custom.md 预设；
        - 然后在 main 的 AGENTS.md 里自动加一行 stub（「新建的专岗」小节），
          提示主模型「什么时候派给它」还没写。
        """
        kind_s = str(kind or "").strip()
        if not self._agent_kind_ok(kind_s) or kind_s in _BUILTIN_AGENT_KINDS:
            raise KeyError(f"不能这样建专岗：{kind_s or '(空)'}")
        title_s = str(title or "").strip()[:40]
        # SOUL：从 MaiBot 同步（沿用 agent_sync_soul 的「覆盖前存 .bak」流程没意义——
        # 新 kind 一定是空目录；直接走 got 渲染更省事，而且行为一致）
        await self.agent_sync_soul(kind_s)
        # AGENTS：custom 预设
        self.agent_reset_agents(kind_s)
        # main AGENTS.md 加一行 stub（幂等：同一 kind 不重复加）
        self._append_custom_stub_to_main_agents(kind_s, title_s)

    def _append_custom_stub_to_main_agents(self, kind: str, title: str) -> None:
        """在 main 的 AGENTS.md「新建的专岗」小节里补一行 stub（管理员可再改）。
        位置：优先找既有的「## 新建的专岗」小节；没有就挂在文末（新建这一节）。"""
        try:
            path = self._agent_path("main", "agents")
        except KeyError:
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        stub = f"- 「{title}」({kind})：什么时候派给它？（请补上）"
        # 幂等：已经有一行 (kind) 就不重复（管理员改过名字行还在）
        if f"({kind})" in text:
            return
        new_text = self._insert_stub_under_heading(text, _CUSTOM_STUB_HEADING, stub)
        self._ensure_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(path.parent, 0o700)
        self._write_file(path, new_text)

    @staticmethod
    def _insert_stub_under_heading(text: str, heading: str, stub: str) -> str:
        """把 stub 插在「## 新建的专岗」那一段末尾；没有这段就追加在文末（新建这一节）。"""
        lines = str(text or "").splitlines()
        # 1) 优先挂在「## 新建的专岗」小节的最后一行（小节末尾或下一个 ## 前）
        anchor_idx: int | None = None
        for i, ln in enumerate(lines):
            if ln.strip().startswith("##") and heading in ln:
                anchor_idx = i
                break
            # main.md 预设的小节名可能写的是「## 派给哪个专岗」+注释；抓那个「下面是你新建的专岗」
            # 提示行也可以当作同一小节的末尾（插在那一行后面）
            if "下面是你新建的专岗" in ln or "新建的专岗" in ln and ln.strip().startswith("-"):
                anchor_idx = i
        if anchor_idx is not None:
            # 小节从 anchor 往下，找到下一个 ## 或文末尾；stub 插在前面
            j = anchor_idx + 1
            end = len(lines)
            while j < len(lines):
                if lines[j].strip().startswith("## ") and j > anchor_idx:
                    end = j
                    break
                # 预设的注释块（<!-- … -->）里的「例如」是说明，不是 stub；stub 插在注释块后面
                if lines[j].strip() == "-->":
                    end = j + 1
                    break
                j += 1
            # 往上找最后一行非空
            while end > anchor_idx + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, stub)
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        # 2) 没找到小节：追加在文末，自带小节标题
        parts = [str(text or "").rstrip("\n"), "", heading, "", stub, ""]
        return "\n".join(parts)

    def _remove_custom_stub_from_main_agents(self, kind: str) -> bool:
        """DELETE 时把 main AGENTS.md 里「(<kind>)」那一行删掉——只删「还没被管理员改过」
        的（还是 stub 原型的那行）；管理员已经在前面/后面写过什么时候派给它，就保留不动
        （他的判断可能比软件更准）。返回 True=删过一行；False=没动（行不在 / 已被改过）。"""
        try:
            path = self._agent_path("main", "agents")
        except KeyError:
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        lines = str(text or "").splitlines()
        stub_mark = f"({kind})"
        proto_mark = "什么时候派给它？（请补上）"
        for i, ln in enumerate(lines):
            if stub_mark in ln and proto_mark in ln:
                # 还是 stub 原型（管理员没动）→ 删整行
                lines.pop(i)
                self._ensure_dirs()
                self._write_file(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
                return True
        return False

    def agent_trash_custom(self, kind: str) -> Path | None:
        """删专岗时把它的文档目录挪到 identity/agents/.trash/<时间戳>_<kind>/（不硬删）。
        返回挪到的新位置；源目录本来就不在（已删过 / 没建过）返回 None。"""
        kind_s = str(kind or "").strip()
        if not self._agent_kind_ok(kind_s):
            raise KeyError(f"没有这个专岗：{kind_s or '(空)'}")
        src = self._root / "agents" / kind_s
        if not src.is_dir():
            return None
        self._remove_custom_stub_from_main_agents(kind_s)
        trash = self._root / "agents" / _CUSTOM_TRASH_DIRNAME
        trash.mkdir(parents=True, exist_ok=True)
        self._chmod(trash, 0o700)
        # 名字带时间戳，删了再建同 kind 不撞
        ts = clock.bj(clock.now()).strftime("%Y%m%d-%H%M%S")
        dst = trash / f"{ts}_{kind_s}"
        n = 0
        while dst.exists():
            n += 1
            dst = trash / f"{ts}_{kind_s}.{n}"
        try:
            src.rename(dst)
        except OSError:
            logger.exception("挪专岗 %s 的文档到 .trash 失败（留着不硬删）", kind_s)
            return None
        return dst

    def _agent_synced_mark_path(self, kind: str) -> Path:
        return self._agent_dir(kind) / ".soul_synced"

    def _agent_soul_synced_flag(self, kind: str) -> bool:
        try:
            return self._agent_synced_mark_path(kind).read_text(encoding="utf-8").strip() == "1"
        except OSError:
            return False

    def _agent_set_soul_synced(self, kind: str, flag: bool) -> None:
        try:
            self._write_file(self._agent_synced_mark_path(kind), "1" if flag else "0")
        except OSError:
            pass

    def agent_prompt_block(self, kind: str, which: str) -> str:
        """专岗的 SOUL / AGENTS 提示词块（标题和全局版一致；空文件出空串）。

        which 只认 "soul" / "agents"；其他 ValueError。大小同 prompt_block：«## MaiWork 的身份»
        或 «## 做事规矩»，按 UTF-8 截 16KB，末尾空行。
        """
        w = str(which or "").strip()
        if w == "soul":
            text = self._cut_utf8(self.agent_read(kind, "soul")["text"], self._limit_of("soul")).strip()
            return f"## MaiWork 的身份\n{text}\n\n" if text else ""
        if w == "agents":
            text = self._cut_utf8(self.agent_read(kind, "agents")["text"], self._limit_of("agents")).strip()
            return f"## 做事规矩\n{text}\n\n" if text else ""
        raise ValueError(f"agent_prompt_block 只认 soul / agents，收到 {w or '(空)'}")

    # ------------------------------------------------------------------
    # 首次启动 + 从 MaiBot 同步
    # ------------------------------------------------------------------

    async def ensure_started(self) -> None:
        """插件启动时调一次：
        - 旧的全局 SOUL.md / AGENTS.md / MEMORY.md / memory/ 就位（兼容老部署）；
        - 每个内建专岗（main/news/idea/goal/task）的 identity/agents/<kind>/SOUL.md、
          AGENTS.md 就位——main 拷旧全局；其余 SOUL 从 MaiBot 同步、AGENTS 用岗位预设
          （预设 + 旧 instructions 非空时附加「## 原职责」一节），迁移只做一次
          （identity/agents/.migrated），之后管理员改过不再被覆盖。
        """
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
                self._set_soul_synced(False)  # 兜底模板不是从 MaiBot 来的，标「未同步」
        else:
            # 兜底：老部署可能只有空文件/半截文件——不自动覆盖，只保证文件存在
            self._chmod(soul, 0o600)
        # 专岗文档迁移：只做一次；标记文件存在即跳过
        mark = self._root / _MIGRATION_MARK
        if not mark.exists():
            try:
                await self._migrate_agent_docs_once()
            except Exception:
                logger.exception("专岗 SOUL/AGENTS 首次迁移出错（下轮启动再试）")
                return  # 不写标记：下轮重启再迁
            try:
                mark.parent.mkdir(parents=True, exist_ok=True)
                self._chmod(mark.parent, 0o700)
                self._write_file(mark, "1")
            except OSError:
                pass

    async def _migrate_agent_docs_once(self) -> None:
        """identity/agents/<内建 kind>/ 就位（只做一次）。可重入安全：单文件已存在就不覆盖。"""
        # 1) MaiBot 人格只拉一次，五个内建专岗共用（main 不需要——它优先用旧全局 SOUL）
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
        persona_text = self._render_soul(got, {}) if got else ""

        root_agents = self._root / "agents"
        root_agents.mkdir(parents=True, exist_ok=True)
        self._chmod(root_agents, 0o700)

        # 旧的全局 SOUL / AGENTS（可能不存在，给 main 拷贝用）
        global_soul = self._read_text_or_empty(self._path_of("soul"))
        global_agents = self._read_text_or_empty(self._path_of("agents"))

        for kind in _BUILTIN_AGENT_KINDS:
            d = root_agents / kind
            d.mkdir(parents=True, exist_ok=True)
            self._chmod(d, 0o700)
            soul_p = d / "SOUL.md"
            agents_p = d / "AGENTS.md"
            # 幂等：文件已存在就不动（管理员改过也保留）
            if not soul_p.exists():
                if kind == "main" and global_soul.strip():
                    self._write_file(soul_p, global_soul)
                elif persona_text:
                    self._write_file(soul_p, persona_text)
                else:
                    self._write_file(soul_p, "")
                # 迁移来的不算「从 MaiBot 同步出来的」（main 那份内容其实是管理员维护过的）
                # 但首次没有主管理员动过，标 synced 让前端「已和 MaiBot 同步」不闪——main
                # 若真来自旧全局就给 False，否则（用了 MaiBot 人格或空兜底）给 True
                # 没读到人格写的空份也不算同步（外部审查 2026-10-02：别冒充「已同步」）
                self._agent_set_soul_synced(
                    kind, not (kind == "main" and global_soul.strip()) and bool(persona_text),
                )
            if not agents_p.exists():
                # main：老部署管理员改过的全局 AGENTS.md 拷过来；但全局还是出厂默认模板
                # （没人维护过）时别拷那份旧的，直接用 main 岗位预设（它含「派给哪个专岗」
                # 和「新建的专岗」提示小节，是给主模型量身写的）。
                if kind == "main" and global_agents.strip() and global_agents.strip() != _AGENTS_DEFAULT.strip():
                    text = global_agents
                else:
                    text = self._agent_preset_text(kind)
                    # 旧 instructions 非空就附在末尾「## 原职责」小节；预设已经涵盖的就跳过重复
                    old_instr = self._old_agent_instructions(kind)
                    if old_instr and old_instr not in text:
                        text = (text.rstrip("\n") + "\n\n## 原职责\n\n" + old_instr.strip() + "\n")
                self._write_file(agents_p, text)

    @staticmethod
    def _read_text_or_empty(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _old_agent_instructions(kind: str) -> str:
        """内建专岗「旧 instructions / 职责」字段的默认文案（agents.py 出厂值）。迁移时
        附到 AGENTS.md 末尾（只对确实装过这条职责的岗位；main / task 的预设已经包含
        「干什么」的话，就不再重复附加）。admin 真改过的 instructions 走 agents.py 的
        kv（agents.profiles），identity 看不到；老的出厂值足够代表「原来的职责」。"""
        return {
            "news": "为群找值得看的资讯：先读画像与关注点，再撒网搜索、逐条打开核对。",
            "idea": "为群出可落地的构想：结合画像与聊天线索做调研，给出依据和下一步。",
            "goal": "为群推进目标：调查进展、核验收依据，绝不自己立目标或改进度。",
            "task": "既有的派活流程：主模型派子 agent、验收、交付——专岗不替代它。",
        }.get(kind, "")

    async def sync_soul_from_maibot(self) -> dict:
        """从 MaiBot 重新生成全局 SOUL；规则见 _sync_soul_file（兜底是带边界三条的模板）。
        改了就把 main 的专岗 SOUL 跟着同步（前端「主模型」页点同步走的就是它）。

        返回单项 {"text","updated_ts","synced_from_maibot","preview_changed"[, "persona_missing"]}。
        """
        self._ensure_dirs()
        soul_path = self._path_of("soul")
        changed, missing = await self._sync_soul_file(
            soul_path, fallback=self._render_soul({}, {}), set_flag=self._set_soul_synced,
        )
        if changed and not missing:
            try:
                main_path = self._agent_path("main", "soul")
                main_path.parent.mkdir(parents=True, exist_ok=True)
                self._chmod(main_path.parent, 0o700)
                self._overwrite_with_bak(main_path, soul_path.read_text(encoding="utf-8"))
                self._agent_set_soul_synced("main", True)
            except (KeyError, OSError):
                pass
        out = self.read("soul")
        out["preview_changed"] = changed
        if missing:
            out["persona_missing"] = True
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
        # global：先归一化（拆空格 / 零宽 / 全角 / 中文数字 / 大小写都还原），再过闸。
        # 日期里的「-」不去，所以「2026-10-02」不会被当成长数字。
        folded = privacy.fold(text)
        # 一、不许写长数字（群号/QQ 号）
        if re.search(rf"\d{{{_GLOBAL_NUMBER_RUN},}}", folded):
            return "全局记忆里不能写具体的群和人（不许带 QQ 号 / 群号），改记到本群记忆"
        # 二、不许点名：全部服务群关注成员 + 「名字数>3」的群成员名单
        names = self._global_forbidden_names()
        for name in names:
            key = privacy.fold(name)
            if len(key) >= 2 and key in folded:
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
