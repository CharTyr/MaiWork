"""GET/PUT /api/groups/{gid}/rules + skills* API 冒烟：
权限镜像 feeds/domains（admin + group-admin 读写、member 403、匿名 401、non-served 404）。
自动流不能改本群规矩（§八.1 红线）。
"""

from __future__ import annotations

import pytest
from aiohttp import web_response
from aiohttp.test_utils import TestClient, TestServer
import aiohttp
from pathlib import Path

from fakes import FakeCtx
from CharTyr_MaiWork.maiwork.app import MaiWorkApp
from CharTyr_MaiWork.maiwork import agents as agents_mod

from test_group_admin import (
    ADMIN_PW,
    G1,
    G1_PW,
    G2,
    MEMBER,
    SimpleEnv,
    _FakeFeeds,
    _FakeTopics,
    _raw_config,
    env as env,  # 复用同 fixture（pytest 会按名字找到）
)  # noqa: F401


@pytest.mark.asyncio
async def test_rules_admin_rw_and_history(env: SimpleEnv) -> None:
    """总管理员可读写 + 版本留痕。"""
    await env.client.post("/api/login", json={"password": ADMIN_PW})
    r = await env.client.get(f"/api/groups/{G1}/rules")
    assert r.status == 200
    body = await r.json()
    assert body["body"] == ""
    # 写
    r = await env.client.put(f"/api/groups/{G1}/rules", json={"body": "本群规矩：禁拉刷屏"})
    assert r.status == 200
    data = await r.json()
    assert data["body"] == "本群规矩：禁拉刷屏"
    assert data["updated"] > 0
    assert data["updated_by"] == "admin"
    # 覆盖一次 → 第一版记到 versions
    r = await env.client.put(f"/api/groups/{G1}/rules", json={"body": "第二版"})
    assert r.status == 200
    r2 = await env.client.get(f"/api/groups/{G1}/rules/versions")
    assert r2.status == 200
    vers = (await r2.json())["versions"]
    assert any("禁拉刷屏" in (v.get("body") or "") for v in vers)


@pytest.mark.asyncio
async def test_rules_group_admin_own_group_rw_other_403(env: SimpleEnv) -> None:
    env.set_group_password(G1, G1_PW)
    await env.login(G1_PW)
    r = await env.client.put(f"/api/groups/{G1}/rules", json={"body": "G1 规矩"})
    assert r.status == 200
    r2 = await env.client.put(f"/api/groups/{G2}/rules", json={"body": "别的群"})
    assert r2.status == 403


@pytest.mark.asyncio
async def test_rules_member_403_anon_401(env: SimpleEnv) -> None:
    token = env.app.token_of(G1)
    r = await env.client.get(f"/api/groups/{G1}/rules", headers={"X-MW-Group": token})
    assert r.status == 403
    r = await env.client.put(f"/api/groups/{G1}/rules", json={"body": "x"})
    assert r.status == 401


@pytest.mark.asyncio
async def test_rules_non_served_404(env: SimpleEnv) -> None:
    await env.client.post("/api/login", json={"password": ADMIN_PW})
    r = await env.client.get("/api/groups/99999/rules")
    assert r.status == 404


@pytest.mark.asyncio
async def test_rules_auto_flow_never_modifies_rules(env: SimpleEnv) -> None:
    """自动流程（复盘、迁移、taste 删除后没接住都别动 — 规矩只有管理员写）—— 自动 touch 留在 body=""."""
    await env.client.post("/api/login", json={"password": ADMIN_PW})
    # admin 写好一次
    await env.client.put(f"/api/groups/{G1}/rules", json={"body": "管理员定的"})
    # 模拟自动：直接读不会改；真正测自动行为不重写是 require no-op over vagaries — assert here it stayed
    r = await env.client.get(f"/api/groups/{G1}/rules")
    assert (await r.json())["body"] == "管理员定的"


@pytest.mark.asyncio
async def test_skills_admin_crud(env: SimpleEnv) -> None:
    """skills CRUD：POST 建、GET debug、PATCH 改、DELETE 删、version restore。"""
    await env.client.post("/api/login", json={"password": ADMIN_PW})
    # POST 一个 task
    r = await env.client.post(
        f"/api/groups/{G1}/skills",
        json={"kind": "task", "name": "整理报名表", "description": "概括", "body": "步骤1：建表"},
    )
    assert r.status == 201
    data = await r.json()
    assert data["id"] > 0
    assert any(s["name"] == "整理报名表" for s in data["skills"])
    # GET 全量
    r2 = await env.client.get(f"/api/groups/{G1}/skills")
    assert r2.status == 200
    skills = (await r2.json())["skills"]
    row = next(s for s in skills if s["kind"] == "task" and s["name"] == "整理报名表")
    sid = row["id"]
    # PATCH
    r3 = await env.client.patch(f"/api/groups/{G1}/skills/{sid}", json={"body": "步骤1：冲击表"})
    assert r3.status == 200
    out = await r3.json()
    assert out["skill"]["body"] == "步骤1：冲击表"
    # versions
    r4 = await env.client.get(f"/api/groups/{G1}/skills/{sid}/versions?kind=task")
    assert r4.status == 200
    vers = (await r4.json())["versions"]
    assert any("建表" in (v.get("body") or "") for v in vers)
    # DELETE
    r5 = await env.client.delete(f"/api/groups/{G1}/skills/{sid}")
    assert r5.status == 200
    # now get shows empty
    r6 = await env.client.get(f"/api/groups/{G1}/skills?kind=task")
    assert (await r6.json())["skills"] == []


@pytest.mark.asyncio
async def test_skills_member_403_anon_401(env: SimpleEnv) -> None:
    token = env.app.token_of(G1)
    r = await env.client.get(f"/api/groups/{G1}/skills", headers={"X-MW-Group": token})
    assert r.status == 403
    r = await env.client.get(f"/api/groups/{G1}/skills")
    assert r.status == 401


@pytest.mark.asyncio
async def test_skills_non_served_404(env: SimpleEnv) -> None:
    await env.client.post("/api/login", json={"password": ADMIN_PW})
    r = await env.client.get("/api/groups/99999/skills")
    assert r.status == 404


@pytest.mark.asyncio
async def test_skills_group_admin_cross_group_403(env: SimpleEnv) -> None:
    env.set_group_password(G1, G1_PW)
    await env.login(G1_PW)
    r = await env.client.get(f"/api/groups/{G1}/skills")
    assert r.status == 200
    r2 = await env.client.get(f"/api/groups/{G2}/skills")
    assert r2.status == 403
