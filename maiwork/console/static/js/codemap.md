# console/static/js/ — 浏览器应用

## Responsibility

真实控制台的原生 ES 模块（无前端构建步骤）。把服务端 JSON 映射为群/设置/对话页面，并把用户操作发回控制台 API；浏览器不是最终权限边界。

## Design

- `main.js` 是入口：挂 `click` / `submit` / `keydown` / `hashchange` 等事件，导入只注册输入事件的 `events.js`，最后调用 `reboot()`。按钮统一由 `data-act` 委托给 `actions.act()`；首次引导的 `onb-*` 交 `onboarding.onbAct()`。
- `state.js` 是单页应用的共享状态对象：身份 `me`、可见群 `groups`、当前群 `g`、`GroupView` 缓存 `view`、tab/detail/sheet、设置及对话；`admin()` / `gadmin()` 只控制浏览器显示。
- `api.js` 包装同源 `fetch`（JSON、cookie，群友链接时带 `X-MW-Group`），报错转为可显示的异常；`grp()` / `gview()` 保证当前视图对应选中群，`groupRef()` 区分管理员 ID 和群友 token。
- `router.js` 用 URL hash 导航：`parseHash()` / `applyHash()` / `syncHash()` / `go()`；`reboot()` 依次载入 `/api/me`、`/api/groups`，选择可见群并取其 `/api/groups/{ref}`，按需载入设置及详情。前台每 30 秒刷新当前群数据，避免越群覆盖；更新提醒单独定时检查。
- `render.js` 从状态生成顶栏、底栏、左栏、主视图和桌面右栏；`renderView()` 分发到 [五个群页面](pages/codemap.md)、[设置](settings/codemap.md) 或聊天；仅轮询时相同 HTML 不重画，以免图解 iframe 闪烁。空服务群提示依赖 `state.groups.length`。
- `actions.js` 处理 `data-act` 点击（群切换、反馈/评价、任务批准、设置及扩展操作等），`events.js` 处理输入、上传、群内推送开关和长名滚动；写操作之后按场景调用 `loadView()` / `loadSettings()` 和局部重画。
- `sheet.js` 复用单个抽屉展示登录、编辑、群列表、对话列表或详情；桌面详情改画进 `#side`。`detail.js` 读 `/api/tasks/{id}` 并组任务/目标/构想详情。
- `chat.js` 管理管理员对话的消息列表与未完成对话轮询；`onboarding.js` 引导模型、服务群、管理员和头像；`update.js` 显示只提示不自动安装的新版信息；`pulse.js` 画群状态概况；`util.js` 提供 HTML 转义、URL 检查、图标与北京时间格式化。

## Flow

`index.html` 加载 `main.js` → `router.reboot()` 获取身份及群列表 → `applyHash()` 选群/子页 → `loadView()` 获取群快照 → `render()` 选择五页/设置/对话 → 用户点击 `data-act` 或提交表单 → `api()` 写入 → 重新取数据并渲染。保存 `groups.serve` 的两个路径分别在 `main.js` 的 `rules-form` 与 `onboarding.js` 的 `onbSave()`：都调用 `loadGroups()` 更新服务群列表，不只刷新设置缓存。

## Integration

- [群页面](pages/codemap.md) ｜ [设置页面](settings/codemap.md) ｜ [静态入口及样式](../codemap.md) ｜ [服务端控制台](../../codemap.md)。
- 重要 API：`/api/me`、`/api/groups`、`/api/groups/{ref}`、`/api/settings`、`/api/settings/config`、`/api/chat/*`；后端的 `ConsoleServer` 负责权限/群隔离和存储。
- 数据以服务端结果为准；`localStorage` 只记当前浏览器的反馈、评价与客户端标识，不能当作服务端授权或真相。
