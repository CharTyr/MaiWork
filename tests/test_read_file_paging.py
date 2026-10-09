"""read_file / inspect_file 分页读取回归测试（线上 T-10）。

线上教训（当时）：`read_file` 只支持 `path`，同一个文件反复读也只能拿开头；子 agent 那侧
单条 tool 消息还硬截 6000 字，于是 5 万字的资料实际只到前 6000 字，下游错过
`research.md` 的后半，被迫把资料拆成许多 ≤3KB 的小文件分片。

这个文件按「先证明拿不全，再证明能逐页重建」写：

1. 老实现：一次调用就把整份文件（最多 5 万字）塞回来，没有页码也没有下一页游标，
   子 agent 只看到前 6000 字——重建用的大文件在这里必然红。
2. 新实现：`offset` / `limit`（单位都是**字符**，offset 0 = 文件第一个字；单页
   正文加元信息 ≤ 6000 字，正好落在单条 tool 消息预算里）。按返回的下一页游标一页页读，
   20KB / 50KB 中文文件能一字不差地拼回来。
3. 兼容：不给 offset / limit 的旧调用、短文件、越界路径、成品目录隔离，行为都不变。

2026-10 更新（docs/27 §8 P1）：worker 那层**不再做任何 6000 字硬截断**——归档不了（没有
工作区 / 写不进去）就原样保留完整正文，装不下交给预算闸明确报错（本节末尾
`TestWorkerTruncationGuard` 记的就是这条）。所以这里第 2 条「单页 ≤ 6000 字」是**分页工具
自己的预算**，不再是「超了就丢」的兜底。
"""

from __future__ import annotations

import json
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

# --- 和实现对齐的用户可见契约（故意硬编码：改格式就得改这里，等于改契约）---

BODY_START = "----- 正文开始"
BODY_END = "----- 正文结束"
TOOL_MSG_BUDGET = 6000  # workers._TOOL_MSG_MAX / coordinator 的工具消息上限


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


def _ctx(ws: Path, **over) -> ToolContext:
    base = dict(group_id="g1", task_id="T-1", actor="子 agent #1", role="worker", workspace=ws)
    base.update(over)
    return ToolContext(**base)


def _main_ctx(ws: Path) -> ToolContext:
    return _ctx(ws, role="main", actor="主模型")


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def _cn_text(chars: int) -> str:
    """chars 个字符的中文内容，每行都带唯一编号（丢字/错位/重复都能被比对抓出来）。"""
    parts: list[str] = []
    total = 0
    i = 0
    while total < chars:
        piece = f"第{i:05d}行：分页回归测试内容，一个字都不能丢。\n"
        parts.append(piece)
        total += len(piece)
        i += 1
    return "".join(parts)[:chars]


def _body(out: str) -> str:
    """从工具返回里取出「原样正文」（元信息在正文标记之外，不进正文）。"""
    assert BODY_START in out, f"返回里找不到正文起始标记，前 200 字：{out[:200]!r}"
    assert BODY_END in out, f"返回里找不到正文结束标记，后 200 字：{out[-200:]!r}"
    rest = out.split(BODY_START, 1)[1]
    rest = rest.split("\n", 1)[1]  # 去掉起始标记那一行的尾巴
    return rest.rsplit("\n" + BODY_END, 1)[0]


async def _read_all(tools: Tools, ctx: ToolContext, path: str, *, limit: int | None = None, max_pages: int = 200) -> str:
    """按工具自己给的下一页游标一页页读，拼回完整正文；顺带断言每页都在预算内。"""
    offset = 0
    parts: list[str] = []
    for _ in range(max_pages):
        args: dict = {"path": path, "offset": offset}
        if limit is not None:
            args["limit"] = limit
        r = await tools.call("read_file", args, ctx)
        assert r.ok, f"第 {offset} 字起读失败：{r.error}"
        assert len(r.output) <= TOOL_MSG_BUDGET, (
            f"单页返回 {len(r.output)} 字，超过单条 tool 消息 {TOOL_MSG_BUDGET} 字预算"
            "（worker 会再截一刀，正文就丢了）"
        )
        parts.append(_body(r.output))
        nxt = (r.data or {}).get("next_offset")
        if nxt is None:
            return "".join(parts)
        assert isinstance(nxt, int) and nxt > offset, f"下一页游标必须往前走：{offset} -> {nxt}"
        offset = nxt
    raise AssertionError(f"{max_pages} 页还没读完，游标可能没往前走")


# ----------------------------------------------------------------------
# 1. 分页重建（T-10 的核心回归）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestPagingRebuild:
    async def test_50k_chinese_file_rebuilds_page_by_page(self, tools, ws):
        text = _cn_text(50_000)
        (ws / "research.md").write_text(text, encoding="utf-8")
        rebuilt = await _read_all(tools, _ctx(ws), "research.md")
        assert len(text) == 50_000
        assert rebuilt == text  # 一字不差

    async def test_20k_chinese_file_rebuilds_page_by_page(self, tools, ws):
        text = _cn_text(20_000)
        (ws / "notes.md").write_text(text, encoding="utf-8")
        rebuilt = await _read_all(tools, _ctx(ws), "notes.md")
        assert rebuilt == text

    async def test_small_explicit_limit_rebuilds_too(self, tools, ws):
        """自己指定小 limit 也要能拼全（游标按真实页长算，不是按请求的 limit 算）。"""
        text = _cn_text(5_555)
        (ws / "a.md").write_text(text, encoding="utf-8")
        rebuilt = await _read_all(tools, _ctx(ws), "a.md", limit=700)
        assert rebuilt == text

    async def test_page_with_max_limit_and_long_path_stays_in_budget(self, tools, ws):
        text = _cn_text(15_000)
        long_rel = f"{'d' * 80}/{'e' * 60}/research.md"
        (ws / long_rel).parent.mkdir(parents=True)
        (ws / long_rel).write_text(text, encoding="utf-8")
        r = await tools.call("read_file", {"path": long_rel, "limit": 5000}, _ctx(ws))
        assert r.ok
        assert len(r.output) <= TOOL_MSG_BUDGET
        assert len(_body(r.output)) == 5000
        # 元信息齐全：总长度、本页范围、下一页游标
        assert "共 15000 字" in r.output
        assert "offset=5000" in r.output
        assert (r.data or {}).get("next_offset") == 5000

    async def test_page_budget_matches_workers_tool_message_cap(self, tools, ws):
        """跨模块不变量：页预算必须 ≤ workers 的单条 tool 消息上限，否则页会被再截一刀。

        workers 那边把上限改小了，这条就该红——改页大小或改上限，两边一起动。
        """
        from CharTyr_MaiWork.maiwork import tools_exec, workers

        assert tools_exec._TOOL_MSG_BUDGET <= workers._TOOL_MSG_MAX
        text = _cn_text(12_000)
        (ws / "cap.md").write_text(text, encoding="utf-8")
        r = await tools.call("read_file", {"path": "cap.md"}, _ctx(ws))
        assert r.ok and len(r.output) <= workers._TOOL_MSG_MAX

    async def test_worker_tool_message_keeps_the_whole_page(self, tools, ws):
        """端到端：真跑 Workers，子 agent 看到的那条 tool 消息不得被再截一刀。"""
        text = _cn_text(30_000)
        (ws / "research.md").write_text(text, encoding="utf-8")
        models = _Replay(
            [
                _Chat(tool_calls=[_tool_call("read_file", {"path": "research.md"}, "c1")]),
                _Chat(tool_calls=[_tool_call("submit_result", {"summary": "读到了后半"}, "c2")]),
            ]
        )
        w = Workers(models, tools)
        report = await w.run(
            "读这份材料", group_id="1", tools=["read_file"], task_id="T-1", workspace=ws
        )
        assert report.ok, report.error
        tool_msg = [m for m in models.calls[1][1] if m.get("role") == "tool"][0]
        content = str(tool_msg["content"])
        assert len(content) <= TOOL_MSG_BUDGET
        assert "已截断" not in content
        assert "下一页" in content
        assert _body(content) == text[:5000]


# ----------------------------------------------------------------------
# 2. 页参数严格校验
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestPageParamsStrict:
    @pytest.fixture(autouse=True)
    def _file(self, ws):
        (ws / "f.md").write_text(_cn_text(12_000), encoding="utf-8")

    @pytest.mark.parametrize("bad", ["100", 1.5, True, [100], {"n": 1}, "", "  ", "\t"])
    async def test_offset_must_be_int(self, tools, ws, bad):
        """契约：只有「没给 offset」或「offset 是 null」才走默认；别的（含空/空白串）一律拒。"""
        r = await tools.call("read_file", {"path": "f.md", "offset": bad}, _ctx(ws))
        assert not r.ok and "offset" in r.error

    async def test_offset_negative_rejected(self, tools, ws):
        r = await tools.call("read_file", {"path": "f.md", "offset": -1}, _ctx(ws))
        assert not r.ok and "offset" in r.error

    @pytest.mark.parametrize("bad", ["500", 1.5, True, [500], "", "  "])
    async def test_limit_must_be_int(self, tools, ws, bad):
        """契约：只有「没给 limit」或「limit 是 null」才走默认；别的（含空/空白串）一律拒。"""
        r = await tools.call("read_file", {"path": "f.md", "limit": bad}, _ctx(ws))
        assert not r.ok and "limit" in r.error

    async def test_none_and_missing_mean_default(self, tools, ws):
        """唯一放行的「没给值」形态：缺省，或显式 JSON null（经 tools.call 就是 None）。"""
        missing = await tools.call("read_file", {"path": "f.md"}, _ctx(ws))
        explicit_null = await tools.call(
            "read_file", {"path": "f.md", "offset": None, "limit": None}, _ctx(ws)
        )
        for r in (missing, explicit_null):
            assert r.ok, r.error
            assert (r.data or {}).get("offset") == 0
            assert (r.data or {}).get("chars") == 5000  # 默认每页 5000 字

    @pytest.mark.parametrize("bad", [0, -5, 5001, 999999])
    async def test_limit_out_of_range_rejected(self, tools, ws, bad):
        r = await tools.call("read_file", {"path": "f.md", "limit": bad}, _ctx(ws))
        assert not r.ok and "limit" in r.error
        assert "5000" in r.error  # 报出可用范围，模型一次就能改对

    async def test_offset_beyond_total_rejected(self, tools, ws):
        r = await tools.call("read_file", {"path": "f.md", "offset": 999_999}, _ctx(ws))
        assert not r.ok and "offset" in r.error and "12000" in r.error

    async def test_offset_exactly_at_total_is_a_clear_notice(self, tools, ws):
        r = await tools.call("read_file", {"path": "f.md", "offset": 12_000}, _ctx(ws))
        assert r.ok and "末尾" in r.output
        assert (r.data or {}).get("next_offset") is None

    async def test_bad_params_do_not_touch_out_of_scope_path(self, tools, ws):
        """越界/隔离优先级不变：坏 offset 也不能变成「读别的任务文件」的侧信道。"""
        ctx = _ctx(ws, artifact_scope=("artifacts/T-1",))
        r = await tools.call("read_file", {"path": "artifacts/T-2/x.md", "offset": -1}, ctx)
        assert not r.ok and "别的任务" in r.error


# ----------------------------------------------------------------------
# 3. 兼容与单位
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestCompatAndUnits:
    async def test_small_file_verbatim_without_params(self, tools, ws):
        """旧调用（只给 path）：短文件原样返回，不加任何元信息。"""
        (ws / "a.txt").write_text("第一版内容\n第二行", encoding="utf-8")
        r = await tools.call("read_file", {"path": "a.txt"}, _ctx(ws))
        assert r.ok and r.output == "第一版内容\n第二行"
        assert BODY_START not in r.output

    async def test_small_file_verbatim_when_limit_covers_all(self, tools, ws):
        (ws / "a.txt").write_text("abc", encoding="utf-8")
        r = await tools.call("read_file", {"path": "a.txt", "offset": 0, "limit": 10}, _ctx(ws))
        assert r.ok and r.output == "abc"

    async def test_empty_file(self, tools, ws):
        (ws / "empty.txt").write_text("", encoding="utf-8")
        r = await tools.call("read_file", {"path": "empty.txt"}, _ctx(ws))
        assert r.ok and r.output == ""

    async def test_offset_unit_is_characters_not_bytes(self, tools, ws):
        # 每个汉字 3 字节：offset 若按字节算，这里会从半个字中间开始
        (ws / "cn.txt").write_text("あいうえお漢字テスト", encoding="utf-8")
        r = await tools.call("read_file", {"path": "cn.txt", "offset": 2, "limit": 3}, _ctx(ws))
        assert r.ok and _body(r.output) == "うえお"
        assert (r.data or {}).get("offset") == 2

    async def test_small_limit_pages_even_a_small_file(self, tools, ws):
        (ws / "s.txt").write_text("0123456789", encoding="utf-8")
        r = await tools.call("read_file", {"path": "s.txt", "limit": 4}, _ctx(ws))
        assert r.ok and _body(r.output) == "0123"
        assert (r.data or {}).get("next_offset") == 4
        assert "还有 6 字" in r.output

    async def test_last_page_says_eof_and_no_cursor(self, tools, ws):
        (ws / "s.txt").write_text("0123456789", encoding="utf-8")
        r = await tools.call("read_file", {"path": "s.txt", "offset": 8, "limit": 4}, _ctx(ws))
        assert r.ok and _body(r.output) == "89"
        assert "末尾" in r.output
        assert (r.data or {}).get("next_offset") is None
        assert (r.data or {}).get("eof") is True

    async def test_metadata_states_unit_and_origin(self, tools, ws):
        (ws / "s.txt").write_text(_cn_text(6_000), encoding="utf-8")
        r = await tools.call("read_file", {"path": "s.txt"}, _ctx(ws))
        assert r.ok
        assert "字" in r.output and "字符" in r.output  # 单位写清楚
        assert "offset=0" in r.output  # 起点从 0 数
        assert "共 6000 字" in r.output  # 文件总长度

    async def test_page_body_is_exact_no_strip(self, tools, ws):
        text = "  前导空格\n\n\t制表符与空行\n结尾无换行"
        (ws / "w.txt").write_text(text, encoding="utf-8")
        r = await tools.call("read_file", {"path": "w.txt", "limit": 5}, _ctx(ws))
        assert r.ok and _body(r.output) == text[:5]
        r2 = await tools.call("read_file", {"path": "w.txt", "offset": 5, "limit": 5}, _ctx(ws))
        assert r2.ok and _body(r2.output) == text[5:10]

    async def test_long_file_first_call_never_dumps_everything(self, tools, ws):
        """老实现一次回最多 5 万字（worker 再截到 6000）；新实现单页必须收得住。"""
        text = _cn_text(50_000)
        (ws / "big.md").write_text(text, encoding="utf-8")
        r = await tools.call("read_file", {"path": "big.md"}, _ctx(ws))
        assert r.ok
        assert len(r.output) <= TOOL_MSG_BUDGET
        assert _body(r.output) == text[:5000]
        assert (r.data or {}).get("next_offset") == 5000


# ----------------------------------------------------------------------
# 4. 隔离与越界不变
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestScopeUnchanged:
    async def test_scope_violation_still_rejected_with_paging(self, tools, ws):
        (ws / "artifacts/T-2").mkdir(parents=True)
        (ws / "artifacts/T-2/other.md").write_text(_cn_text(9_000), encoding="utf-8")
        ctx = _ctx(ws, artifact_scope=("artifacts/T-1",))
        r = await tools.call("read_file", {"path": "artifacts/T-2/other.md", "offset": 0}, ctx)
        assert not r.ok and "别的任务" in r.error

    async def test_own_scope_pages_normally(self, tools, ws):
        (ws / "artifacts/T-1").mkdir(parents=True)
        text = _cn_text(9_000)
        (ws / "artifacts/T-1/mine.md").write_text(text, encoding="utf-8")
        ctx = _ctx(ws, artifact_scope=("artifacts/T-1",))
        assert await _read_all(tools, ctx, "artifacts/T-1/mine.md") == text

    async def test_escape_still_rejected_with_offset(self, tools, ws, tmp_path):
        outside = ws.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("shh", encoding="utf-8")
        r = await tools.call("read_file", {"path": "../outside/secret.txt", "offset": 0}, _ctx(ws))
        assert not r.ok and "不允许" in r.error

    async def test_absolute_path_still_rejected_with_offset(self, tools, ws):
        r = await tools.call("read_file", {"path": "/etc/passwd", "offset": 0}, _ctx(ws))
        assert not r.ok and "不允许" in r.error


# ----------------------------------------------------------------------
# 5. 主模型 inspect_file 同样分页 + 工具说明准确
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestInspectFilePaging:
    async def test_inspect_file_pages_like_read_file(self, tools, ws):
        text = _cn_text(12_000)
        (ws / "out.md").write_text(text, encoding="utf-8")
        ctx = _main_ctx(ws)
        r = await tools.call("inspect_file", {"path": "out.md", "offset": 6_000, "limit": 5000}, ctx)
        assert r.ok
        assert _body(r.output) == text[6_000:11_000]
        r2 = await tools.call("inspect_file", {"path": "out.md", "offset": 11_000}, ctx)
        assert r2.ok and _body(r2.output) == text[11_000:]
        assert "末尾" in r2.output

    async def test_inspect_file_small_file_verbatim(self, tools, ws):
        (ws / "out.md").write_text("# 交付报告", encoding="utf-8")
        r = await tools.call("inspect_file", {"path": "out.md"}, _main_ctx(ws))
        assert r.ok and r.output == "# 交付报告"

    async def test_schema_declares_offset_and_limit(self, tools):
        for name, role in (("read_file", "worker"), ("inspect_file", "main")):
            spec = next(
                s["function"] for s in tools.specs(role) if s["function"]["name"] == name
            )
            props = spec["parameters"]["properties"]
            assert props["offset"]["type"] == "integer", name
            assert props["limit"]["type"] == "integer", name
            assert spec["parameters"]["required"] == ["path"], name
            desc = spec["description"]
            assert "字符" in desc and "offset" in desc, f"{name} 说明没说清单位/分页"
            assert "下一页" in desc or "一页页" in desc, f"{name} 说明没教怎么往下读"
            assert "字" in props["offset"]["description"], name
            assert "5000" in props["limit"]["description"], name


# ----------------------------------------------------------------------
# 6. 超过单次读取字节上限的文件（说清楚，不假装读全）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestByteCap:
    async def test_oversized_file_reports_byte_cap_honestly(self, tools, ws):
        # 8 万汉字 ≈ 24 万字节 > 单次 20 万字节上限
        (ws / "huge.md").write_text("汉" * 80_000, encoding="utf-8")
        ctx = _ctx(ws)
        r = await tools.call("read_file", {"path": "huge.md"}, ctx)
        assert r.ok
        assert len(r.output) <= TOOL_MSG_BUDGET
        assert (r.data or {}).get("truncated_by_byte_limit") is True
        assert "20 万字节" in r.output
        # 读得到的部分照样能分页重建
        first = _body(r.output)
        assert first == "汉" * len(first)
        assert (r.data or {}).get("total_chars", 0) < 80_000

    async def test_byte_capped_last_page_says_no_more(self, tools, ws):
        (ws / "huge.md").write_text("汉" * 80_000, encoding="utf-8")
        ctx = _ctx(ws)
        r0 = await tools.call("read_file", {"path": "huge.md"}, ctx)
        total = (r0.data or {})["total_chars"]
        r = await tools.call("read_file", {"path": "huge.md", "offset": total - 10, "limit": 100}, ctx)
        assert r.ok and (r.data or {}).get("next_offset") is None
        assert "读不到" in r.output or "上限" in r.output
        # 最后一页也不能说成「文件读完」（total 只是本次可读前缀）
        assert "已到文件末尾" not in r.output
        assert "可读" in r.output or "前缀" in r.output
        assert (r.data or {}).get("source_complete") is False

    async def test_prefix_end_with_byte_cap_does_not_claim_file_complete(self, tools, ws):
        """offset 正好等于「本次可读前缀长度」且顶到字节上限：不能说源文件读完。"""
        (ws / "huge.md").write_text("汉" * 80_000, encoding="utf-8")
        ctx = _ctx(ws)
        r0 = await tools.call("read_file", {"path": "huge.md"}, ctx)
        total = (r0.data or {})["total_chars"]
        assert (r0.data or {}).get("truncated_by_byte_limit") is True
        r = await tools.call("read_file", {"path": "huge.md", "offset": total}, ctx)
        assert r.ok
        assert "没有更多内容" not in r.output
        assert "已到文件末尾" not in r.output
        assert "上限" in r.output and "20 万字节" in r.output
        assert "读不到" in r.output and "可读前缀" in r.output
        d = r.data or {}
        assert d.get("next_offset") is None
        assert d.get("eof") is True  # eof 只表示「本次可读前缀读完了」
        assert d.get("truncated_by_byte_limit") is True
        assert d.get("source_complete") is False  # 但整份源文件没读完
        assert d.get("total_chars_is_lower_bound") is True

    async def test_offset_beyond_readable_prefix_also_says_prefix(self, tools, ws):
        """顶到字节上限时，连报错也不能把「本次可读前缀」说成文件总长度。"""
        (ws / "huge.md").write_text("汉" * 80_000, encoding="utf-8")
        ctx = _ctx(ws)
        r0 = await tools.call("read_file", {"path": "huge.md"}, ctx)
        total = (r0.data or {})["total_chars"]
        r = await tools.call("read_file", {"path": "huge.md", "offset": total + 5}, ctx)
        assert not r.ok
        assert "本次可读" in r.error and "字节上限" in r.error and "20 万字节" in r.error
        assert f"共 {total} 字" not in r.error  # 不能说成文件一共就这么多字

    async def test_normal_file_at_end_still_says_file_complete(self, tools, ws):
        """没顶到字节上限时，末尾提示照旧（别为了修上面那条把好消息也说糊）。"""
        text = _cn_text(3_000)
        (ws / "ok.md").write_text(text, encoding="utf-8")
        r = await tools.call("read_file", {"path": "ok.md", "offset": len(text)}, _ctx(ws))
        assert r.ok and "已经在文件末尾" in r.output and "没有更多内容" in r.output
        d = r.data or {}
        assert d.get("eof") is True and d.get("source_complete") is True
        assert d.get("truncated_by_byte_limit") is False


# ----------------------------------------------------------------------
# 7. workers 那层：文件工具页万一超预算，不许「悄悄截断当读完」
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestWorkerTruncationGuard:
    """tools_exec 保证每页 ≤6000 字；真被改坏了（页超预算）也不许再截正文。

    2026-10 口径（docs/27 §8 P1）：worker 那层**不再做任何 6000 字硬截断**——能归档就归档
    （指针在正文里），归档不了 / 没有工作区就**原样保留完整正文**，装不下交给预算闸明确报错。
    「悄悄丢掉中段、还看着像读完了」才是要被灭掉的那种行为。
    """

    @pytest.fixture
    def guard_tools(self, tmp_path):
        s = Store(tmp_path / "guard.db")
        s.migrate()
        t = Tools(s)

        async def big_file(ctx, args):
            return ToolResult(ok=True, output="长" * 7000, data={"chars": 7000})

        async def big_fetch(ctx, args):
            return ToolResult(ok=True, output="网" * 20000)

        async def submit_result(ctx, args):
            return ToolResult(
                ok=True, output="行",
                data={"summary": str(args.get("summary") or ""), "data": None, "evidence": []},
            )

        for name, handler in (("read_file", big_file), ("fetch_page", big_fetch), ("submit_result", submit_result)):
            t.register(
                Tool(
                    name=name,
                    description="桩",
                    parameters={"type": "object", "properties": {}, "required": []},
                    roles=frozenset({"worker"}),
                    handler=handler,
                    timeout_s=5.0,
                )
            )
        yield t
        s.close()

    async def _tool_msg(self, tools, name: str) -> str:
        models = _Replay(
            [
                _Chat(tool_calls=[_tool_call(name, {}, "c1")]),
                _Chat(tool_calls=[_tool_call("submit_result", {"summary": "行"}, "c2")]),
            ]
        )
        report = await Workers(models, tools).run("干活", group_id="1", tools=[name])
        assert report.ok, report.error
        return str([m for m in models.calls[1][1] if m.get("role") == "tool"][0]["content"])

    async def test_oversized_file_page_keeps_whole_page_and_logs(self, guard_tools, caplog):
        """分页工具页超预算（tools_exec 那层坏了）：留日志，但正文一个字不删。"""
        with caplog.at_level("WARNING", logger="maiwork.workers"):
            content = await self._tool_msg(guard_tools, "read_file")
        assert content == "长" * 7000, "不许把超预算的页截掉（连「别往后翻」那套截断也不做了）"
        assert "已截断" not in content
        assert any(
            "超过单条 tool 消息上限" in r.getMessage() for r in caplog.records
        ), "页超预算要留日志痕迹（tools_exec 应保证每页 ≤ 预算）"

    async def test_other_tools_keep_full_body_never_truncate(self, guard_tools):
        """没有工作区（归档不了）也保留完整正文：装不下由预算闸明确失败，不许悄悄丢中段。"""
        content = await self._tool_msg(guard_tools, "fetch_page")
        assert content == "网" * 20000
        assert "已截断" not in content
        assert "别往后翻" not in content


# ----------------------------------------------------------------------
# 假模型（回放一串工具调用）
# ----------------------------------------------------------------------


@dataclass
class _Chat:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    model: str = "fake-worker"
    prompt_tokens: int = 10
    completion_tokens: int = 5
    raw_message: dict = field(default_factory=dict)


class _Replay:
    def __init__(self, results):
        self.queue = list(results)
        self.calls: list = []

    async def chat(self, role=None, messages=None, **kwargs):
        self.calls.append((role, [dict(m) for m in messages], kwargs))
        if not self.queue:
            return _Chat(text="（队列空了）")
        return self.queue.pop(0)


def _tool_call(name: str, arguments, call_id: str = "call-1") -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
