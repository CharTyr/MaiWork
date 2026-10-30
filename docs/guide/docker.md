# 用 Docker 跑的 MaiBot

[← 回到 README](../../README.md)

如果你的 MaiBot 是装在 Docker 容器里的，MaiWork 照样能跑，而且会自动识别自己正在容器里，在首次引导和「设置 → 运行状态」里提醒你有哪些地方还没配好。

下面是 Docker 环境下需要注意的 **7 件事**。

> 注：本篇按插件源码整理，**尚未在真实的 Docker 版 MaiBot 上完整实测过**；文中标「未实测」的部分属于基于代码的推断，遇到出入欢迎提 Issue。

---

### 1. 把数据目录和配置文件挂到容器外面（最重要）

MaiWork 的数据库、自动生成的网页管理员密码、SSH 密钥都存在 `<MaiBot>/data/maiwork/` 里；插件配置文件 `config.toml` 在插件目录里。

**这两处都必须挂在容器外的持久化卷（volume / bind mount）里**，否则下次更新镜像、重建容器时数据就全丢了。如果容器不是以 root 身份跑的，还要确保运行账号对这两个目录有读写权限。

MaiWork 启动后会自动检查 `<MaiBot>/data/maiwork/` 有没有挂到容器外，没挂的话会在网页上提醒你。

---

### 2. 改网页监听地址，并把端口映射出来

默认情况下，MaiWork 网页只监听容器内部的 `127.0.0.1:18650`，你在容器外面是打不开的。

先在 `config.toml` 里把监听地址改成 `0.0.0.0`：

```toml
[console]
listen = "0.0.0.0:18650"   # 只填 IPv4:端口
```

再在启动容器时把 `18650` 端口映射出来（比如 `docker run -p 18650:18650` 或 `docker-compose.yml` 里的 `ports: ["18650:18650"]`）。如果你只在这台宿主机本机访问，写成 `127.0.0.1:18650:18650` 更安全。

---

### 3. 群友打开网页链接（默认免配；挂反向代理要注意两点）

- **默认不用配**：你不填外网网址时，MaiWork 会自动把群友要看的页面发到 [here.now](https://here.now)（匿名临时网页，24 小时过期），群里的 `/mw 网页` 和资讯卡片照常有链接可点。
- **想用自己的固定域名**：配好 nginx / Caddy 等反向代理后，在 `config.toml` 里填上外网地址和代理 IP：

```toml
[console]
public_url = "https://maiwork.example.com"   # 必须以 http:// 或 https:// 开头
trusted_proxies = ["127.0.0.1", "172.17.0.0/16"]  # 换成你反向代理所在的 IP 或容器网段
```

挂反向代理时**务必注意两件事**：
1. **把原始 `Host` 头原样传给容器**（nginx 里加一行 `proxy_set_header Host $http_host;`）。因为网页保存设置时会核对浏览器请求的来源域名和 `Host` 是否一致；要是 nginx 传进来的是容器内网 IP，两边对不上，网页上点保存会被安全检查拦下。
2. **填上 `trusted_proxies` 并让代理传 `X-Forwarded-For`**。网页登录有防爆破限流（10 分钟内连续输错 5 次密码就锁 10 分钟）；填了可信代理 IP 后，限流才会按真实访客 IP 算，不会因为一个人输错密码把所有人都锁在外面。

---

### 4. 资讯卡片画图：在网页上一键装字体和浏览器

精简版 Docker 镜像里往往既没有中文字体（画出来的中文变方块），也没有 Chromium 浏览器（没法画图，只能自动退回发文字列表）。

不用你改 Dockerfile，直接打开 MaiWork 网页 **「设置 → 工具 → 可选组件」**：
- **中文字体**：点一下自动下载开源的思源黑体（约 8 MB）到 MaiWork 自己的数据目录；
- **画图浏览器**：点一下自动把 Chromium（约 150 MB）装进 MaiWork 自己的目录。

**唯一需要你动手的特殊情况**：有些极简镜像连 Chromium 需要的底层系统库（如 `libnss3.so`）都没有。如果网页提示缺系统库，进 MaiBot 容器终端里用 root 跑一行命令就齐了（真 Docker 内的一键下载与启动待实测）：

```bash
playwright install-deps chromium
```

---

### 5. 传 QQ 群文件：小文件自动直传，大文件建议共享目录

如果你的 MaiBot 和 NapCat 装在**两个不同的 Docker 容器**里，NapCat 默认是看不到 MaiWork 容器里的文件路径的：
- **20 MB 以内的文件**：MaiWork 会自动把文件内容直接打包传给 NapCat（待实测），还是传不上去就改发下载网页链接，并在交付说明和「运行状态」里写明原因；
- **超过 20 MB 的大文件**：想稳定直传群文件，把 `<MaiBot>/data/maiwork/` 用**一模一样的容器内路径**同时挂载给 MaiBot 容器和 NapCat 容器即可。

---

### 6. 跑代码 / 跑命令的活：二选一

Docker 容器里通常没有 systemd，MaiWork 会自动关闭「在本机容器内跑命令」。遇到需要跑代码的任务，你有两条路可选：

- **省事的路（不用装 SSH）：开 Freestyle 临时机器**。Freestyle 全程走 HTTPS 接口，容器里连 `ssh` 命令都不需要。去「设置 → 干活的机器」打开 Freestyle 并填上 API key 就行（Freestyle 真密钥待实测）。
- **免密钥的路（用 Railway 临时机器或自己的 VPS）：在容器里装上 SSH 客户端**。Railway 和自有服务器都靠 `ssh`、`ssh-keygen`、`scp` 三个命令通信。进 **MaiBot 容器终端**（注意是装在容器里，不是装在宿主机上）跑：

```bash
apt-get update && apt-get install -y --no-install-recommends openssh-client
```

> 装完可以顺手把这行写进你的 Dockerfile 里，免得下次重建容器又要重装。

---

### 7. 网络要求

容器需要能正常访问外网（调大模型、联网搜索、拉 RSS、发 here.now 临时网页都要用）。

- **大模型端点地址**可以填宿主机或内网地址（比如连宿主机的 Ollama：`http://host.docker.internal:11434`）；
- 但 **RSS 订阅源和让 MaiWork 去抓取的网页必须是公网地址**——内网和本机地址会被安全闸门直接拦下，防止它被人诱导去刺探你的内网服务。
