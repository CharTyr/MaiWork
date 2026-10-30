"""read_logs 统一读日志（模型调用 + 工具调用）+ list_news 漏斗行（10-30-admin-capabilities / 10-30-admin-tools-consolidation）。

管理员问「这批资讯到底有没有用上 flovia_x？」时要有地方看事实：

- read_logs：一个入口读两份日志。
  * kind="models"（默认）：照旧读 model_calls；kind="tools"：读 tool_calls 表里
    「谁、在哪个群、调了什么工具、入参/输出/错误、成败、耗时」；
  * 没给 kind 但传了 tool 或 task_id → 自动按 kind="tools" 查；
  * kind="all"：两段都查，data={"model_calls": [...], "tool_calls": [...]}；
  * 只给服务群和全局（没群号）的记录——下掉的群残留的调用不是管理员该看的；
  * group_id 默认当前聚焦群，填 * 看所有服务群 + 全局；非服务群直接拒；
  * task_id 前缀匹配、tool 包含匹配、failed_only 只看失败、limit 默认 30 最多 200；
  * 每条一行概要 + 缩进的入参（≤300 字）/ 输出（≤500 字）/ 错误行，data 给原始行；
  * 旧的 read_tool_calls 工具名不再注册。
- list_news：批次带 funnel 时，在条目行之前加一行漏斗要点——搜了几次、候选多少（按来源
  计数，含补找带来的 agent:<工具名>）、打开多少、留下多少；补找交回 / 留下 / 丢弃 / 出错。
  老批次没有 funnel 就不加这行，不许把整坨 JSON 倒给管理员。
"""

from __future__ import annotations

import json

import pytest

from test_tools_admin_real import G1, G2, G_BAD, NOW, _call, app  # noqa: F401


def _insert_call(c, ts, gid, task_id, actor, tool, inp="", outp="", ms=0, ok=1, error=""):
    c.execute(
        "INSERT INTO tool_calls (ts, group_id, task_id, actor, tool, input, output, ms, ok, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, gid, task_id, actor, tool, inp, outp, ms, ok, error),
    )


def _seed_calls(app) -> None:  # noqa: F811
    """种几笔工具调用：服务群两笔（一成一败）、一笔全局、一笔非服务群。"""
    with app.store.tx() as c:
        _insert_call(
            c, NOW - 10, G1, "feeds-verify:abc", "资讯核验 子 agent #0", "mcp_flovia_flovia_x",
            inp='{"q":"AI 新闻"}', outp='{"items":[{"title":"x"}]}', ms=1230,
        )
        _insert_call(
            c, NOW - 9, G_BAD, "feeds-verify:zzz", "资讯核验 子 agent #0", "mcp_flovia_flovia_x",
            inp="{}", outp="别群的记录不该看见", ms=50,
        )
        _insert_call(
            c, NOW - 8, G1, "T-9", "任务 子 agent #1", "run_command",
            inp='{"cmd":"ls"}', outp="", ms=500, ok=0, error="命令炸了",
        )
        _insert_call(
            c, NOW - 7, "", "admin-tool:3", "主模型（管理员对话）", "fetch_page",
            inp='{"url":"https://a.com"}', outp="页面正文", ms=200,
        )
        _insert_call(
            c, NOW - 6, G2, "feeds-verify:def", "资讯核验 子 agent #0", "web_search",
            inp='{"q":"x"}', outp="[]", ms=100,
        )


class TestReadLogsToolCalls:
    """read_logs 的 kind="tools" 分支：原 read_tool_calls 行为原样保留。"""

    @pytest.mark.asyncio
    async def test_served_groups_and_global_only(self, app) -> None:  # noqa: F811
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "tools", "group_id": "*"})
        assert r.ok, r.error
        assert isinstance(r.data, list) and r.data
        # 非服务群的调用一行都不给（output 和 data 都查）
        assert all(str(row["group_id"]) in (G1, G2, "") for row in r.data)
        assert "别群的记录不该看见" not in r.output
        assert "feeds-verify:zzz" not in r.output
        # 服务群的 + 全局的（group_id='' 的 admin-tool 记录）照常给
        assert any(row["tool"] == "mcp_flovia_flovia_x" for row in r.data)
        assert any(row["task_id"] == "admin-tool:3" for row in r.data)

    @pytest.mark.asyncio
    async def test_default_group_is_ctx(self, app) -> None:  # noqa: F811
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "tools"})  # ctx 聚焦 G1
        assert r.ok, r.error
        assert r.data and all(str(row["group_id"]) == G1 for row in r.data)

    @pytest.mark.asyncio
    async def test_group_filter_and_non_served_rejected(self, app) -> None:  # noqa: F811
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "tools", "group_id": G2})
        assert r.ok, r.error
        assert {str(row["group_id"]) for row in r.data} == {G2}
        r2 = await _call(app, "read_logs", {"kind": "tools", "group_id": G_BAD})
        assert not r2.ok and "服务群" in r2.error

    @pytest.mark.asyncio
    async def test_task_id_prefix_filter(self, app) -> None:  # noqa: F811
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "tools", "group_id": "*", "task_id": "feeds-verify"})
        assert r.ok, r.error
        assert r.data
        assert all(str(row["task_id"]).startswith("feeds-verify") for row in r.data)
        assert any(row["task_id"] == "feeds-verify:abc" for row in r.data)

    @pytest.mark.asyncio
    async def test_tool_substring_filter(self, app) -> None:  # noqa: F811
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "tools", "group_id": "*", "tool": "flovia"})
        assert r.ok, r.error
        assert r.data and all("flovia" in str(row["tool"]) for row in r.data)

    @pytest.mark.asyncio
    async def test_auto_routes_to_tools_on_tool_or_task_id(self, app) -> None:  # noqa: F811
        """没给 kind、但传了 tool 或 task_id → 自动按 kind="tools" 查。"""
        _seed_calls(app)
        r = await _call(app, "read_logs", {"group_id": "*", "tool": "flovia"})
        assert r.ok, r.error
        assert r.data and all("flovia" in str(row["tool"]) for row in r.data)
        r2 = await _call(app, "read_logs", {"group_id": "*", "task_id": "admin-tool"})
        assert r2.ok, r2.error
        assert r2.data and all(str(row["task_id"]).startswith("admin-tool") for row in r2.data)

    @pytest.mark.asyncio
    async def test_failed_only_shows_error_line(self, app) -> None:  # noqa: F811
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "tools", "group_id": "*", "failed_only": True})
        assert r.ok, r.error
        assert r.data and all(int(row["ok"]) == 0 for row in r.data)
        assert "run_command" in r.output
        assert "命令炸了" in r.output and "错误" in r.output

    @pytest.mark.asyncio
    async def test_line_shape_and_truncation(self, app) -> None:  # noqa: F811
        with app.store.tx() as c:
            _insert_call(
                c, NOW - 5, G1, "T-1", "任务 子 agent #1", "big_tool",
                inp="x" * 400, outp="y" * 600, ms=42,
            )
        r = await _call(app, "read_logs", {"tool": "big_tool"})  # 不传 kind：tool 参数自动路由
        assert r.ok, r.error
        in_line = next(l for l in r.output.splitlines() if "入参:" in l)
        out_line = next(l for l in r.output.splitlines() if "输出:" in l)
        assert len(in_line.split("入参: ", 1)[1]) <= 300 and in_line.endswith("…")
        assert len(out_line.split("输出: ", 1)[1]) <= 500 and out_line.endswith("…")
        # 概要行：时间 actor 群 工具 成败 耗时
        head = next(l for l in r.output.splitlines() if "big_tool" in l)
        assert "任务 子 agent #1" in head and "成功" in head and "42ms" in head

    @pytest.mark.asyncio
    async def test_empty_result_is_friendly(self, app) -> None:  # noqa: F811
        r = await _call(app, "read_logs", {"kind": "tools", "group_id": "*", "tool": "ghost_tool_xyz"})
        assert r.ok, r.error
        assert "没有" in r.output and r.data == []

    @pytest.mark.asyncio
    async def test_limit_clamped(self, app) -> None:  # noqa: F811
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "tools", "group_id": "*", "limit": 999999})
        assert r.ok, r.error
        assert len(r.data) <= 200


class TestReadLogsMerged:
    """read_logs 的合并契约：默认 models、kind 校验、kind="all" 双段。"""

    def _seed_model_call(self, app) -> None:  # noqa: F811
        with app.store.tx() as c:
            c.execute(
                "INSERT INTO model_calls(ts,purpose,role,model,group_id,ok,status,ms,error)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (NOW - 3, "admin_chat", "main", "m1", G1, 1, 200, 88, ""),
            )

    @pytest.mark.asyncio
    async def test_default_is_models(self, app) -> None:  # noqa: F811
        """旧行为兼容：read_logs({}) / read_logs({failed_only}) 照旧查 model_calls。"""
        self._seed_model_call(app)
        _seed_calls(app)
        r = await _call(app, "read_logs", {})
        assert r.ok, r.error
        assert isinstance(r.data, list)
        assert all("tool" not in row or "model" in row for row in r.data)
        assert "mcp_flovia_flovia_x" not in r.output  # 没给 tool/task_id 不自动跳 tools

    @pytest.mark.asyncio
    async def test_bad_kind_rejected(self, app) -> None:  # noqa: F811
        r = await _call(app, "read_logs", {"kind": "bogus"})
        assert not r.ok and "models" in (r.error or "") and "tools" in (r.error or "")

    @pytest.mark.asyncio
    async def test_kind_all_returns_both_sections(self, app) -> None:  # noqa: F811
        self._seed_model_call(app)
        _seed_calls(app)
        r = await _call(app, "read_logs", {"kind": "all", "group_id": "*"})
        assert r.ok, r.error
        assert isinstance(r.data, dict)
        assert set(r.data) == {"model_calls", "tool_calls"}
        assert r.data["model_calls"] and r.data["tool_calls"]
        assert "模型" in r.output and "工具" in r.output

    @pytest.mark.asyncio
    async def test_read_tool_calls_no_longer_registered(self, app) -> None:  # noqa: F811
        names = {(s.get("function") or {}).get("name") for s in app.tools.specs("admin")}
        assert "read_logs" in names
        assert "read_tool_calls" not in names
        r = await _call(app, "read_tool_calls", {})
        assert not r.ok


class TestListNewsFunnel:
    def _seed_batch_with_funnel(self, app, supp: dict | None = None) -> None:  # noqa: F811
        """一批带 funnel 的资讯（含补找带来的 agent:<工具名> 来源计数）。"""
        with app.store.tx() as c:
            c.execute(
                "INSERT INTO news_batches(group_id,slot_ts,found,kept,skipped,created) VALUES(?,?,?,?,?,?)",
                (G1, NOW - 100, 35, 3, 0, NOW - 100),
            )
            b2 = int(c.execute("SELECT max(id) FROM news_batches").fetchone()[0])
            c.execute(
                "INSERT INTO news_items(batch_id,group_id,icon,title,summary,why,sources,score,status_kind,expires_ts,created)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    b2, G1, "rocket", "补找工具找到的条目", "s", "w",
                    json.dumps([{"url": "https://x.example.com", "site": "x.example.com", "title": "t"}]),
                    0.9, "new", NOW + 3600, NOW - 100,
                ),
            )
            app.store.kv_set(c, f"feeds.batch_stats.{b2}", {
                "searches": 12, "pages": 8, "kept": 3,
                "funnel": {
                    "queries": 12, "discovered": 35,
                    "providers": {"web": 20, "agent:mcp_flovia_flovia_x": 3},
                    "opened": 8, "kept": 3,
                    "supplement": supp if supp is not None else
                    {"ran": True, "returned": 5, "kept": 3, "not_in_output": 2, "error": ""},
                },
            })

    @pytest.mark.asyncio
    async def test_funnel_line_with_agent_provider_and_supplement(self, app) -> None:  # noqa: F811
        self._seed_batch_with_funnel(app)
        r = await _call(app, "news_manage", {"action": "list", "group_id": G1})
        assert r.ok, r.error
        assert "漏斗" in r.output
        assert "搜 12 次" in r.output
        assert "候选 35" in r.output
        # 来源计数含 agent:<工具名>——管理员问「这批有没有用上 flovia_x」就看这里
        assert "agent:mcp_flovia_flovia_x 3" in r.output
        assert "打开 8" in r.output and "留下 3" in r.output
        # 补找三段计数
        assert "补找" in r.output and "交回 5" in r.output and "丢弃 2" in r.output

    @pytest.mark.asyncio
    async def test_funnel_line_supplement_error(self, app) -> None:  # noqa: F811
        self._seed_batch_with_funnel(
            app, supp={"ran": True, "returned": 2, "kept": 0, "not_in_output": 0, "error": "TimeoutError"},
        )
        r = await _call(app, "news_manage", {"action": "list", "group_id": G1})
        assert r.ok, r.error
        assert "补找" in r.output and "出错" in r.output and "TimeoutError" in r.output

    @pytest.mark.asyncio
    async def test_no_funnel_no_line(self, app) -> None:  # noqa: F811
        # fixture 种的那批没有 stats.funnel → 不该出现漏斗行
        r = await _call(app, "news_manage", {"action": "list", "group_id": G1})
        assert r.ok, r.error
        assert "漏斗" not in r.output


def test_admin_prompt_tells_new_capabilities(app) -> None:  # noqa: F811
    """提示词「工具归属」段要如实说这三个能力，模型才能用对。"""
    from CharTyr_MaiWork.maiwork.admin_chat import AdminChat

    p = AdminChat(app)._system_prompt({"group_id": ""})
    assert "read_logs" in p, "提示词没告诉模型能看子 agent 真实工具调用"
    assert "read_tool_calls" not in p, "提示词不许再提已合并掉的旧工具名"
    assert "tool_run" in p, "提示词没告诉模型能试调子 agent 工具"
    assert "漏斗" in p, "提示词没说 list_news 里带每批漏斗"
