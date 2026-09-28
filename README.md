# MaiWork

补全 MaiBot 的群聊生产力能力：为 MaiBot 提供一个并行的完整 Agent，理解一个群，主动找资讯、提构想、追目标，交付任何工作。

它是 MaiBot 的一个插件（插件 ID `chartyr.maiwork`，目录 `CharTyr_MaiWork`），和 MaiBot 并行运行：从群聊里理解一个群，主动为这个群找资讯、提构想、追目标，也接群友派的活（按配置经管理员批准）；结果放到网页、传到群文件，或让 MaiBot 在聊天里顺口提起。灵感来自 Muse 的资讯 / 构想 / 目标三页。

## 从这里开始

1. 先读 `AGENTS.md`：开发规则和红线，**动线上之前必读**。
2. 按顺序读文档：

| 文件 | 内容 |
|---|---|
| `docs/01-需求.md` | 要做成什么样、硬约束、暂不做什么 |
| `docs/02-设计.md` | 完整设计 v6.0（唯一设计依据，含已拍板决定、与 v5.4 的差异和 M0 实测结果） |
| `docs/03-线上环境.md` | 线上 MaiBot 在哪、怎么连、热重载的坑、执行用户、测试群 |
| `docs/04-部署与验证.md` | 本地环境、宿主契约测试、部署、部署后检查、卸载 |
| `docs/05-开发计划.md` | 分阶段任务和验收标准，**当前从「阶段 0」开始** |
| `docs/06-宿主接口事实.md` | 已核实的 MaiBot 钩子和能力用法 |
| `docs/07-代码接口.md` | 模块之间的接口约定、网页接口和返回结构 |
| `docs/08-待你同意的事.md` | 要你同意的部署步骤、要你决定的事（每个阶段一节） |

3. 跑一遍测试确认环境：

```bash
cd plugin/CharTyr_MaiWork
~/.venvs/maiwork/bin/python -m pytest tests -q -p no:cacheprovider     # 本地单测
# 宿主契约测试见 docs/04-部署与验证.md 第二节（在服务器上跑，不影响线上）
```

## 目录

```text
AGENTS.md                  开发规则（给 agent 看）
docs/                      需求、设计、环境、部署、计划、接口
plugin/CharTyr_MaiWork/    插件代码（部署时整个目录放到线上 plugins/ 下）
  plugin.py                入口：两个钩子（收消息、给 MaiBot 加备忘），默认关闭
  app.py                   把各模块接起来、后台循环
  console/                 网页（后端 + static/ 前端）
  tests/                   本地单测（Mac 上跑）
  tests_host/              宿主契约测试（服务器上用 MaiBot 的 venv 跑）
reference/
  m0-probe/                M0 线上实测用过的探针插件：钩子、发消息、传群文件的真实写法（频率和发送闸 v6 不再使用）
  jev/                     另一个插件里调用 Jev（TypeSafe）的代码，只参考调用方式
  maibot_sdk-2.8.1/        线上正在用的插件 SDK 源码副本（去掉了旧版兼容层）
```

## 一眼看懂的现状（2026-09-27）

- M1（理解群）、M2（资讯 / 构想 / 冷场开话题）、M3（目标与派活）的代码都写完了：本地 1004 个测试通过；在服务器上用 MaiBot 自己的 Python 环境跑，全部单测（Linux、root）和 8 项宿主契约测试也通过（放在 `plugins/` 以外的临时目录，不触发重载）。
- **还没部署到线上**。部署、上线测试要你同意，步骤和要你决定的事都在 `docs/08-待你同意的事.md`。
- M4（共享工作区以外的扩展：专用 SSH 机器、Railway、skill / MCP、群空间）按计划「按需」，没有做。
- 网页原型在 `prototype/console/`；真网页在 `plugin/CharTyr_MaiWork/console/static/`，按原型做，手机 / 平板 / 桌面三种布局。

## 本地环境说明

仓库在 exFAT 外置盘上，不支持符号链接，所以 Python 环境放在 `~/.venvs/maiwork`（Python 3.13 + SDK 2.8.1 + pytest + aiohttp + httpx，已建好）。重建方法见 `docs/04-部署与验证.md`。
