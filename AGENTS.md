# MaiWork 开发规则

## 项目一句话

MaiBot 插件 `chartyr.maiwork`：补全 MaiBot 的群聊生产力能力：为 MaiBot 提供一个并行的完整 Agent，理解一个群，主动找资讯、提构想、追目标，交付任何工作。

怎么做到：和 MaiBot 并行运行（不抢 MaiBot 的话筒）；从群聊理解群 → 主动出资讯、构想、追目标，接群友派的活（按配置经管理员批准）→ 主模型派子 agent 干活、验收 → 通过网页、群文件、MaiBot 顺口提起来交付。需求见 `docs/01-需求.md`，设计见 `docs/02-设计.md`，从 `docs/05-开发计划.md` 的「阶段 0」开始。

## 线上红线（违反任何一条都要停下来先问用户）

1. **MaiBot 宿主代码零改动。** 不改 `/opt/MaiBot` 下除 `plugins/CharTyr_MaiWork/` 以外的任何文件，不改 `config/bot_config.toml`（包括 `[[models]]`、表达学习设置）。确实需要，先说明理由，等用户单独授权。
2. **不重启** MaiBot（screen `mai`）、SnowLuma（`snowluma.service`）或服务器上任何服务。重启由用户自己做。
3. **往 `plugins/` 放、改、删任何文件都会触发全部插件热重载**，每次部署都要用户当次同意。开发和契约测试一律在 `plugins/` 之外的目录做。
4. **不往任何 QQ 群发消息、不改任何群的回复频率、不传群文件**，除非用户当次同意并指定了群（目前指定过的测试群：`900000001`）。测完恢复原状并回读确认。
5. **先备份再改。** 线上改动前把旧文件备份到 `/root/backups/`。
6. **密钥不落仓库、不打印、不写进聊天。** 服务器上的 `/root/.typesafe_key`、插件 `config.toml` 里的 `api_key` 只由代码读取；模型密钥由用户自己填（线上 `config.toml` 或 MaiWork 网页的模型设置），网页上只进不出。
7. 读 MaiBot 消息库一律只读模式：`sqlite3.connect("file:data/MaiBot.db?mode=ro", uri=True)`。
8. 不要碰 `/root/MaiBot.disabled`（旧备用档）。

## 设计红线

- **MaiWork 内部没有 planner。** 「planner」只指 MaiBot 的回复管线。文档和代码里用 MaiBot / MaiWork / Jev / 主模型 / 子 agent 这些明确名称，不要用「她」指代 MaiBot。
- 只服务配置里列出的群；非服务群零读取、零 Jev、零模型调用、零发送。
- **不抢 MaiBot 的话筒**：不改群回复频率、不拦 MaiBot 的发送、没有 CHAT/WORK 模式切换。想让群知道的事优先交给 MaiBot 说；MaiWork 自己发的只限冷场开场白（只开一句，后续对话归 MaiBot）、交付说明、派活状态固定话、`/mw` 回复、故障报错，且受每日上限和睡觉时段约束。
- 收消息的钩子永远不中止收到的消息。
- 群友派的活（任务、agent 目标）按配置要 bot 管理员批准，未批准不开工。
- 关注成员的个人画像只给管理员看，不出现在群消息里、不跨群。
- 主模型不亲自干长活；子 agent 不能宣布任务完成。
- 不给 MaiBot 的 planner 注册工具。
- 不要臃肿：细粒度权限先不做（派活批准除外）；但隔离、资源限制、真实验收、去重、推送节制必须做对。
- 不参考、不复用 `plugins/` 里的 `CharTyr_IronClaw_Bridge` 和 `chat_summary`。

## 工作方式

- 分工（用户定）：前端（网页）由主会话亲自写；具体执行派`commandcode/deepseek/deepseek-v4.1-flash`或者 `newapi-messages/kimi-k3`（k3速度比较慢，优先选择ds，除非是比较复杂的任务） 子 agent，**简单任务**（收尾接线、小改、文档同步）改派 `commandcode/deepseek/deepseek-v4.1-flash`，它更快；探查 / 调研也用 deepseek-v4.1-flash。

- 宿主行为不要凭 SDK 名字猜：先查 `docs/06-宿主接口事实.md`，没有的就 SSH 上去读线上源码，查到的新事实补进 `06`，没实测的标「待实测」。
- 先写测试再写实现；新测试要先确认它能失败（临时去掉修复跑一次变红），再让它通过。
- 测试入口找不到插件目录要直接报错，不许 skip 造成假绿。
- 改了 manifest、插件类、组件注册，必须跑宿主契约测试（`docs/04-部署与验证.md` 第二节）。
- 部署后必做检查清单见 `docs/04-部署与验证.md` 第四节（其他插件正常、A_Memorix 没卡）。
- 每完成一小步 git 提交；文档和实现同步更新，但不要提前宣称完成。
- 做不到、测不了就直说，给出已核实的证据；不编造输出。

## 汇报

用户不是科班出身。汇报先给结论和取舍，用大白话、短句；分清「实测过」和「推断」；少用内部代号。

## SSH

```bash
ssh -o KexAlgorithms=curve25519-sha256 -o ConnectTimeout=25 -i ~/.ssh/<你的私钥> root@<服务器IP>
```
远程通配符加引号；多行命令用 `'bash -s' <<'EOF'`。详见 `docs/03-线上环境.md`。
