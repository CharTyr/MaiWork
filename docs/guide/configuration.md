# 配置说明

[← 回到 README](../../README.md)

日常使用中，**95% 的设置都直接在 MaiWork 网页上点点鼠标就行**，改完立刻生效，不用手改配置文件，也不用重启 MaiBot。

这篇指南先讲清楚几个核心设置是怎么配合的，再附上 `config.toml` 的常用配置表供你查阅。

---

## 一、日常最常碰的几样设置

### 1. 模型怎么配：先连服务商，再给各岗位挑模型

MaiWork 把模型分成好理解的三步（都在网页「设置 → 模型」和「设置 → 专岗」里操作）：

1. **加一个端点（服务商）**：填上接口格式（`openai` / `anthropic` / `responses`）、地址和 API 密钥。如果你的中转站或服务商要求带特殊的请求头（比如自定义 `User-Agent` 或额外鉴权头），点开端点里的「高级设置」，按行填或直接粘贴一段 JSON 即可；填进去的头跟密钥一样只进不出。
2. **往模型库里加模型**：从这个端点下拉取模型列表勾选，或者直接手填模型名字；顺手标一下它支不支持看图（`vision`）、思考强度（`efforts`）以及是用来聊天干活（`chat`）还是用来画图（`image`）。
3. **给专岗挑模型**：MaiWork 里有主模型、资讯、构想、目标、任务这几个岗位。**你不单独给专岗挑模型也完全能用——没挑的岗位默认直接跟主模型用同一个。** 等你想省钱或提速了（比如让主模型用聪明的大模型、找资讯用便宜快的小模型），再去「设置 → 专岗」里分开选，还可以给每个岗位设一个备用模型。

> 找资讯时为了不卡死在一处，如果某个模型网络报错，它会直接换你设好的备用模型，不会在同一个超时模型上傻等五次。

### 2. 联网搜索与做法（skill）

- **联网搜索**：首次引导时直接勾几个预设搜索服务就行（有免密钥即开即用的），排第一的当主搜索，其余当替补——主搜索偶尔抽风时替补自动顶上。平时随时能在「设置 → 工具」里调整。就算你一个搜索都不配，只要给群绑了 RSS 源，资讯照样能靠 RSS 出内容。
- **做法（skill）与专岗工具**：插件自带了 6 份写好的做事规范（调研报告、资讯核实、通用任务、目标追踪、构想整理、办公文档），在「设置 → 工具」里能随时开关或新建。新建或编辑做法时，你还可以勾选这份做法**要用的工具**（包括内置工具或已连上的 MCP 工具）——把这份做法挂给资讯、构想或目标专岗后，它们干活时就能直接调用这些工具了（勾选跑命令、写文件这类执行工具时，系统会先弹确认框让你拍板）。
- **办公文档与画图组件**：做 Word / Excel / PPT 的依赖包、画资讯卡片要用的浏览器和中文字体，都在「设置 → 工具 → 可选组件」里一键安装，装在 MaiWork 自己的目录里，不污染 MaiBot 的环境。

### 3. 记忆、「本群规则」和「本群做法」

MaiWork 记东西分两层，绝不串群：
- **全局记忆**（在「设置 → 记忆」）：只记跟具体哪个群、哪个人无关的通用经验。
- **每个群自己的规矩与经验**（在对应群页面的「本群」区）：
  - **本群规则**：你或本群管理员定下的硬规矩，MaiWork 只读不改（连往后追加都不会自己动手）。
  - **本群做法**：它从每次任务验收、群友反应和你的对话里自己总结出来的经验，每天最多复盘一次、每周整理一次。你可以随时动手改它；如果没锁定，MaiWork 仍会按新反馈继续完善，不想让它再改就锁定。锁住或归档的做法它绝不再碰，改坏了也能一键回退到历史版本。
  - **群画像与关注成员画像**：只在网页后台给管理员看，绝不会发到群消息里。

### 4. 多平台与群友网页链接

- **支持哪些聊天平台**：服务群支持写成 `qq:群号`（NapCat / OneBot）、`telegram:群号`（或 `tg:群号`）、`qqbot:群openid`（QQ 官方机器人）。其中传群文件、发群公告、传群相册是 QQ（OneBot）独有的能力，其他平台做完任务统一发网页链接。
- **群友怎么打开网页**：默认不用你配域名或反向代理——MaiWork 会把群友要看的那一页自动发到 here.now（匿名临时网页，24 小时过期）。如果你有自己的服务器域名，在设置里填上 `public_url`，就会优先发你自己的固定链接；如果完全不想对外发链接，把「用 here.now 自动公开」关掉即可，群里就只发纯文字。

---

## 二、`config.toml` 常用配置项速查

完整带中文注释的范例见插件目录下的 [`config.example.toml`](../../config.example.toml)。下面挑最常用的列出来：

### 群与关注

| 配置项 | 类型 | 默认值 | 用大白话说 |
|:------|:-----|:-------|:-----|
| `plugin.enabled` | bool | `false` | 插件总开关，改成 `true` 才开始工作 |
| `groups.serve` | list | `[]` | 要服务的群列表，每项写 `group = "平台:群号"`（如 `"qq:123456789"`），没写进来的群完全不碰 |
| `focus.max_members` | int | `5` | 每个群最多重点关注几位活跃成员 |
| `focus.personal_profile` | bool | `true` | 是否为关注成员整理私下画像（只给管理员看） |

### 资讯与每日推送

| 配置项 | 类型 | 默认值 | 用大白话说 |
|:------|:-----|:-------|:-----|
| `feeds.news_slots` | list | `["08:30","14:00","19:00"]` | 每天几点备好新资讯（北京时间） |
| `feeds.max_items` | int | `10` | 每一批最多留几条资讯 |

> **每个群自己的推送开关**（每天最多主动发几条、几点到几点免打扰、开不开资讯卡片和构想提一嘴）都按群保存在数据库里，直接在网页「群 → 主动发言」里改。

### 派活审批与群内连续对话

| 配置项 | 类型 | 默认值 | 用大白话说 |
|:------|:-----|:-------|:-----|
| `approval.auto_review` | bool | `true` | 低风险小活（查资料、做个小网页等）让主模型审一眼就自动放行开工 |
| `approval.auto_review_daily` | int | `5` | 每个群每天最多自动放行几件，`0` 表示全部要人批 |
| `approval.remind` | bool | `true` | 有人派了活超过 24 小时还没批时，在群里轻声提醒一次 |
| `session.enabled` | bool | `true` | 群内连续对话：任务开工后，派活的人在群里直接 @ 就能改要求、问进度、说结束 |
| `session.idle_minutes` | int | `60` | 连续对话多久没人说话就自动收起（分钟） |
| `tasks.token_limit` | int | `2000000` | 单个任务最多花多少 token，到了自动暂停等你决定 |
| `tasks.run_seconds` | int | `10800` | 单个任务最长跑多少秒（默认 3 小时） |

> **谁能批这个群的活、要不要开启审批、谁免批**，同样是每个群单独一份，在网页「群 → 任务审批」里设。

### 模型

模型推荐直接在网页「设置 → 模型」里添加和编辑，它会自动帮你写成下面的格式：

| 配置项 | 类型 | 默认值 | 用大白话说 |
|:------|:-----|:-------|:-----|
| `[[endpoints]] id` | string | — | 服务商短代号（小写字母、数字、`_`、`-`） |
| `[[endpoints]] protocol` | string | `"openai"` | 接口格式：`openai` / `anthropic` / `responses` |
| `[[endpoints]] base_url` | string | `""` | 接口地址（`http://` 或 `https://` 开头） |
| `[[endpoints]] api_key` | string | `""` | API 密钥（网页上只进不出） |
| `[[endpoints]] headers` | table | `{}` | 可选的自定义请求头覆盖（最多 32 项，值只进不出） |
| `[[endpoints]] retries` / `retry_delay_s` | int | `5` / `10` | 请求失败重试几次 / 每次间隔几秒 |
| `[[endpoints]] max_concurrency` / `max_rpm` | int | `2` / `0` | 同时最多几个请求 / 每分钟最多几次（`0` 不限） |
| `[[model_list]] id` | string | — | 模型库里的条目代号 |
| `[[model_list]] endpoint` | string | — | 属于上面哪个服务商端点 |
| `[[model_list]] model` | string | — | 服务商那边的真实模型名 |
| `[[model_list]] name` | string | `""` | 在网页上显示的好认名字（留空就显示真实模型名） |
| `[[model_list]] efforts` | list | `[]` | 支持的思考强度（`low` / `medium` / `high` / `xhigh` / `max`） |
| `[[model_list]] vision` | bool | `false` | 能不能看图片 |
| `[[model_list]] context_window` | int | `128000` | 最大上下文长度（tokens） |
| `[[model_list]] max_tokens` | int | `32768` | 单次回复最大输出长度（必须小于上下文长度） |
| `[[model_list]] use` | string | `"chat"` | 用途：`chat`（聊天干活）或 `image`（AI 生图） |
| `[[model_list]] image_api` | string | `"openai"` | 生图接口风格：`openai`（含火山方舟）或 `siliconflow`（硅基流动） |
| `[[model_list]] image_default_size` | string | `""` | 生图默认尺寸，留空按服务商默认 |
| `[[model_list]] image_max_n` | int | `1` | 单次最多生成几张图（1~4） |

### 干活环境

| 配置项 | 类型 | 默认值 | 用大白话说 |
|:------|:-----|:-------|:-----|
| `environments.ssh` | list | `[]` | 你自己的专用服务器列表，每台填 `name` / `host`（`user@IP:端口`）/ `note` |
| `environments.railway` | bool | `true` | 允许借用 railway.new 免密钥一次性云端机器 |
| `environments.freestyle` | bool | `false` | 允许借用 Freestyle 一次性云端机器（需填 API key，支持与 Railway 同时开互为替补） |
| `environments.vm_platform` | string | `"railway"` | 两家临时机器都开时优先用哪家（`railway` 或 `freestyle`） |
| `environments.freestyle_api_key` | string | `""` | Freestyle 的密钥（也可通过环境变量 `FREESTYLE_API_KEY` 传入） |
| `environments.freestyle_snapshot` | string | `"freestyle/ubuntu-sm"` | Freestyle 机器规格：`freestyle/ubuntu-sm`（2 核 4G）或 `freestyle/ubuntu`（4 核 8G） |
| `environments.freestyle_ttl_minutes` | int | `90` | Freestyle 单台机器最长存活多少分钟 |
| `environments.freestyle_idle_minutes` | int | `10` | Freestyle 机器闲置几分钟自动休眠 |
| `environments.freestyle_daily_max` | int | `5` | 每天最多开几台 Freestyle 机器 |
| `environments.run_as` | string | `"maiwork"` | 本机 Linux 隔离跑命令用的系统账号（**禁止填 `root`**） |
| `environments.local_network` | bool | `false` | 是否允许任务代码访问本机（`127.0.0.1`）和内网地址，默认严禁 |
| `environments.memory_max` | string | `"512M"` | 本机隔离跑命令的内存上限 |
| `environments.runtime_max_sec` | int | `1800` | 本机单次命令最长运行秒数 |
| `environments.verify_enabled` | bool | `false` | 资讯实测：挑几条技术资讯真开台机器跑一下试试 |
| `group_space.enabled` | bool | `true` | 是否允许操作 QQ 群文件、群公告、群相册 |

### 网页控制台

| 配置项 | 类型 | 默认值 | 用大白话说 |
|:------|:-----|:-------|:-----|
| `console.listen` | string | `"127.0.0.1:18650"` | 网页监听地址（Docker 里需改成 `"0.0.0.0:18650"`） |
| `console.password` | string | `""` | 总管理员密码，留空会自动生成在 `data/maiwork/console_password.txt` |
| `console.public_url` | string | `""` | 你自己配好反向代理后的外网访问网址；填了就优先发这个网址 |
| `console.trusted_proxies` | list | `[]` | 反向代理服务器的 IP 或网段（填上后，密码输错限流才按真实访客 IP 算） |
| `console.herenow_share` | bool | `true` | 没填外网网址时，自动把群友要看的页面发到 here.now 临时公开页 |
| `console.update_check` | bool | `true` | 打开网页时自动看一眼 GitHub 有没有发新版本 |
| `console.maibot_webui_url` | string | `""` | MaiBot 自身 WebUI 的地址，填了在更新提醒里会有直达按钮 |
| `console.board_hours` | int | `24` | 「看板」页默认显示最近多少小时内的动态（1~168） |
