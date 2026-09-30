# plugin/CharTyr_MaiWork/maiwork/platforms/

上级地图：[../codemap.md](../codemap.md)。平台差异设计依据：[设计文档](<../../../../docs/02-设计.md>) §10、[宿主接口事实](<../../../../docs/06-宿主接口事实.md>)「QQ 适配器与群空间接口」。

## Responsibility

平台档案层：把「某个平台/协议端的差异」隔离在这一层，核心代码（app/coordinator/tools）只跟平台无关的接口打交道。目前只有一个平台档案：

- `__init__.py` — 只有一行 docstring，声明这层是各平台/协议端差异的安放处（[设计文档](<../../../../docs/02-设计.md>) §10）。
- `qq_onebot.py` — QQ / NapCat-OneBot 平台档案，唯一实现：`GroupSpace` 类（群文件管理、群公告、群相册）。`GroupSpace` 被 `app.py` 构造、被 `tools_groupspace.py` 包成主模型工具、被 `coordinator.py` 在交付前的小回合里用，被网页 GroupView / 健康检查读能力。

## Design

**能力开关模型（接口 × 身份两道闸）。** 线上适配器可能还是旧版（0.8.5，无群文件/公告/相册接口），所以 GroupSpace 不假设接口存在：

- 启动时 `probe()` 调 `host.list_apis()`（宿主 `api.list`）拿适配器已开放的 API 名集合，缓存 6 小时（`PROBE_TTL_S`）；探测失败只记日志不抛（不影响启动），且失败也记探测时间，6 小时内不重试，避免宿主未就绪时反复打。
- 机器人在某群的身份（owner/admin/member）经 `host.bot_qq()` + `host.group_member_role()` 现查，缓存 1 小时（`ROLE_TTL_S`）。
- 能力键 `CAPABILITY_KEYS = files_list / files_manage / notice_read / notice_send / album_list / album_upload`：各键 = 「适配器接口在名单里」且（管理系还需）「身份 ∈ {owner, admin}」。两种 API 都满足才开放，error message 一律中文，对管理员可读。
- 适配器 API 名是模块级常量（`API_*`，如 `adapter.napcat.file.get_group_root_files`）；`api.call` 的参数名（`group_id` / `file_id` 等）也是可配置常量并标〔待实测〕——升到 v1.0.1 后只对 constants，不动逻辑。

**防手滑（只动自己传的）。** 删除/改名/移动只许动机器人自己上传的文件、只删自己建且内容全自有的文件夹：

- `register_owned`（outbox 群文件上传成功钩子）把 `(group_id, file_id)` 写进 `group_files_owned` 表；`register_folder_owned`（create_folder 成功后）写 `group_folders_owned` 表。两表均在 store.py 的 migration 里建（`_m_group_space`、`_m_group_folders`）。
- 动手前 `_require_own` / `delete_folder` 的内容逐条校验 file_id / folder_id 在表里，否则 `PermissionError`（中文原因）。登记本身失败只记日志，不拖垮发送/建目录流程（最坏后果：该文件以后不能删）。
- `_owned(...)` 的 SQL 中表名/列名是代码写死的常量，不是外部输入（注释里明确说明）。

**非服务群零调用。** `_check_served` 兜底所有入口：群不在 settings.is_served 名单里 → `PermissionError`，连身份/接口都不查（符合设计红线「非服务群零读取、零调用」）。

## Flow

**启动（app.start，app.py:451-471）**：`_make_group_space()` 读 `[group_space] enabled`（false → None）；建成 `GroupSpace(host, store, get_settings)` 后：① `outbox.set_group_file_hook(register_owned)` 挂防手滑登记；② 首次 `probe()` 探测接口名单（失败不影响启动）；③ `register_groupspace_tools(tools, group_space, announce=..., get_settings=...)` 注册 4 个主模型工具。任一步异常只记日志，群空间跳过、健康页可见。

**运行时巡检（app._m3_round，app.py:2306-2310）**：每轮调 `probe(now=now)`，TTL 未到直接返回，到了才真正 `api.list`。

**主模型用群空间（coordinator._groupspace_round）**：验收通过、交付前，若 group_space 存在且 `capabilities_async(gid)` 有任一键为真，给主模型开一个至多 `_GROUPSPACE_TOOL_LIMIT = 4` 轮工具调用的小回合；只给该群能力允许的工具（`_GROUPSPACE_TOOL_CAPS` 映射 tool→cap 键）；全 False → 连模型都不叫。小回合出错不影响交付。

**操作三道闸（每条写路径）**：`_require(group, cap)` = 服务群闸 + `[group_space] enabled` 总开关 + 接口在名单（admin 系再查身份）；写操作前再过 `_require_own` / 内容校验。公告特殊：`send_notice` 先经 `announce` 回调（app 接 `outbox.enqueue`，`push_kind="status"`，受推送节制且按群+内容 sha1 去重）在群里说「我要发一条群公告：<前 30 字>」，再调适配器；每群每天上限由 `_notice_limit()`（`[group_space] notice_per_day`，默认 1）按 `group_space.notice_sent` 事件数（北京时间 day_key 比对）控制。

**事件留痕**：每个写操作调 `_write_event(kind, ...)` 写 `events` 表（`entity="group_space"`），kind 形如 `group_space.file_registered / file_deleted / notice_sent / album_uploaded …`。事件写入失败只记日志，不做挂已完成的主操作；公告计数那条走「调用方传当时的 ts」的专用 INSERT 路径，保证每日上限数得准。

**网页读（console/views.py）**：GroupView 仅管理员可见 `group_space` 块（`capabilities` 同步版 + `role_of` 缓存身份）；健康页 `_groupspace_health` 用 `adapter_open()`（`API_GET_ROOT_FILES` 与 `API_SEND_NOTICE` 都在名单里才算开放）区分 off / ok / warn（旧版适配器提示升级）。

## Integration

- **host.py**：`list_apis()`（api.list）、`bot_qq()`（config.get bot.qq_account，带缓存）、`group_member_role()`（`adapter.napcat.group.get_group_member_info`）、`call_adapter(api_name, args)`（api.call 透传，retcode 非 0 抛 `HostError`）。GroupSpace 对宿主的全部依赖就这四个异步方法 + get_settings 回调。
- **outbox.py**：`set_group_file_hook(register_owned)`——群文件上传成功后登记 `group_files_owned`。
- **tools_groupspace.py**：把 GroupSpace 包成 `group_files_list / group_file_manage(delete/rename/move/mkdir/rmdir) / group_notice_send / group_album_upload` 四个工具，`roles={"main"}`（子 agent 拿不到）；PermissionError 原样返回中文错误给主模型；`group_album_upload` 的 path 经 `tools_admin._check_upload_path` 校验必须在该群工作区内、非符号链接（防路径注入把服务器任意文件传进群相册）。
- **coordinator.py**：交付前小回合（见 Flow）。
- **config.py**：`[group_space]` 节（`enabled` 默认 true、`notice_per_day` 默认 1，rules.py 网页可改，上限 5/天）。热更时 app `_reconcile_group_space` 关掉就摘组件、打开就重建并重挂 hook/probe/tools。
- **store.py**：表 `group_files_owned` / `group_folders_owned`（migration 建），事件表 `events`。
- **console/views.py**：GroupView（仅管理员）与健康页读取（见 Flow）。

## 隔离与错误行为

- 所有调用方都把 GroupSpace 当「可能有、可能坏」的可选模块：构造失败 / enabled=false → app.group_space = None，coordinator 的小回合直接返回，网页健康显示 off。
- 本模块自己的约定：准入失败抛 `PermissionError`（中文原因），宿主/适配器失败冒泡为 `HostError`（tools 层 catch 后变成 ToolResult.error），登记/事件等副作用失败只记日志不回滚主操作。
- 零影响非服务群：所有入口先过 `_check_served`。

## 不确定 / 〔待实测〕

- `api.call` 各接口的参数名（`ARG_FOLDER` 与 `ARG_FOLDER_ID` 之别、相册上传的 `image` vs `file`）、建文件夹返回体字段名（代码试 `folder_id/folderId/folder/id` 四个）、相册列表返回结构——均按 NapCat 通行写法写成常量并标〔待实测〕，升级 v1.0.1 后需实测核对。
- `refresh_role()`（「网页 GroupView 在拿缓存前先调一次」）在仓库内**没有调用方**（仅有定义和 `capabilities` docstring 里的一处提及）：要么前端尚未接，要么属遗留接口，待确认。
- `role_of()` 里有一行不可达代码（`return self._role_cached(group_id)` 之后的 `return hit[1] ...`，hit 未定义，永不会执行）——无害但属死代码。
- 适配器版本阈值（v1.0.1「44 个 API」）来自文件 docstring 引用的[宿主接口事实](<../../../../docs/06-宿主接口事实.md>)，未在本仓库代码中验证。
