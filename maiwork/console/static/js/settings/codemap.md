# console/static/js/settings/ — 管理设置界面

## Responsibility

管理员设置页的 HTML 模板、表单读取和按需加载。导航由 `index.js` 汇总；实际按钮、上传、提交事件由上一级 `actions.js`、`events.js` 和 `main.js` 处理，服务端负责最终校验、鉴权和持久化。

## Design

| 文件 | 主要职责 |
|---|---|
| `index.js` | `SET_SUBS`、`settingsPage()` / `settingsSide()` 分发九个设置子页（用量 / 日志同页）；`loadGA()` 拉取群管理员资料，`groupPicker()` 供抽屉复用；总览读取 `state.settings` / `state.groups`。 |
| `models.js` | 端点/模型库管理、连接测试与模型验证，`draft.models` 保留旧引导共用列表；端点默认收起的「高级设置」按行编辑请求头覆盖，只取 `header_names`、值用 password 且不回显，已存行留空保留/删行移除；也可切到 JSON 方式（`mdl-header-mode`，默认只列已存名称且值为空；`parseHeadersJson()` 只收字符串/null 值，错误不带原文）；保存与测试同读 `readEndpointHeaders()`，两种方式共用 `checkedHeaders()`（大小写去重、非法字符/传输头校验）；增删行和切换方式都局部更新，不丢其他未保存输入。`fullLink()` 组群链接；`feedsSettings(feeds, groups)` 被资讯来源页复用——屏蔽名单 2026-10 起按群（`blocked_domains` / `auto_blocked` 都是 `{群号: [域名]}`），按群分行渲染、解除按钮带 `data-g`。 |
| `rules.js` | `loadRules()` 取 `/api/settings/config` 的分节字段元数据；`rulesPage()` 根据后端 `advanced` 标记把技术参数折进默认关闭的「高级」，再按字段类型渲染开关、账号 chips、服务群/SSH 行表格及密钥输入；`readRules()` 只提交变更字段。与 `onboarding.js` 共用 `chipEditor()` / `rowsEditor()`。 |
| `ext.js` | `loadExt()` 加载 MCP、skill、搜索绑定及 `/api/extensions/presets`；`extPage()` 将未打开的预设与普通 MCP 同列，已打开预设显示 logo、密钥入口、备用/广撒网标签。`searchCard()` 配主搜索、可另选的正文抓取、备用顺序和广撒网服务；后二者只列已启用预设，`checkedProviders()` 读勾选并排除主搜索。`parseMcpConfig()` 抽取粘贴的远程 HTTPS MCP；`readHeaders()` 和 `readRoles()` 读取表单。 |
| `identity.js` | 「记忆」页 + 头像小块（头像画在专岗页「主模型」栏）：`loadIdentity()` 取 `/api/identity`，只显示全局工作记忆（每群偏好已并进群页「这个群」的本群规矩）；`avatarBlock()` / `avatarSaved()` 管头像。SOUL / AGENTS 已搬到 `agents.js`（专岗页） |
| `jev.js` | 「模型」页里的「快速判断」块（`models.js` 用 `jevSection()` 嵌进来，`loadModels` 顺带 `loadJev()`）：`GET /api/settings/jev` 到 `state.jev`，列内置 TypeSafe + 自己加的判断服务（正在用的标「正在用」、内置和正在用的不给删）、没接的预设列在「还可以接：」；表单（`jv-*`）建/改，地址只收 https（本机可 http）、`{}` 占位没换掉不让存，新建必须填密钥、改的时候留空 = 保留；测试走 `POST .../{id}/test`，换用走 `PUT /api/settings/jev/use`。底部「没有 Jev 时怎么判断派活」（`qj-*`）显示 `quick_judge` 现值和今天判断次数，保存写 `PUT /api/settings/config`（`quick_judge.*` 四个键）。动作在 `actJev()`，`actions.js` 转过来。 |
| `sources.js` | RSS、优质来源与来源屏蔽界面。`trustedGroup(g)` 懒加载 `GET /api/groups/{id}/trusted-sources` 到 `state.trusted[id]`，显示高分条数、有用反馈和手动移出项；移出/放回由 `actions_news.js` 发 POST 并更新缓存。来源屏蔽复用 `models.js::feedsSettings(feeds, groups)`（按群列，解除走 `POST /api/groups/{gid}/feeds/domains`）。RSS 保留增删/启停按钮。 |
| `usage.js` | `loadUsage()` / `loadUsageDay()` 拉近 7/14/30 天与单日用量并画柱形/明细表；`usageAndLogsPage()` 在同页接上日志摘要。 |
| `logs.js` | `loadLogSummary()` 进页只取摘要；显式展开后 `loadLogs()` 分页读取模型 / 工具记录，按失败筛选；完整请求与回复再折叠一层，不默认展示原文。 |

## Flow

`router.enterSettings(sub)` → 需要时 `loadSettings()`，按子页再拉 rules/ext/identity/usage/logs/group-admin → 写 `state.*` → `render.renderView()` 调 `settingsPage()` → 渲染控件（`data-act` 或表单 ID） → `actions.act()` / `main.js` / `events.js` 发写入请求 → `repaintSheet()` 更新视图。例：保存服务群时 `main.js` 的 `rules-form` 发 `PUT /api/settings/config` → `loadSettings()` → `loadGroups()` → `applyHash()` → `render()`，从而同步首页群列表；首次引导见上级 `onboarding.js`。

## Integration

- 父模块：[浏览器控制层](../codemap.md)；页面复用 [群页面组件](../pages/codemap.md) 的 loading/反馈。
- 服务端：[控制台 API](../../../codemap.md)。密钥只在浏览器输入并提交，不通过 GET 回显；表单隐藏不构成权限边界。
- 数据出处：`/api/settings`、`/api/settings/config`、`/api/extensions`、`/api/extensions/search`、`/api/extensions/presets`、`/api/groups/{id}/trusted-sources`、`/api/usage/history`、`/api/logs/*`、`/api/identity`；预设打开/密钥提交用 `POST /api/extensions/presets/{id}`，首次引导批量设置另用 `POST /api/extensions/presets-setup`。
- `rules.js` 的字段元数据由后端生成，服务群行表单提示支持 `qq:群号` / `telegram:群 ID` / `qqbot:群 openid`；`index.js::groupPicker()` 通过 `platTag()` 标平台。
