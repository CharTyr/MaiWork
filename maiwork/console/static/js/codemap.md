# console/static/js/ — 浏览器应用

## Responsibility

真实控制台的原生 ES 模块（无前端构建步骤）。把服务端 JSON 映射为群/设置/对话页面，并把用户操作发回控制台 API；浏览器不是最终权限边界。

## Design

- `main.js` 是入口：挂 `click` / `submit` / `keydown` / `hashchange` 等事件，导入只注册输入事件的 `events.js`，最后调用 `reboot()`。按钮统一由 `data-act` 委托给 `actions.act()`；首次引导的 `onb-*` 交 `onboarding.onbAct()`。
- `state.js` 是单页应用的共享状态对象：身份 `me`、可见群 `groups`、当前群 `g`、`GroupView` 缓存 `view`、tab/detail/sheet、设置及对话；页面模块按需挂自己的按群缓存（`gctx` 规矩 / 做法、`groupControls` 往群里发 / 批准、`trusted`、`openFunnel`、`extPresets`），每份都绑 `{gid, role}` 并带 `sameGroup` / `current()` 判定，换群或换身份整份作废，慢请求回来不覆盖新群。`admin()` / `gadmin()` 只控制浏览器显示。
- `api.js` 包装同源 `fetch`（JSON、cookie，群友链接时带 `X-MW-Group`），报错转为可显示的异常；`grp()` / `gview()` 保证当前视图对应选中群，`groupRef()` 区分管理员 ID 和群友 token。
- `router.js` 用 URL hash 导航：`parseHash()` / `applyHash()` / `syncHash()` / `go()`；`reboot()` 依次载入 `/api/me`、`/api/groups`，选择可见群并取其 `/api/groups/{ref}`，按需载入设置及详情。前台每 30 秒刷新当前群数据，避免越群覆盖；更新提醒单独定时检查。**失败态与重试（2026-10-12）**：`state.loadErr` 按块记失败原因（view / settings / rules / ext / usage / identity / chat；成功即删，`reboot()` 清空），`retryLoad(el)` 按按钮上的 `data-what` 重取那一块（`trusted` 按群懒加载，删掉 `state.trusted[群号]` 后重画即重取）；没登录却带着群号 / 设置 / 聊天地址进来 → `linkProblem(h)` 判 `state.badLink = "expired"`（管理员登录过期），群友链接码失效仍是 `"link"`。
- `render.js` 从状态生成顶栏、底栏、左栏、主视图和桌面右栏；`renderView()` 分发到 [四个群页面](pages/codemap.md)、[设置](settings/codemap.md) 或聊天；仅轮询时相同 HTML 不重画，以免图解 iframe 闪烁。左栏（`renderRail()`）用 `WeakMap` 记录上次渲染的 HTML，相同内容直接跳过，避免群头像重复加载和闪烁。空服务群提示依赖 `state.groups.length`。页面没内容时画 `loading(原因, what, 群)`（`pages/news.js`）：没原因 = 三颗呼吸点，给了原因就原地显示「加载失败 + 重试」；群视图**有旧内容但刷新失败**时顶部加「连接中断，正在重试」提示；落地页按 `state.badLink` 分「登录已过期，请重新登录」/「链接已失效…」两种文案。
- `actions.js` 处理 `data-act` 点击（群切换、反馈/评价、任务批准、设置及扩展操作等）；`act()` 先调用新增的 `actions_news.js::actNews(a, el)`，返回 `true` 即不再进入通用分支。资讯专用分支负责预设搜索服务打开/改密钥、优质来源移出/恢复、漏斗展开；再问 `pages/groupcontrols.js::actControls()`（群页「往群里发」/「谁能批本群的活」的 `gctl-*`）和 `pages/groupctx.js::actCtx()`（群页「这个群」区的 `gctx-*`）。`search-save` 同时提交主搜索、正文抓取、`fallback` 与 `broad`；`checkedProviders()` 去掉主搜索自身。
- `events.js` 处理输入、上传、群内推送开关和长名滚动；写操作之后按场景调用 `loadView()` / `loadSettings()` 和局部重画。资讯新增缓存按群存在 `state.trusted`，漏斗展开批次存在 `state.openFunnel`，预设列表存在 `state.extPresets`。
- `sheet.js` 复用单个抽屉展示登录、编辑、群列表、对话列表或详情；桌面详情改画进 `#side`。`detail.js` 读 `/api/tasks/{id}` 并组任务/目标/构想详情；任务暂停块认四种 `paused_reason.kind`——`tokens` / `time` / `tokens_unknown`（2026-10-12：「暂时无法核算本任务的用量（上限 N token），为防止超支已暂停」）/ `capability`。
- `handoff.js` 是交接包（docs/24）：构想 / 任务详情底部「交给我的 agent」→ `openHandoff()` 取 `GET /api/handoff/{idea|task}/{id}`（构想带当前勾选的 `items`；`seq` 挡住慢请求）→ `sheet.js` 画 `handoffSheet()` 预览；复制（剪贴板不可用时选中全文）/ 下载（浏览器里存成 `.md`）后 `POST .../taken` 记一次，失败静默。手机上从详情抽屉打开的，关掉回到详情（`state.handoff.back`）。「被带走 N 次」只在 `gadmin()` 下显示。
- `goal-chat.js` 管理目标详情的持久管理员对话：按群和目标缓存草稿 / 历史 / 待确认操作，调用 `/api/goals/{id}/chat` 并在有未完成回合时轮询；工具确认复用 `/api/chat/pending/{pid}`。会话变化后同步群视图和目标详情，异步结果守群 / 目标边界；`preserveGoalChatInput()` 供桌面右栏和手机详情共用，保留焦点 / 选区，输入法组合中推迟重画。入口仅总管理员可见，最终权限仍由后端检查。
- `board.js` 是看板（2026-10-30 改版，只总管理员、只看不动）：`loadBoard()` 取 `GET /api/board`，按 `lanes` 每个专岗一栏平铺（主模型 / 资讯 / 构想 / 目标 / 通用任务 / 开话题 / 自定义专岗；栏头是该岗小鱼，种子规则同 `api.js::agentFish`），栏内分「进行中」（超过 10 分钟没进展标橙，带最近一步）和「历史」（最近 N 小时）；顶部可按群筛选（`state.boardFilter`）。点卡片（`board-item`）开抽屉 `boardItemSheet()`，`loadBoardItem()` 取 `GET /api/board/items/{source}/{id}`，按时间画过程：工具（名字 / 耗时 / 成败，输入输出折叠，展开状态存 `state.boardOpen`）、思考（模型回复文字；线上没存思考链）、状态；`link === "time_window"` 时提示过程是按同群同时段推断的。**小鱼 `fishOf(kind)` 只读 `lanes[].fish_seed`**（`state.board.agent_fish` 字段已随「running/agents/recent」四段一起删掉，不再兜底）。路由 `#/board`（`router.js::enterBoard()`），停在看板时每 10 秒刷新列表、抽屉开着就按 `?after=` 增量补步骤；看板页 `body.on-board` 放开 620px 限宽并去掉右栏。入口在左栏底部和手机顶栏左侧第二个圆钮。
- `chat.js` 管理管理员对话的消息列表与未完成对话轮询；`onboarding.js` 引导模型、服务群、可选密钥、联网搜索、管理员和头像。搜索步骤懒加载 `/api/extensions/presets`，按预设清单顺序提交勾选项到 `/api/extensions/presets-setup`（第一家主搜索，其余备用，而非点击先后顺序），必填密钥先在表单校验，实际激活与秘密保存由后端完成。
- `onboarding-social.js` 在搜索之后提供可跳过的社交平台勾选步骤，进入时懒加载 `/api/social/platforms`，下一步逐个启停变更项；安装由后端异步处理，部分成功后的重试不重复提交。Cookie 统一引导到「设置 → 社交平台」中各平台行的「填写登录态」按钮，填写抽屉内附有平台特定的获取步骤。
- `social-brand.js` 只做一件事：`socialLogo(id)` 把已登记平台映射到 `/static/assets/logos/social/<id>-128.png` 本地品牌图，未知 ID 回退通用图标——设置页、登录态页和首次引导共用，避免三处各写一份白名单；图片是装饰性的（`alt=""`），平台名仍在旁边以文字呈现。
- `main.js` 的 `edit` 提交管群画像条目和关注成员；口味小结 / 资讯偏好的编辑已删（并进群页「这个群」区，见 `pages/groupctx.js`）。
- `api.js::gplat()` / `platTag()` 根据后端 `platform` 显示 Telegram / QQ 官方标识（旧数据缺省 `qq`），用于左栏、群选择和群页；`update.js` 显示只提示不自动安装的新版信息；`pulse.js` 画群状态概况；`util.js` 提供 HTML 转义、URL 检查、图标与北京时间格式化，以及「ⓘ」说明提示 `tip(html)` / `tipLabel(forId, text, html)`（`<details class="tip">`；调用方负责转义内容；不能放进别的 `<summary>`）；`events.js` 负责提示同一时间只开一个、点外面或 Esc 关闭、贴边时横向挪回屏内。说明性文字一律收进提示，状态、警告、错误和空状态留在原处（见 `design.md` §8.5 / §11，`tests/test_ui_copy_formal.py` 守口语词）。
- 主题与音效（design.md §4.3）：`theme_core.js` 纯数据（`THEMES`、`resolveTheme(个人, 全站默认)`、localStorage 键 `mw-theme` / `mw-theme-default` / `mw-sound`）；`theme.js` 拉 `GET /api/theme`、设 `<html data-theme>`（默认主题不设）、「外观」抽屉与 `actTheme()`；`sound.js` 按主题绑定的音效包用 WebAudio 放 hover / click / open / close / success / error，只响应真实操作，`sfx()` 由 `sheet.js` 开关抽屉和 `util.js::toast()` 调用；首屏防闪是 `index.html` `<head>` 的内联脚本。

## Flow

`index.html` 加载 `main.js` → `router.reboot()` 获取身份及群列表 → `applyHash()` 选群/子页 → `loadView()` 获取群快照 → `render()` 选择四页/设置/对话 → 用户点击 `data-act` 或提交表单 → `api()` 写入 → 重新取数据并渲染。保存 `groups.serve` 的两个路径分别在 `main.js` 的 `rules-form` 与 `onboarding.js` 的 `onbSave()`：都调用 `loadGroups()` 更新服务群列表，不只刷新设置缓存。

## Integration

- [群页面](pages/codemap.md) ｜ [设置页面](settings/codemap.md) ｜ [静态入口及样式](../codemap.md) ｜ [服务端控制台](../../codemap.md)。
- 重要 API：`/api/me`、`/api/groups`、`/api/groups/{ref}`、`/api/settings`、`/api/settings/config`、`/api/chat/*`；资讯相关新增 `/api/extensions/presets`、`/api/extensions/presets/{id}`、`/api/extensions/presets-setup`、`/api/groups/{id}/trusted-sources`、`/api/groups/{id}/rules*` / `/api/groups/{id}/skills*`（本群规则 / 本群做法）、`/api/groups/{id}/push`（每群「主动发言」；旧 `card-push` 是别名）、`/api/groups/{id}/approval`（每群「任务审批」名单）、`/api/groups/{id}/feeds/domains`（按群屏蔽来源）和 `/go/{id}`。后端的 `ConsoleServer` 负责权限/群隔离和存储，不能从前端按钮可见性推定接口授权。
- 数据以服务端结果为准；`localStorage` 只记当前浏览器的反馈、评价与客户端标识，不能当作服务端授权或真相。
