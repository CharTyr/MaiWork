# plugin/CharTyr_MaiWork/maiwork/

MaiWork 插件（`chartyr.maiwork`）的运行时主体：一个和 MaiBot 并行运行的完整 Agent，为配置里列出的服务群提供「理解群 → 主动出资讯/构想、追目标、接派活 → 主模型派子 agent 干活并验收 → 多渠道交付」。宿主入口 `plugin.py`（插件目录上一级）只做生命周期转发，全部逻辑都在这个包里。

## Responsibility

- **装配与生命周期** — `app.py` 的 `MaiWorkApp` 把约 50 个组件接起来：`start()`（load_settings → Store → Host → Models → 各模块 → 控制台 → 后台循环）、`stop()`、`update_config()`（热更新：关→开整 app 重启；开→关全收；开着改配置更新 settings，`console.listen` 变了重启控制台）。`enabled=false` 时什么都不起（不开库、不开端口、不跑循环）。
- **收消息** — `plugin.py` 的 `maiwork_intake` 钩子（`chat.receive.after_process`，BLOCKING，1500ms）转发给 `app.on_message()` → `intake.Intake.handle`；`maiwork_mentions` 钩子（`maisaka.planner.before_request`）转发给 `app.on_planner_before_request()` → `Mentions.inject`。两个钩子永远返回 `{"action": "continue"}`，任何异常吞掉。
- **后台调度** — `app.run_loop_once()`（默认 30 秒一圈）对每个服务群串行跑：profiles.tick（读消息+统计）→ persona/personal 巡检 → topics 开话题 → 排程（资讯/构想；画像成形的群顺带排「反馈 + 本群做法复盘 + 自动订阅/退订」小时轮 `_feedback_round` → feedback_jobs → auto_sources）→ card_push → 名册对名字 → 资讯图解 → 构想七天收起；长活（画像提炼、备资讯、构想、图解、反馈轮）spawn 成独立后台任务（`_spawn_long_job`，同群同种同时只跑一个）；随后 M3 巡检（发件箱 flush、批准提醒/过期、目标到期、queued 任务派工、任务安全网）、用量提醒。

## Design

**装配模式（Composition Root）**：`MaiWorkApp.__init__` 声明全部组件槽位，`_start_stack()` 按依赖顺序装配（库 → 宿主 → 模型/画像 → M2 模块 → M3 模块 → 收消息 → 控制台 → 后台循环）。模块级可选组件用 `_import_m2_class` 惰性导入——feeds/scheduler/coordinator/profile 等没写好时返回 None，不拖垮 app（对应线并行开发的遗留保护）。测试注入点成排暴露（`profiles_cls/feeds_factory/http_transport/jev_transport/...`），外部 HTTP 全部可换 `httpx.MockTransport`。

**单一配置出口**：`app.get_settings()` 是所有模块读配置的唯一入口：`config.toml` 原始 Settings（frozen dataclass，热更新后直接换对象）× 执行能力判定后的实际工作区根（`_ws_root`，dynamic 落 `/var/lib/private/maiwork/workspaces`）。2026-10 之前还有一层 `kv["rules.override"]` 网页覆盖会赢过文件值，已在 docs/18 第一步删掉（启动迁移把 kv 值搬进 config.toml 后删键）——配置只有 config.toml 一个真实来源。`Settings` 是 frozen dataclass，修正用 `dataclasses.replace` 只换子节。

**数据层纯数据**：`store.py`（SQLite，单写入者 `threading.RLock` + `BEGIN IMMEDIATE`）、`tasks.py`、`goals.py`、`approvals.py` 只放数据和规则：不调模型、不调宿主、不发消息。状态机转移严格按 [设计文档](<../../../docs/02-设计.md>) §7.2 表，非法转移抛 `ValueError`，转移在一个事务里完成（改状态 + 写事件）。取消/终态后晚到的结果一律不接（`accept_result=False`），任务状态绝不复活。

**主模型协调器 — 子 agent 执行**（`coordinator.py` + `workers.py`）：主模型不亲自干长活。`Coordinator.run_task(task_id)` 拥有任务整个生命周期：`asyncio.Lock` per workspace（同一工作区同一时刻一个主模型回合）、`asyncio.Semaphore` per workspace 限子 agent 并发（`[environments] max_parallel`）。一轮尝试 = `_plan`（json_mode 出 criteria/deliver_kind/jobs/env，或用 ≤6 轮只读工具查资料再定）→ 派 `Workers.run(brief, tools=[...])`（子 agent 多轮循环，只能 `submit_result` 交回，不能宣布完成）→ `_review`（验收：deliver_kind≠text 时 artifact 必须真实存在于 `artifacts/<task_id>/`，调研类任务做引用核对）→ 交付/重来/失败。执行环境三选一：`local`（本机隔离）/`ssh`（专用机器 `machine_*` 工具）/`railway`（一次性 VM `vm_*` 工具），远端成品必须拷回本机工作区才算数。

**工具注册表 — 角色门控**（`tools.py`）：`Tools.call()` 是子 agent/主模型/管理员调工具的唯一入口，每次调用（无论成败）写 `tool_calls` 表，密钥参数统一替换成 `***`。`ToolContext.role`（worker/main/admin）决定能用哪些工具——`specs("worker")` 给子 agent，`specs("main")` 给主模型（验收/排计划回合），admin 工具由 `tools_admin.register_admin_tools` 注册。通用执行另用每回合独立的 `ToolContext.used_tools` 集合，只在权限 / 参数闸都通过、实际派发 handler 时记名（不含 `submit_result`，handler 失败也算真实调用）；经交回 / 失败 / 验收保留到服务器拥有的 `handoff.review.used_tools`（最多 24 种），不从模型成果 `data` 取，不靠 task_id / actor 日志猜并发归属。

**信号驱动**（`intake.py` 的 `Signals`）：收消息钩子只往内存 map 记「这个群有新消息」（mark）；后台循环 `signals.take()` 取走后驱动 profiles.tick。planner 钩子靠 `groups.session_id` 持久映射 + 内存信号认群，非服务群零读取。

**隐私与红线编码化**：`privacy.scrub`（关注成员 note/persona 长片段不得出现在群可见文字）、`members.render`（`{@平台id}` token 统一换当前显示名，平台 id 不外漏）、`host.py` 是唯一允许出现 `ctx.call_capability` 的文件（别的模块禁止，有源码扫描测试守着）、`delivery.Pushes`（每日上限+睡觉时段）、`names.py`（QQ 群名乱码清洗）。

## Flow

**收消息 → 理解**：群消息 → `plugin.maiwork_intake` → `app.on_message` → `Intake.handle`：非服务群立刻 continue（零 Jev/零 Store）；记 Signal、记名册、记 chatlog；`/mw` 开头的转 `commands.py`（纯代码固定回复，不调模型，回复进 Outbox）；被 @ 时在 1500ms 内等 `jev.Jev` 判 kind（prepare/goal/reminder/none，把握 ≥0.6）→ prepare/goal 后台 `approvals.create`（未批准不开工）、reminder 后台回调主模型解析时间建成员目标、判不了/超时写 `pending_asks` 表留给读群提炼。

**主动产出（后台循环一圈）**：
1. `profiles.tick(gid)` 增量读群消息、维护统计；needs_refresh → spawn 画像提炼（主模型）。
2. `scheduler.check` 报时辰：资讯时段（`[feeds] news_slots` + 按「群号+日期+时段」播种的固定随机偏移，45 分钟内有效）、构想时段；睡觉时段读**每群** `group_push.<群号>.quiet_hours` 静默，画像没成形永远空。（主动提目标 2026-10 已删，时段表里不再有它。）
3. 到点 → spawn 长活 `feeds.prepare_news(gid)`：主模型按画像出 3–5 关注点（提示词带本群规矩 + 本群资讯做法（`group_context`）、近 14 天反馈、资讯评价、饱和话题、历史方向）→ 找候选走两段式（`_collect_two_phase`：撒网→保底→粗筛→挑→核验，候选从程序侧 `discovery` 登记簿拿，不信子 agent 交回；2026-09-30 起这是唯一的路——老单子 agent `_collect` 和「每群开关」都没了，配置里的 `[feeds] two_phase` 已删）；RSS 源从 `rss.py` **在补打开之前**并进候选池、和搜索候选一起过 `news_recheck` 补打开的内容核验（水文/低质转载/洗稿，不合格丢弃；RSS-only 轮也走，2026-10-05 docs/10 §九 第一步 3）→ 三道门槛（硬性淘汰/打分上网页/进话题候选池；「重复」和「后续进展」分开判，后续标 `followup` 放行、同一件事每轮限一条）→「有人味」写帖子（写完对原文自检撑不住的回落摘要）→ 入库 `news_batches/news_items`（批次带工具用量统计，两段式再带漏斗 funnel），过第三道的进 `topics` 候选池。构想 `feeds.make_idea`：先过「堆积闸」（本群未处理的 new/wanted/pending ≥3 就不调模型）、模型自报 `worth=low` 跳过，产出一条通常 0–1 个构想。
   - 2026-10-04 晚间故障修复（22:59 已部署 `583b77c`）：资讯的关注点 / 挑选 / 打分 / 写帖 / 原文自检用 `_parse_model_json` 兼容完整外层 JSON 或无语言代码围栏；不提取任意片段、不修补坏 JSON、原有校验不变。关注点（含补问）/ 挑选 / 打分传 `retries=0`，每个候选模型只试一次，网络失败走已有备用链；打分 240 秒、内容坏或漏项的一次补问仍保留，其他岗位与全局端点设置不改。
4. 「反馈 + 本群做法」小时轮（feedback_jobs.run，画像成形后每群每小时最多一轮）：`news_feedback.mention_round` 挑候选交主模型判「群里接着聊」→ `lessons.run` 做每群每岗的**本群做法复盘**（有新的验收 / 反馈信号才调模型）+ 每周整理（专岗 ≥800 字只 patch；task ≥4 份未锁定做法带原文合并）/ 30 天未用自动归档；不往群里发任何东西。第 4 条「反馈 + 本群做法」小时轮里还接了 `auto_sources.run`（docs/10 §九 第二步）：自动退订 + 门槛订阅每天一次、来源地图 + push 判断每周一次（节流在模块里），不往群里发东西。（口味小结 2026-10-03 已删：迁进本群 news 做法 skill 正文。）
5. `topics.check(gid)` 冷场开话题：代码第一层（每群的时段/额度/开关 + 安静时长）→ Jev 第二层（ok≥0.6 且 fit≥0.5；给 Jev 的消息里机器人自己的话标 `[机器人自己]`、另报 `last_message_is_bot` / `last_human_minutes_ago` / `note`，`reason` 那题问「现在群里的状态是哪一种」；门槛数字没动）→ 主模型按人设写开场白，**只 enqueue 进发件箱**（不再自己直接发）。
6. 构想七天收起巡检：`feeds.shelve_ignored_ideas` 每个服务群每轮一次，把 7 天没人理（`created` 算、无任务、非个人向、没有 pending/approved 请求）的 new/wanted 构想改成 dismissed，只改状态不删行、不往群里发、不学沉默。
7. `card_push.scan`（资讯卡片）+ `idea_mention.scan`（构想提一嘴）只建待发行并 enqueue；画图（`news_card.render_png` playwright 截图）与投递都走发件箱 `flush`。
8. M3 巡检：`outbox.flush`（发件箱统一投递：TTL/开关/每群额度/每群睡觉时段，安全失败重试一次、超时标 uncertain）、`approvals` 提醒/过期、`goals` 到期提醒（成员目标）和 `Coordinator.check_goal`（agent 目标周期验收）、queued 任务 `Coordinator.run_task` 派工、`tasks.net_check` 任务安全网（token/时长超线自动 paused）。

**派活（M3 全链路）**：群友 @ 派的活 → `approvals.create`（读**每群**批准名单；低风险轻活 `auto_review` 自动批，否则等管理员在网页或 `/mw 批准`）→ `Tasks.create(status=queued)` → `Coordinator.run_task`：`_plan` 定完成标准/交付形态/子任务/执行环境 → **开工前能力自检**（按岗位实际工具核对；缺执行工具先补、补不了有界重排一次、再不行暂停不扣尝试）→ `tasks.transition(running)` + `start_attempt` → 子 agent 干活（成品写工作区 `artifacts/<task_id>/`）→ `submit_result` 交回 → `_review` 验收（只读工具 inspect_file(s) 核对，真实存在闸、引用核对、符号链接逃逸检查）→ 通过 → `Delivery.deliver` 经 `Outbox` 交付（view=here.now 发布网页 / file=群文件+说明 / text=群文字），渠道失败时回落备选方式，成功进可提起清单（ttl 6 小时）。

**配置热更新**：宿主文件监控/`on_config_update` 或网页「全部配置」保存（`config_file.write_fields` 直写 config.toml，`apply_config_text` 立刻本进程应用）→ `update_config`：幂等比较 → 重判执行能力 → `_ensure_groups` → `_reconcile_unserved`（删掉的服务群残留就地标记：发件 cancelled/任务 cancelled/待批 expired）→ console.listen 变了重启控制台 → 群空间开关热生效。

## Integration

- **MaiBot 宿主**：唯一通道 `host.py`（`Host(ctx)`，`call_capability` 统一超时、返回统一 dataclass、异常包装 `HostError`）。用的宿主事实都查自 [宿主接口事实](<../../../docs/06-宿主接口事实.md>)：bot_qq、chat 消息读取、knowledge.search（长期记忆）、send_text/传群文件、proactive_trigger。绝不改 MaiBot planner、不改群回复频率（红线）。多平台：`set_platform_resolver` 接上 `settings.platform_of` 后按群号自动认平台（调用方也可显式传 `platform=`）、`platform_of_session` 按会话认平台、`bot_accounts` 把 bot.platforms 解析成 {平台: 账号}（tg 归一为 telegram）；Telegram 没有真 @ 段，`send_text` 的 `at_name` 把 @ 退成正文「@名字 」。
- **console/**（子目录，详图见 [console/codemap.md](console/codemap.md)）：`ConsoleServer`（aiohttp，监听 `[console] listen`）+ `views.py`（GroupSummary/GroupView/Settings 纯函数拼装）+ `auth.py`（管理员/群管理员 cookie、群友链接码、登录限流）+ `avatar.py`（头像缓存与签发）+ `usage_history.py`（用量历史）。app 在 `_start_stack` 第 6 步建 avatar 再 start console。
- **environments/**（子目录，详图见 [environments/codemap.md](environments/codemap.md)）：执行环境三件套——`capability.probe` 判定 fixed/dynamic/stopped → `local.LocalEnv`（systemd-run 固定用户或 DynamicUser 隔离；direct 模式仅供本机开发）、`railway.RailwayEnv`（railway.new 一次性 VM，每天限额、同时 1 台）、`ssh.SshEnv`（用户 VPS，ed25519 key 部署）。`Coordinator` 按计划 JSON 的 `env` 字段选择；`tools_exec/tools_railway/tools_ssh` 的工具落点都由对应 env 的 resolve 校验防越界。
- **platforms/**（子目录，详图见 [platforms/codemap.md](platforms/codemap.md)）：`qq_onebot.GroupSpace`（群空间：群文件/公告/相册/群相册上传；只有 qq 平台有 `has_onebot`，Telegram 等平台按平台关闭），启动 `probe()` 探测适配器能力（`host.list_apis()`）+ 机器人群身份缓存；防手滑登记 `group_files_owned` 表（outbox 上传成功 → `register_owned`），只许动机器人自己传的文件。
- **plugin.py**（上级目录）：声明 config model、生命周期（on_load/on_unload/on_config_update）转发、两个 HookHandler 注册到 MaiBot；`MaiWorkApp` 由它创建并持有。
- **外部服务**：OpenAI 兼容模型端点（`models.py`，端点级限流/冷却/重试/备用模型）、TypeSafe Jev（`jev.py`，熔断+judgments 落库）、Jina Reader（`reader.py`，打开网页首选路）、MCP 扩展（`extensions.py` + `mcp_client.py`，Streamable HTTP 最小客户端；联网搜索 `search.py` 走 kv["extensions.search"] 绑定的主搜索家，主家挂了按绑定的 fallback 名单递补、撒大网多搜几家按 broad 名单；6 家预设服务的地址/参数/结果解析定死在 `search_presets.py`，网页一键开/填密钥走 `search_presets_web.py`）、here.now 匿名发布（`herenow.py`，24 小时过期的交付网页）、RSS 源（`rss.py`）、GitHub 更新检查（`update_check.py`，只提醒不自动更新）。

## 模块导航（按特性分组）

### 装配与配置
| 文件 | 职责 |
|---|---|
| `app.py` | MaiWorkApp 装配、生命周期、后台循环、热更新、planner 钩子、长活派工；Feeds 构造时注入 `search=self.search`（保底补搜/撒网多家要吃它）；`_feedback_round` 起 feedback_jobs 小时轮 |
| `config.py` | pydantic 配置模型 + `load_settings` 规范化成 Settings（非法条目丢弃记中文问题，绝不抛）；多平台：服务群写成「平台:群号」——`parse_serve_group`（qq / telegram / tg 别名 / qqbot）、`Settings.platform_of(gid)`（不认得的群按 qq 老行为）、`has_onebot`（只有 qq 有 OneBot 群空间）、`host_platform`、工作区名按平台清洗（`_default_workspace`，Telegram 群 ID 的 `:、=` 等字符换 `_`） |
| `config_file.py` | config.toml 读写层（tomlkit 保注释；写前备份到数据目录；绝不在插件目录建临时文件） |
| `rules.py` | 「全部配置」表单表（`CONFIG_SCHEMA`）+ 通用校验 + `save_config_patch` / `reset_config_field` 直写 config.toml（旧 kv 覆盖层 2026-10 已删） |
| `migrations.py` | 启动时一次性迁移（幂等）：数据库旧覆盖层/secrets → config.toml；kv["rules.override"] → config.toml（值一致只删键）；搜索配置 → 扩展绑定（2026-10 修正：tavily 官方 MCP 工具名是下划线版 tavily_search/tavily_extract，老的连字符版只存在于旧文档） |
| `store.py` | SQLite 存储：连接、表迁移（_m1 起一串，**库号到 34**：第 31 步 `_m_chat_feeds` / 第 2 步 `_m_profile` 保持 0.7.9 原文，第 32/33 步 drop `member_interactions` / `chat_feeds`，第 34 步 `_m_agent_skills_group_rules` 建 skill / 规矩表并 DROP 从没上线的 `agent_lessons`；**不 DROP `agent_memory_notes`**——它是每群规矩启动迁移的输入）、事务、kv/secrets/events 助手 |
| `clock.py` | epoch/北京时间换算、睡觉时段判断（存库一律 epoch 秒） |
| `onboarding.py` | 首次安装引导状态（kv["onboarding"]） |

### 收消息与理解群
| 文件 | 职责 |
|---|---|
| `intake.py` | 钩子逻辑：Signal 记录、/mw 分流、@ 识别（Jev）、pending_asks、回复/引用资讯卡片记自动好评（`news_feedback.CardIndex`，任何错误吞掉）；永不中止消息 |
| `profile.py` | 群画像：增量读消息、活跃度统计、主模型提炼画像（攒批/到点/每周整理）、PROFILE-<群号>.md 同步、关注成员管理。群友对机器人下的指令原文不写进画像（提示词 + `_is_bot_command_verbatim` 兜底，2026-10） |
| `members.py` | 成员名册（平台 id 认人、显示名跟进改名、`{@id}` token 渲染出口） |
| `names.py` | QQ 群名乱码清洗（进库/进视图统一过 clean_group_name） |
| `chatlog.py` | 最近 14 天群发言只读副本（FTS5 trigram，search_chat 给资讯写原话依据） |
| `persona.py` | 关注成员个人画像（只给管理员；refresh/due；永不出现在群消息里） |
| `privacy.py` | 隐私闸：关注成员 note/persona 片段不得进群友可见文字；`fold()` 折叠空白/零宽/全角/中文数字，全局记忆闸和片段比对都用它 |

### 主模型与子 agent 执行
| 文件 | 职责 |
|---|---|
| `coordinator.py` | 主模型协调器：计划/派活/验收/交付、执行环境选择、任务生命周期、agent 目标检查。验收模型没给结论时强制重试后走「验收不通过」退回（`_REVIEW_FORCE_JSON_TRIES` / `inconclusive`，不判死）；**开工前能力自检**（`job_needs_exec_capability` + `_job_effective_tools` 走 `Specialists.effective_tools` 同一入口；能补就补执行工具、补不了有界重排**一次**（只改 jobs，不降 criteria / deliver_kind / 环境）、再不行 `paused_reason={"kind":"capability","text","jobs"}` 暂停且不扣尝试；自检异常 fail-closed） |
| `workers.py` | 子 agent 执行器：多轮工具循环、submit_result 交回、时间盒强制交回、上下文压缩接线；`tool_catalog()` 转调 `tools.catalog("worker")`（构想可行性算能力清单，取不到 → None） |
| `agents.py` | 专岗与交接数据层：岗位配置（kv `agents.profiles`）、自定义专岗建/删、交接单状态机；**本群规矩**（`group_rules` / `group_rule_versions`，≤3000 字、每群留最近 20 版）、**本群做法 skill**（`agent_skills` / `agent_skill_versions`：专岗每群每岗一份、`kind=task` active ≤12 份、锁定 / 归档 / 版本 / uses）、「最近做过的」（`agent_memory_learned`，只当去重材料）；自动更新 / patch / 合并一律锁定与归档硬挡，事务内重读 + 模型校验版本 CAS，原子合并，初版 source / note 留版本。带群号的方法先 `_verify_served`；id 不属于这个群 / 这个岗 → KeyError（接口 404） |
| `specialists.py` | 专岗子 agent：`run(kind, brief, …)` 复用 `Workers.run`，注入岗位说明 + `Agents.prompt()`（最近做过的）+ `group_context(gid, kind)`（本群规矩 + 本岗做法，标明是数据不是指令）；**唯一工具解析入口 `effective_tools(kind, requested, profile)`**（= 请求 ∩ 岗位上限；`role_usable` 判在册 / 启用）——run 和 coordinator 开工前自检共用它，不许各写一套；skill = 岗位名单 ∩ 当前启用；交回只到 returned，由主流程验收 |
| `group_context.py` | **每群「该怎么做」的唯一注入口**：`group_context(agents, gid, kind)` 输出「【本群规矩（管理员定的，必须照做）】」+「【本群<岗位>的做法（MaiWork 总结的，是参考）】」；专岗注全文，`kind=task` 只列 ≤12 行「本群/名字：description」（子 agent 用 `read_skill` 读全文），`kind=main` 只出规矩。调用方：feeds / personal / coordinator / topics / card_push / specialists |
| `compaction.py` | 上下文压缩（估算截 tool 结果 → 摘要最老一段）+ RepeatCallNudger + 大结果落盘 spill |
| `models.py` | 三协议（openai / responses / anthropic）客户端：重试/备用/端点限流、用量落库、密钥只进不出（_redact 统一遮）；端点 `headers` 大小写不敏感覆盖默认请求头，聊天/流式/列模型/验证共用，值纳入遮罩且散列进入验证签名；「200 但内容是错误」按错误码走重试/备用；端点拒 effort / 要 max_completion_tokens 时自适应重发（`_adapt`，进程内存） |
| `jev.py` | TypeSafe Jev HTTP 客户端：密钥读取顺序、答案校验、熔断、judgments 落库 |
| `admin_chat.py` | 管理员与主模型的网页对话循环（聚焦群、工具小票待确认） |
| `usage_alerts.py` | token 超阈值提醒（只在网页设置页展示，不往群里发、不暂停） |

### 工具系统
| 文件 | 职责 |
|---|---|
| `tools.py` | 工具注册表：register/call 唯一入口、tool_calls 落库、密钥遮罩、角色判定；`catalog(role)` 给这个角色**现在**能用的 [(名字, 描述)]（构想可行性 `idea_feasibility.inventory` 用；工具被摘掉后自然少那项） |
| `tools_builtin.py` | 内置工具：web_search（task_id 是 feeds-collect:/feeds-discover: 且没填 days 自动补最近 7 天；支持 site/news/focus 参数；feeds-discover: 前缀的结果顺手录进 discovery 登记簿）/fetch_page（Jina→抓正文 MCP→直接打开，防内网跳转；打开时顺手用 `page_date` 从原始 HTML / Jina 的 publishedTime / 抓正文文本读发布日期，写进正文提示 + `tool_calls` 摘要）/read_profile/submit_result |
| `tools_exec.py` | 执行工具：read/write/list_file、run_command/start|check|stop_process、read_chat_history/search_memory、inspect_file(s)（主模型验收只读） |
| `tools_railway.py` | 一次性 VM 工具：vm_run/vm_put_file/vm_read_file/vm_fetch_file |
| `tools_ssh.py` | 专用机器工具：machine_run/machine_put_file/machine_read_file/machine_fetch_file |
| `tools_admin.py` | 管理员对话工具（roles=admin；危险动作写待确认小票 admin_chat_pending） |
| `tools_groupspace.py` | 群空间工具（roles=main）：group_files_list/group_file_manage/group_notice_send/group_album_upload |
| `skills.py` | skill 目录读取（front matter 极简解析、roles 过滤、内置 skill 优先、只读） |
| `skills_tools.py` | list_skills/read_skill 工具（内容按调用者角色过滤；认识「本群/<名>」= 本群 task + 当前岗做法，`group_id` + 真实 `agent_type` + 专岗白名单一致过滤，跨群 / 归档 / 带 file 一律当「没有」且不计 uses） |
| `skills_web.py` | 网页管理 skill（落 <data_dir>/skills/，只管网页新增删掉） |
| `builtin_skills/` | 随插件发布的内置 skill（`news-standard`：资讯标准，程序按环节注入提示词；`search-keenable/tavily/exa/you/firecrawl/tinyfish` 六家搜索服务官方用法，找资讯 brief 按预设附对应那份；`find-skills`：只给主模型排计划的找技能导航，`metadata.maiwork-roles=main`，只推荐不自动装、不强制外搜） |
| `extensions.py` | MCP 扩展加载器：[[extensions.mcp]] → McpSessionClient → tools/list → 注册 mcp_* 工具 |
| `mcp_client.py` | MCP Streamable HTTP 最小客户端（initialize/initialized/tools/call，会话失效重连） |
| `extensions_web.py` | 网页管理 MCP 扩展（kv 存配置不含密钥值，密钥存 secrets["mcp.*"]） |
| `search.py` | 联网搜索适配层：`Search.search`（主绑定先搜、失败按 binding["fallback"] 递补、全挂抛最后错误；指定一家 `search_with` 撒大网用；`broad_providers` 给撒网名单；每条结果标 `provider` 认来源）；扩展 url 被预设认出走 `search_presets` 定死参数，认不出走通用 schema 映射；5xx 重试一次；`Search.extract` 抓正文（可和搜索分属两家 MCP）；错误文本遮扩展 headers 密钥 |
| `search_binding.py` | kv["extensions.search"] 搜索绑定读写/状态/候选：{mcp, tool, extract_mcp, extract_tool, fallback[], broad[]}；fallback（主家挂了递补）/ broad（撒大网多搜几家）名单只收存在且 url 被搜索预设认得出的扩展；`guess_tool_role` 按名字猜搜索/抓正文 |
| `search_presets.py` | 六家搜索服务（keenable/tavily/exa/you/firecrawl/tinyfish）的定死知识：免密钥/密钥端点与头、`Preset` dataclass、`PRESETS` 表、`preset_of_url` 认地址、`entry_for` 生成扩展条目、`search_args`/`extract_args` 每家参数映射、`filter_to_schema` 按运行时 schema 滤键（2026-09-30 实测 tools/list + 官方文档；tinyfish 没密钥未实测） |
| `search_presets_web.py` | 预设搜索服务的网页层：`presets_view`（各家状态）、`activate`（打开/换密钥/改回免密钥，还没绑定时顺手设主搜索）、`setup`（首次引导一次配好：先全部校验再逐个开，第一家主搜索、其余 fallback、原有 broad 名单保留）；落到的仍是普通网页 MCP 条目 |
| `reader.py` | Jina Reader 客户端（打开网页首选路；本地限速+冷却；有任何问题算失败换下一条） |
| `news_standard.py` | 按环节读内置 skill 段落注入资讯流水线提示词 |

### 任务、目标、批准（M3 数据层）
| 文件 | 职责 |
|---|---|
| `tasks.py` | 任务状态机、尝试记录（取消时同事务作废开着的尝试）、net_check 安全网（token/时长线）、interrupt_orphaned（顺带收残留 running 尝试） |
| `goals.py` | agent 目标（G-n）/成员目标（M-n）：提醒、问进展、循环续期 |
| `approvals.py` | 派活待批：免批判断（读**每群**批准名单 `group_approval`）、approve/reject、pending 提醒/过期、取消权限（取消权限的群主/管理员判断按 `settings.platform_of` 认平台，`is_admin(uid, platform=..., group_id=gid)`；群命令只管本群） |
| `auto_review.py` | 低风险轻活主模型自动批（每群每日上限；goal 类/构想含 goal 永远留人批） |
| `group_admins.py` | 按群管理员：密码哈希只进库（`/mw 批准` 要用）；名单转发到 `group_approval.approvers`，不再自己存第二份 |
| `group_push.py` | 每群「往群里发」的**唯一真源**（`kv["group_push.<群号>"]`）：三个自动群发开关 + 一个每日总上限 `daily_max` + 每群 `quiet_hours`；第一次读惰性迁移旧 `kv["cardpush.<群号>"]` 并删源；非服务群 / 坏记录零读零写 + 保守默认；退役字段所有调用口都拒 |
| `group_approval.py` | 每群批准名单的**唯一真源**（`kv["group_approval.<群号>"]`）：`approvers / exempt_users / exempt_group / required`；PUT 必须四字段全给；惰性从全局 `approval.*` ∪ 旧 `kv["group_admins.<群号>"]` 种一次并删源；坏记录 / 读失败按「要批、无批准人」fail-closed |

### 资讯与构想
| 文件 | 职责 |
|---|---|
| `feeds.py` | 资讯/构想流水线：关注点（`_plan_focus`，提示词带「本群规矩 + 本群资讯做法」（`group_context`）/近 14 天反馈/资讯评价/饱和话题/历史方向；每个方向可带 `intl` 标记，国际话题要求至少 2 条英文问法，模型没给按 `_looks_intl_focus` 兜底）→ 找候选 → 三道门槛（打分时本群资讯做法当相关度/值得聊参考；写帖子时当写法/角度参考但不许编事实）→ 写帖子 → 入库（构想还要一个「由头」`origin`：接的是群里之前聊过的哪件事，`clean_idea_origin` 清洗 ≤16 字、无 QQ 号，入库前过隐私闸、命中只置空 origin，进话题候选池给开场白用）。找候选一条两段式 `_collect_two_phase`（2026-09-30 起唯一的路；老 `_collect` 和 kv["feeds.two_phase"] 名单开关已删）：①`_run_planned_searches` 代码照关注点里定好的搜索计划直接并发搜（2026-10-01 起不再派撒网子 agent；结果进 `discovery` 登记簿；一轮 ≤30 条、同 (q,site,news,kind) 去重、并发 4 路）→ ②`_floor_searches` 代码保底补搜（饿着 = 问 <2 种问法或候选 <6 条的方向；主家 + broad 多家各补；需要一手来源的方向主家多补一次；`intl=true` 而英文问法 <2 条的方向另补一次英文搜索）→ ③`_prefilter` 不调模型粗筛（非公开链接/重复/首页栏目页/屏蔽/太旧/标题近似；方向均衡每方向 ≤40%，留 ≤24 条，排序看 7 天内发布日期 + 优质来源先验分 `source_prior`）→ ④`_pick` 主模型一次挑 8~12 条带一句话理由（hook；失败回落前 10 条；提示词避开 SEO 站/采购指南/聚合站）→ ⑤`_verify_batch` 最多 3 个只用 fetch_page 的核验子 agent 并发打开核对：按**内容**判水文/低质转载/洗稿（`quality=false` 直接拒）、转载认得出原始出处交 `original_url`（必须先打开确认），回来的条目接回 src_query/src_provider，链接被改写立刻用 `_dup_url_key_check` 对最近已发布∪最近被拒再查一次重（入库前还有最后一道）（漏斗 funnel：各环节计数/每方向/每家搜索/耗时/预筛拒因排行/**中外比例 `query_langs`·`discovered_langs`·`kept_langs`**，随批次统计 kv["feeds.batch_stats.<id>"] 落库，网页「这一轮怎么找的」用）。2026-10-05（docs/10 §九 第一步）：① 来源名一个函数 `_source_site`＝`source_name.site_of_url`（真实域名，github/substack/medium/dev.to 按作者），候选、RSS 并池、入库、`_prefilter` 全用它；域名级判断（屏蔽名单 `_domain_blocked`、同域名 ≤3、自动屏蔽、政府站）取域名；② 并池挪到补打开之前、RSS 条目也送补打开；③ 核验 / 补打开交回后模型没日期时用 `_fill_dates_from_pages` 把代码读到的日期补进 `published_ts`（「必须有日期」不放宽）。构想（`make_idea`）入库前过 `idea_feasibility.check`（docs/18 §八）：提示词带能力清单段，不过 → `record_blocked` + 返回 None，过了入库存 normalized JSON。2026-10-05（docs/10 §九 第二步）：RSS 候选入库 `src_provider="rss:<feed_id>"`；`_merge_rss_candidates` 给自动源每轮 ≤2 条候选；入库后 `auto_sources.note_round` 记每源统计。2026-10-05（docs/10 §「第二步剩下三项」第 1 项「按成绩软倾斜搜索」）：定关注点提示词里带近 14 天各类问法的成绩（`search_stats.style_prompt_lines`，贴在「资讯标准」段前，样本 <10 条不列）；`_floor_searches` 里成绩差的「其他家」这轮不补搜（`search_stats.filter_extras`，每周仍放一次，主家不动，漏斗记 `weak_providers`） |
| `idea_feasibility.py` | 构想可行性把关（2026-10-05，docs/18 §八）：`inventory(tool_catalog)` 按子 agent **真实**工具名算能力清单（search=web_search/fetch_page、chat=read_chat_history/search_memory/read_profile、write=write_file、code=run_command、vm=vm_run、machine=machine_*、watch 总有、`mcp_*` → `ext:<名>`≤12；拿不到名单 → 基本能力 search/chat/write/watch；群文件/公告/相册不算）；`CANNOT` / `DELIVER` / `prompt_section(inv)`（群向 feeds 与个人向 personal 共用的中文提示词段）；`check(raw, inv) → (ok, 中文原因, normalized)` 硬拦（level 必须 `ok`、needs_members 必须明确 `false`、uses 非空且全在清单、deliver 认得出且与能力对得上，缺字段一律不过）；`record_blocked` / `blocked_view` 读写 kv `ideas.blocked.<群号>`（近 14 天、≤30 条；view = `{"count", "recent"}`，最多 5 条新的在前） |
| `discovery.py` | 撒网登记簿（两段式第一阶段）：`open_run(task_id)` 开本 / `record(...)` 由 web_search handler 顺手记 / `close_run` 取走关掉；按规范化链接去重，同链留先见、别的问法攒进 queries；没开着的 task_id 忽略 |
| `news_feedback.py` | 自动收的资讯反馈（显式反馈近零的实测后加）：表 news_feedback（`ensure_schema`/`_ensure` 按库对象标记惰性建，不走 store 迁移清单，老库直接用），(kind,item_id,actor,message_id) 唯一幂等；三种——`CardIndex.on_message`（回复/引用资讯卡片，卡片清单每群 60 秒缓存）、`click`（网页 /go/<条目> 点开原文，302 到库里存的原链接；同浏览器同条一天一次、每分钟超 30 次不记）、`mention_round`（群里接着聊：关键词命中候选交 judge 判，判过的 kv 记 7 天，每轮每群最多 5 条/30 句）；actor 一律「群号:账号」sha256 前 16 位；沉默不记负分；`summary` 汇总按条目/总数 |
| `feedback_jobs.py` | 每小时一轮的后台活（app `_feedback_round` 调；`due` 判断）：①mention_round 用主模型当 judge（feeds.mention_judge，json_mode {"yes":[...]}）②**本群做法复盘 + 整理**：调 `lessons.run`（`self.agents` 传进去；画像「最近在聊」`recent_topics` 顺路传进去做话题对照；炸一次不拖垮本轮）；返回 dict 带 `lessons` 计数（本轮改动数）③**自动订阅 + 来源地图** `auto_sources.run`（docs/10 §九 第二步；放在「模型没配好就返回」之前——退订 / 门槛订阅不用模型，照跑；真做了事才把摘要并进 `auto` 键）。口味小结 2026-10-03 已删：迁进本群 news 做法 skill 正文，本文件不再刷新 |
| `lessons.py` | 每群做法的复盘 + 整理（docs/17 §七.3–§七.5、§八）：挂在 `feedback_jobs.run` 后，不另起循环不往群里发。**专岗复盘**（`purpose="skills_reflect.<kind>"`）——每群每岗（news / idea / goal / 自定义）距上次 ≥20h 且自上次起有 ≥1 条新信号才调一次 json_mode 主模型（首次回看 7 天）；信号只取摘要（交接单被打回/失败/通过带意见、资讯点踩快照/评价/自动反应、构想驳回/开工/点赞点踩、目标提议批驳），外加**管理员原话**（聚焦本群、截 200 字、≤10 条；有新原话且那段对话安静 ≥30 分钟 → 门降到 ≥1h）和**群聊话题对照**（只给 news/idea，画像「最近在聊」指纹变了且 ≥72h，本身算一条新信号）。模型回 `{"patch":[{old,new}×≤4]} / {"write":整篇（只在正文空时）} / {"skip"} / {"pass"}`：old 必须恰好出现一处，否则整批作废；改完过隐私闸 + 可疑指令过滤 + 长度上限。**通用执行复盘**（`skills_reflect.task`，每天每群一次）：材料是上次复盘之后结束的 task 交接单（要求 / 交回摘要 / 验收意见各截 300，验收要求保存当轮 `plan.criteria` 快照（≤8 项、每项 200），真实工具名最多 24 种；先按实际工具 ≥3 种 / 被打回 / 失败筛选再取最近 ≤8 条。旧成功记录无可信遥测则保守跳过；不从可用名单或模型成果猜。窗口有界检查 4096 条，撞上检查上限不调模型、不推进进度），和这批活最像的 ≤2 份做法直接带全文（只能改带了全文的），可以 patch 或 create（名字是一类活；满 12 份不能新建）。**每周整理**（`skills_curate.task`）：距上次 ≥7 天且自动未锁定的 task 做法 ≥4 份才调模型；另外 30 天没被读过的自动归档（不调模型）。写库走 `agents.skill_patch_body` / `skill_add`（锁定的 / 已归档的自动流程一律不碰）。状态 kv `lessons.state.<群>.<岗>` = {last_reflect, last_curate, votes, topics_fp, last_topics, last_attempt, last_curate_attempt}；模型失败 / 非 JSON 一律不推进；每个实际改动记真实 `skills.change` 事件（自动归档不因同轮模型失败丢审计）。系统提示写明「没人回应不代表不需要：不许凭没人理写「别发 X」」（用户 2026-10-03 定） |
| `source_name.py` | 来源名统一（2026-10-05，docs/10 §九 第一步 1）：`site_of_url`（真实域名、去 www.；github.com/owner、xxx.substack.com、medium.com/@作者、dev.to/作者；YouTube/B 站视频地址拿不到频道，不按频道拆）、`domain_of`（域名级判断）、`normalize_site`（旧数据里的 site / url_key 归一）。写入、统计、判断都从这一份走 |
| `page_date.py` | 代码从网页读发布日期（2026-10-05，docs/10 §九 第一步 2）：`published_from_html`（meta article:published_time/og:published_time/datePublished/pubdate/date… → JSON-LD datePublished/dateCreated → `<time datetime>`）、`published_from_text`（抓正文工具的 Published Time / 发布时间行）、`normalize_date`（统一 YYYY-MM-DD）、`note`/`date_from_summary`（tool_calls 摘要里的「页面发布日期」标记）、`dates_from_rows`/`page_dates`（按 task_id 取「哪条链接读到哪天」） |
| `source_stats.py` | 本群优质来源名单：近 30 天上网页且五项平均 ≥4 分的条目按**来源名**计数（`source_name`：真实域名；github/medium/dev.to 按作者；**一律按 url 重算**，老库里 site 是「机核」这类中文站名的行也归到域名），半衰期 15 天衰减，「有用」净值每票再 +0.5 条；至少 2 条高分才上名单（样本少不算），管理员移出的（kv["feeds.trusted_removed.<群号>"]，`set_removed`；移出域名时该域名下的作者标签一起移出，移出作者标签只影响那一个）和屏蔽名单不上；`trusted_domains`（≤8 个）喂找资讯 brief（site 直奔最多约三分之一，给新来源留位置）、`source_prior`（第 1 名 1.0 递减到最低 0.3，不在名单 0）给两段式预筛 `_prefilter` 方向内排序用；`view` 给网页 |
| `search_stats.py` | 按成绩软倾斜搜索（2026-10-05 用户拍板，docs/10 §「第二步剩下三项」第 1 项）：不硬改名额，只按成绩倾斜。`provider_rates` / `weak_providers` 算近 30 天各搜索服务的群向候选与进网页率（群向、非 RSS、非个人向；候选 ≥ `PROVIDER_MIN_CANDS`=20 且进网页率 < 主家 `WEAK_RATIO`=0.5 倍 → 弱；主家永不弱，主家样本不够或一条没进时谁都不弱）；`allow_weak_retry` 让弱的那家每周放一次（kv["search.weak_retry.<群号>"] 记上次放行时间，`RETRY_DAYS`=7；没记过也算第一次机会）；`filter_extras` 给 `feeds._floor_searches` 用，返回「这轮还能用的其他家 + 跳过的」，任何出错都原样放行；`style_rates` / `style_prompt_lines` 算近 14 天「中文 / 英文 × 资讯 / 文章」四类问法的成绩（问法语种粗判：有拉丁字母且无汉字 = 英文，跟 `feeds._query_is_english` 同口径、本地实现避免循环导入；kind 只分 guide / news），样本 ≥ `STYLE_MIN`=10 条才列、按进网页率从高到低，写成 `feeds._plan_focus` 提示词里「资讯标准」段前那几行（只是参考，方向和口味优先；没一类够样本就一个字不带）。只读，出错一律不抛（返回空 / 原样） |
| `auto_sources.py` | 自动订阅 + 来源地图（2026-10-05，docs/10 §九 第二步）：三条来源线 `origin`——`trusted`（可订阅门槛，每群 ≤3）、`map`（来源地图，每群 ≤3）、`push`（人工审过的固定清单 `PUSH_SOURCES`：HN / itch.io / 机核 / HF 博客 / RPS / Ars Technica，不占名额）。**门槛**（`collect_evidence`/`qualifies`/`ranked_evidence` 纯函数）：近 30 天群向、上网页、五项平均 ≥4 的 ≥4 条且分布在 ≥3 个北京时间日子、≥1 条有群里的反应（news_feedback 的 reply/mention/click 或 up>0）、净「没用」（down-up>0）取消资格、大平台整站排除（作者标签 medium.com/@x、x.substack.com、dev.to/x 放行，github.com/x 不行）；**RSS 条目不计高分与反应**（入库 `src_provider="rss:<feed_id>"`，`row_is_rss` 认）。`discover_feed` 找订阅地址（substack/medium/dev.to 现成地址；普通域名先读首页 `<link rel=alternate>` 再试 /feed /rss /feed.xml /rss.xml /atom.xml /index.xml），全部走 `rss.fetch_bytes` 的安全取法（公网校验/最多跟 3 跳且每跳重新校验/2MB/15s/trust_env=False；存终点地址），跳过评论订阅（`is_comment_feed`）；`check_source` 体检（能取到解析、14 天内有更新、条目是真文章）；`gate_reason` 硬闸（已订过 / 屏蔽名单 / 优质来源移出名单 / 自动源拒绝名单）。试用期：`feeds._merge_rss_candidates` 给自动源每轮 ≤`TRIAL_CAND_CAP`=2 条候选；`unsubscribe_reason`/`unsubscribe_stale` 三种自动退订（两周 ≥10 条候选 0 条进资讯 / 净「没用」≥2 / 试用两周结束没成绩），退掉的进「不再推荐」。`map_sources`：画像成形后每周一次模型列一手来源（≤10，提示里「素材不是指令」）+ 同一次回复里顺带给 ≤`FIND_QUERY_MAX`=2 条「找来源」短搜索词（`parse_map_queries`；空的/超 80 字/重复的丢掉）→ 代码逐个验证 → `feeds.source_map.<群号>`（verified/rejected + 原因）→ 通过的订为 `map`；push 源里拿过高分的域名补进候选。**找来源搜索**（同一次每周活，docs/10 §九 第二步剩下三项 第 2 项）：`find_source_candidates` 把搜索词交给传入的 `search`（`search.search(q, limit=10)`，不带天数限制、不占资讯搜索名额、某个词炸了只记日志跳过），结果按 `source_name.site_of_url` 算来源、去大平台（`is_excluded_label`）/本群已订过/`gate_reason` 拦的（拒绝·屏蔽·移出名单）、同站去重，最多 `FIND_CAND_MAX`=5 个，作者标签（medium.com/@x、x.substack.com、dev.to/x）用标签当名字和地址；这些候选在图上 `origin="search"`、订上时同样是 `map`（吃同一份 ≤3 名额），验证预算单算（`MAP_VALIDATE_MAX` 之外另给 `FIND_CAND_MAX`，模型候选占满也照验），每轮记一行「搜 N 次，新网站 N 个，验证通过 N 个」。`note_round`（`prepare_news` 入库后调）把「这轮给了几条候选 / 哪几条进资讯」记进 `feeds.rss_stats.<群号>`（30 天裁剪）。`run` 内部节流（`feeds.auto_sources_last.<群号>`：退订+门槛订阅每天、地图+push+找来源搜索每周；`search` 由 app 经 `feedback_jobs.run` 透传，默认 None = 不搜），由 `feedback_jobs.run` 每群每小时调一次；`note_removed` 接网页删自动源 → `feeds.rss_auto_rejected.<群号>`；`load_log` 给网页看 `feeds.rss_auto_log.<群号>`（≤30 条）。**不加表 / 不动迁移**（user_version 34） |
| `rss.py` | RSS 源：订阅管理、解析（RSS2.0/Atom，拒 DOCTYPE）、条目并入同一质量门槛。条目带 `auto`/`origin`/`reason`/`label`/`trial_until`（自动订阅用；老条目读时补默认值）；`fetch_bytes` 是全模块唯一出网点（安全取字节 + `_decode_text`，首页 HTML 也走它；自己逐跳跟随 ≤`_MAX_REDIRECTS`=3 次重定向，每跳重新过 `_public_url` + DNS 固定公网 IP、总时限 / 字节上限共用、跳内网 / 跳太多 / 绕圈都失败且那一跳不发请求；返回 `url` 终点地址） |
| `news_recheck.py` | 「补打开」子 agent：没真打开过的候选重新打开核对（drop/stale/重写摘要）；2026-10-05 起 `from_rss` 的条目不再跳过（排最前，总量照旧 `_RECHECK_CAP`=8 封顶），交回后模型没日期时用 `page_date` 读到的补上 |
| `news_rating.py` | 群友评价资讯（理由+一句话；汇总进下一轮找资讯提示词） |
| `news_viz.py` | 资讯图解：挑数据多的资讯让子 agent 写纯 CSS HTML 小图（程序严格校验） |
| `news_card.py` | 资讯卡片 HTML/PNG 渲染（playwright 截图；cover 配图补齐） |
| `card_push.py` | 往群里发的两种小推送（0.8.0 起设置转发 `group_push`、**只 enqueue 进发件箱**、真发出由结果 hook 回写）：资讯卡片（CardPush；没封面但有图解的条目把图解截图画进卡片；`prune_card_cache` 每日清 7 天前的旧卡片图）/ 构想提一嘴（IdeaMention：按人设**关心式问一句**「话说之前那个……怎么样了？要我帮忙吗？」、≤60 字、结尾问句、不推销；有 `ideas.origin` 就用由头，没有就把标题剥掉「我可以帮…」的头；过 `_leaky` / `_pitchy`（`_PITCH_WORDS`）双词表 + `_template` 兜底），每群开关默认关；Telegram 没有真 @ 段，发消息带 `at_name` 让 @ 退成正文名字 |
| `update_check.py` | 查 GitHub 最新版本提醒（只提醒不自动更新） |

### 开话题与交付
| 文件 | 职责 |
|---|---|
| `topics.py` | 冷场开话题：代码门槛（0.8.0 起开关 / 每日额度 / 睡觉时段都读每群 `group_push`）→ Jev 判（给足判断材料：`speaker` 标 `[机器人自己]`、`last_message_is_bot` / `last_human_minutes_ago` / `note`，机器人自己没人接的话不算「有人在等」；`reason` 题面改成问群里的状态）→ 主模型写开场白（人设走 `voice.persona`，只认 SOUL；自我介绍 / 寒暄 → `rejected:self_intro`；news 候选「随口一提」，idea 候选**关心式问法**，按候选 ref_id 从 `ideas` 读 origin/title/body，不读 basis）→ **只 enqueue 进发件箱**（`push_kind="topic"` + `expires_ts`；`speaker="maibot"` 请 MaiBot 开口那条已退役），真发出由结果 hook 回写 opener / 候选；candidate 池/follow_up |
| `voice.py` | 开口时的人设：`persona(identity) → Persona`（只读 identity 的 main SOUL，不回退读 MaiBot 人格）；`Persona.section()` = SOUL + 「不自我介绍 / 不寒暄、直接说事」规矩，`system()`；`is_self_intro(text)` 生成结果护栏。读不到一律当空、绝不抛。开场白和构想提一嘴共用 |
| `delivery.py` | Mentions 可提起清单（inject 进 planner 请求）、TopicMatcher 关键词接话、Pushes 推送节制（0.8.0 起每群一个每日总上限 + 每群睡觉时段，都从 `group_push` 读；`PUSH_EXEMPT_KINDS` = error/command/admin/awaited_delivery 永远放行；结果不明安全保留额度）。<br/>（原 `chat_feed.py` 已删 2026-10 docs/18：关键词 / 清洗 / 新鲜事问话助手挪回本文件，接得上照旧，只是不再记 `chat_feeds` 表、不再认「聊到了」） |
| `outbox.py` | 发件箱（**0.8.0 起是唯一发送路径**：三种自制消息都只 enqueue；状态机 pending/sending/sent/uncertain/failed/dropped，按 key 去重；发送前 TTL 过期作废 + 生产者 preflight + `pushes.can_push`；图片载荷严格闸（工作区路径 + PNG 魔数 + 1B~8MB）；安全失败隔 300 秒只重试一次、超时 / recover 标 uncertain 不重放；`follow_up_of` 的说明不占第二份额度、不过第二道闸）、任务交付 Delivery（view/file/text 三渠道+回落）、report_error |
| `herenow.py` | here.now 匿名发布客户端（publish/upload/finalize 三步） |
| `scheduler.py` | 什么时辰干什么事（资讯时段偏移 / 构想窗口；睡觉时段读**每群**那份 `kv["group_push.<群号>"].quiet_hours`，`[delivery] quiet_hours` 只作新群种子；只读 kv+groups，不调模型） |
| `commands.py` | /mw 群指令（纯代码固定回复：状态/网页/批准/取消/领取/帮助）。批准 / 拒绝只认**本群**批准名单，请求不属于本群一律拒（群命令只管本群，总管理员也不跨群批）；取消判权限也带 `group_id` |
| `identity.py` | 身份与工作记忆（专岗改版 3/4 + 每群三份收尾）：`agents/<kind>/SOUL.md` / `AGENTS.md`、全局 `MEMORY.md`，`agent_prompt_block()` / `prompt_block()` 注入，「记忆」页只放全局那份。**每群身份层整层退役**（`group_read` / `group_write` / `group_memory_map`、每群记忆段、`note_useless_feedback`）；每群内容归本群规矩 / 本群做法（见 `group_context.py`）。主模型那份 `remember`（roles={"main"}）**只认 `scope=global`，传 group 一律拒且零写入**；写本群规矩只走管理员对话那条独立工具（`tools_admin.remember`，落 `updated_by=admin_chat`） |
| `personal.py` | 关注成员个人向资讯/构想（第二人称写法；绝不能进群视图）；个人向构想的由头 `origin` = **他自己在群里说过想做的那件事**，入库前过 privacy.scrub、命中只置空 origin；构想入库前还过 `idea_feasibility.check`（不过 → `record_blocked(kind="personal")` 不入库，title 只存标题） |

#### 子目录
| 目录 | 职责 | 详图 |
|---|---|---|
| `console/` | MaiWork 网页控制台（aiohttp）：路由/鉴权/视图拼装/头像/用量历史/静态前端 | [console/codemap.md](console/codemap.md) |
| `environments/` | 执行环境：能力判定 + 本机 systemd-run 隔离 + railway.new 一次性 VM + 专用 SSH 机器 | [environments/codemap.md](environments/codemap.md) |
| `platforms/` | 平台档案：QQ/NapCat-OneBot 群空间（群文件/公告/相册） | [platforms/codemap.md](platforms/codemap.md) |
| `builtin_skills/` | 内置 skill（news-standard + 六家搜索服务 search-* + 只给主模型排计划的 find-skills），随插件发布、只读 | （单目录 skill，无子图） |

## 边界规则（改动时必读）

1. **只有 `host.py` 能出现 `call_capability`**；别的模块需要宿主能力，给 Host 加方法（有源码扫描测试守着）。
2. **数据层（store/tasks/goals/approvals/scheduler/commands）不调模型、不调宿主、不发消息**；要副作用的往上层放。
3. **收消息钩子永不中止消息**：intake 里不许慢活（批准/解析全 spawn 后台）；@ 时等 Jev ≤ [jev] timeout_ms 外面再兜 wait_for。
4. **密钥只进不出**：`Tools.call` 遮罩 + `models._redact` + jev/headers 值存 secrets；日志/网页/usage 错误里不许出现密钥。
5. **区分网页写入路径**：业务数据及部分网页扩展设置写 SQLite；「全部配置」和模型等明确修改插件配置的操作经 `config_file` 写回 `config.toml` 并应用。线上写 `plugins/` 会触发全部插件热重载，部署前须经用户当次同意。
6. **个人向/画像内容严格分层**：persona/个人向条目（`target_user_id` 非空）绝不出现在群视图/话题候选/TopicMatcher；`privacy.scrub` 是所有群可见出口的闸。
7. **成品必须回本机工作区** `artifacts/<task_id>/` 才能交付；远端（railway/ssh）产物用 fetch 工具拷回；交付路径闸要求解析后的真实路径在本任务成品目录内。
8. **exFAT 兼容**：数据目录可能在 exFAT 上——chmod 失败静默放行；不依赖原子 rename/临时文件 link（skills_web 新建先 touch 再写）；config.toml 在插件目录（ext 分区）用 open(path,"w") 直接覆盖写。
9. **非服务群零读取**：intake/planner 钩子/排程/群空间，第一步永远是 `is_served` 判断。
10. 改 manifest、插件类、组件注册后必须跑宿主契约测试（[部署与验证](<../../../docs/04-部署与验证.md>) 第二节）。
