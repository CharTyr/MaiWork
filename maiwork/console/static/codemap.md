# console/static/ — 真实控制台静态资源

## Responsibility

为控制台提供 HTML 骨架、响应式样式、原生 JS ES 模块和图标。区别于根目录 `prototype/console/` 的虚构演示：这里通过后端 API 读取真实群数据并提交设置。

## Design

- `index.html` 只放 `#rail`、`#main`（内含 `#top` / `#view`）、`#side`、`#tabbar`、`#sheet`、`#scrim`、`#toast` 等容器；`<script type="module" src="/static/js/main.js">` 启动应用。
- `style.css` 定义颜色/字号等 CSS 变量和布局；小屏单栏加底部标签栏，`min-width: 760px` 显示左栏，`min-width: 1180px` 显示右栏详情；覆盖深色模式和 `prefers-reduced-motion`。图解 iframe、引导、聊天和设置的样式也集中于此文件。
- `style.css` 还集中定义预设搜索服务 logo/密钥表单、备用与广撒网勾选项、首次引导搜索步骤、两段式漏斗、口味及后续标签的样式；平台标签区分 Telegram 与 QQ 官方机器人。
- `assets/` 存放图标和头像等图片文件，仅作静态素材，未进入代码哈希清单；`assets/logos/` 为六家预设搜索服务提供标识，出处见 [SOURCES.md](assets/logos/SOURCES.md)。逻辑集中在 [JS 模块地图](js/codemap.md)。

## Flow

`ConsoleServer` 响应 `GET /` 提供 HTML，并为引用的静态脚本/样式生成缓存版本参数；浏览器加载 `main.js` → 发 `/api/*` → `render.js` 把 HTML 填入骨架节点 → `style.css` 依屏宽/系统偏好布局。静态文件不直接读服务端数据文件。

## Integration

- 服务端路由、静态响应与鉴权见 [控制台后端](../codemap.md)；前端模块职责、页面路由和请求路径见 [JS 地图](js/codemap.md)。
- [原型界面](../../../../../prototype/console/codemap.md)是无服务器、用虚构数据演示的独立页面；不要把它的前端角色切换当成真实鉴权。
