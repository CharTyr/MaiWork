# console/static/js/settings/ — 管理设置界面

## Responsibility

管理员设置页的 HTML 模板、表单读取和按需加载。导航由 `index.js` 汇总；实际按钮、上传、提交事件由上一级 `actions.js`、`events.js` 和 `main.js` 处理，服务端负责最终校验、鉴权和持久化。

## Design

| 文件 | 主要职责 |
|---|---|
| `index.js` | `SET_SUBS`、`settingsPage()` / `settingsSide()` 分发九个子页；`loadGA()` 拉取群管理员资料，`groupPicker()` 供抽屉复用；总览读取 `state.settings` / `state.groups`。 |
| `models.js` | 模型选择与只进不出的密钥表单，`draft.models` 暂存测试连接返回的模型列表；`fullLink()` 组群链接。`feedsSettings()` 被资讯来源页复用。 |
| `rules.js` | `loadRules()` 取 `/api/settings/config` 的分节字段元数据；`rulesPage()` 按字段类型渲染开关、账号 chips、服务群/SSH 行表格及密钥输入；`readRules()` 只提交变更字段。与 `onboarding.js` 共用 `chipEditor()` / `rowsEditor()`。 |
| `ext.js` | `loadExt()` 加载 MCP、skill 及搜索绑定；`extPage()` 绘制连接状态/配置表单；`parseMcpConfig()` 抽取粘贴的远程 HTTPS MCP；`readHeaders()` 和 `readRoles()` 读取表单。 |
| `identity.js` | 管理头像、SOUL、AGENTS、全局与每群工作记忆；`loadIdentity()` 取 API，`avatarSaved()` 更新界面。 |
| `sources.js` | RSS 与来源屏蔽界面，按群展示来源及增删/启停按钮。 |
| `usage.js` | `loadUsage()` / `loadUsageDay()` 拉近 7/14/30 天与单日用量并画柱形/明细表。 |
| `logs.js` | `loadLogs()` 分页读取模型/工具调用记录及摘要，按失败筛选；`logsPage()` 展示请求、回复和错误。 |

## Flow

`router.enterSettings(sub)` → 需要时 `loadSettings()`，按子页再拉 rules/ext/identity/usage/logs/group-admin → 写 `state.*` → `render.renderView()` 调 `settingsPage()` → 渲染控件（`data-act` 或表单 ID） → `actions.act()` / `main.js` / `events.js` 发写入请求 → `repaintSheet()` 更新视图。例：保存服务群时 `main.js` 的 `rules-form` 发 `PUT /api/settings/config` → `loadSettings()` → `loadGroups()` → `applyHash()` → `render()`，从而同步首页群列表；首次引导见上级 `onboarding.js`。

## Integration

- 父模块：[浏览器控制层](../codemap.md)；页面复用 [群页面组件](../pages/codemap.md) 的 loading/反馈。
- 服务端：[控制台 API](../../../codemap.md)。密钥只在浏览器输入并提交，不通过 GET 回显；表单隐藏不构成权限边界。
- 数据出处：`/api/settings`、`/api/settings/config`、`/api/extensions`、`/api/extensions/search`、`/api/usage/history`、`/api/logs/*`、`/api/identity`；`rules.js` 的字段元数据由后端生成。
