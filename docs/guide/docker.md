# 用 Docker 跑的 MaiBot

[← 回到 README](../../README.md)

MaiBot 装在 Docker 里时，MaiWork 要额外配置的地方。

MaiWork 可以装在 Docker 版 MaiBot 里，不会因为在容器里就崩；但下面几项要你自己配，有一项在常见的「MaiBot、NapCat 分两个容器」装法下用不了。

> 这一节是按插件源码整理的，**还没在真的 Docker 版 MaiBot 上完整测过**；标「未实测」的是推断。遇到和这里说的不一样，欢迎提 issue。

**1. 数据要挂在容器外面。** 数据库、自动生成的管理员密码、SSH key 都在 `<MaiBot>/data/maiwork/`；插件自己的 `config.toml` 在插件目录里。这两处都要在挂载卷里，否则重建容器就没了。容器如果不是以 root 运行，要保证它对这些目录有写权限。管理员密码可以从挂载目录里的 `console_password.txt` 看，或者直接在 `config.toml` 的 `[console] password` 自己设。

**2. 网页要改监听地址、开端口。** 默认只监听容器内部的 `127.0.0.1`，在容器外打不开：

```toml
[console]
listen = "0.0.0.0:18650"   # 只认「IPv4:端口」，不能写域名或 IPv6
```

再把端口映射出来（`docker run -p 18650:18650 …`，或 compose 里的 `ports: ["18650:18650"]`）。只在服务器本机用的话，映射成 `127.0.0.1:18650:18650` 更安全。

**3. 想让群友打开网页，要做反向代理并填 `public_url`。** 不填的话，`/mw 网页` 和资讯卡片里都不会带链接。

```toml
[console]
public_url = "https://maiwork.example.com"   # 必须 http:// 或 https:// 开头
```

反向代理要注意两点：
- **把浏览器访问的原始地址原样传进来**（nginx 写 `proxy_set_header Host $http_host;`）。网页保存设置时会核对「请求来自哪个网址」和「访问的是哪个网址」是否一致；nginx 默认传进来的是容器地址，对不上，所有保存都会被拒。
- 网页按访问来源限制密码试错（10 分钟内错 5 次就暂时锁住）。经过代理后，所有人看起来都来自代理，**一个人连续输错密码会把大家一起锁 10 分钟**。外网请用 HTTPS，并把管理员密码设得够长。

**4. 资讯卡片画图要镜像里有这些东西。** 画卡片用 MaiBot 环境里的 playwright + Chromium 和 Pillow，插件不会自己装。镜像里没有的话，卡片会自动改发文字列表，不会报错。另外精简镜像通常**没有中文字体**，卡片上的中文会变成方块（未实测，按代码推断：插件不自带字体）。Debian / Ubuntu 系镜像可以这样补：

```bash
pip install playwright pillow
playwright install --with-deps chromium
apt-get install -y fonts-noto-cjk
```

**5. 群文件、群相册：两个容器要共用同一个文件夹。** MaiWork 上传群文件 / 传群相册时，交给 NapCat 的是**文件在本机的路径**。如果 NapCat 在另一个容器里、看不到 MaiWork 的工作区，上传就会失败。按代码，失败后会自动改发一个下载页链接，任务不会卡住，只是群里收不到文件（未实测）。想真发群文件，就把 `<MaiBot>/data/maiwork/` 用**完全相同的路径**同时挂进 NapCat 容器。发图片（资讯卡片等）不受影响：图片是直接打包进消息发的。

**6. 本机不跑命令。** 容器里一般没有 systemd，MaiWork 会自动关掉「本机跑命令」（见[运行环境](environment.md#本机能不能跑命令)），查资料、写文件、做网页照常。要跑命令的活，请配置[专用机器](environment.md#专用机器自己的-vps--vm可以配多台)或 railway.new 一次性 VM。这两种都要容器里有 `ssh`（Debian / Ubuntu 系装 `openssh-client`）。

**7. 联网和内网地址。** 容器要能访问外网（模型、搜索、RSS、更新提醒、下载页都要用）。模型端点可以填内网或宿主机地址（比如 `http://host.docker.internal:11434`）；但 **RSS 源和让 MaiWork 打开的网页必须是公网地址**，内网、本机地址会被安全检查拦下。这是有意的设计，防止它被骗去访问你的内部服务。
