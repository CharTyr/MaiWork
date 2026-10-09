"""群空间（platforms/qq_onebot.py + tools_groupspace.py + outbox 登记）测试。

要点（docs/02-设计.md §10、docs/06-宿主接口事实.md 末尾「QQ 适配器与群空间接口」）：
- 线上适配器还是旧版 0.8.5：启动时探测有哪些接口，有才开放；
  旧版接口集（44 个，没有公告/相册/文件管理）→ 能力全 False。
- 新版 v1.0.1 开放后按机器人在群里的身份（owner/admin/member）出能力。
- 防手滑：删除/改名/移动只动机器人自己上传的文件（group_files_owned 表）。
- 发公告前先在群里说一句预告（走 outbox，push_kind=status）；每群每天最多 1 条公告。
- 非服务群：零调用。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fakes import FakeCtx

from CharTyr_MaiWork.maiwork import clock
from CharTyr_MaiWork.maiwork.config import load_settings
from CharTyr_MaiWork.maiwork.host import Host, HostError
from CharTyr_MaiWork.maiwork.outbox import Outbox
from CharTyr_MaiWork.maiwork.platforms.qq_onebot import GroupSpace
from CharTyr_MaiWork.maiwork.store import Store
from CharTyr_MaiWork.maiwork.tools import ToolContext, Tools

pytestmark = pytest.mark.asyncio

BJ = timezone(timedelta(hours=8))
GID = "900000001"
OTHER = "987654321"
BOT = "99999"


def _ts(day: int, hour: int = 12, minute: int = 0) -> float:
    """北京时间 2026-10-{day} 某时刻的 epoch。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=BJ).timestamp()


NOW = _ts(15)
LATER = _ts(16)

# 旧版 0.8.5 的接口集（44 个；群文件只有上传和取链接）
OLD_APIS = [
    "adapter.napcat.file.upload_group_file",
    "adapter.napcat.file.get_group_file_url",
    "adapter.napcat.group.get_group_info",
    "adapter.napcat.group.get_group_member_info",
]
# 新版 v1.0.1 追加的群空间接口
NEW_APIS = OLD_APIS + [
    "adapter.napcat.file.get_group_root_files",
    "adapter.napcat.file.get_group_files_by_folder",
    "adapter.napcat.file.get_group_file_system_info",
    "adapter.napcat.file.create_group_file_folder",
    "adapter.napcat.file.delete_group_file",
    "adapter.napcat.file.delete_group_folder",
    "adapter.napcat.file.rename_group_file",
    "adapter.napcat.file.move_group_file",
    "adapter.napcat.group.get_group_notice",
    "adapter.napcat.group.send_group_notice",
    "adapter.napcat.group.delete_group_notice",
    "adapter.napcat.file.get_qun_album_list",
    "adapter.napcat.file.get_group_album_media_list",
    "adapter.napcat.file.upload_image_to_qun_album",
    "adapter.napcat.file.del_group_album_media",
]


class FakeHost:
    """假的宿主口（群空间用）：预置 api 名单和成员身份，记录 api.call。

    results：api_name → 返回值 / 可调用对象（吃 args）；没预置的按空 data 成功返回。
    """

    def __init__(self, apis: list[str], role: str = "admin", results: dict | None = None) -> None:
        self.apis = list(apis)
        self.role = role
        self.api_calls: list[dict] = []
        self.role_calls: list[tuple[str, str]] = []
        self.fail_names: set[str] = set()  # api_name 在这里面的 call_adapter 抛 HostError
        self.results: dict = dict(results or {})

    async def list_apis(self) -> list[str]:
        return list(self.apis)

    async def group_member_role(self, group_id: str, user_id: str) -> str:
        self.role_calls.append((str(group_id), str(user_id)))
        return self.role

    async def bot_qq(self) -> str:
        return BOT

    async def call_adapter(self, api_name: str, args: dict) -> dict:
        self.api_calls.append({"api_name": str(api_name), "args": dict(args)})
        if str(api_name) in self.fail_names:
            raise HostError("调用宿主能力失败: api.call")
        r = self.results.get(str(api_name))
        if callable(r):
            r = r(args)
        if isinstance(r, BaseException):
            raise r
        return r if r is not None else {"status": "ok", "retcode": 0, "data": {}}

    def api_names(self) -> list[str]:
        return [c["api_name"] for c in self.api_calls]


class _Settings:
    """最小的 settings 替身：is_served + group_space 节 + 视图要的零碎。"""

    def __init__(self, groups: dict[str, object], *, enabled: bool = True, notice_per_day: int = 1) -> None:
        self._groups = groups
        self.group_space = type("GS", (), {"enabled": enabled, "notice_per_day": notice_per_day})()
        self.delivery = type("D", (), {"quiet_hours": "23:00-08:00"})()
        self.profile = type("P", (), {"read_interval_minutes": 10})()

    def is_served(self, group_id: str) -> bool:
        return isinstance(group_id, str) and group_id in self._groups

    def workspace_of(self, group_id: str) -> str:
        return f"g{group_id}"


def _mk_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "gw.db")
    store.migrate()
    return store


def _mk_group_space(
    tmp_path: Path,
    host: FakeHost,
    *,
    store: Store | None = None,
    groups: list[str] | None = None,
    enabled: bool = True,
    notice_per_day: int = 1,
) -> tuple[GroupSpace, Store]:
    store = store or _mk_store(tmp_path)
    settings = _Settings({g: None for g in (groups if groups is not None else [GID])},
                         enabled=enabled, notice_per_day=notice_per_day)
    return GroupSpace(host, store, lambda: settings), store


# ----------------------------------------------------------------------
# 探测：旧版 → 全 False；新版 → 按身份
# ----------------------------------------------------------------------


async def test_probe_old_adapter_all_false(tmp_path: Path) -> None:
    host = FakeHost(OLD_APIS, role="owner")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    caps = await gs.capabilities_async(GID, now=NOW)
    assert caps == {
        "files_list": False,
        "files_manage": False,
        "notice_read": False,
        "notice_send": False,
        "album_list": False,
        "album_upload": False,
    }
    assert gs.adapter_open() is False


async def test_probe_new_adapter_by_role(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    caps = await gs.capabilities_async(GID, now=NOW)
    assert caps["files_list"] is True
    assert caps["files_manage"] is True
    assert caps["notice_send"] is True   # 管理员可以发公告
    assert caps["notice_read"] is True
    assert caps["album_list"] is True
    assert caps["album_upload"] is True
    assert gs.adapter_open() is True


async def test_member_role_cannot_send_notice(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="member")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    caps = await gs.capabilities_async(GID, now=NOW)
    assert caps["notice_send"] is False   # 普通成员不能发公告
    assert caps["album_upload"] is False  # 普通成员不能传相册
    assert caps["files_manage"] is False
    assert caps["files_list"] is True     # 看还是都能看
    assert caps["notice_read"] is True


async def test_role_cached_one_hour(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    await gs.capabilities_async(GID, now=NOW)
    await gs.capabilities_async(GID, now=NOW + 59 * 60)
    assert host.role_calls == [(GID, BOT)]  # 1 小时内只查一次
    # 1 小时后再查
    await gs.capabilities_async(GID, now=NOW + 61 * 60)
    assert host.role_calls == [(GID, BOT), (GID, BOT)]


async def test_probe_cache_refresh_six_hours(tmp_path: Path) -> None:
    host = FakeHost(OLD_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    assert gs.adapter_open() is False
    # 6 小时内再 probe 是真缓存：host 换了名单也不刷新
    host.apis = list(NEW_APIS)
    await gs.probe(now=NOW + 3 * 3600)
    assert gs.adapter_open() is False
    # 6 小时后刷新
    await gs.probe(now=NOW + 7 * 3600)
    assert gs.adapter_open() is True
    caps = await gs.capabilities_async(GID, now=NOW + 7 * 3600)
    assert caps["files_manage"] is True


async def test_probe_failure_no_throw_and_all_closed(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    assert gs.adapter_open() is True

    class _FailHost(FakeHost):
        async def list_apis(self) -> list[str]:
            raise HostError("调用宿主能力超时: api.list")

    fail_host = _FailHost([], role="admin")
    gs2, _ = _mk_group_space(tmp_path / "f2", fail_host)
    await gs2.probe(now=NOW)  # 首次失败 → 什么都没开，但不抛
    assert gs2.capabilities(GID, now=NOW)["files_list"] is False


# ----------------------------------------------------------------------
# 只动自有文件
# ----------------------------------------------------------------------


async def test_manage_own_files_only(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    gs.register_owned(GID, "/f-own-1", "我的文件.txt", task_id="T-1", now=NOW)
    # 自己的文件可以删
    await gs.delete_file(GID, "/f-own-1")
    assert host.api_calls[-1]["api_name"] == "adapter.napcat.file.delete_group_file"
    # 别人的文件：拒绝，且没有真的调适配器
    calls_before = len(host.api_calls)
    with pytest.raises(PermissionError, match="只能动我自己传"):
        await gs.delete_file(GID, "/f-other-9")
    assert len(host.api_calls) == calls_before


async def test_rename_move_own_only(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    with pytest.raises(PermissionError):
        await gs.rename_file(GID, "/f-other", "新名字")
    with pytest.raises(PermissionError):
        await gs.move_file(GID, "/f-other", "/folder-1")
    assert host.api_calls == []  # 一个真调用都没发出去
    # 删文件夹同样只删自己建的（没登记 → 拒绝，且在调适配器之前）
    with pytest.raises(PermissionError, match="只能删我自己建的文件夹"):
        await gs.delete_folder(GID, "/folder-9")
    assert host.api_calls == []


async def test_rename_updates_owned_name_and_delete_removes(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    gs.register_owned(GID, "/f-1", "旧名.txt", task_id=None, now=NOW)
    await gs.rename_file(GID, "/f-1", "新名.txt")
    row = store.read().execute(
        "SELECT name FROM group_files_owned WHERE group_id=? AND file_id=?", (GID, "/f-1")
    ).fetchone()
    assert row is not None and row["name"] == "新名.txt"
    await gs.delete_file(GID, "/f-1")
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM group_files_owned WHERE group_id=? AND file_id=?", (GID, "/f-1")
    ).fetchone()
    assert int(row["c"]) == 0  # 删掉后从登记里移除


# ----------------------------------------------------------------------
# 只删自己建的文件夹（插件中心审核整改：delete_folder 归属 + 内容校验）
# ----------------------------------------------------------------------


def _listing(files=(), folders=()) -> dict:
    """按 NapCat 返回的形状造一份目录清单。

    FakeHost 站在 Host 的位置上，所以这里给的是**已经解包过的 data**
    （真 Host.call_adapter 会把信封里的 data 拿出来，见 test_host_call_adapter_passthrough）。
    """
    return {
        "files": [
            {"file_id": fid, "file_name": nm, "file_size": 1} for fid, nm in files
        ],
        "folders": [
            {"folder_id": fid, "folder_name": nm} for fid, nm in folders
        ],
    }


async def test_delete_folder_not_registered_refused(tmp_path: Path) -> None:
    """没登记的文件夹（别人建的 / 老版本建的）→ 拒绝，不调适配器。"""
    host = FakeHost(NEW_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    with pytest.raises(PermissionError, match="只能删我自己建的文件夹"):
        await gs.delete_folder(GID, "/folder-other")
    assert host.api_calls == []


async def test_delete_folder_with_someone_elses_file_refused(tmp_path: Path) -> None:
    """文件夹里混了别人的文件 → 拒绝，不调删除接口。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={
            "adapter.napcat.file.get_group_files_by_folder": _listing(
                files=[("/f-mine", "我的.txt"), ("/f-other", "别人的.txt")]
            )
        },
    )
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    gs.register_folder_owned(GID, "/folder-mine", "我的文件夹", now=NOW)
    gs.register_owned(GID, "/f-mine", "我的.txt", now=NOW)
    with pytest.raises(PermissionError, match="文件夹里有别人"):
        await gs.delete_folder(GID, "/folder-mine")
    assert "adapter.napcat.file.delete_group_folder" not in host.api_names()
    # 登记行原样保留（拒绝之后还能再试）
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM group_folders_owned WHERE group_id=? AND folder_id=?",
        (GID, "/folder-mine"),
    ).fetchone()
    assert int(row["c"]) == 1


async def test_delete_folder_with_someone_elses_subfolder_refused(tmp_path: Path) -> None:
    """子文件夹不是自己建的 → 同样拒绝。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={
            "adapter.napcat.file.get_group_files_by_folder": _listing(
                folders=[("/sub-other", "别人的子文件夹")]
            )
        },
    )
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    gs.register_folder_owned(GID, "/folder-mine", "我的文件夹", now=NOW)
    with pytest.raises(PermissionError, match="文件夹里有别人"):
        await gs.delete_folder(GID, "/folder-mine")
    assert "adapter.napcat.file.delete_group_folder" not in host.api_names()


async def test_delete_folder_all_own_allowed(tmp_path: Path) -> None:
    """文件夹是自己建的、里面的文件和子文件夹也都是自己的 → 放行，删完清登记。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={
            "adapter.napcat.file.get_group_files_by_folder": _listing(
                files=[("/f-mine", "我的.txt")], folders=[("/sub-mine", "我的子文件夹")]
            )
        },
    )
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    gs.register_folder_owned(GID, "/folder-mine", "我的文件夹", now=NOW)
    gs.register_folder_owned(GID, "/sub-mine", "我的子文件夹", now=NOW)
    gs.register_owned(GID, "/f-mine", "我的.txt", now=NOW)
    await gs.delete_folder(GID, "/folder-mine")
    assert host.api_calls[-1]["api_name"] == "adapter.napcat.file.delete_group_folder"
    assert host.api_calls[-1]["args"]["folder_id"] == "/folder-mine"
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM group_folders_owned WHERE group_id=? AND folder_id=?",
        (GID, "/folder-mine"),
    ).fetchone()
    assert int(row["c"]) == 0  # 删成功后登记行清掉
    # 事件也记了
    ev = store.read().execute(
        "SELECT kind FROM events WHERE kind='group_space.folder_deleted'"
    ).fetchone()
    assert ev is not None


async def test_delete_folder_empty_allowed(tmp_path: Path) -> None:
    """自己建的空文件夹 → 放行。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={"adapter.napcat.file.get_group_files_by_folder": _listing()},
    )
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    gs.register_folder_owned(GID, "/folder-empty", "空文件夹", now=NOW)
    await gs.delete_folder(GID, "/folder-empty")
    assert host.api_calls[-1]["api_name"] == "adapter.napcat.file.delete_group_folder"


async def test_delete_folder_cannot_list_refused(tmp_path: Path) -> None:
    """列不出文件夹内容（适配器报错）→ 不敢删，拒绝。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={"adapter.napcat.file.get_group_files_by_folder": HostError("调用宿主能力失败: api.call")},
    )
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    gs.register_folder_owned(GID, "/folder-mine", "我的文件夹", now=NOW)
    with pytest.raises(PermissionError, match="列不出"):
        await gs.delete_folder(GID, "/folder-mine")
    assert "adapter.napcat.file.delete_group_folder" not in host.api_names()


async def test_create_folder_registers_returned_folder_id(tmp_path: Path) -> None:
    """适配器返回里带 folder_id → 直接登记。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={"adapter.napcat.file.create_group_file_folder": {"folder_id": "/new-1"}},
    )
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    await gs.create_folder(GID, "我的文件夹")
    row = store.read().execute(
        "SELECT folder_id, name, created_ts FROM group_folders_owned WHERE group_id=?", (GID,)
    ).fetchone()
    assert row is not None
    assert row["folder_id"] == "/new-1"
    assert row["name"] == "我的文件夹"
    assert float(row["created_ts"]) > 0
    # 建之前会先列一次父目录（为「返回值里没 id」那条路做准备），但不该有第二次列目录：
    # 返回值里有 id 就直接用它，不再建后重列
    assert host.api_names().count("adapter.napcat.file.get_group_root_files") == 1
    assert host.api_names().count("adapter.napcat.file.create_group_file_folder") == 1


async def test_create_folder_registers_by_before_after_diff(tmp_path: Path) -> None:
    """返回里没有 folder_id → 建前建后各列一次，取「新出现且同名」的那个。"""
    calls = {"n": 0}

    def _root_files(args: dict) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:  # 建之前：只有一个旧文件夹
            return _listing(folders=[("/old-1", "旧文件夹")])
        return _listing(folders=[("/old-1", "旧文件夹"), ("/new-2", "我的文件夹")])

    host = FakeHost(
        NEW_APIS, role="admin", results={"adapter.napcat.file.get_group_root_files": _root_files}
    )
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    await gs.create_folder(GID, "我的文件夹")
    row = store.read().execute(
        "SELECT folder_id FROM group_folders_owned WHERE group_id=?", (GID,)
    ).fetchone()
    assert row is not None and row["folder_id"] == "/new-2"
    assert calls["n"] == 2  # 建前一次、建后一次


async def test_create_folder_unknown_id_only_logs(tmp_path: Path) -> None:
    """两次列出都拿不到新 id → 不登记、不抛（最坏后果是这文件夹以后不能删）。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={
            "adapter.napcat.file.get_group_root_files": _listing(folders=[("/old-1", "旧文件夹")]),
            "adapter.napcat.file.create_group_file_folder": HostError("调用宿主能力失败: api.call"),
        },
    )
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    # create 本身失败会抛（适配器错误照旧往外抛）
    with pytest.raises(HostError):
        await gs.create_folder(GID, "我的文件夹")
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM group_folders_owned WHERE group_id=?", (GID,)
    ).fetchone()
    assert int(row["c"]) == 0

    # 适配器成功但两边都列不出新文件夹 → 不登记、不抛
    host2 = FakeHost(
        NEW_APIS,
        role="admin",
        results={"adapter.napcat.file.get_group_root_files": _listing(folders=[("/old-1", "旧文件夹")])},
    )
    gs2, store2 = _mk_group_space(tmp_path / "n2", host2)
    await gs2.probe(now=NOW)
    assert await gs2.create_folder(GID, "我的文件夹") is None
    row2 = store2.read().execute(
        "SELECT COUNT(*) AS c FROM group_folders_owned WHERE group_id=?", (GID,)
    ).fetchone()
    assert int(row2["c"]) == 0


async def test_create_folder_unserved_group_not_registered(tmp_path: Path) -> None:
    """只登记服务群：非服务群在建之前就被拒，零写入。"""
    host = FakeHost(NEW_APIS, role="admin")
    gs, store = _mk_group_space(tmp_path, host, groups=[GID])
    await gs.probe(now=NOW)
    with pytest.raises(PermissionError, match="不服务"):
        await gs.create_folder(OTHER, "别人的")
    row = store.read().execute(
        "SELECT COUNT(*) AS c FROM group_folders_owned"
    ).fetchone()
    assert int(row["c"]) == 0
    assert host.api_calls == []


# ----------------------------------------------------------------------
# 能力不具备 → 方法直接中文拒绝
# ----------------------------------------------------------------------


async def test_method_refuses_when_capability_missing(tmp_path: Path) -> None:
    host = FakeHost(OLD_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    with pytest.raises(PermissionError, match="适配器没开放"):
        await gs.list_files(GID)
    with pytest.raises(PermissionError, match="适配器没开放"):
        await gs.send_notice(GID, "大家好")
    with pytest.raises(PermissionError, match="适配器没开放"):
        await gs.upload_to_album(GID, "album-1", "/x/1.png")


# ----------------------------------------------------------------------
# 发公告：先预告、再发公告；每天上限
# ----------------------------------------------------------------------


async def test_notice_announce_then_send(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, store = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)
    sent: list[dict] = []

    async def announce(group_id: str, text: str) -> None:
        sent.append({"group_id": group_id, "text": text})

    content = "本周六晚上八点例会，记得来" + "长" * 40
    await gs.send_notice(GID, content, announce=announce, now=NOW)
    # 先预告一句「我要发一条群公告：<前 30 字>」
    assert len(sent) == 1
    assert sent[0]["text"].startswith("我要发一条群公告：")
    assert sent[0]["text"] == f"我要发一条群公告：{content[:30]}"
    # 再调适配器发公告
    assert host.api_calls[-1]["api_name"] == "adapter.napcat.group.send_group_notice"
    assert host.api_calls[-1]["args"]["group_id"] == GID
    assert host.api_calls[-1]["args"]["content"] == content


async def test_notice_daily_limit(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host, notice_per_day=1)
    await gs.probe(now=NOW)

    async def announce(group_id: str, text: str) -> None:
        return None

    await gs.send_notice(GID, "第一条公告", announce=announce, now=NOW)
    with pytest.raises(PermissionError, match="今天已经发过公告"):
        await gs.send_notice(GID, "第二条公告", announce=announce, now=NOW + 3600)
    # 第二天（北京时间）可以再发
    await gs.send_notice(GID, "第二天的公告", announce=announce, now=LATER)
    assert host.api_calls[-1]["args"]["content"] == "第二天的公告"


async def test_notice_member_role_refused(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="member")
    gs, _ = _mk_group_space(tmp_path, host)
    await gs.probe(now=NOW)

    async def announce(group_id: str, text: str) -> None:
        raise AssertionError("不是管理员就不该走到预告这一步")

    with pytest.raises(PermissionError, match="不是群主或管理员"):
        await gs.send_notice(GID, "大家好", announce=announce, now=NOW)


# ----------------------------------------------------------------------
# 自有文件登记（outbox hook）
# ----------------------------------------------------------------------


def _mk_outbox(tmp_path: Path, store: Store):
    """按 test_outbox 的搭法建一个最小 Outbox（文本/文件两路假 host）。"""
    from CharTyr_MaiWork.maiwork.delivery import Mentions, Pushes

    class _Host:
        def __init__(self) -> None:
            self.texts: list[dict] = []
            self.uploads: list[dict] = []

        async def send_text(self, session_id: str, text: str, *, reply_to: str = ""):
            self.texts.append({"session_id": session_id, "text": text, "reply_to": reply_to})
            return type("SendResult", (), {"sent": True, "message_id": "m1"})()

        async def upload_group_file(self, group_id: str, path: str, name: str) -> str:
            self.uploads.append({"group_id": group_id, "path": path, "name": name})
            return "/file-id-abc"

    host = _Host()
    settings, _ = load_settings({
        "plugin": {"enabled": True},
        "groups": {"serve": [{"group": f"qq:{GID}", "workspace": "t"}]},
        "storage": {"data_dir": str(tmp_path)},
        "environments": {"workspace_root": str(tmp_path)},
    })
    pushes = Pushes(store, lambda: settings)
    mentions = Mentions(store, lambda: settings)
    outbox = Outbox(store, host, pushes, mentions, lambda: settings)
    with store.tx() as conn:
        conn.execute("UPDATE groups SET session_id=? WHERE group_id=?", ("sess-1", GID))
    return outbox, host


async def test_outbox_upload_registers_owned(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, workspace, token, created) VALUES (?, 't', 'tok1', 1)",
            (GID,),
        )
    outbox, host = _mk_outbox(tmp_path, store)
    # 挂上登记 hook（app 启动时做的事）
    host_gs = FakeHost(NEW_APIS, role="admin")
    settings = _Settings({GID: None})
    gs = GroupSpace(host_gs, store, lambda: settings)
    outbox.set_group_file_hook(gs.register_owned)  # hook：outbox 上传成功 → 登记

    f = tmp_path / "产品图.png"
    f.write_bytes(b"png")
    outbox.enqueue(
        "deliver:test", GID, "file",
        {"path": str(f), "name": "产品图.png", "push_kind": "delivery"},
        task_id="T-7",
    )
    await outbox.flush(NOW)
    row = store.read().execute(
        "SELECT group_id, file_id, name, task_id, uploaded_ts FROM group_files_owned WHERE group_id=?",
        (GID,),
    ).fetchone()
    assert row is not None
    assert row["file_id"] == "/file-id-abc"
    assert row["name"] == "产品图.png"
    assert row["task_id"] == "T-7"
    assert float(row["uploaded_ts"]) > 0


# ----------------------------------------------------------------------
# 工具：能力不足时的返回；worker 不能用
# ----------------------------------------------------------------------


async def _mk_tools(tmp_path: Path, host: FakeHost, *,
                    enabled: bool = True) -> tuple[Tools, GroupSpace, Store, list[dict]]:
    store = _mk_store(tmp_path)
    tools = Tools(store)
    outbox_sent: list[dict] = []

    from CharTyr_MaiWork.maiwork.tools_groupspace import register_groupspace_tools

    settings = _Settings({GID: None}, enabled=enabled)
    gs = GroupSpace(host, store, lambda: settings)
    await gs.probe(now=NOW)

    async def announce(group_id: str, text: str) -> None:
        outbox_sent.append({"group_id": group_id, "text": text})

    register_groupspace_tools(tools, gs, announce=announce)
    return tools, gs, store, outbox_sent


async def test_tool_notice_happy_path(tmp_path: Path) -> None:
    tools, gs, store, sent = await _mk_tools(tmp_path, FakeHost(NEW_APIS, role="owner"))
    ctx = ToolContext(group_id=GID, actor="主模型", role="main")
    res = await tools.call("group_notice_send", {"content": "明天放假一天"}, ctx)
    assert res.ok, res.error
    assert sent and sent[0]["text"].startswith("我要发一条群公告：")
    # 写操作记了事件
    row = store.read().execute(
        "SELECT kind FROM events WHERE kind='group_space.notice_sent'"
    ).fetchone()
    assert row is not None


async def test_tool_notice_old_adapter_message(tmp_path: Path) -> None:
    tools, gs, store, sent = await _mk_tools(tmp_path, FakeHost(OLD_APIS, role="owner"))
    ctx = ToolContext(group_id=GID, actor="主模型", role="main")
    res = await tools.call("group_notice_send", {"content": "明天放假一天"}, ctx)
    assert not res.ok
    assert "适配器没开放" in res.error
    assert sent == []  # 预告都没发


async def test_tool_notice_member_message(tmp_path: Path) -> None:
    tools, gs, store, sent = await _mk_tools(tmp_path, FakeHost(NEW_APIS, role="member"))
    ctx = ToolContext(group_id=GID, actor="主模型", role="main")
    res = await tools.call("group_notice_send", {"content": "明天放假一天"}, ctx)
    assert not res.ok
    assert "不是群主或管理员" in res.error


async def test_tool_files_list_old_adapter(tmp_path: Path) -> None:
    tools, gs, store, sent = await _mk_tools(tmp_path, FakeHost(OLD_APIS, role="owner"))
    ctx = ToolContext(group_id=GID, actor="主模型", role="main")
    res = await tools.call("group_files_list", {}, ctx)
    assert not res.ok
    assert "适配器没开放" in res.error


async def test_tool_manage_not_own_file(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    tools, gs, store, sent = await _mk_tools(tmp_path, host)
    ctx = ToolContext(group_id=GID, actor="主模型", role="main")
    res = await tools.call(
        "group_file_manage", {"action": "delete", "file_id": "/f-not-mine"}, ctx
    )
    assert not res.ok
    assert "只能动我自己传" in res.error
    assert host.api_calls == []  # 没真调适配器


async def test_tool_rmdir_own_folder_only(tmp_path: Path) -> None:
    """工具层 rmdir：不是自己建的 → 中文拒绝；自己建的 → 放行。"""
    host = FakeHost(
        NEW_APIS,
        role="admin",
        results={"adapter.napcat.file.get_group_files_by_folder": _listing()},
    )
    tools, gs, store, sent = await _mk_tools(tmp_path, host)
    ctx = ToolContext(group_id=GID, actor="主模型", role="main")
    res = await tools.call("group_file_manage", {"action": "rmdir", "folder_id": "/not-mine"}, ctx)
    assert not res.ok
    assert "只能删我自己建的文件夹" in res.error
    assert "adapter.napcat.file.delete_group_folder" not in host.api_names()
    gs.register_folder_owned(GID, "/folder-mine", "我的文件夹", now=NOW)
    res = await tools.call("group_file_manage", {"action": "rmdir", "folder_id": "/folder-mine"}, ctx)
    assert res.ok, res.error
    assert host.api_calls[-1]["api_name"] == "adapter.napcat.file.delete_group_folder"


async def test_tool_manage_description_states_folder_rule(tmp_path: Path) -> None:
    """插件中心审核要求：工具说明写清「只能删自己建的、里面只有自己东西的文件夹」。"""
    tools, gs, store, sent = await _mk_tools(tmp_path, FakeHost(NEW_APIS, role="owner"))
    spec = next(s for s in tools.specs("main") if s["function"]["name"] == "group_file_manage")
    desc = spec["function"]["description"]
    assert "rmdir" in desc
    assert "自己建的" in desc
    assert "里面只有" in desc


async def test_tool_roles_main_only(tmp_path: Path) -> None:
    tools, gs, store, sent = await _mk_tools(tmp_path, FakeHost(NEW_APIS, role="owner"))
    # 主模型看得到这 4 个工具，子 agent 看不到
    main_names = {s["function"]["name"] for s in tools.specs("main")}
    worker_names = {s["function"]["name"] for s in tools.specs("worker")}
    for name in ("group_files_list", "group_file_manage", "group_notice_send", "group_album_upload"):
        assert name in main_names
        assert name not in worker_names


async def test_tool_disabled_config(tmp_path: Path) -> None:
    tools, gs, store, sent = await _mk_tools(tmp_path, FakeHost(NEW_APIS, role="owner"), enabled=False)
    ctx = ToolContext(group_id=GID, actor="主模型", role="main")
    res = await tools.call("group_notice_send", {"content": "x"}, ctx)
    assert not res.ok
    assert "群空间功能关着" in res.error


# ----------------------------------------------------------------------
# 非服务群：零调用
# ----------------------------------------------------------------------


async def test_unserved_group_zero_calls(tmp_path: Path) -> None:
    host = FakeHost(NEW_APIS, role="admin")
    gs, _ = _mk_group_space(tmp_path, host, groups=[GID])
    await gs.probe(now=NOW)
    caps = await gs.capabilities_async(OTHER, now=NOW)
    assert all(v is False for v in caps.values())
    with pytest.raises(PermissionError, match="不服务"):
        await gs.list_files(OTHER)
    with pytest.raises(PermissionError, match="不服务"):
        await gs.send_notice(OTHER, "x", announce=lambda *a: asyncio.sleep(0), now=NOW)
    # 没为非服务群发过任何 api.call、也没查身份
    assert host.api_calls == []
    assert host.role_calls == []


# ----------------------------------------------------------------------
# 健康状态文案（console/views.py._groupspace_health）
# ----------------------------------------------------------------------


async def test_health_text_old_adapter(tmp_path: Path) -> None:
    from CharTyr_MaiWork.maiwork.console import views

    store = _mk_store(tmp_path)
    settings = _Settings({GID: None})
    gs = GroupSpace(FakeHost(OLD_APIS, role="admin"), store, lambda: settings)

    class _Svc:
        def __init__(self, gs, store, settings) -> None:
            self.group_space = gs
            self.store = store
            self._settings = settings

        def get_settings(self):
            return self._settings

    item = views._groupspace_health(_Svc(gs, store, settings))
    assert item["key"] == "group_space"
    assert item["state"] == "warn"
    assert "旧版" in item["text"] and "v1.0.1" in item["text"]

    # 新版：ok
    gs2 = GroupSpace(FakeHost(NEW_APIS, role="admin"), store, lambda: settings)
    # 假装探测过了（直接写缓存）
    gs2._apis = set(NEW_APIS)
    gs2._probed = True
    item2 = views._groupspace_health(_Svc(gs2, store, settings))
    assert item2["state"] == "ok"
    assert "能管群文件" in item2["text"]

    # 没开（group_space=None）→ off
    class _Svc2:
        group_space = None

        def __init__(self, settings) -> None:
            self.store = None
            self._settings = settings

        def get_settings(self):
            return self._settings

    item3 = views._groupspace_health(_Svc2(settings))
    assert item3["state"] == "off"


async def test_group_view_group_space_admin_only(tmp_path: Path) -> None:
    from CharTyr_MaiWork.maiwork.console import views

    store = _mk_store(tmp_path)
    settings = _Settings({GID: None})
    gs = GroupSpace(FakeHost(NEW_APIS, role="owner"), store, lambda: settings)
    gs._apis = set(NEW_APIS)
    gs._probed = True
    await gs.capabilities_async(GID, now=NOW)  # 触发身份写缓存

    with store.tx() as conn:
        conn.execute(
            "INSERT INTO groups (group_id, workspace, token, created) VALUES (?, 't', 'tok1', 1)",
            (GID,),
        )

    class _Svc:
        def __init__(self) -> None:
            self.group_space = gs
            self.store = store
            self.profiles = None
            self.feeds = None
            self.topics = None
            self.goals = None
            self.tasks = None
            self.delivery = None
            self.approvals = None
            self.scheduler = None
            self.models = type("M", (), {"settings": staticmethod(lambda: type("S", (), {"ready": lambda self: False})())})()
            self.signals = type("Sig", (), {"last_ts": lambda self, g: 0.0})()
            self._settings = settings

        def get_settings(self):
            return self._settings

    svc = _Svc()
    admin_view = views.group_view(svc, GID, admin=True)
    assert "group_space" in admin_view
    gsp = admin_view["group_space"]
    assert gsp["role"] == "owner"
    assert gsp["files_list"] is True and gsp["files_manage"] is True
    assert gsp["notice_send"] is True and gsp["album_upload"] is True
    member_view = views.group_view(svc, GID, admin=False)
    assert "group_space" not in member_view


# ----------------------------------------------------------------------
# Host.list_apis 封装本身（SDK 解包键 apis）
# ----------------------------------------------------------------------


async def test_host_list_apis_unpack() -> None:
    """api.list 是独立能力（线上 components.py _cap_api_list 实读）：
    返回 {"success": True, "apis": [{"plugin_id", "name", "version", ...}]}；SDK 可能已解包成列表。"""
    entries = [{"plugin_id": "p", "name": "a", "version": "1"},
               {"plugin_id": "p", "name": "b", "version": "1"}]
    ctx = FakeCtx({"api.list": {"success": True, "apis": entries}})
    h = Host(ctx)
    assert await h.list_apis() == ["a", "b"]
    name, kw = ctx.calls[0]
    assert name == "api.list"
    assert "api_name" not in kw
    # SDK 已按 _CAPABILITY_RESULT_KEYS 解包成列表
    assert await Host(FakeCtx({"api.list": entries})).list_apis() == ["a", "b"]
    # 纯字符串名单也认
    assert await Host(FakeCtx({"api.list": ["x"]})).list_apis() == ["x"]
    with pytest.raises(HostError):
        await Host(FakeCtx({"api.list": 42})).list_apis()


def test_manifest_declares_api_list() -> None:
    """宿主按 manifest capabilities 逐项鉴权（authorization.py），没声明就调不了。"""
    import json
    from pathlib import Path
    m = json.loads((Path(__file__).resolve().parents[1] / "_manifest.json").read_text("utf-8"))
    assert "api.list" in m["capabilities"]


# ----------------------------------------------------------------------
# Host.call_adapter 通用透传
# ----------------------------------------------------------------------


async def test_host_call_adapter_passthrough() -> None:
    ctx = FakeCtx({"api.call": {"status": "ok", "retcode": 0, "data": {"x": 1}}})
    h = Host(ctx)
    out = await h.call_adapter("adapter.napcat.file.get_group_root_files", {"group_id": GID})
    assert out == {"x": 1}
    name, kw = ctx.calls[0]
    assert kw["api_name"] == "adapter.napcat.file.get_group_root_files"
    assert kw["version"] == "1"
    assert kw["args"] == {"params": {"group_id": GID}}  # 适配器 1.x 动作直通接口
    # retcode 非 0 → HostError
    ctx2 = FakeCtx({"api.call": {"status": "failed", "retcode": 100, "data": {}}})
    with pytest.raises(HostError):
        await Host(ctx2).call_adapter("adapter.napcat.group.send_group_notice", {})
