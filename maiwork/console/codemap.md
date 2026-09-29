# plugin/CharTyr_MaiWork/maiwork/console/

## Responsibility

MaiWork 的网页控制台后端（Presentation / HTTP Adapter 层）：一个 aiohttp 服务，把 `app.py` 的服务对象 `svc`（store / models / profiles / feeds / tasks / approvals / goals / extensions …）暴露成浏览器可用的 JSON API + 静态前端。相关接口约定见 `docs/07-代码接口.md` §9、§10；实际路由以 `server.py` 为准。本目录不写业务决策——`views.py` / `usage_history.py` 是纯函数拼返回；`server.py` 只做身份闸、校验、接线。前端（`static/`）由主会话维护，见 [static/codemap.md](static/codemap.md)。父模块见 [../codemap.md](../codemap.md)。

## Design

模块划分（`__init__.py` 导出 `ConsoleServer` / `create_app` / `views`）：

- **[server.py](server.py)**（~3150 行）：`ConsoleServer` 类持有 `web.Application`、`_runner`、`port`。`start(host, port)` 端口被占 → 记错误日志返回 `False`（插件照常运行）；`stop()` 里 `_wait_port_free` 等端口真正释放（支持立刻重 bind）。`create_app(svc)` 是免启动的组装入口。`_build_app()` 内用 `get()` / `post()` 两个装饰器注册路由；**所有非 GET 处理器都过 `_write()` 包装 = `_origin_guard` 同源检查**（Origin 的 host:port 必须等于请求 Host，见 `auth.same_origin`）。
- **[auth.py](auth.py)**：`ConsoleAuth`。单一 cookie 名 `mw_admin`（HttpOnly、SameSite=Strict、Path=/、7 天），两种值格式：
  - 总管理员：`<到期>.<HMAC("admin|<到期>|<密码指纹>")>`；密码 = config.toml `[console] password`（明文比对），否则首次 `ensure_password()` 生成 16 位随机密码（`sha256$<salt>$<hash>` 存 `secrets.admin_password_hash`，明文写 `<data_dir>/console_password.txt` 0600）。
  - 群管理员：`g:<群号>.<到期>.<HMAC("group|<群号>|<到期>|<该群密码指纹>")>`；指纹来自 `GroupAdmins`，改/清群管理员密码 → 旧 cookie 即刻失效。改总管理员密码同理（指纹混入签名）。
  - 登录限流：同 IP 10 分钟错 5 次 → 429（内存 `_fails`，两种角色共用）。
- **身份模型**：`Identity(role, group_id)`，role ∈ `admin | group_admin | member | none`。`_identify()`：cookie 优先（`g:` 前缀 → 群管理员且该群仍是服务群），否则 `X-MW-Group: <链接码>` 头 → 群友（`views.group_id_by_token` 反查）。`AUTH_KEY = web.AppKey("auth", ...)` 挂 app，避免魔术字符串。
- **三级授权闸**：`_require_admin`（只总管理员）/ `_require_group_admin(request, gid)`（总管理员或本群群管理员；群友 403、匿名 401）/ `_ident_for_group_action(need_admin=...)`（M2 用，群友可动本群条目）。M3 的 `_wrong_group` / `_personal_deny` / `_group_of` 实现条目级群隔离。
- **[views.py](views.py)**：纯函数视图拼装（读多写零）：`list_summaries`（GET /api/groups）、`group_view`（GET /api/groups/{ref}：pulse 15 分钟桶 / 五类画像 / news / guides / ideas / goals / tasks / topic_log / upcoming；`admin=False` 群友版裁掉 token / focus / workspace / verdict）、`settings_view`（健康项：模型端点 / Jev / 搜索 / 打开网页 / 本机干活 / 群空间 / SSH / Railway；群链接 `/<token>/news` 用 settings.console.public_url 拼）。`purpose_name` 把模型调用 purpose 翻成中文。`icon_for_group` 按群号 md5 稳定挑 `static/assets/icons/*.png`。群链接码反查 `group_id_by_token`（只认仍在配置的服务群）。
- **[usage_history.py](usage_history.py)**：GET /api/usage/history 的拼装。只读 `usage`（每次模型调用）和 `judgments`（每次 Jev）两表；`?days=N`（1..30，默认 7，缺天补 0）或 `?date=YYYY-MM-DD`（优先）；按天/按模型/按用途/按群（非服务群并入"其他"）/按北京时间小时；任何查询异常 → 当空，绝不 500。
- **[avatar.py](avatar.py)**：`AvatarService`（bot / 群 / 关注成员头像）。bot 头像来源优先级：自定义上传文件（`<data_dir>/console/avatar_bot.<ext>`）→ 自定义网址（`kv["avatar.bot_url"]`，302 重定向）→ MaiBot QQ 头像（`host.bot_qq()` → qlogo，磁盘缓存 24h，失败回落旧缓存）→ 404（前端回落默认图）。群 / 成员头像 URL 用 HMAC token（`avatar-g|<群号>` / `avatar-m|<群号>:<QQ>` 各取签名前 16 位 hex），QQ 号不进 URL/响应头/日志。MIME 按文件魔数判定（png/jpeg/gif/webp），出站 httpx 超时 8s、上限 2MB；`kv["avatar.version"]` 变更时 +1，前端 `?v=` 破缓存。`service_of(svc)` 惰性建并挂回 `svc.avatar`。
- **安全头中间件**：`_errors_mw` 兜底未捕获异常 → JSON 500，并给所有响应加 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`。日志接口与对话响应走 `_redact_full` 密钥遮罩（secrets 表 + 当前 api_key）。
- **群号引用**：`_resolve_ref(ref)` 接受群号或链接码，只认配置里的服务群；`_looks_like_token`（非纯数字 + 口令字符集）区分两种形态供群友路径校验。

## Flow

1. `app.py` 启动（§6 控制台）：先建 `AvatarService` 挂 `svc.avatar` → `self.console = ConsoleServer(self)`（构造即 `_build_app()`：`ConsoleAuth(svc.store, svc.get_settings)` 建好后与 `GroupAdmins` 双向 bind（`bind_console_auth` / `bind_group_admins`），挂 `app[AUTH_KEY]`）→ `app[AUTH_KEY].ensure_password(settings.data_dir)` → `console.start(*settings.console.listen)`。
2. 浏览器请求 → `_errors_mw` → 路由；非 GET（POST/PUT/DELETE/PATCH）先过 `_origin_guard`。
3. `self._identify(request)` 定身份 → 授权闸 → 处理器调 `svc.*` 模块和/或 `views.*` / `usage_history.*` / 父包模块（`rules` / `config_file` / `extensions_web` / `skills_web` / `search_binding` / `rss` / `card_push` / `news_rating` / `news_viz` / `onboarding` / `members`）→ `web.json_response`。
4. 改配置的写路径：模型 PUT → `svc.models.save(patch)` 后 `_cf.read_text` + `svc.apply_config_text` 同步应用；通用配置 PUT → `rules.save_config_patch` 直写 config.toml，再回读应用（宿主文件监控会补一次，幂等）；规则 PUT → `rules.save_patch` 写 `kv["rules.override"]`（不写 config.toml）。
5. 热重载：`new_settings.console.listen != self._listen` → `console.stop()` 后重新 `start()`（app.py §配置应用）。
6. 首页请求 `/` → `_index_html()` 读 `static/index.html`，给 `js/main.js`、`style.css` 和 import map 里每个 `static/js/**/*.js` 模块加 `?v=<sha256 前 12 位>`（按 `(mtime, size)` 缓存，`_ASSET_VER_CACHE`）→ 新版部署浏览器必拿新文件。`/g/{token}` → 302 到 `/#/{token}/news`。

## Integration

**消费者**：浏览器前端（`static/js/**`，见 [static/codemap.md](static/codemap.md)）——单页应用经 `/api/*` 读写，Hash 路由用 `/g/{token}` 302 落地。被 `app.py`（`ConsoleServer(self)` 的唯一建造者、start/stop/重启）消费，`svc` 即 `MaiWorkApp` 本体。

**HTTP API 全清单**（`@get` = GET 直挂；其余全部过 `_write` 同源守卫）：

- 身份/会话：`GET /api/me`（任何身份，回 `{role, group, bot, now}`）、`POST /api/login`（密码 → admin 或 group_admin cookie，限流 429）、`POST /api/logout`。
- 群：`GET /api/groups`（admin 全量 / group_admin 只本群带 token / member 只本群群友版）、`GET /api/groups/{ref}`（按角色返回 `views.group_view` 管理版/群友版）。
- 画像/关注：`POST /api/groups/{gid}/profile`、`PATCH|DELETE /api/profile/{entry_id}`（条目归属群判授权）、`POST /api/groups/{gid}/focus`（remove 只总管理员）、`POST /api/groups/{gid}/token`（重置链接码）。
- 群管理员（只总管理员）：`GET|PUT /api/groups/{gid}/group-admin`、`DELETE .../group-admin/password`。
- 设置（只总管理员）：`GET /api/settings`；`PUT /api/settings/models` + `POST /api/settings/models/test`（试连写 `kv["models.checked"]`）；`GET|PUT /api/settings/rules` + `POST .../rules/reset`（kv 覆盖层）；`GET|PUT /api/settings/config` + `POST .../config/reset`（直写 config.toml）；`GET /api/onboarding`、`POST /api/onboarding`；`GET|POST|DELETE /api/settings/avatar`。
- 日志/用量（只总管理员）：`GET /api/logs/model-calls[/{id}]`、`GET /api/logs/tool-calls[/{id}]`（支持 `limit/before_id/failed/purpose/group` 过滤，密钥遮罩）、`GET /api/logs/summary`、`GET /api/usage/history?days|date`。
- M2 资讯/构想/话题：`POST /api/news/{id}/feedback`、`POST /api/ideas/{id}/feedback`、`POST /api/ideas/{id}/{want|do|dismiss}`（want 触发 `svc.on_idea_want` 批准接线；do/dismiss 要管理身份）、`POST /api/topics/{id}/verdict`、`GET|PUT /api/groups/{gid}/feeds-pref`、`GET|PUT /api/groups/{gid}/card-push`、`POST /api/groups/{gid}/{news,ideas}/run`（现在就跑）、`POST /api/feeds/domains`（屏蔽名单写 `kv["feeds.blocked_domains"]`）、`POST /api/news/{id}/rate`、`GET /api/news/{id}/viz`（CSP 整页图解）、`POST /api/news/{id}/mention-to-member`（个人向，只总管理员）、`GET|POST /api/groups/{gid}/rss`、`DELETE|POST(toggle) /api/groups/{gid}/rss/{id}`。
- M3 批准/任务/目标：`GET /api/tasks/{id}`（群友版裁 env/timeline/tokens/workspace/source/request_id/requester_id）、`POST /api/requests/{id}/{approve|reject}`（批准后 `svc.spawn_run_task`）、`POST /api/tasks/{id}/{pause|resume|cancel|retry|redeliver}`、`POST /api/goals/{id}/{pause|resume|cancel}`。
- 身份与工作记忆（`svc.identity`）：`GET /api/identity`、`PUT /api/identity/{soul|agents|memory}`、`POST /api/identity/soul/sync`、`GET|PUT /api/identity/group-memory/{gid}`（本群群管理员可用）。
- 管理员对话（`svc.admin_chat`）：`GET|POST /api/chat`、`PATCH /api/chat/{id}`、`GET /api/chat/{id}?after=`、`POST /api/chat/{id}/messages`（202）、`POST /api/chat/{id}/compact`、`POST /api/chat/pending/{pid}`（确认/拒绝）；整份响应过密钥遮罩。
- 头像：`GET /api/avatar/bot`（匿名可，302/bytes/404）、`GET /api/avatar/m/{token}`（只总管理员）、`GET /api/avatar/g/{token}`（任何已识别身份，群友/群管理员限本群）。
- 扩展（只总管理员）：`GET /api/extensions`（MCP + skill 清单，headers 绝不回显）、`POST /api/extensions/mcp[/test]`、`PUT|DELETE /api/extensions/mcp/{name}`（config 来源 409 只能开关）、`POST /api/extensions/mcp/{name}/{reload|toggle}`、`GET|PUT|DELETE /api/extensions/search`（联网搜索绑定存 `kv["extensions.search"]`）、`GET /api/extensions/skills/{name}`、`POST /api/extensions/skills[/upload]`（zip，raw 或 multipart）、`PUT|DELETE /api/extensions/skills/{name}`。
- 静态/页面：`GET /`（版本化首页）、`GET /g/{token}`（302 → `/#/{token}/news`）、`/static/`（`add_static`，show_index=False）；兜底 `* /api/{tail:.*}` → JSON 404。

**依赖**：父包 `clock`（北京时间）、`names.clean_group_name`、`members.render`（`{@QQ号}` 渲染）、`group_admins.GroupAdmins`；兄弟模块 `views` / `usage_history` / `auth` / `avatar`。数据读写都经 `svc.store`（kv_set/kv_get、secret_get/secret_set、read/tx）；配置读 `svc.get_settings()`（优先 `svc.base_settings()` 当基线）。测试注入点：`svc.avatar_transport` / `svc.extensions_transport` / `svc.rss_transport`。`../codemap.md` 描述父包全貌。
