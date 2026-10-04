# console/static/js/pages/ — 群页面

## Responsibility

按当前群的 `GroupView` 生成资讯、构想、在做的事和群资料的 HTML；这里是浏览器的**展示层**，不负责后台调度或权限判定。入口是上级 `render.js` 的 `renderView()`，依据 `state.tab` 调用对应 `view*()`。

## Design

- `news.js`：`viewNews(g, v)` 按时段展示资讯批次、被淘汰的候选与文章；复用 `emptyState()`、`loading()`、`fbButtons()`。`RATE_KEY` / `FB_KEY` 和 `clientId()` 管理本浏览器的评价/反馈标识，实际提交在上级的 `main.js` / `actions.js`。首个来源链接走 `/go/{id}?c={clientId}`，群友链接另带 `g` token，由服务端鉴权、记点击并重定向；其他来源仍直链。
- 资讯 `followup` 显示「后续」、旧标题 `of_title` 和新增事实 `new_fact`；淘汰候选的 `src.query/provider` 显示搜索出处。`batchStats()` 兼容旧批次和 `stats.funnel`；`funnelBlock()` 仅全局管理员可展开搜索→线索→预筛→挑选→打开→核对→上网页各步计数，并列出 `per_focus`、`providers`、`rejects`、`timings_s`（包括保底补搜耗时）和「中文和外文来源」（`langMix()` 读 `query_langs` / `discovered_langs` / `kept_langs`，旧批次没有就不显示）。没有留下资讯的批次也可看漏斗。
- 资讯页顶上的「这个群想看」和「口味小结」已删（2026-10，docs/17 §八）：并进群页「这个群」区的本群规矩 / 资讯做法。原来按群切两段式新找法的 `twoPhaseBtn()`（`feeds-two-phase`）已删：2026-09-30 起两段式恒生效，没有开关。
- 图解通过 `GET /api/news/{id}/viz` 按需取 HTML，装入仅 `allow-scripts` 的沙箱 iframe；`MutationObserver` 填充、`postMessage` 校正高度，内存缓存避免刷新时重取。
- `ideas.js`：`viewIdeas()` 和 `ideaDetail()` 展示构想及选项；`ideaAsk()` 拼出复制到群内交给 MaiBot 的要求，`findIdea()` 从当前群视图找构想。
- `goals.js`：`goalSections()` 在「在做的事」里区分 MaiWork 推进的目标与群友的提醒，`nextText()` 解释下次检查时间。
- `tasks.js`：`viewTasks()` 展示「在做的事」：待批、按状态筛选的任务、持续目标与成员提醒；`taskRow()` 供列表与详情复用；待批构想项通过 `findIdea()` 交叉引用。
- `group.js`：`viewGroup()` 组织群状态、开话题记录、「这个群」区（只给总管理员 / 本群群管理员，`ctxBlock()` → `groupctx.js`）、群画像、群空间能力及关注成员；管理员视图接上 `controlsSection()` → `groupcontrols.js`（「往群里发」+「谁能批本群的活」）——批准 / 免批名单只读、总管理员才显示「改名单」。通过 `gplat()` / `platTag()` 区分平台；非 `qq` 群的 `groupSpaceBlock(v, g)` 不画 OneBot 群文件/公告/相册控件，而说明成品走网页链接。个人画像只在管理视图渲染，服务器仍需独立执行鉴权。

## Flow

`router.loadGroups()/loadView()` → `state.groups/state.view` → `render.renderView()` → `viewNews/viewIdeas/viewTasks/viewGroup` → 生成有 `data-act` 的按钮 → 上级 `actions.act()` / `events.js` 发 API 请求并重拉/重画。任务/目标/构想详情由上级 `detail.js` / `sheet.js` 接管；群页「往群里发」/批准名单保存走 `PUT /api/groups/{id}/push` 和 `PUT /api/groups/{id}/approval`，本地草稿跟着输入走、轮询重画不吞字。展示用 `esc()` / `safeUrl()` 来处理数据文本和链接。

## Integration

- 上游：[JS 控制层](../codemap.md)（`state.js`、`router.js`、`render.js`、`actions.js`）；数据来自服务端 `GET /api/groups` 和 `GET /api/groups/{ref}`，不是此目录自行生成。
- 设置页的加载态、部分反馈组件复用 `news.js`；群页复用上级 `pulse.js`。
- 服务端路由与鉴权：[控制台后端](../../../codemap.md)；源码在 `console/server.py` 与 `console/views.py`。
- `groupctx.js`：群页「这个群」区（docs/17 §八.4）。`ctxSection(gid)` 懒加载 `GET /api/groups/{gid}/rules` + `/skills`，先放 `.gctx-slot` 占位，回来后 `repaintCtx()` 原地填；状态在 `state.gctx`（换群整份换掉，编辑中的文字存 `drafts`，整页重画不丢）。本群规矩：看 / 改（PUT，≤3000 字）/ 历史回退；本群做法：资讯 / 构想 / 目标 / 自定义岗每岗一份 + 通用执行列表，改、锁定、归档 / 恢复、删除、历史回退。`actCtx()` 处理 `gctx-*` 点击，写之前核对群没换。
