/* MaiWork 控制台原型 · 示例数据（全部是虚构的，只为看效果） */
window.MW = {
  now: { label: "周六 19:40", minutes: 19 * 60 + 40 },
  bot: { name: "东雪莲", avatar: "assets/bot.jpg" },

  usage: {
    today: [
      { label: "主模型", value: "212.4k", unit: "tokens" },
      { label: "子 agent", value: "486.9k", unit: "tokens" },
      { label: "Jev 判断", value: "37", unit: "次" },
    ],
    alert: "每日提醒线 1.5M tokens，今天用了 46%",
  },
  // 模型设置：密钥只存在服务器上，网页只知道「填了没有」
  models: {
    baseUrl: "https://api.example.com/v1",
    keySet: true,
    main: "deepseek-v4",
    mainBackup: "qwen3-max",
    worker: "qwen3-coder-plus",
    workerBackup: "",
    available: ["deepseek-v4", "deepseek-v4-flash", "qwen3-max", "qwen3-coder-plus", "kimi-k2", "glm-4.6"],
    checked: "今天 19:12 测过 · 找到 6 个模型",
  },
  health: [
    { icon: "robot", name: "模型端点", state: "ok", text: "正常 · 中位响应 2.1 秒" },
    { icon: "sparkles", name: "Jev", state: "ok", text: "正常 · 中位响应 540 毫秒" },
    { icon: "monitor", name: "本机执行环境", state: "ok", text: "正常 · 空闲内存 2.1G · 2 个子 agent 在跑" },
    { icon: "cloud", name: "Railway 临时机", state: "off", text: "没有启用" },
  ],
  rules: [
    { icon: "moon", name: "睡觉时段", text: "23:00 – 08:00 不开话题，其他主动消息推迟到早上" },
    { icon: "speech", name: "开话题", text: "每群每天最多 2 次，两次至少隔 3 小时" },
    { icon: "bell", name: "主动推送上限", text: "每群每天 3 条（含开话题）" },
    { icon: "lock", name: "派活批准", text: "需要管理员批准 · 管理员 2 人" },
  ],

  groups: [
    /* ───────────────────────── 折腾研究所 ───────────────────────── */
    {
      id: "tinker",
      token: "k7Qm2x",
      name: "折腾研究所",
      icon: "tools",
      members: 214,
      quiet: { text: "安静 9 分钟", usual: "平时这个点 4 分钟一条" },
      pulse: {
        factor: 1,
        seed: 7,
        spells: [
          { from: "14:29", to: "15:10", note: "冷场 41 分钟 · 有问题还没人回，没开话题" },
          { from: "18:57", to: "19:31", note: "冷场 34 分钟 · 开了话题" },
        ],
        topics: [{ at: "19:31", label: "Muse early access", replies: 6 }],
      },
      today: { news: 3, topics: 1, pending: 2, running: 2 },
      upcoming: [
        { icon: "alarm", time: "20:00", text: "提醒蓝莓山竹：今晚把 case 发群里" },
        { icon: "bullseye", time: "21:00", text: "检查「插件踩坑文档」进度" },
        { icon: "newspaper", time: "明早 08:30", text: "下一批资讯备料" },
      ],

      news: [
        {
          slot: "周六傍晚",
          meta: "19:06 备料 · 找了 23 条，留下 2 条",
          items: [
            {
              icon: "robot",
              title: "Muse 新功能开 early access：发一句话就能排号",
              body:
                'Meta 昨天在 X 上放了个入口：对 Muse 说一句「Can you let the Muse team know I want to be part of the Muse early access program?」，就排进了 <a href="#">Muse 新功能的 early access</a>。这批包括能视频聊天的实时数字分身、一大波购物和连接器合作，还有 Mac 端的电脑操作。',
              why: "周四群里聊了一整晚「个人 agent 该不该主动找你」，这条正好是现成的例子。",
              sources: [
                { site: "x.com", title: "Muse early access 入口" },
                { site: "about.fb.com", title: "Connect 2026 发布汇总" },
              ],
              status: { kind: "used", text: "19:31 冷场时拿来开了话题 · 6 人接话" },
            },
            {
              icon: "rocket",
              title: "railway.new：一行 ssh 拿到一台临时 Linux，60 分钟内随便折腾",
              body:
                '<a href="#">railway.new</a> 大约 1.4 秒给你一台 2 核 2G 的机器，预装 Python、Node 和几个编码 agent。不认领的话 24 小时后连文件一起删，适合跑一次性的脚本。',
              why: "阿柒昨天问哪里能白嫖一台临时机跑备份迁移的测试。",
              sources: [{ site: "railway.new", title: "官方说明" }],
              status: { kind: "pool", text: "在话题候选里 · 还能放 11 小时" },
            },
          ],
        },
        {
          slot: "周六午后",
          meta: "14:10 备料 · 找了 14 条，留下 0 条",
          skipped: "这一批没有值得看的，跳过了。",
        },
        {
          slot: "周六早上",
          meta: "08:44 备料 · 找了 19 条，留下 2 条",
          items: [
            {
              icon: "chart",
              title: "有人把 GLiNER 做成了 0.3B 的「决策」小模型",
              body:
                '作者在 <a href="#">GLiNER2.5-Decide</a> 的说明里给了延迟对比：CPU 上单次判断 40 毫秒左右，适合放在请求前面做快速的是非判断。',
              why: "群里这周在比 Jev 和本地小模型，缺的就是一个能直接跑的对照。",
              sources: [{ site: "huggingface.co", title: "GLiNER2.5-Decide 模型卡" }],
              status: { kind: "mentioned", text: "10:15 MaiBot 在大家聊到 Jev 时顺口提过" },
            },
            {
              icon: "floppy",
              title: "一篇讲「拿 SQLite 当消息队列」的长文，作者在 WAL 上踩了坑",
              body:
                '<a href="#">这篇文章</a> 从单写入者讲到 WAL 检查点，结论是：量不大时 SQLite 当队列完全够用，但要自己管好检查点。',
              why: "zzh 在写的插件打算把发件箱放在 SQLite 里。",
              sources: [{ site: "blog", title: "SQLite as a queue" }],
              status: { kind: "expired", text: "没找到合适的时机，已过期" },
            },
          ],
        },
      ],

      ideas: [
        {
          icon: "monitor",
          title: "我可以每天只读巡检一次群里那台服务器，有真问题才说",
          body:
            "每天早上检查各容器的真实运行状态、磁盘和内存水位，只在容器真挂、磁盘告急时在群里提一句。sub2api 那种健康检查误报会被过滤掉。",
          basis: "老K不熬夜这周两次说「sub2api 又挂了」，最后都是误报。",
          step: "先列出要看的容器和告警线，发给老K确认。",
          effort: "半天搭好，之后每天几分钟",
          state: "new",
        },
        {
          icon: "chart",
          title: "把大家比 Jev 和 GLiNER 的 case 攒成一张对照表",
          body:
            "从这周的群聊里把大家贴过的判断 case 收集起来，本地并行跑两边，对比延迟、成本和判断准不准，出一份基于群里真实数据的结论。",
          basis: "周三到周五讨论了 40 多条，但结论散在聊天里。",
          step: "从群记录里抽出所有贴过的 case。",
          effort: "1 到 2 小时",
          state: "pending",
        },
        {
          icon: "calendar",
          title: "每周五晚上出一份「这周群里折腾了什么」",
          body:
            "周五是群里的分享夜。我可以提前把这周大家做成的东西、踩过的坑、贴过的好链接整理成一页，晚上八点发出来当开场。",
          basis: "群里常有人问「上周谁发的那个链接」。",
          step: "先拿这周的记录试做一期，给你看看。",
          effort: "每周十几分钟",
          state: "new",
        },
      ],

      goals: {
        agent: [
          {
            id: "G-12",
            icon: "bullseye",
            title: "把 MaiBot 插件开发的坑整理成一份群文档",
            body: "大家这两个月在群里踩过的坑散落各处，整理成一页能直接查的网页。",
            criteria: [
              { text: "收集群里相关的讨论", done: true },
              { text: "按主题归类，去掉过时的", done: true },
              { text: "写成网页草稿", done: true },
              { text: "请 zzh 过一遍", done: false },
              { text: "发到群里", done: false },
            ],
            next: "今晚 21:00 检查",
            by: "zzh 发起 · 管理员批准",
            last: "18:40 草稿写完，等 zzh 有空",
            task: "T-0927",
          },
          {
            id: "G-11",
            icon: "sparkles",
            title: "盯着 Jev 1.14 发布，出了就整理变更",
            body: "发布后把变更说明整理成一页，标出会影响群里现有用法的地方。",
            criteria: [
              { text: "Jev 1.14 发布", done: false },
              { text: "变更整理成网页并发群里", done: false },
            ],
            next: "明天 09:00 检查",
            by: "蓝莓山竹 发起 · 管理员批准",
            last: "今天 09:00 看过，还没发布",
          },
        ],
        member: [
          { icon: "alarm", who: "蓝莓山竹", title: "今晚把 Jev 对照的 case 发到群里", due: "今天 21:00", remind: "20:00 提醒" },
          { icon: "floppy", who: "阿柒", title: "月底前把 lachar 的备份脚本迁完", due: "9 月 30 日", remind: "9 月 28 日 20:00 提醒" },
          { icon: "joystick", who: "Kiriko", title: "周日交 STS2 mod 的第一版", due: "明天 18:00", remind: "明天 12:00 提醒" },
          { icon: "moon", who: "老K不熬夜", title: "每晚 12 点前睡", due: "每天", remind: "23:30 提醒 · 循环还剩 19 天" },
        ],
      },

      tasks: {
        pending: [
          {
            id: "T-0932",
            icon: "magnifier",
            title: "汇总这周群里提到的 NAS 方案",
            who: "Kiriko",
            when: "19:22",
            quote: "@东雪莲 帮我把这周群里大家提到的 NAS 方案汇总一下，价格和优缺点列个表",
            via: "群里 @ · Jev 判断是「请求准备东西」（把握 0.91）",
          },
          {
            id: "T-0931",
            icon: "chart",
            title: "Jev 和 GLiNER 对照表",
            who: "蓝莓山竹",
            when: "18:52",
            quote: "这个可以做吧，我正好想看",
            via: "来自构想 · 蓝莓山竹说「做吧」",
          },
        ],
        list: [
          {
            id: "T-0927",
            icon: "books",
            title: "插件踩坑文档：写成网页",
            status: "running",
            meta: "2 个子 agent · 本机 · 已跑 14 分钟 · 38.2k tokens",
            goal: "G-12",
            detail: {
              req: "把整理好的坑按主题写成一页网页，手机上能看，每条附上原始讨论的时间。",
              criteria: ["至少覆盖 6 个主题", "每条有「现象 / 原因 / 怎么绕开」", "手机上排版正常"],
              env: "本机 · maiwork 用户 · 内存上限 512M",
              timeline: [
                { t: "19:26:04", actor: "主模型", tool: "read_workspace", input: "tasks/T-0927/notes.md", output: "读到 11 个主题、47 条记录", ms: 120, ok: true },
                { t: "19:26:09", actor: "主模型", tool: "dispatch_worker", input: "子 agent #1：写 1–6 主题；子 agent #2：写 7–11 主题", output: "已派出 2 个", ms: 40, ok: true },
                { t: "19:26:31", actor: "子 agent #1", tool: "read_chat_history", input: "折腾研究所 · 8/02 – 9/25 · 关键词「热重载」", output: "63 条消息", ms: 880, ok: true },
                { t: "19:28:47", actor: "子 agent #1", tool: "write_file", input: "artifacts/plugin-pitfalls/index.html（第 1–6 节）", output: "写入 18.4KB", ms: 35, ok: true },
                { t: "19:31:02", actor: "子 agent #2", tool: "run_command", input: "python check_links.py index.html", output: "退出码 1 · 2 个链接打不开", ms: 4210, ok: false },
                { t: "19:33:15", actor: "子 agent #2", tool: "write_file", input: "去掉失效链接，改成原始消息时间", output: "写入 9.1KB", ms: 28, ok: true },
              ],
              delivery: [],
              review: "",
            },
          },
          {
            id: "T-0925",
            icon: "testtube",
            title: "查清 sub2api 健康检查为什么老误报",
            status: "reviewing",
            meta: "子 agent 交回了，主模型验收中",
            detail: {
              req: "查清楚为什么健康检查报挂了但服务其实正常，给出能直接改的配置。",
              criteria: ["能复现误报", "给出原因和证据", "给出改法并实际验证"],
              env: "本机 · maiwork 用户",
              timeline: [
                { t: "18:02:11", actor: "子 agent #1", tool: "fetch_page", input: "sub2api 仓库 README · healthcheck 一节", output: "取到正文 6.2KB", ms: 1320, ok: true },
                { t: "18:05:40", actor: "子 agent #1", tool: "run_command", input: "bash repro.sh（连续请求 200 次）", output: "退出码 0 · 超时 3 次，都在 5 秒整", ms: 61234, ok: true },
                { t: "18:07:02", actor: "子 agent #1", tool: "submit_result", input: "原因：检查超时 5 秒，冷启动时偶尔超过；建议改成 15 秒", output: "已交回，附复现日志", ms: 12, ok: true },
              ],
              delivery: [],
              review: "正在核对复现日志里的超时是否都发生在冷启动。",
            },
          },
          {
            id: "T-0921",
            icon: "memo",
            title: "把群精华整理成一份年表",
            status: "waiting",
            meta: "等 zzh 回答：从哪一年开始算？· 已问 4 小时，2 小时后再提醒",
            detail: {
              req: "把群里值得留念的事整理成时间线。",
              criteria: ["时间准确", "每件事附原始消息"],
              env: "本机",
              timeline: [
                { t: "15:40:22", actor: "主模型", tool: "ask_in_group", input: "问 zzh：年表从哪一年开始算？", output: "已发出，等回复", ms: 510, ok: true },
              ],
              delivery: [],
              review: "",
            },
          },
          {
            id: "T-0918",
            icon: "palette",
            title: "Aseprite 批量导出脚本",
            status: "done",
            meta: "已交付到群文件 · 9 月 24 日",
            detail: {
              req: "把一个文件夹里的 .ase 批量导出成 PNG 序列和 GIF。",
              criteria: ["支持图层合并导出", "附使用说明", "在 3 个真实文件上跑通"],
              env: "本机 · maiwork 用户",
              timeline: [
                { t: "9/24 20:11", actor: "子 agent #1", tool: "write_file", input: "tools/ase_export.py", output: "写入 4.8KB", ms: 22, ok: true },
                { t: "9/24 20:14", actor: "子 agent #1", tool: "run_command", input: "python ase_export.py samples/", output: "退出码 0 · 导出 3 个 GIF、41 张 PNG", ms: 3120, ok: true },
                { t: "9/24 20:16", actor: "主模型", tool: "upload_group_file", input: "ase-export.zip", output: "上传成功", ms: 1480, ok: true },
              ],
              delivery: [
                { icon: "package", kind: "群文件", text: "ase-export.zip", state: "已发出，附了说明" },
              ],
              review: "3 个样例文件都导出正常，GIF 帧数和原文件一致。",
            },
          },
          {
            id: "T-0915",
            icon: "newspaper",
            title: "折腾研究所 8 月月报",
            status: "done",
            meta: "here.now 链接已过期 · 网页里有副本",
            detail: {
              req: "把 8 月群里做成的东西整理成一页好看的月报。",
              criteria: ["手机上好看", "每项附链接"],
              env: "本机",
              timeline: [
                { t: "9/01 20:02", actor: "主模型", tool: "publish_herenow", input: "monthly-2026-08.html", output: "已发布，24 小时有效", ms: 2210, ok: true },
              ],
              delivery: [
                { icon: "link", kind: "here.now", text: "8 月月报网页", state: "已过期 · 可以重新发布" },
                { icon: "filebox", kind: "网页副本", text: "monthly-2026-08.html", state: "一直保留" },
              ],
              review: "排版在手机上检查过。",
            },
          },
          {
            id: "T-0912",
            icon: "camera",
            title: "抓取某论坛的一组帖子",
            status: "failed",
            meta: "失败：目标网站要登录，执行环境里没有账号",
            detail: {
              req: "把一个帖子合集抓下来存成文档。",
              criteria: ["帖子完整", "图片保留"],
              env: "本机",
              timeline: [
                { t: "9/22 15:10", actor: "子 agent #1", tool: "fetch_page", input: "论坛帖子列表页", output: "HTTP 302 → 登录页", ms: 940, ok: false },
              ],
              delivery: [],
              review: "没有账号就拿不到内容。已在群里说明原因。",
            },
          },
        ],
      },

      topicLog: [
        {
          time: "19:31",
          quiet: "安静了 34 分钟",
          usual: "平时这个点 4 分钟一条",
          jev: "适合开 · 把握 0.86",
          pick: "Muse early access（接得上 0.81）",
          opener: "刚看到 Meta 那个 Muse 开 early access 了，对它说一句话就能排号……你们周四聊的那种会主动找你的 agent，它这回算是做出来了？",
          result: "6 人接话，MaiBot 接着聊了 4 句",
          verdict: null,
        },
        {
          time: "15:10",
          quiet: "安静了 41 分钟",
          usual: "平时这个点 6 分钟一条",
          jev: "不适合 · 有问题还没人回（zzh 14:29 问的适配器报错）",
          pick: "没有开",
          opener: "",
          result: "",
          verdict: "right",
        },
      ],

      profile: [
        {
          name: "最近在聊",
          entries: [
            { text: "个人 agent 该不该主动找你、怎么主动才不烦", meta: "依据 86 条消息 · 周四起" },
            { text: "Jev 和本地小模型谁更适合做快速判断", meta: "依据 41 条消息 · 周三到周五" },
            { text: "sub2api 健康检查误报", meta: "依据 12 条消息 · 本周两次" },
          ],
        },
        {
          name: "长期兴趣",
          entries: [
            { text: "自部署服务和家里的服务器", meta: "依据 300+ 条消息 · 两个月" },
            { text: "MaiBot 插件开发", meta: "依据 210 条消息 · 两个月" },
            { text: "像素画工具链（少数人）", meta: "依据 35 条消息" },
          ],
        },
        {
          name: "约定和说法",
          entries: [
            { text: "周五晚上是「分享夜」，大家贴本周的折腾成果", meta: "管理员锁定", locked: true },
            { text: "「上机」= 登服务器操作", meta: "依据 18 条消息" },
          ],
        },
      ],

      focus: [
        { name: "阿柒", tone: "#FFD8A8", reasons: ["最活跃"], note: "在迁移 lachar 的备份脚本；关心低成本的临时机器。" },
        { name: "蓝莓山竹", tone: "#D0BFFF", reasons: ["和雪莲关系好"], note: "在做 Jev 和小模型的对照，常贴 case。" },
        { name: "Kiriko", tone: "#A5D8FF", reasons: ["提过请求"], note: "在写 STS2 mod；刚请求汇总 NAS 方案。" },
        { name: "zzh", tone: "#B2F2BB", reasons: ["最活跃"], note: "在写 MaiBot 适配器补丁；发件箱想用 SQLite。" },
      ],
    },

    /* ───────────────────────── 像素练习室 ───────────────────────── */
    {
      id: "pixel",
      token: "p4Rt9a",
      name: "像素练习室",
      icon: "palette",
      members: 88,
      quiet: { text: "有人在聊", usual: "最近 10 分钟 12 条" },
      pulse: { factor: 0.55, seed: 3, spells: [], topics: [{ at: "13:40", label: "调色板生成器", replies: 3 }] },
      today: { news: 1, topics: 1, pending: 0, running: 0 },
      upcoming: [{ icon: "newspaper", time: "明早 08:30", text: "下一批资讯备料" }],
      news: [
        {
          slot: "周六傍晚",
          meta: "19:02 备料 · 找了 9 条，留下 0 条",
          skipped: "这一批没有值得看的，跳过了。",
        },
        {
          slot: "周六午后",
          meta: "13:05 备料 · 找了 11 条，留下 1 条",
          items: [
            {
              icon: "palette",
              title: "一个按「光源方向」自动生成阴影色的调色板工具",
              body: '<a href="#">这个网页小工具</a> 选好主色和光源角度，会给出一组冷暖偏移过的阴影色，可以直接导出成 Aseprite 调色板。',
              why: "群里上周连着三天在讨论阴影偏色。",
              sources: [{ site: "lospec.com", title: "Palette tool" }],
              status: { kind: "used", text: "13:40 冷场时拿来开了话题 · 3 人接话" },
            },
          ],
        },
      ],
      ideas: [
        {
          icon: "camera",
          title: "我可以把 .ase 文件解析成帧、图层和动画预览",
          body: "大家发 .ase 到群里时，我自动出一张帧和图层的预览图，不用下载打开就能看。",
          basis: "群里每周有十几个 .ase 文件，很多人手机上打不开。",
          step: "先拿群文件里最近的 5 个 .ase 试一下。",
          effort: "半天",
          state: "new",
        },
      ],
      goals: { agent: [], member: [{ icon: "palette", who: "糯米团子", title: "周日前画完 OC 的待机动画", due: "明天", remind: "明天 14:00 提醒" }] },
      tasks: { pending: [], list: [] },
      topicLog: [],
      profile: [
        { name: "最近在聊", entries: [{ text: "阴影偏色、冷暖对比", meta: "依据 58 条消息" }] },
        { name: "长期兴趣", entries: [{ text: "像素角色动画、Aseprite 技巧", meta: "依据 400+ 条消息" }] },
      ],
      focus: [{ name: "糯米团子", tone: "#FFC9C9", reasons: ["最活跃", "和雪莲关系好"], note: "在画 OC 的待机动画。" }],
    },

    /* ───────────────────────── 雪莲的后花园（刚开始服务） ───────────────────────── */
    {
      id: "garden",
      token: "g8Lw3n",
      name: "雪莲的后花园",
      icon: "lotus",
      members: 1480,
      fresh: true,
      quiet: { text: "还在熟悉这个群", usual: "已读 3 天的聊天记录" },
      pulse: { factor: 1.6, seed: 11, spells: [], topics: [] },
      today: { news: 0, topics: 0, pending: 0, running: 0 },
      upcoming: [{ icon: "seedling", time: "明天", text: "读满一周后开始出资讯" }],
      news: [],
      ideas: [],
      goals: { agent: [], member: [] },
      tasks: { pending: [], list: [] },
      topicLog: [],
      profile: [],
      focus: [],
    },
  ],
};
