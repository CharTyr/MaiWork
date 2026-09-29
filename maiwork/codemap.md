# plugin/CharTyr_MaiWork/maiwork/

MaiWork 插件（`chartyr.maiwork`）的运行时主体：一个和 MaiBot 并行运行的完整 Agent，为配置里列出的服务群提供「理解群 → 主动出资讯/构想、追目标、接派活 → 主模型派子 agent 干活并验收 → 多渠道交付」。宿主入口 `plugin.py`（插件目录上一级）只做生命周期转发，全部逻辑都在这个包里。

## Responsibility

- **装配与生命周期** — `app.py` 的 `MaiWorkApp` 把约 50 个组件接起来：`start()`（load_settings → Store → Host → Models → 各模块 → 控制台 → 后台循环）、`stop()`、`update_config()`（热更新：关→开整 app 重启；开→关全收；开着改配置更新 settings，`console.listen` 变了重启控制台）。`enabled=false` 时什么都不起（不开库、不开端口、不跑循环）。
- **收消息** — `plugin.py` 的 `maiwork_intake` 钩子（`chat.receive.after_process`，BLOCKING，1500ms）转发给 `app.on_message()` → `intake.Intake.handle`；`maiwork_mentions` 钩子（`maisaka.planner.before_request`）转发给 `app.on_planner_before_request()` → `Mentions.inject`。两个钩子永远返回 `{"action": "continue"}`，任何异常吞掉。
- **后台调度** — `app.run_loop_once()`（默认 30 秒一圈）对每个服务群串行跑：profiles.tick（读消息+统计）→ persona/personal 巡检 → topics 开话题 → 排程（资讯/构想/提目标）→ card_push → 名册对名字 → 资讯图解；长活（画像提炼、备资讯、构想、图解）spawn 成独立后台任务（`_spawn_long_job`，同群同种同时只跑一个）；随后 M3 巡检（发件箱 flush、批准提醒/过期、目标到期、queued 任务派工、任务安全网）、用量提醒。

## Design

**装配模式（Composition Root）**：`MaiWorkApp.__init__` 声明全部组件槽位，`_start_stack()` 按依赖顺序装配（库 → 宿主 → 模型/画像 → M2 模块 → M3 模块 → 收消息 → 控制台 → 后台循环）。模块级可选组件用 `_import_m2_class` 惰性导入——feeds/scheduler/coordinator/profile 等没写好时返回 None，不拖垮 app（对应线并行开发的遗留保护）。测试注入点成排暴露（`profiles_cls/feeds_factory/http_transport/jev_transport/...`），外部 HTTP 全部可换 `httpx.MockTransport`。

**单一配置出口**：`app.get_settings()` 是所有模块读配置的唯一入口，三层合并——`config.toml` 原始 Settings（frozen dataclass）× `kv["rules.override"]` 网页规则覆盖（`rules.effective_settings`，一层缓存）× 执行能力判定后的实际工作区根（`_ws_root`，dynamic 落 `/var/lib/private/maiwork/workspaces`）。`Settings` 是 frozen dataclass，覆盖用 `dataclasses.replace` 只换子节。

**数据层纯数据**：`store.py`（SQLite，单写入者 `threading.RLock` + `BEGIN IMMEDIATE`）、`tasks.py`、`goals.py`、`approvals.py` 只放数据和规则：不调模型、不调宿主、不发消息。状态机转移严格按 docs/02 §7.2 表，非法转移抛 `ValueError`，转移在一个事务里完成（改状态 + 写事件）。取消/终态后晚到的结果一律不接（`accept_result=False`），任务状态绝不复活。

**主模型协调器 — 子 agent 执行**（`coordinator.py` + `workers.py`）：主模型不亲自干长活。`Coordinator.run_task(task_id)` 拥有任务整个生命周期：`asyncio.Lock` per workspace（同一工作区同一时刻一个主模型回合）、`asyncio.Semaphore` per workspace 限子 agent 并发（`[environments] max_parallel`）。一轮尝试 = `_plan`（json_mode 出 criteria/deliver_kind/jobs/env，或用 ≤6 轮只读工具查资料再定）→ 派 `Workers.run(brief, tools=[...])`（子 agent 多轮循环，只能 `submit_result` 交回，不能宣布完成）→ `_review`（验收：deliver_kind≠text 时 artifact 必须真实存在于 `artifacts/<task_id>/`，调研类任务做引用核对）→ 交付/重来/失败。执行环境三选一：`local`（本机隔离）/`ssh`（专用机器 `machine_*` 工具）/`railway`（一次性 VM `vm_*` 工具），远端成品必须拷回本机工作区才算数。

**工具注册表 — 角色门控**（`tools.py`）：`Tools.call()` 是子 agent/主模型/管理员调工具的唯一入口，每次调用（无论成败）写 `tool_calls` 表，密钥参数统一替换成 `***`。`ToolContext.role`（worker/main/admin）决定能用哪些工具——`specs("worker")` 给子 agent，`specs("main")` 给主模型（验收/排计划回合），admin 工具由 `tools_admin.register_admin_tools` 注册。

**信号驱动**（`intake.py` 的 `Signals`）：收消息钩子只往内存 map 记「这个群有新消息」（mark）；后台循环 `signals.take()` 取走后驱动 profiles.tick。planner 钩子靠 `groups.session_id` 持久映射 + 内存信号认群，非服务群零读取。

**隐私与红线编码化**：`privacy.scrub`（关注成员 note/persona 长片段不得出现在群可见文字）、`members.render`（`{@平台id}` token 统一换当前显示名，平台 id 不外漏）、`host.py` 是唯一允许出现 `ctx.call_capability` 的文件（别的模块禁止，有源码扫描测试守着）、`delivery.Pushes`（每日上限+睡觉时段）、`names.py`（QQ 群名乱码清洗）。

## Flow

**收消息 → 理解**：群消息 → `plugin.maiwork_intake` → `app.on_message` → `Intake.handle`：非服务群立刻 continue（零 Jev/零 Store）；记 Signal、记名册、记 chatlog；`/mw` 开头的转 `commands.py`（纯代码固定回复，不调模型，回复进 Outbox）；被 @ 时在 1500ms 内等 `jev.Jev` 判 kind（prepare/goal/reminder/none，把握 ≥0.6）→ prepare/goal 后台 `approvals.create`（未批准不开工）、reminder 后台回调主模型解析时间建成员目标、判不了/超时写 `pending_asks` 表留给读群提炼。

**主动产出（后台循环一圈）**：
1. `profiles.tick(gid)` 增量读群消息、维护统计；needs_refresh → spawn 画像提炼（主模型）。
2. `scheduler.check` 报时辰：资讯时段（`[feeds] news_slots` + 按「群号+日期+时段」播种的固定随机偏移，45 分钟内有效）、构想时段、主动提目标时段；睡觉时段静默，画像没成形永远空。
3. 到点 → spawn 长活 `feeds.prepare_news(gid)`：主模型按画像出 3–5 关注点 → `Workers.run`（web_search/fetch_page）找资讯+好文（RSS 源从 `rss.py` 并入同走质量门槛，没真打开的经 `news_recheck` 补打开）→ 三道门槛（硬性淘汰/打分上网页/进话题候选池）→「有人味」写帖子 → 入库 `news_batches/news_items`，过第三道的进 `topics` 候选池。构想 `feeds.make_idea` 类似。
4. `topics.check(gid)` 冷场开话题：代码第一层（时段/上限/安静时长）→ Jev 第二层（ok≥0.6 且 fit≥0.5）→ 主模型按人设写开场白发出去。
5. `card_push.scan/flush` 发资讯卡片图（`news_card.render_png` playwright 截图）和构想提一嘴。
6. M3 巡检：`outbox.flush`（发件箱投递，失败重试、超时标 uncertain）、`approvals` 提醒/过期、`goals` 到期提醒（成员目标）和 `Coordinator.check_goal`（agent 目标周期验收）、queued 任务 `Coordinator.run_task` 派工、`tasks.net_check` 任务安全网（token/时长超线自动 paused）。

**派活（M3 全链路）**：群友 @ 派的活 → `approvals.create` →（低风险轻活 `auto_review` 自动批，否则等管理员在网页或 `/mw 批准`）→ `Tasks.create(status=queued)` → `Coordinator.run_task`：`_plan` 定完成标准/交付形态/子任务/执行环境 → `tasks.transition(running)` + `start_attempt` → 子 agent 干活（成品写工作区 `artifacts/<task_id>/`）→ `submit_result` 交回 → `_review` 验收（只读工具 inspect_file(s) 核对，真实存在闸、引用核对、符号链接逃逸检查）→ 通过 → `Delivery.deliver` 经 `Outbox` 交付（view=here.now 发布网页 / file=群文件+说明 / text=群文字），渠道失败时回落备选方式，成功进可提起清单（ttl 6 小时）。

**配置热更新**：宿主文件监控/`on_config_update` 或网页「全部配置」保存（`config_file.write_fields` 直写 config.toml，`apply_config_text` 立刻本进程应用）→ `update_config`：幂等比较 → 重判执行能力 → `_ensure_groups` → `_reconcile_unserved`（删掉的服务群残留就地标记：发件 cancelled/任务 cancelled/待批 expired）→ console.listen 变了重启控制台 → 群空间开关热生效。

## Integration

- **MaiBot 宿主**：唯一通道 `host.py`（`Host(ctx)`，`call_capability` 统一超时、返回统一 dataclass、异常包装 `HostError`）。用的宿主事实都查自 docs/06：bot_qq、chat 消息读取、knowledge.search（长期记忆）、send_text/传群文件、proactive_trigger。绝不改 MaiBot planner、不改群回复频率（红线）。
- **console/**（子目录，详图见 <console/codemap.md>）：`ConsoleServer`（aiohttp，监听 `[console] listen`）+ `views.py`（GroupSummary/GroupView/Settings 纯函数拼装）+ `auth.py`（管理员/群管理员 cookie、群友链接码、登录限流）+ `avatar.py`（头像缓存与签发）+ `usage_history.py`（用量历史）。app 在 `_start_stack` 第 6 步建 avatar 再 start console。
- **environments/**（子目录，详图见 <environments/codemap.md>）：执行环境三件套——`capability.probe` 判定 fixed/dynamic/stopped → `local.LocalEnv`（systemd-run 固定用户或 DynamicUser 隔离；direct 模式仅供本机开发）、`railway.RailwayEnv`（railway.new 一次性 VM，每天限额、同时 1 台）、`ssh.SshEnv`（用户 VPS，ed25519 key 部署）。`Coordinator` 按计划 JSON 的 `env` 字段选择；`tools_exec/tools_railway/tools_ssh` 的工具落点都由对应 env 的 resolve 校验防越界。
- **platforms/**（子目录，详图见 <platforms/codemap.md>）：`qq_onebot.GroupSpace`（群空间：群文件/公告/相册/群相册上传），启动 `probe()` 探测适配器能力（`host.list_apis()`）+ 机器人群身份缓存；防手滑登记 `group_files_owned` 表（outbox 上传成功 → `register_owned`），只许动机器人自己传的文件。
- **plugin.py**（上级目录）：声明 config model、生命周期（on_load/on_unload/on_config_update）转发、两个 HookHandler 注册到 MaiBot；`MaiWorkApp` 由它创建并持有。
- **外部服务**：OpenAI 兼容模型端点（`models.py`，端点级限流/冷却/重试/备用模型）、TypeSafe Jev（`jev.py`，熔断+judgments 落库）、Jina Reader（`reader.py`，打开网页首选路）、MCP 扩展（`extensions.py` + `mcp_client.py`，Streamable HTTP 最小客户端；搜索 `search.py` 只能走 kv["extensions.search"] 绑定的那个 MCP 工具）、here.now 匿名发布（`herenow.py`，24 小时过期的交付网页）、RSS 源（`rss.py`）、GitHub 更新检查（`update_check.py`，只提醒不自动更新）。

## 模块导航（按特性分组）

### 装配与配置
| 文件 | 职责 |
|---|---|
| `app.py` | MaiWorkApp 装配、生命周期、后台循环、热更新、planner 钩子、长活派工 |
| `config.py` | pydantic 配置模型 + `load_settings` 规范化成 Settings（非法条目丢弃记中文问题，绝不抛） |
| `config_file.py` | config.toml 读写层（tomlkit 保注释；写前备份到数据目录；绝不在插件目录建临时文件） |
| `rules.py` | kv["rules.override"] 网页规则覆盖层 + `effective_settings` 合并 + 字段校验 |
| `migrations.py` | 启动时一次性迁移（幂等）：数据库旧覆盖层/secrets → config.toml；搜索配置 → 扩展绑定 |
| `store.py` | SQLite 存储：连接、表迁移（_m1 起一串）、事务、kv/secrets/events 助手 |
| `clock.py` | epoch/北京时间换算、睡觉时段判断（存库一律 epoch 秒） |
| `onboarding.py` | 首次安装引导状态（kv["onboarding"]） |

### 收消息与理解群
| 文件 | 职责 |
|---|---|
| `intake.py` | 钩子逻辑：Signal 记录、/mw 分流、@ 识别（Jev）、pending_asks；永不中止消息 |
| `profile.py` | 群画像：增量读消息、活跃度统计、主模型提炼画像（攒批/到点/每周整理）、PROFILE-<群号>.md 同步、关注成员管理 |
| `members.py` | 成员名册（平台 id 认人、显示名跟进改名、`{@id}` token 渲染出口） |
| `names.py` | QQ 群名乱码清洗（进库/进视图统一过 clean_group_name） |
| `chatlog.py` | 最近 14 天群发言只读副本（FTS5 trigram，search_chat 给资讯写原话依据） |
| `persona.py` | 关注成员个人画像（只给管理员；refresh/due；永不出现在群消息里） |
| `privacy.py` | 隐私闸：关注成员 note/persona 片段不得进群友可见文字 |

### 主模型与子 agent 执行
| 文件 | 职责 |
|---|---|
| `coordinator.py` | 主模型协调器：计划/派活/验收/交付、执行环境选择、任务生命周期、agent 目标检查 |
| `workers.py` | 子 agent 执行器：多轮工具循环、submit_result 交回、时间盒强制交回、上下文压缩接线 |
| `compaction.py` | 上下文压缩（估算截 tool 结果 → 摘要最老一段）+ RepeatCallNudger + 大结果落盘 spill |
| `models.py` | OpenAI 兼容客户端：重试/备用/端点限流、用量落库、密钥只进不出（_redact 统一遮） |
| `jev.py` | TypeSafe Jev HTTP 客户端：密钥读取顺序、答案校验、熔断、judgments 落库 |
| `admin_chat.py` | 管理员与主模型的网页对话循环（聚焦群、工具小票待确认） |
| `usage_alerts.py` | token 超阈值提醒（只在网页设置页展示，不往群里发、不暂停） |

### 工具系统
| 文件 | 职责 |
|---|---|
| `tools.py` | 工具注册表：register/call 唯一入口、tool_calls 落库、密钥遮罩、角色判定 |
| `tools_builtin.py` | 内置工具：web_search/fetch_page（Jina→抓正文 MCP→直接打开，防内网跳转）/read_profile/submit_result |
| `tools_exec.py` | 执行工具：read/write/list_file、run_command/start|check|stop_process、read_chat_history/search_memory、inspect_file(s)（主模型验收只读） |
| `tools_railway.py` | 一次性 VM 工具：vm_run/vm_put_file/vm_read_file/vm_fetch_file |
| `tools_ssh.py` | 专用机器工具：machine_run/machine_put_file/machine_read_file/machine_fetch_file |
| `tools_admin.py` | 管理员对话工具（roles=admin；危险动作写待确认小票 admin_chat_pending） |
| `tools_groupspace.py` | 群空间工具（roles=main）：group_files_list/group_file_manage/group_notice_send/group_album_upload |
| `skills.py` | skill 目录读取（front matter 极简解析、roles 过滤、内置 skill 优先、只读） |
| `skills_tools.py` | list_skills/read_skill 工具（内容按调用者角色过滤） |
| `skills_web.py` | 网页管理 skill（落 <data_dir>/skills/，只管网页新增删掉） |
| `builtin_skills/` | 随插件发布的内置 skill（`news-standard`：资讯标准，程序按环节注入提示词） |
| `extensions.py` | MCP 扩展加载器：[[extensions.mcp]] → McpSessionClient → tools/list → 注册 mcp_* 工具 |
| `mcp_client.py` | MCP Streamable HTTP 最小客户端（initialize/initialized/tools/call，会话失效重连） |
| `extensions_web.py` | 网页管理 MCP 扩展（kv 存配置不含密钥值，密钥存 secrets["mcp.*"]） |
| `search.py` | 搜索通用 MCP 适配层：参数映射/结果归一化/extract 抓正文（只走扩展绑定） |
| `search_binding.py` | kv["extensions.search"] 搜索绑定读写、候选猜测、状态说明 |
| `reader.py` | Jina Reader 客户端（打开网页首选路；本地限速+冷却；有任何问题算失败换下一条） |
| `news_standard.py` | 按环节读内置 skill 段落注入资讯流水线提示词 |

### 任务、目标、批准（M3 数据层）
| 文件 | 职责 |
|---|---|
| `tasks.py` | 任务状态机、尝试记录、net_check 安全网（token/时长线）、interrupt_orphaned |
| `goals.py` | agent 目标（G-n）/成员目标（M-n）：提醒、问进展、循环续期 |
| `approvals.py` | 派活待批：免批判断、approve/reject、pending 提醒/过期、取消权限 |
| `auto_review.py` | 低风险轻活主模型自动批（每群每日上限；goal 类/构想含 goal 永远留人批） |
| `goal_proposal.py` | MaiWork 主动提目标（每日一次，永远要管理员批准） |
| `group_admins.py` | 按群管理员：密码哈希/本群名单只进库（/mw 批准 要用） |

### 资讯与构想
| 文件 | 职责 |
|---|---|
| `feeds.py` | 资讯/构想流水线：关注点 → 子 agent 找候选 → 三道门槛 → 写帖子 → 入库 |
| `rss.py` | RSS 源：订阅管理、解析（RSS2.0/Atom，拒 DOCTYPE）、条目并入同一质量门槛 |
| `news_recheck.py` | 「补打开」子 agent：没真打开过的候选重新打开核对（drop/stale/重写摘要） |
| `news_rating.py` | 群友评价资讯（理由+一句话；汇总进下一轮找资讯提示词） |
| `news_viz.py` | 资讯图解：挑数据多的资讯让子 agent 写纯 CSS HTML 小图（程序严格校验） |
| `news_card.py` | 资讯卡片 HTML/PNG 渲染（playwright 截图；cover 配图补齐） |
| `card_push.py` | 往群里发的两种小推送：资讯卡片（CardPush）/ 构想提一嘴（IdeaMention），每群开关默认关 |
| `update_check.py` | 查 GitHub 最新版本提醒（只提醒不自动更新） |

### 开话题与交付
| 文件 | 职责 |
|---|---|
| `topics.py` | 冷场开话题：代码门槛 → Jev 判 → 主模型写开场白；candidate 池/follow_up |
| `delivery.py` | Mentions 可提起清单（inject 进 planner 请求）、TopicMatcher 关键词接话、Pushes 推送节制 |
| `outbox.py` | 发件箱（状态机 pending/sending/sent/uncertain/failed，按 key 去重，recover 不重放）、任务交付 Delivery（view/file/text 三渠道+回落）、report_error |
| `herenow.py` | here.now 匿名发布客户端（publish/upload/finalize 三步） |
| `scheduler.py` | 什么时辰干什么事（资讯时段偏移/构想/提目标窗口；只读 kv+groups，不调模型） |
| `commands.py` | /mw 群指令（纯代码固定回复：状态/网页/批准/取消/领取/帮助） |
| `identity.py` | 身份与工作记忆：SOUL.md/AGENTS.md/MEMORY.md/memory/<群号>.md，prompt_block 注入，remember 工具 |
| `personal.py` | 关注成员个人向资讯/构想（第二人称写法；绝不进群视图） |

#### 子目录
| 目录 | 职责 | 详图 |
|---|---|---|
| `console/` | MaiWork 网页控制台（aiohttp）：路由/鉴权/视图拼装/头像/用量历史/静态前端 | [console/codemap.md](console/codemap.md) |
| `environments/` | 执行环境：能力判定 + 本机 systemd-run 隔离 + railway.new 一次性 VM + 专用 SSH 机器 | [environments/codemap.md](environments/codemap.md) |
| `platforms/` | 平台档案：QQ/NapCat-OneBot 群空间（群文件/公告/相册） | [platforms/codemap.md](platforms/codemap.md) |
| `builtin_skills/` | 内置 skill（news-standard），随插件发布、只读 | （单目录 skill，无子图） |

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
10. 改 manifest、插件类、组件注册后必须跑宿主契约测试（docs/04 第二节）。
