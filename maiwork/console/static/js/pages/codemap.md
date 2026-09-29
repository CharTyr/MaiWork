# console/static/js/pages/ — 群页面

## Responsibility

按当前群的 `GroupView` 生成资讯、构想、目标、任务和群资料的 HTML；这里是浏览器的**展示层**，不负责后台调度或权限判定。入口是上级 `render.js` 的 `renderView()`，依据 `state.tab` 调用对应 `view*()`。

## Design

- `news.js`：`viewNews(g, v)` 按时段展示资讯批次、被淘汰的候选与文章；复用 `emptyState()`、`loading()`、`fbButtons()`。`RATE_KEY` / `FB_KEY` 和 `clientId()` 管理本浏览器的评价/反馈标识，实际提交在上级的 `main.js` / `actions.js`。图解通过 `GET /api/news/{id}/viz` 按需取 HTML，装入仅 `allow-scripts` 的沙箱 iframe；`MutationObserver` 填充、`postMessage` 校正高度，内存缓存避免刷新时重取。
- `ideas.js`：`viewIdeas()` 和 `ideaDetail()` 展示构想及选项；`ideaAsk()` 拼出复制到群内交给 MaiBot 的要求，`findIdea()` 从当前群视图找构想。
- `goals.js`：`viewGoals()` 区分 MaiWork 推进的目标与群友的提醒，`nextText()` 解释下次检查时间。
- `tasks.js`：`viewTasks()` 展示待批、已开工和按状态筛选的任务；`taskRow()` 供列表与详情复用；待批构想项通过 `findIdea()` 交叉引用。
- `group.js`：`viewGroup()` 组织群状态、开话题记录、群画像、群空间能力及关注成员；管理员视图提供「往群里发」的每群开关。个人画像只在管理视图渲染，服务器仍需独立执行鉴权。

## Flow

`router.loadGroups()/loadView()` → `state.groups/state.view` → `render.renderView()` → `viewNews/viewIdeas/viewGoals/viewTasks/viewGroup` → 生成有 `data-act` 的按钮 → 上级 `actions.act()` / `events.js` 发 API 请求并重拉/重画。任务/目标/构想详情由上级 `detail.js` / `sheet.js` 接管；群页 `data-cp` 开关由 `events.js` 保存到 `PUT /api/groups/{id}/card-push`。展示用 `esc()` / `safeUrl()` 来处理数据文本和链接。

## Integration

- 上游：[JS 控制层](../codemap.md)（`state.js`、`router.js`、`render.js`、`actions.js`）；数据来自服务端 `GET /api/groups` 和 `GET /api/groups/{ref}`，不是此目录自行生成。
- 设置页的加载态、部分反馈组件复用 `news.js`；群页复用上级 `pulse.js`。
- 服务端路由与鉴权：[控制台后端](../../../codemap.md)；源码在 `console/server.py` 与 `console/views.py`。
