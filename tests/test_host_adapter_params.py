"""适配器 api.call 传参形状（2026-10-10 线上巡检）。

线上实况：SnowLuma 适配器 1.x 的群文件 / 群公告 / 群相册这类「动作直通」接口签名是
`api_action_xxx(self, params=None)`——参数要整包放在 `params` 里；而
`get_group_info` / `get_group_member_info` / `get_group_member_list` 是按关键字收参。
旧 MaiWork 一律平铺传 `{"group_id": ..., "file": ...}`，T-11 交付时报
`api_action_upload_group_file() got an unexpected keyword argument 'group_id'`，群文件没传上去。

宿主失败时 `api.call` 返回 `{"success": False, "error": "..."}`（SDK 拿不到 result 键就原样给），
旧 call_adapter 把没有 status 的失败当成功、返回 {}——失败被吞。

另：老一些的适配器（0.8.x）这些接口是平铺收参的，所以碰到「多了/少了关键字」的 TypeError
要换另一种形状重试一次（参数绑定失败发生在动作执行前，重试不会重复上传）。
"""

from __future__ import annotations

import pytest

from fakes import FakeCtx

from CharTyr_MaiWork.maiwork.host import Host, HostError

GID = "900000001"


def _unexpected(name: str) -> dict:
    return {
        "success": False,
        "error": f"QQFileApiMixin.api_action_x() got an unexpected keyword argument '{name}'",
    }


class TestActionApisWrapParams:
    @pytest.mark.asyncio
    async def test_upload_group_file_wraps_in_params(self) -> None:
        ctx = FakeCtx({"api.call": {"status": "ok", "retcode": 0, "data": {"file_id": "/f-1"}}})
        fid = await Host(ctx).upload_group_file(GID, "/tmp/a.docx", "a.docx")
        assert fid == "/f-1"
        _, kw = ctx.calls[0]
        assert kw["args"] == {"params": {"group_id": GID, "file": "/tmp/a.docx", "name": "a.docx"}}

    @pytest.mark.asyncio
    async def test_group_file_url_wraps_in_params(self) -> None:
        ctx = FakeCtx({"api.call": {"status": "ok", "retcode": 0, "data": {"url": "https://x/y"}}})
        assert await Host(ctx).group_file_url(GID, "/f-1") == "https://x/y"
        _, kw = ctx.calls[0]
        assert kw["args"] == {"params": {"group_id": GID, "file_id": "/f-1"}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "api",
        [
            "adapter.napcat.file.get_group_root_files",
            "adapter.napcat.file.delete_group_file",
            "adapter.napcat.group.send_group_notice",
            "adapter.napcat.file.upload_image_to_qun_album",
        ],
    )
    async def test_call_adapter_wraps_action_apis(self, api: str) -> None:
        ctx = FakeCtx({"api.call": {"status": "ok", "retcode": 0, "data": {"x": 1}}})
        out = await Host(ctx).call_adapter(api, {"group_id": GID})
        assert out == {"x": 1}
        _, kw = ctx.calls[0]
        assert kw["args"] == {"params": {"group_id": GID}}

    @pytest.mark.asyncio
    async def test_typed_apis_stay_flat(self) -> None:
        ctx = FakeCtx({"api.call": {"role": "admin"}})
        assert await Host(ctx).group_member_role(GID, "10001") == "admin"
        _, kw = ctx.calls[0]
        assert kw["args"] == {"group_id": GID, "user_id": "10001"}


class TestFailureNotSwallowed:
    @pytest.mark.asyncio
    async def test_call_adapter_success_false_raises(self) -> None:
        ctx = FakeCtx({"api.call": {"success": False, "error": "适配器没连上"}})
        with pytest.raises(HostError, match="适配器没连上"):
            await Host(ctx).call_adapter("adapter.napcat.file.get_group_root_files", {"group_id": GID})

    @pytest.mark.asyncio
    async def test_upload_error_message_carries_reason(self) -> None:
        ctx = FakeCtx({"api.call": {"success": False, "error": "群文件空间已满"}})
        with pytest.raises(HostError, match="群文件空间已满"):
            await Host(ctx).upload_group_file(GID, "/tmp/a.docx", "a.docx")


class TestShapeFallback:
    @pytest.mark.asyncio
    async def test_old_adapter_rejects_params_then_flat_succeeds(self) -> None:
        seen: list[dict] = []

        async def api_call(**kw):
            seen.append(kw["args"])
            if "params" in kw["args"]:
                return _unexpected("params")
            return {"status": "ok", "retcode": 0, "data": {"file_id": "/old"}}

        ctx = FakeCtx({"api.call": api_call})
        fid = await Host(ctx).upload_group_file(GID, "/tmp/a.docx", "a.docx")
        assert fid == "/old"
        assert seen == [
            {"params": {"group_id": GID, "file": "/tmp/a.docx", "name": "a.docx"}},
            {"group_id": GID, "file": "/tmp/a.docx", "name": "a.docx"},
        ]

    @pytest.mark.asyncio
    async def test_flat_rejected_then_params_succeeds(self) -> None:
        seen: list[dict] = []

        async def api_call(**kw):
            seen.append(kw["args"])
            if "params" not in kw["args"]:
                return _unexpected("group_id")
            return {"status": "ok", "retcode": 0, "data": {"role": "owner"}}

        ctx = FakeCtx({"api.call": api_call})
        assert await Host(ctx).group_member_role(GID, "10001") == "owner"
        assert len(seen) == 2 and seen[1] == {"params": {"group_id": GID, "user_id": "10001"}}

    @pytest.mark.asyncio
    async def test_other_failure_not_retried(self) -> None:
        calls = 0

        async def api_call(**kw):
            nonlocal calls
            calls += 1
            return {"success": False, "error": "timeout"}

        ctx = FakeCtx({"api.call": api_call})
        with pytest.raises(HostError):
            await Host(ctx).upload_group_file(GID, "/tmp/a.docx", "a.docx")
        assert calls == 1

    @pytest.mark.asyncio
    async def test_learned_shape_is_remembered(self) -> None:
        seen: list[dict] = []

        async def api_call(**kw):
            seen.append(kw["args"])
            if "params" in kw["args"]:
                return _unexpected("params")
            return {"status": "ok", "retcode": 0, "data": {"x": 1}}

        h = Host(FakeCtx({"api.call": api_call}))
        await h.call_adapter("adapter.napcat.file.get_group_root_files", {"group_id": GID})
        await h.call_adapter("adapter.napcat.file.get_group_root_files", {"group_id": GID})
        # 第二次直接用学到的平铺形状，不再先撞一次 params
        assert seen[-1] == {"group_id": GID}
        assert len(seen) == 3
