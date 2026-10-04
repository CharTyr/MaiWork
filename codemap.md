# plugin/CharTyr_MaiWork/ — MaiBot 插件发布单元

## Responsibility

MaiWork 的插件根目录。包含宿主插件声明、生命周期入口、示例配置和全部业务代码；部署时这一整个目录才对应 MaiBot 的 `plugins/CharTyr_MaiWork/`（本仓库只是**本地开发目录**，读写它不等于线上部署）。核心实现见 [maiwork/codemap.md](maiwork/codemap.md)。

## Design

- `_manifest.json` 声明 `chartyr.maiwork`、SDK/宿主兼容区间、依赖、宿主能力及图标；`plugin.py` 的 `PLUGIN_ID` / `PLUGIN_VERSION` 与清单保持一致。
- `plugin.py`：`MaiWorkPlugin(MaiBotPlugin)`，`config_model = MaiWorkConfig`；`create_plugin()` 是实例工厂。`on_load()` 仅在 `plugin.enabled` 时构建并启动 `MaiWorkApp`，`on_unload()` 调 `stop()`，`on_config_update(scope="self")` 热应用配置或首次启动。缺少宿主上下文或启动失败时记录日志，不影响插件加载。
- 两个 `@HookHandler` 钩子：`chat.receive.after_process` 将消息交 `app.on_message()`；`maisaka.planner.before_request` 将可提起备忘交 `app.on_planner_before_request()`。**这里的 planner 专指 MaiBot 回复管线，不是 MaiWork 内部规划器**；两钩子出错均返回 `{"action":"continue"}`，不抢宿主消息。
- `__init__.py` 暴露插件类和版本；兼容只导入包内模块的测试环境。
- `config.example.toml` 是默认关闭的配置样本：服务群以 `[[groups.serve]] group = "qq:号码"` 配置；**「往群里发」和「谁能批本群的活」不在这里**（0.8.0 每群一份，存数据库、在网页群页里改）；旧的 `[topics] enabled / per_day`、`[delivery] push_per_day / quiet_hours`、`[approval] required / admins / exempt_*` 只作**新群第一次的迁移种子**。生产密钥写运行时配置，`config.toml` 不进仓库，也不纳入地图扫描。

## Flow

MaiBot 加载清单和 `create_plugin()` → `MaiWorkPlugin.on_load()` 获取配置 → 启用时 `MaiWorkApp.start()` 挂接存储、后台调度与网页服务；消息/备忘钩子只转发到 app。热配置 `on_config_update()` → `MaiWorkApp.update_config()`；卸载 `on_unload()` → `MaiWorkApp.stop()`。消息去向、任务/资讯/交付和隔离细节见 [业务包地图](maiwork/codemap.md)。

## Integration

- 宿主边界：`maibot_sdk.MaiBotPlugin` / `HookHandler`；真实宿主能力封装在 `maiwork/host.py`，先查 [宿主接口事实](../../docs/06-宿主接口事实.md)，不要猜 SDK 行为。
- [maiwork/codemap.md](maiwork/codemap.md) 向下索引 console、environments、platforms；[测试目录](tests/) 为本地单测，[宿主契约测试](tests_host/) 仅在授权的安全环境中运行，测试和文档没有进入哈希跟踪。
- [开发入门](../../docs/00-开发入门.md) 给目录与测试入口；本地 `plugin/` 和线上路径的热重载风险见根目录 [AGENTS.md](../../AGENTS.md)。
