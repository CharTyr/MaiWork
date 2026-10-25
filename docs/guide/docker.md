# 用 Docker 跑的 MaiBot

[← 回到 README](../../README.md)

MaiBot 装在 Docker 里时，MaiWork 要额外配置的地方。

MaiWork 可以装在 Docker 版 MaiBot 里，不会因为在容器里就崩；但下面几项要你自己配，有一项在常见的「MaiBot、NapCat 分两个容器」装法下用不了。

> 这一节是按插件源码整理的，**还没在真的 Docker 版 MaiBot 上完整测过**；标「未实测」的是推断。遇到和这里说的不一样，欢迎提 issue。

**1. 数据要挂在容器外面。** 数据库、自动生成的管理员密码、SSH key 都在 `<MaiBot>/data/maiwork/`；插件自己的 `config.toml` 在插件目录里。这两处都要在挂载卷里，否则重建容器就没了。容器如果不是以 root 运行，要保证它对这些目录有写权限。管理员密码可以从挂载目录里的 `console_password.txt` 看，或者直接在 `config.toml` 的 `[console] password` 自己设。MaiWork 会在首次引导和「设置 → 运行状态」里自动检查数据目录有没有挂到容器外并提示（按 `/proc/self/mountinfo` 判断，读不到就不判断）。

**2. 网页要改监听地址、开端口。** 默认只监听容器内部的 `127.0.0.1`，在容器外打不开：

```toml
[console]
listen = "0.0.0.0:18650"   # 只认「IPv4:端口」，不能写域名或 IPv6
```

再把端口映射出来（`docker run -p 18650:18650 …`，或 compose 里的 `ports: ["18650:18650"]`）。只在服务器本机用的话，映射成 `127.0.0.1:18650:18650` 更安全。MaiWork 会在首次引导里自动检查监听地址并提示；默认仍是 `127.0.0.1`，不会替你打开。

**3. 群友打开网页：默认不用配。** 没填 `public_url` 时，MaiWork 会把群友要看的那一页自动发布到 [here.now](https://here.now)（匿名公开页，24 小时过期；见[安全与公开范围](security.md)），`/mw 网页` 和资讯卡片照样带链接。想用自己的网址（长期有效、能看完整网页），再做反向代理并填 `public_url`；不想公开就在「设置 → 全部设置」关掉「用 here.now 自动公开」。

```toml
[console]
public_url = "https://maiwork.example.com"   # 必须 http:// 或 https:// 开头
```

反向代理要注意两点：
- **把浏览器访问的原始地址原样传进来**（nginx 写 `proxy_set_header Host $http_host;`）。网页保存设置时会核对「请求来自哪个网址」和「访问的是哪个网址」是否一致；nginx 默认传进来的是容器地址，对不上，所有保存都会被拒。
- 网页按访问来源限制密码试错（10 分钟内错 5 次就暂时锁住）。经过代理后，所有人看起来都来自代理，**一个人连续输错密码会把大家一起锁 10 分钟**。外网请用 HTTPS，并把管理员密码设得够长。

**4. 资讯卡片画图：在网页上一键装中文字体和浏览器。** 画卡片用 MaiBot 环境里的 playwright + Chromium 和 Pillow。精简镜像常常没有中文字体（卡片上的中文变方块）、也没有 Chromium（卡片自动改发文字列表，不会报错）。打开 MaiWork 网页「设置 → 工具 → 可选组件」：

- **中文字体**：一键下载思源黑体（Noto Sans SC，约 8 MB，开源免费）到 MaiWork 自己的目录，下载后核对文件指纹。页面会显示「已安装 / 未安装 / 系统已有中文字体」。
- **画图浏览器**：一键下载 Chromium（约 150 MB）到 MaiWork 自己的目录（MaiBot 环境没有 playwright 时会先把它装到 MaiWork 目录）。装完会真启动一次检查。

这两样都不改镜像、不需要 root。**有一件事插件做不到**：浏览器还依赖一些系统库（`libnss3.so` 之类），精简镜像里可能没有。页面会提示缺哪几个，这时需要你在 MaiBot 容器里用 root 运行：

```bash
playwright install-deps chromium
```

或者换一个自带浏览器的镜像。在那之前资讯卡片会改发文字。（一键安装在 macOS 本机测过；真 Docker 里的下载和启动尚未实测。）

**5. 群文件、群相册：两个容器要共用同一个文件夹。** MaiWork 上传群文件 / 传群相册时，交给 NapCat 的是**文件在本机的路径**。如果 NapCat 在另一个容器里、看不到 MaiWork 的工作区，MaiWork 会自动把文件内容直接传过去再试一次（只限 20MB 以内，未实测）；再不行就改发一个网页链接，交付说明里会写明「群文件没发出去」的原因。大文件想稳定发群文件，就把 `<MaiBot>/data/maiwork/` 用**完全相同的路径**同时挂进 NapCat 容器。发图片（资讯卡片等）不受影响：图片是直接打包进消息发的。

**6. 本机不跑命令，SSH 客户端要装在 MaiBot 容器里。** 容器里一般没有 systemd，MaiWork 会自动关掉「本机跑命令」（见[运行环境](environment.md#本机能不能跑命令)）。要跑命令的活，请配置[专用机器](environment.md#专用机器自己的-vps--vm可以配多台)或 railway.new 一次性 VM。MaiWork 会在首次引导和运行状态里自动检查并提示缺哪个 SSH 命令。

**不想装 SSH：用 Freestyle。** Freestyle 一次性 VM 全程走 HTTPS 接口（建机、跑命令、传文件都是 API 调用），不需要容器里有 SSH 客户端。在「设置 → 全部设置 → 运行环境」打开 Freestyle 并填密钥即可（Freestyle 尚未用真密钥实测）。

专用机器需要 MaiBot 的运行环境里有 `ssh-keygen`（生成公钥）、`ssh`（登录）和 `scp`（传文件）。**只在 NAS / Docker 宿主机或目标机器安装，不会给 MaiBot 容器补上这些命令。** Railway 也需要容器内的 SSH 客户端。下面的安装和验证命令要在 **MaiBot 容器的终端里**执行，适用于 Debian / Ubuntu 系镜像（安装需要 root 权限）：

```bash
apt-get update
apt-get install -y --no-install-recommends openssh-client
command -v ssh-keygen
command -v ssh
command -v scp
```

三条 `command -v` 都应输出命令路径；还应以运行 MaiBot 的账号确认能执行这些工具。其他基础镜像请用对应的包管理器安装 OpenSSH 客户端。目标机器上提供登录服务的是 **`openssh-server`**，它与 MaiBot 容器需要的客户端是两回事。

MaiWork 会在启动后的后台检查中检测这三个命令，即使还没配置机器，也会检查并尝试生成公钥。缺依赖或公钥生成、读取失败，会显示在网页「运行状态 → 自有机器」里；没有公钥时不会提示复制一个不存在的公钥。修好后，后续检查成功会清除旧错误。检查约每 30 分钟进行一次，修改 SSH 机器列表也会触发；单纯刷新网页不会立即重跑检查。

若提示目录权限问题，请确认 `<MaiBot>/data/maiwork/ssh/` 所在挂载卷可写，且 MaiBot 的运行账号有读写权限。不要通过删除已有私钥来修复依赖问题，否则目标机器上已添加的公钥会失效。

在正在运行的容器里手动安装只解决当前容器的问题，**重建容器可能丢失这些软件包**。长期使用请把 `openssh-client` 写进自定义镜像的 Dockerfile（例如 Debian / Ubuntu 基础镜像中的 `RUN apt-get update && apt-get install -y --no-install-recommends openssh-client`），再按第 1 项持久化数据和 SSH 密钥。

**7. 联网和内网地址。** 容器要能访问外网（模型、搜索、RSS、更新提醒、下载页都要用）。模型端点可以填内网或宿主机地址（比如 `http://host.docker.internal:11434`）；但 **RSS 源和让 MaiWork 打开的网页必须是公网地址**，内网、本机地址会被安全检查拦下。这是有意的设计，防止它被骗去访问你的内部服务。
