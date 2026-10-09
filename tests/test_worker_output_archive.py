"""子 agent 工具输出归档（docs/27 §7 / §8 P1：6000~50000 字的缺口）。

线上事实（改造前，本文件第一步先把它跑红）：
- `workers.py` 只在 `len(output) >= 50000` 且 `result.ok` 时才落盘（`compaction.spill_big_output`）；
  6001~49999 字的输出被直接硬截成 6000 字 +「…（已截断）」，中间**一个字都没归档**；
- 报错输出（`result.ok=False`）从来不归档；
- 落盘后回给模型的指针是**绝对路径**，而 `LocalEnv.resolve` 拒绝对路径
  （`environments/local.py:297`「路径必须是工作区内的相对路径」）——模型照指针
  `read_file` 必然拿不到。

新契约（本文件验的）：
1. 任何**会被单条 tool 消息上限（6000 字）截断**的 tool 输出（成功、报错都算），
   只要有可用 spill 目录，就把完整正文归档到 `<workspace>/tool_spill/<task_id>/`；
2. 归档文件逐字等于「本该给模型的完整正文」，对话里只留头 + 尾 + **工作区相对路径**指针；
3. 指针直接可用 `read_file(path=相对路径, offset=, limit=)` 逐页读回——offset 从 0 数、
   单位是字符、一页最多 5000 字、`next_offset` 就是下一页的 offset（tools_exec 的分页契约）；
4. 归档失败**保留完整正文**（不丢中段、不假装能读回），另用一条 user 提示说明没归档，
   把「上下文放不下」交给预算闸明确失败；
5. 没有工作区（资讯 / 构想那种长活）维持老行为：照旧头截断，`…（已截断）` 文案不变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.environments.local import LocalEnv
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import Tool, ToolContext, ToolResult, Tools
from CharTyr_MaiWork.maiwork.tools_exec import register_exec_tools
from CharTyr_MaiWork.maiwork.workers import Workers

from fakes import FakeHost

# 这个文件全是 async 测试（pytest-asyncio 是 strict 模式，要显式打标）
pytestmark = pytest.mark.asyncio

# --- 和实现对齐的用户可见契约（硬编码：改格式就得改这里，等于改契约）---

TOOL_MSG_MAX = 6000          # workers._TOOL_MSG_MAX
BODY_START = "----- 正文开始（原样，未删改）-----"
BODY_END = "----- 正文结束 -----"
REL_SPILL_RE = re.compile(r"tool_spill/[^\s\"'）)]*\.txt")


# ----------------------------------------------------------------------
# 假模型（回放一串工具调用）
# ----------------------------------------------------------------------


@dataclass
class _Chat:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    model: str = "fake"
    prompt_tokens: int = 10
    completion_tokens: int = 5
    raw_message: dict = field(default_factory=dict)


class _Replay:
    """假 models：按队列回放，并把每次调用的 messages 快照记下来。"""

    def __init__(self, results):
        self.queue = list(results)
        self.calls: list[tuple] = []

    async def chat(self, role=None, messages=None, **kwargs):
        self.calls.append((role, [dict(m) for m in messages], kwargs))
        if not self.queue:
            return _Chat(text="（队列空了）")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _tool_call(name: str, arguments, call_id: str) -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _blob_text(chars: int) -> str:
    """chars 个字符的正文：每 18 字一个唯一序号块，丢字 / 错位 / 截断都能比对出来。"""
    parts: list[str] = []
    total = 0
    i = 0
    while total < chars:
        part = f"<{i:06d}>" + "ABCDEFGHIJ"
        parts.append(part)
        total += len(part)
        i += 1
    return "".join(parts)[:chars]


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path):
    s, _ = load_settings(
        {"environments": {"local_mode": "direct", "workspace_root": str(tmp_path / "ws-root")}}
    )
    return s


@pytest.fixture
def env(settings):
    return LocalEnv(lambda: settings)


@pytest.fixture
def tools(store, settings, env):
    """真 exec 工具（read_file 等）+ submit_result。"""
    t = Tools(store)
    register_exec_tools(t, env=env, host=FakeHost(), get_settings=lambda: settings)

    async def submit_result(ctx, args):
        return ToolResult(
            ok=True,
            output=str(args.get("summary") or ""),
            data={
                "summary": str(args.get("summary") or ""),
                "data": args.get("data"),
                "evidence": args.get("evidence", []),
            },
        )

    t.register(
        Tool(
            name="submit_result",
            description="交回",
            parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": []},
            roles=frozenset({"worker"}),
            handler=submit_result,
            timeout_s=5.0,
        )
    )
    return t


@pytest.fixture
def ws(env):
    return env.workspace("ws-1")


def _register_tool(tools: Tools, name: str, handler) -> None:
    if name in {n for n, _ in tools.catalog("worker")}:
        tools.unregister(name)
    tools.register(
        Tool(
            name=name,
            description="桩",
            parameters={"type": "object", "properties": {}, "required": []},
            roles=frozenset({"worker"}),
            handler=handler,
            timeout_s=10.0,
        )
    )


def _ok_handler(payload: str):
    async def handler(ctx, args):
        return ToolResult(ok=True, output=payload)

    return handler


async def _run_one_tool(
    tools: Tools,
    handler,
    *,
    workspace=None,
    task_id: str = "T-arch",
    tool_name: str = "blob",
    settings=None,
    history=None,
):
    """跑一轮「调一次 <tool_name> → 调 submit_result 交回」，返回 (report, models)。"""
    _register_tool(tools, tool_name, handler)
    models = _Replay(
        [
            _Chat(tool_calls=[_tool_call(tool_name, {}, "c1")]),
            _Chat(tool_calls=[_tool_call("submit_result", {"summary": "完"}, "c2")]),
        ]
    )
    w = Workers(models, tools, get_settings=(lambda: settings) if settings is not None else None)
    report = await w.run(
        "干活", group_id="g1", tools=[tool_name], task_id=task_id, workspace=workspace, history=history
    )
    return report, models


def _tool_msg_text(models: _Replay, call_index: int = 1) -> str:
    """第 call_index 次模型调用时看到的 tool 消息正文（本轮唯一那条）。"""
    msgs = models.calls[call_index][1]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, f"本轮应该只有一条 tool 结果，实际 {len(tool_msgs)}"
    return str(tool_msgs[0]["content"])


def _spill_files(workspace, task_id: str) -> list[Path]:
    d = Path(workspace) / "tool_spill" / task_id
    return sorted(d.glob("*.txt")) if d.is_dir() else []


def _rel_pointer(content: str) -> str:
    got = REL_SPILL_RE.search(content)
    assert got is not None, f"tool 消息里没有工作区相对归档指针：{content[:200]!r}"
    return got.group(0)


def _page_body(output: str) -> str:
    """从 read_file 的一页返回里取出「原样正文」（不加不减）。"""
    assert BODY_START in output and BODY_END in output, f"不是分页返回：{output[:200]!r}"
    return output.split(BODY_START + "\n", 1)[1].split("\n" + BODY_END, 1)[0]


async def _read_back_pages(tools: Tools, workspace, rel: str, *, limit: int = 5000) -> str:
    """照分页契约（offset 从 0 数、单位字符、跟 next_offset）逐页读回全文。"""
    ctx = ToolContext(group_id="g1", task_id="T-arch", actor="子 agent #1", role="worker", workspace=workspace)
    parts: list[str] = []
    offset = 0
    for _ in range(64):
        r = await tools.call("read_file", {"path": rel, "offset": offset, "limit": limit}, ctx)
        assert r.ok, f"按归档指针读不回来（offset={offset}）：{r.error}"
        parts.append(_page_body(r.output))
        if (r.data or {}).get("source_complete"):
            return "".join(parts)
        nxt = (r.data or {}).get("next_offset")
        assert isinstance(nxt, int) and nxt > offset, f"next_offset 不对：{nxt!r}"
        offset = nxt
    raise AssertionError("翻页超过 64 页还没读完")


# ----------------------------------------------------------------------
# 1. 6000 ~ 50000 的缺口：会被截断就必须有完整归档
# ----------------------------------------------------------------------


class TestTruncatedOutputIsArchived:
    @pytest.mark.parametrize("size", [6001, 20000, 49999, 50001, 120000])
    async def test_archived_with_relative_pointer(self, tools, ws, settings, size):
        payload = _blob_text(size)
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error

        files = _spill_files(ws, "T-arch")
        assert len(files) == 1, f"{size} 字的输出应该有一条完整归档，实际 {len(files)} 个文件"
        assert files[0].read_text(encoding="utf-8") == payload, "归档必须逐字等于完整正文"

        content = _tool_msg_text(models)
        assert len(content) <= TOOL_MSG_MAX, f"归档后的 tool 消息还是超预算：{len(content)} 字"
        assert files[0].name in content
        rel = _rel_pointer(content)
        assert rel == f"tool_spill/T-arch/{files[0].name}"
        assert str(ws) not in content, "不许把绝对路径给模型（env.resolve 会拒）"
        assert str(size) in content, "要说清完整输出多少字"
        assert "read_file" in content and "offset" in content and "limit" in content
        # 中段确实没塞进对话（否则归档没意义）
        assert payload[4000:4200] not in content

    async def test_exactly_6000_not_archived(self, tools, ws, settings):
        payload = _blob_text(TOOL_MSG_MAX)
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error
        assert _spill_files(ws, "T-arch") == [], "正好放得下的输出不该归档"
        assert _tool_msg_text(models) == payload

    async def test_pointer_reads_back_page_by_page(self, tools, ws, settings):
        payload = _blob_text(20000)
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error
        rel = _rel_pointer(_tool_msg_text(models))
        assert await _read_back_pages(tools, ws, rel) == payload

    async def test_absolute_pointer_would_not_work(self, tools, ws, settings):
        """反证：绝对路径过不了工作区闸——所以指针必须相对。"""
        payload = _blob_text(20000)
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error
        files = _spill_files(ws, "T-arch")
        ctx = ToolContext(group_id="g1", task_id="T-arch", actor="子 agent #1", role="worker", workspace=ws)
        bad = await tools.call("read_file", {"path": str(files[0])}, ctx)
        assert not bad.ok and "相对路径" in bad.error
        outside = await tools.call("read_file", {"path": "../escape.txt"}, ctx)
        assert not outside.ok

    async def test_error_output_is_archived_too(self, tools, ws, settings):
        payload = _blob_text(20000)

        async def handler(ctx, args):
            return ToolResult(ok=False, output="", error=payload)

        report, models = await _run_one_tool(tools, handler, workspace=ws, settings=settings)
        assert report.ok, report.error
        files = _spill_files(ws, "T-arch")
        assert len(files) == 1, "报错的长输出也要归档（改造前从不归档）"
        assert files[0].read_text(encoding="utf-8") == f"出错了：{payload}"
        content = _tool_msg_text(models)
        assert len(content) <= TOOL_MSG_MAX
        assert _rel_pointer(content)

    async def test_archived_files_are_task_scoped(self, tools, ws, settings):
        a = _blob_text(20000)
        report, models = await _run_one_tool(
            tools, _ok_handler(a), workspace=ws, settings=settings, task_id="T-a"
        )
        assert report.ok, report.error
        rel_a = _rel_pointer(_tool_msg_text(models))
        assert rel_a.startswith("tool_spill/T-a/")
        b = _blob_text(9000)
        report, models = await _run_one_tool(
            tools, _ok_handler(b), workspace=ws, settings=settings, task_id="T-b"
        )
        assert report.ok, report.error
        rel_b = _rel_pointer(_tool_msg_text(models))
        assert rel_b.startswith("tool_spill/T-b/")
        assert (ws / rel_a).read_text(encoding="utf-8") == a
        assert (ws / rel_b).read_text(encoding="utf-8") == b
        # 谁也别想从目录结构上串任务
        assert "T-b" not in rel_a and "T-a" not in rel_b

    async def test_file_tool_page_keeps_reread_advice_and_archive(self, tools, ws, settings):
        """分页文件工具页超预算（线上 T-10）：既要「重读本页同一段」的提醒，也要有归档。"""
        payload = _blob_text(20000)
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings, tool_name="read_file"
        )
        assert report.ok, report.error
        content = _tool_msg_text(models)
        assert len(content) <= TOOL_MSG_MAX
        assert "别往后翻" in content and "offset" in content, "T-10 的「重读本页同一段」文案不能丢"
        files = _spill_files(ws, "T-arch")
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == payload


# ----------------------------------------------------------------------
# 2. 归档失败：保留完整正文 + 明确告知没归档，不假装能读回
# ----------------------------------------------------------------------


class TestArchiveFailure:
    async def test_failure_keeps_full_body_and_says_so(self, tools, ws, settings):
        payload = _blob_text(20000)
        # 把 spill 根目录占成文件：建目录 / 写文件都失败
        (ws / "tool_spill").write_text("占位：让归档目录建不出来", encoding="utf-8")
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error

        msgs = models.calls[1][1]
        content = _tool_msg_text(models)
        assert content == payload, "归档失败必须原样保留完整正文（不丢中段、不硬截）"
        assert ".txt" not in content
        assert "省略" not in content
        notes = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
        assert any("归档" in n and "没" in n for n in notes), f"要明确告诉模型没归档：{notes!r}"

    async def test_no_workspace_keeps_full_body(self, tools, settings):
        """没有工作区（资讯 / 构想那种长活）：归档不了 → **完整正文照给**，一个字不删。

        口径（2026-10，docs/27 §8 P1）：worker 不做任何 6000 字硬截断；装不下由预算闸明确
        报错。以前那种「截成 6000 字 + …（已截断）」是悄悄丢证据，已经删掉了。
        """
        payload = _blob_text(20000)
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=None, task_id="", settings=settings
        )
        assert report.ok, report.error
        assert _tool_msg_text(models) == payload
        assert _spill_files(settings.environments.workspace_root, "T-arch") == []


# ----------------------------------------------------------------------
# 3. 归档不清理（活着的指针不许被删）+ 符号链接不许把归档写到工作区外
# ----------------------------------------------------------------------


class TestArchiveDurability:
    async def test_no_pruning_keeps_the_first_pointer_alive(self, ws):
        """归档目录不清理：写过很多个之后，最早那个指针照样能读回（max_files=0 的契约）。"""
        from CharTyr_MaiWork.maiwork import workers as _workers

        zone = _workers._spill_zone(ws, "T-many")
        assert zone is not None
        first_rel = ""
        for i in range(245):                      # 超过 240（旧代码会在这一步删掉最早的）
            payload = f"#{i:04d}" + _blob_text(7000)
            _text, rel = _workers._archive_tool_output(payload, zone)
            assert rel, f"第 {i} 次归档失败"
            if not first_rel:
                first_rel = rel
        assert (ws / first_rel).is_file(), "最早的指针被清掉了（活着的任务就读不回来了）"

    async def test_symlinked_spill_dir_never_writes_outside(self, tools, ws, settings, tmp_path):
        """`tool_spill` 被换成指向工作区外的符号链接：不写出去、不声称能按路径读回。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (ws / "tool_spill").symlink_to(outside, target_is_directory=True)
        payload = _blob_text(20000)
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error
        assert list(outside.glob("*.txt")) == [], "归档被符号链接带出工作区了"
        content = _tool_msg_text(models)
        assert content == payload, "归档不了就原样给完整正文"
        assert ".txt" not in content and "tool_spill/" not in content, "不许给一个读不回来的指针"
        notes = [str(m.get("content") or "") for m in models.calls[1][1] if m.get("role") == "user"]
        assert any("归档" in n for n in notes), f"要说明没归档：{notes!r}"


# ----------------------------------------------------------------------
# 4. 归档目录按任务隔离（tools_exec 的闸）
# ----------------------------------------------------------------------


class TestGiantArchive:
    """远超单次读取字节上限的归档：逐页（按字符 offset）必须能一字不差读回。

    500k 汉字 = 1,500,000 字节，是 LocalEnv 老口径（整份读、20 万字节上限）的 7.5 倍：
    老实现下 offset 一深就「本次可读前缀」到底，尾页永远读不到（那时这份测试是红的）。
    """

    async def test_giant_over_4mb_reconstructs_page_by_page(self, tools, ws, settings):
        unit = "第{:08d}行｜".format   # 每行带唯一序号：丢字 / 错位 / 重复都能被抓出来
        parts: list[str] = []
        total = 0
        i = 0
        while total < 1_700_000:
            line = unit(i) + "中文内容" * 7 + "\n"
            parts.append(line)
            total += len(line)
            i += 1
        payload = "".join(parts)[:1_700_000]
        assert len(payload.encode("utf-8")) > 4 * 1024 * 1024, "要超过 4MB 才有意义"

        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error
        files = _spill_files(ws, "T-arch")
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == payload
        rel = _rel_pointer(_tool_msg_text(models))

        ctx = ToolContext(
            group_id="g1", task_id="T-arch", actor="子 agent #1", role="worker", workspace=ws
        )
        # 抽三页核对：开头、深处（4MB 之后）、以及**尾页**（旧实现一定读不到的那段）
        for offset in (0, 1_200_000, 1_690_000):
            r = await tools.call("read_file", {"path": rel, "offset": offset, "limit": 5000}, ctx)
            assert r.ok, f"offset={offset} 读不了：{r.error}"
            assert _page_body(r.output) == payload[offset:offset + 5000], f"offset={offset} 读回来对不上"
        # 主模型（inspect_file）走同一套读取：深度 offset 也读得到
        main = ToolContext(
            group_id="g1", task_id="T-arch", actor="主模型", role="main", workspace=ws
        )
        r = await tools.call("inspect_file", {"path": rel, "offset": 1_690_000, "limit": 5000}, main)
        assert r.ok, r.error
        assert _page_body(r.output) == payload[1_690_000:1_695_000]

    async def test_deep_offset_reaches_end(self, tools, ws, settings):
        """尾页：按 next_offset 一路读到末尾，最后必须说「读完了」而不是「读不到」。"""
        payload = _blob_text(200_000)          # 20 万汉字 ≈ 60 万字节
        report, models = await _run_one_tool(
            tools, _ok_handler(payload), workspace=ws, settings=settings
        )
        assert report.ok, report.error
        rel = _rel_pointer(_tool_msg_text(models))
        ctx = ToolContext(
            group_id="g1", task_id="T-arch", actor="子 agent #1", role="worker", workspace=ws
        )
        r = await tools.call(
            "read_file", {"path": rel, "offset": len(payload) - 100, "limit": 5000}, ctx
        )
        assert r.ok, r.error
        body = _page_body(r.output)
        assert body == payload[len(payload) - 100:]
        assert (r.data or {}).get("source_complete") is True
        assert (r.data or {}).get("total_chars") == len(payload)

class TestArchiveScope:
    async def _two_tasks(self, tools, ws, settings):
        report, _ = await _run_one_tool(
            tools, _ok_handler(_blob_text(20000)), workspace=ws, settings=settings, task_id="T-a"
        )
        assert report.ok, report.error
        report, _ = await _run_one_tool(
            tools, _ok_handler(_blob_text(9000)), workspace=ws, settings=settings, task_id="T-b"
        )
        assert report.ok, report.error
        mine = _spill_files(ws, "T-a")[0]
        other = _spill_files(ws, "T-b")[0]
        return f"tool_spill/T-a/{mine.name}", f"tool_spill/T-b/{other.name}"

    async def test_other_task_archive_is_refused(self, tools, ws, settings):
        mine_rel, other_rel = await self._two_tasks(tools, ws, settings)
        ctx = ToolContext(group_id="g1", task_id="T-a", actor="子 agent #1", role="worker", workspace=ws)
        ok = await tools.call("read_file", {"path": mine_rel, "offset": 0, "limit": 1000}, ctx)
        assert ok.ok, ok.error
        bad = await tools.call("read_file", {"path": other_rel, "offset": 0, "limit": 1000}, ctx)
        assert not bad.ok and "别的任务" in bad.error, bad.error
        # 主模型（inspect_file）同样不许看别的任务的归档
        main = ToolContext(group_id="g1", task_id="T-a", actor="主模型", role="main", workspace=ws)
        bad_main = await tools.call("inspect_file", {"path": other_rel}, main)
        assert not bad_main.ok and "别的任务" in bad_main.error, bad_main.error

    async def test_without_task_no_archive_is_readable(self, tools, ws, settings):
        _mine_rel, other_rel = await self._two_tasks(tools, ws, settings)
        ctx = ToolContext(group_id="g1", task_id="", actor="子 agent #1", role="worker", workspace=ws)
        bad = await tools.call("read_file", {"path": other_rel}, ctx)
        assert not bad.ok and "没有任务" in bad.error, bad.error

    async def test_listing_hides_other_task_archives(self, tools, ws, settings):
        mine_rel, _other_rel = await self._two_tasks(tools, ws, settings)
        ctx = ToolContext(group_id="g1", task_id="T-a", actor="子 agent #1", role="worker", workspace=ws)
        listing = await tools.call("list_files", {"path": "tool_spill", "depth": 2}, ctx)
        assert listing.ok, listing.error
        shown = str(listing.output)
        assert mine_rel.split("/")[-1] in shown
        assert "T-b" not in shown, f"列目录不该露出别的任务的归档：{shown!r}"
        denied = await tools.call("list_files", {"path": "tool_spill/T-b", "depth": 2}, ctx)
        assert not denied.ok and "别的任务" in denied.error, denied.error

    async def test_write_into_other_task_archive_is_refused(self, tools, ws, settings):
        _mine_rel, other_rel = await self._two_tasks(tools, ws, settings)
        ctx = ToolContext(group_id="g1", task_id="T-a", actor="子 agent #1", role="worker", workspace=ws)
        bad = await tools.call(
            "write_file", {"path": "tool_spill/T-b/多出来的.txt", "content": "塞进去"}, ctx
        )
        assert not bad.ok and "别的任务" in bad.error, bad.error
        assert not (ws / "tool_spill" / "T-b" / "多出来的.txt").exists()
        # 自己的目录照旧能写（不误伤）
        ok = await tools.call(
            "write_file", {"path": "tool_spill/T-a/自己的.txt", "content": "备注"}, ctx
        )
        assert ok.ok, ok.error
