<div align="center">

<img src="logo.png" width="128" alt="MaiWork Logo" />

# MaiWork

**MaiBot 的常驻生产力 Agent**：理解一个群 · 主动找资讯 · 提构想 · 追目标 · 交付任何工作

[![Version](https://img.shields.io/badge/version-0.4.2-blue.svg)](https://github.com/CharTyr/MaiWork)
[![MaiBot](https://img.shields.io/badge/MaiBot-1.2.5%20实测-green.svg)](https://github.com/Mai-with-u/MaiBot)
[![SDK](https://img.shields.io/badge/插件%20SDK-2.x-green.svg)](https://github.com/Mai-with-u/MaiBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/CharTyr/MaiWork)](https://github.com/CharTyr/MaiWork/stargazers)
[![Issues](https://img.shields.io/github/issues/CharTyr/MaiWork)](https://github.com/CharTyr/MaiWork/issues)

[核心特性](#核心特性) • [功能概览](#功能概览) • [界面一览](#界面一览) • [运行环境](#运行环境) • [专用机器](#专用机器自己的-vps--vm可以配多台) • [快速开始](#快速开始) • [配置项说明](#配置项说明) • [指令](#指令) • [安全与公开范围](#安全与公开范围) • [常见问题](#常见问题)

**在线看网页演示：[dewy-vow-gebh.here.now](https://dewy-vow-gebh.here.now/)**

</div>

> MaiWork 是 MaiBot 的插件（插件 ID `chartyr.maiwork`）。它和 MaiBot **并行运行**、共用一套人设，但**不抢 MaiBot 的话筒**：不改回复频率、不拦 MaiBot 说话。想让群里知道的事，优先交给 MaiBot 在聊天里顺口提。
>
> 基本功能已完成，仍在持续迭代。

<div align="center">
<img src="docs/images/news.png" width="92%" alt="资讯页" />
</div>

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **主动资讯** | 每天几个时段，按群里在聊的话题上网找新消息和好文章；打分、去重、控制话题比例，只留值得看的 |
| **构想与目标** | 提「我可以帮你们做这个」的点子；长期的事立成目标，定期检查进度、自己往前推 |
| **接活交付** | 群友 @MaiBot 派活（查资料、做对比、写小网页、出 PDF…），子 agent 去做，主模型验收，不合格打回重做 |
| **不打扰** | 主动发到群里的消息每天有上限，睡觉时段不开话题，冷场时才开一句话题 |
| **管理员说了算** | 群友派的活默认要管理员批准；低风险小活可自动放行（可关） |
| **网页控制台** | 每个群一套资讯 / 构想 / 目标 / 任务页面，管理员能直接和 MaiWork 对话；适配手机、平板、电脑 |

---

## 功能概览

### 资讯
每天几个时段（默认 08:30 / 14:00 / 19:00），先从群聊里定下 3–5 个搜索方向，再让子 agent 上网找、主模型逐条打分。
- **去重**：同一件事（哪怕换了网站、换了标题）不会发第二次。
- **不钻牛角尖**：同一个话题 3 天内最多 3 条，搜索方向会有意往外拓展。
- **只要新的**：超过 7 天的旧闻直接丢掉。
- **支持 RSS**：在网页上给群加 RSS 源，每轮最多拿 6 条新条目一起参加评选（同样要过去重和打分）。

### 构想与目标
- **构想**：写清楚包含哪几件事；挑中想要的，就能开任务或立目标。
- **目标**：需要长期推进的事，MaiWork 定期检查、自己往前做；也会看群里最近在聊什么，偶尔主动提一个（要管理员批准）。

### 任务
群友直接 @MaiBot 说要做什么。MaiWork 接下来 → 主模型拆活、派子 agent 去做 → 做完主模型先验收 → 通过群文件、临时网页（here.now）交付，或者让 MaiBot 聊天时提一句。

### 开话题
群里安静太久时，从资讯和构想里挑合适的内容开一句话题，后面的聊天交给 MaiBot。

### 网页控制台
- **资讯 / 构想 / 目标 / 任务**：群友用本群链接就能看，管理员能批准、反馈有没有用。
- **群**：MaiWork 眼里这个群的样子（群画像、关注成员），管理员可以改。
- **和 MaiWork 聊**：管理员直接问进度、提要求、调整做法。
- **设置**：模型、群、派活批准、推送节奏、扩展（MCP 和 skill）、干活用的机器等。

---

## 界面一览

<table>
<tr>
<td width="50%"><img src="docs/images/ideas.png" alt="构想" /><br /><sub><b>构想</b>：从群聊里想到「可以帮你们做这个」，写清包含哪几件事</sub></td>
<td width="50%"><img src="docs/images/task-delivered.png" alt="任务" /><br /><sub><b>任务</b>：等批准、进行中、交付；每一步谁做了什么都看得到</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/images/chat.png" alt="和 MaiWork 聊" /><br /><sub><b>和 MaiWork 聊</b>：管理员直接问进度、提要求</sub></td>
<td width="50%"><img src="docs/images/settings.png" alt="设置" /><br /><sub><b>运行状态</b>：模型、搜索、本机干活、专用机器、一次性 VM 一眼看清</sub></td>
</tr>
</table>

<div align="center">
<img src="docs/images/mobile.png" width="92%" alt="手机端" />
<br /><sub>手机上也能用：资讯、构想详情、任务批准</sub>
</div>

截图来自[在线演示](https://dewy-vow-gebh.here.now/)，群、成员和内容都是虚构的。

---

## 运行环境

| 项目 | 要求 |
|:-----|:-----|
| MaiBot | 插件 SDK 2.x；在 **MaiBot 1.2.5** 上实测 |
| Python 依赖 | httpx、tomlkit、aiohttp —— MaiBot 本身已带，**不用另装** |
| 模型 | 一个 **OpenAI 兼容接口**：「主模型」负责想和验收，「干活模型」负责执行，可以是同一个 |
| 联网搜索 | 一个能搜索的 **MCP**（如 Tavily、You.com、EXA 的托管 MCP） |
| 可选 | Jev（TypeSafe）密钥，让快速判断更快更省；自己的 VPS / VM（专用机器，可以配多台）；[railway.new](https://railway.new) 一次性 VM |

**子 agent 在本机跑命令的方式，启动时自动判断，不用你建用户：**

| 你的机器 | MaiWork 怎么干活 |
|:-----|:-----|
| Linux + systemd，MaiBot 以 root 运行 | 放进 systemd 隔离单元跑命令：系统目录只读、禁止提权、限内存和时长。已有 `maiwork` 账号就用它；没有就**自动分配临时账号**，不用手动建 |
| macOS / Windows / 非 root / 没有 systemd（如 Docker） | 本机**不跑命令**（安全起见），查资料、写文件、做网页照常；要跑命令的活交给 railway.new 一次性 VM 或者自行配置的其他机器（见下面「专用机器」），都没有就直说做不了 |

自动分配临时账号时，工作区在 `/var/lib/private/maiwork/workspaces`；不跑命令时，工作区在插件数据目录下的 `workspaces/`。启动后在网页设置页「运行状态」里的 **「本机干活」** 一项能看到当前是哪种方式。插件**不提供**「不隔离直接跑」的选项。

### 专用机器（自己的 VPS / VM，可以配多台）

要跑命令、装依赖、编译、跑得久的活，MaiWork 会**优先交给你配置的专用机器**。都在忙或连不上时，改用 railway.new 一次性 VM，再不行才回到本机；本机又不能跑命令的话，就直说做不了。

1. 打开网页设置页「运行状态 → 专用机器」，点「复制公钥」。这是 MaiWork 自己生成的一把 SSH key。
2. 把公钥加进那台机器的 `~/.ssh/authorized_keys`。建议给 MaiWork 单独开一个普通账号，不要用 root。
3. 在「设置 → 执行环境 → 专用 SSH 机器」里一行一台，填三样：名字、地址（`user@1.2.3.4:22`）、备注（配置 / 用途）。
4. 可选：在「做事规矩」（AGENTS.md）里写清每台机器的情况和用途，比如「小黑：4 核 8G，装了 Docker，编译用它」「显卡机：只在跑模型时用」。主模型派活时会按这些说明挑机器。
5. 回到「运行状态」看每台连不连得上。连不上会写明原因：公钥没加 / 网络不通 / 主机指纹变了。

用法和限制：
- 每个任务在机器上用自己的目录 `~/maiwork/<任务ID>/`，做完把成品拷回本机再交付；
- 一台机器同时只接一个任务；
- 这台机器上的权限和隔离由你自己负责，所以请用专门给 MaiWork 的机器或账号。

---

## 快速开始

### 第一步：安装

二选一：
- **插件市场**：MaiBot WebUI → 插件市场 → 搜索「MaiWork」安装
- **手动**：在 MaiBot 的 `plugins/` 目录里执行
  ```bash
  git clone https://github.com/CharTyr/MaiWork.git
  ```
  本仓库根目录就是插件目录。

### 第二步：最少配置

把 `config.example.toml` 复制为 `config.toml`（或在 MaiBot WebUI 的插件配置里改），至少改两处：

```toml
[plugin]
enabled = true

[[groups.serve]]
group = "qq:你的群号"
```

MaiBot 会自动加载插件。

### 第三步：打开网页控制台

1. 管理员密码在 `<MaiBot>/data/maiwork/console_password.txt`（也可以在 `config.toml` 的 `[console] password` 自己设）。
2. 打开 `http://127.0.0.1:18650`，用管理员密码登录。
3. **首次引导**会带你填完：模型、群、可选密钥、管理员和群管理员、头像。
4. 在「设置 → 扩展」里加一个搜索 MCP，点「用作联网搜索」。

想让群友从外网访问网页，需要自己做反向代理，并把地址填到 `[console] public_url`。

### 第四步：在群里用

- 发 `/mw` 看本群进度，发 `/mw 网页` 拿本群网页链接
- 直接 @MaiBot 派活，比如「@MaiBot 帮我对比一下这三款键盘」

> 所有设置都有默认值，默认什么都不做。网页上改的设置会直接写回 `config.toml`。

---

## 配置项说明

完整说明见 [`config.example.toml`](config.example.toml)（每项都有中文注释）。常用的：

### 群与关注

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `plugin.enabled` | bool | `false` | 总开关，必须显式写 `true` |
| `groups.serve` | list | `[]` | 服务群列表，每项 `group = "qq:群号"`，可选 `workspace` 共享工作区 |
| `focus.max_members` | int | `5` | 每群最多关注几个人 |
| `focus.personal_profile` | bool | `true` | 给关注成员建个人画像（只给管理员看） |

### 资讯与推送

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `feeds.news_slots` | list | `["08:30","14:00","19:00"]` | 每天备资讯的时段（北京时间） |
| `feeds.max_items` | int | `10` | 每批最多入选几条 |
| `feeds.ideas_per_day` | int | `1` | 每群每天最多几个构想 |
| `feeds.blocked_domains` | list | `[]` | 屏蔽的来源域名（含子域） |
| `topics.per_day` | int | `2` | 每群每天最多开几个话题 |
| `delivery.push_per_day` | int | `3` | 每群每天主动推到群里的上限 |
| `delivery.quiet_hours` | string | `"23:00-08:00"` | 睡觉时段，不开话题 |

### 派活与批准

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `approval.required` | bool | `true` | 群友派的活要 bot 管理员批准 |
| `approval.admins` | list | — | bot 管理员，格式 `qq:号码` |
| `approval.auto_review` | bool | `true` | 低风险小活由主模型审一眼直接开工 |
| `approval.auto_review_daily` | int | `5` | 每群每天最多自动放行几件，`0` = 关 |
| `tasks.token_limit` | int | `2000000` | 单任务 token 上限，到了自动暂停 |
| `tasks.run_seconds` | int | `10800` | 单任务时长上限（秒） |

### 模型

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `models.base_url` | string | `""` | OpenAI 兼容端点 |
| `models.api_key` | string | `""` | 端点密钥（网页上只进不出） |
| `models.main` / `main_backup` | string | `""` | 主模型 / 备用 |
| `models.worker` / `worker_backup` | string | `""` | 干活模型 / 备用 |
| `models.max_concurrency` | int | `2` | 同时最多几个请求 |

### 干活环境

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `environments.ssh` | list | `[]` | 专用机器，每台写 `name` / `host`（`user@地址:端口`）/ `note`，可以多台 |
| `environments.railway` | bool | `true` | 允许用 railway.new 一次性 VM |
| `environments.memory_max` | string | `"512M"` | 本机隔离单元内存上限 |
| `environments.runtime_max_sec` | int | `1800` | 本机隔离单元最长运行秒数 |
| `environments.verify_enabled` | bool | `false` | 资讯实测：挑几条上 VM 试一试再发 |
| `group_space.enabled` | bool | `true` | 群文件 / 群公告 / 群相册操作（适配器支持时） |

### 网页

| 配置项 | 类型 | 默认值 | 说明 |
|:------|:-----|:-------|:-----|
| `console.listen` | string | `"127.0.0.1:18650"` | 网页监听地址 |
| `console.password` | string | `""` | 管理员密码，空则自动生成 |
| `console.public_url` | string | `""` | 反向代理后的外网地址 |

---

## 指令

| 指令 | 说明 | 权限 |
|:-----|:-----|:-----|
| `/mw` | 看本群进行中的目标、任务和待批准的事 | 所有人 |
| `/mw 网页` | 拿本群网页链接 | 所有人 |
| `/mw 批准 [ID]` | 批准派的活 / 目标 | bot 管理员、本群群管理员 |
| `/mw 拒绝 [ID]` | 拒绝 | bot 管理员、本群群管理员 |
| `/mw 取消 [ID]` | 取消任务（T-）、目标（G-）、提醒（M-） | 发起人、群主 / 群管、bot 管理员 |

> **两种管理员**：总管理员看得到所有群、能改所有设置；群管理员由每个群单独设网页密码，只管自己这个群。

---

## 安全与公开范围

装之前请知道这几件事。

**临时网页（here.now）是公开的。**
- 任务做完、需要给群友看网页或文件时，MaiWork 会把成品匿名发布到 here.now，生成一个链接。
- 只发布这个任务自己的成品目录（工作区里的 `artifacts/<任务ID>/`），不会发工作区里别的东西。
- 成品是干活的模型生成的。**发出去的内容 24 小时内任何拿到链接的人都能看到**，不要让它做含隐私或机密的东西。
- 群文件传不上去时，也会用 here.now 代替。

**群空间操作**（群文件、群公告、群相册）只在适配器支持时出现，`[group_space] enabled = false` 能全部关掉。群里现有的东西只有群公告会动到（发新公告）；其余操作只针对 MaiWork 自己传的文件和自己建的文件夹。

| 操作 | 限制 |
|:-----|:-----|
| 删除 / 改名 / 移动群文件 | 只能动 MaiWork 自己传的文件 |
| 新建群文件夹 | 会记下是自己建的 |
| 删除群文件夹 | 只能删自己建的；里面有任何别人的文件就不删 |
| 发群公告 | 机器人要是群主或管理员；发之前先在群里说一句；每群每天最多 1 条（可调） |
| 上传到群相册 | 机器人要是群主或管理员；只传本群工作区里的图 |

**隐私**
- **只碰你指定的群**：没写进配置的群，MaiWork 一条消息都不读。
- **个人画像不外传**：只有管理员能看，不出现在群消息里，也不跨群。
- **密钥只进不出**：网页上填的密钥不会再显示出来，也不写进日志。

---

## 常见问题

**Q：需要给 MaiWork 单独建一个系统用户吗？**
A：不需要。Linux + systemd 且 MaiBot 以 root 运行时，MaiWork 会自动分配临时账号隔离运行；你已经建了 `maiwork` 账号的话就用它。其他环境本机不跑命令，其余功能照常。

**Q：在 Windows / macOS 上能用吗？**
A：能装能用：资讯、构想、目标、网页、查资料写文件的任务都正常。只是本机不跑命令（没有能用的隔离方式），这类活可以交给自己配置的专用机器，或者 railway.new 一次性 VM。

**Q：装上之后什么都没发生？**
A：默认什么都不做。检查 `[plugin] enabled = true`，并且 `[[groups.serve]]` 里写了群号（格式 `qq:群号`）；再到网页里完成首次引导、填好模型。

**Q：资讯一直是空的？**
A：找资讯要联网搜索。到网页「设置 → 扩展」加一个搜索 MCP 并点「用作联网搜索」；RSS 源只是补充。

**Q：会不会在群里刷屏？**
A：不会。主动推送每群每天有上限（默认 3 条，含开话题），睡觉时段不开话题；大多数内容只放在网页上，想让群知道的事优先交给 MaiBot 顺口提。

**Q：群友派的活会直接开工吗？**
A：默认要 bot 管理员批准。低风险的小活（调研、做个小网页、出个 PDF）可以由自动审核放行，每群每天有上限，也能关掉；agent 目标永远要人批。

**Q：数据存在哪？**
A：默认在 `<MaiBot>/data/maiwork/`（数据库、网页密码、头像等），可以用 `[storage] data_dir` 改。

---

## 支持与致谢

如果这个插件对你有帮助，欢迎点亮 Star；有问题和建议请提交 [Issue](https://github.com/CharTyr/MaiWork/issues) 或 [Pull Request](https://github.com/CharTyr/MaiWork/pulls)。

- [MaiBot](https://github.com/Mai-with-u/MaiBot) 开源聊天机器人框架
- [here.now](https://here.now) 临时网页托管
- [railway.new](https://railway.new) 一次性 VM

## 许可证与作者

本项目采用 **AGPL-3.0-or-later** 开源协议：可以自由使用、修改、分发；如果你改了代码并对外提供服务（包括网页控制台），需要把改过的源码也开放出来。

[@CharTyr](https://github.com/CharTyr)
