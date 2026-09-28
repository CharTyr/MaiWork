# railway.new 一次性 VM 无人值守可用性实测

- 实测时间：2026-09-27 02:52–03:05 UTC（本机 macOS + MaiWork 服务器 Debian 13）
- 共用掉 **2 台** 匿名 VM（本机 1 台、服务器 1 台；第 3 次尝试被拒，未创建），全程 **没有 claim、没有登录、没有产生费用**。
- 测试 key 全部临时生成在 `$TMPDIR/rw-test/`（本机）和 `/tmp/rw-test/`（服务器），结束后已删除；没有使用也没有改动 `~/.ssh` 下任何东西（`~/.ssh/known_hosts` 中 railway.new 条目数 = 0）。

---

## 一、官方说明原文（附 URL）

`curl -sL https://railway.new` → HTTP 200，最终地址 `https://railway.com/free-vm`。
同一页有 markdown 版：**https://railway.com/free-vm.md**（在 https://railway.com/llms.txt 第 33 行被索引）。原文摘录：

> Run one command and get a Linux VM on Railway, with no account and no card: `ssh railway.new`
> Railway identifies you by your SSH key. **The same key lands you in the same VM every time.**

> ## What you get
> - 2 vCPU and 2 GB of memory, ready in about 1.4 seconds, in the region nearest you.
> - A preview URL that serves from the first second. Run your app on `$PORT` (8080) …
> - Coding agents preinstalled: the Railway Agent (`agent`), Claude Code, Codex, OpenCode, Cursor CLI, Grok, and pi …
> - Node, Python, gh, mise, the Railway CLI, and Chromium with Playwright.

> ## Limits
> - **60 minutes to build, then 24 hours to claim. Unclaimed boxes are deleted with their files.**
> - **Up to 3 boxes per IP address per day**, and a shared AI budget per IP per day.
> - The preview URL answers only the IP address that created the box until it is claimed.
> - IPv4 only for now.
> - Compute for unclaimed boxes is free.

> ## For agents
> Any agent that can run `ssh` can use this. When an agent runs commands over SSH, **its first connects include a JSON manifest on stderr** with the preview URL, the build deadline, and a claim link for the human.

> **What if it says to try again later?** When demand is high, we cap how many free VMs run at once in each region. If you hit the cap, you'll see **"Anonymous trials are temporarily disabled. Try again shortly, or sign up to keep building."** That attempt doesn't count against your daily limit…

VM 内部还自带两份说明（实测读到）：

- `/app/AGENTS.md`（29 行，trial 说明）里写明取状态的正规接口：
  > `curl -fsS -H "Authorization: Bearer $AI_AGENT_KEY" "$AI_GATEWAY_URL/status"`
  > The response is `{trial, build_expires_at, claim_expires_at, claim_url, budget: {used_microdollars, total_microdollars}}` … It expires after 30 minutes, so fetch it when needed rather than storing it.
  > **Nothing inside the VM can claim it.**
  > A server bound to 127.0.0.1 only is not reachable from the URL.
- `/etc/railway/AGENTS.md`（cloud agent 通用说明）：
  > Detach servers with `setsid -f <cmd> >/var/log/dev.log 2>&1` so they outlive your session — **not `nohup … &`**, which the command runner may reap.

---

## 二、实测结果（命令 + 原样输出）

统一 SSH 前缀（下文简写为 `ssh …`）：

```bash
ssh -o IdentitiesOnly=yes -i $TMPDIR/rw-test/id \
    -o UserKnownHostsFile=$TMPDIR/rw-test/known_hosts -o StrictHostKeyChecking=accept-new \
    -o KexAlgorithms=curve25519-sha256 -o ConnectTimeout=25 railway.new
```

### 1. 首次连接返回什么 / JSON 怎么触发

**a) 带命令、非 tty、stdin=/dev/null（第一次连接，创建机器）**

```
$ ssh … railway.new 'echo HELLO_FROM_VM; uname -a' </dev/null
EXIT=0
== STDOUT ==
HELLO_FROM_VM
Linux fvm-b1389c15-3c15-4814-ad1b-ee67433fe54a-1-0 6.18.46-railway #1 SMP Mon Sep 21 09:36:13 UTC 2026 x86_64 GNU/Linux
== STDERR ==
Warning: Permanently added 'railway.new' (ED25519) to the list of known hosts.
{"status":"trial_starting","state":"starting","description":"Railway VM, provided until build_expires_at. A user can claim it with human_claim_url. preview_url shows /app as static files until a server listens on 0.0.0.0:$PORT, then routes to that server; from your IP only until claimed.","usage":{"run":"ssh railway.new COMMAND_HERE","copyFiles":"scp ./localfile railway.new:/app/","forwardPort":"ssh -L 9090:127.0.0.1:9090 railway.new","installDependencies":"ssh railway.new mise use --global node@22","railwayCli":"ssh railway.new railway --help"},"preview_url":"https://preview-3ebf7a73da7ee112.up.railway.app","human_claim_url":"https://railway.com/ssh-signup?code=gTTu_wChDx0AQo_b-6EgWA","build_expires_at":"2026-09-27T03:52:19Z"}
```

**b) 不带命令、非 tty（`ssh … railway.new </dev/null`）→ JSON 打在 stdout，每次都打印**

```
stdout: {"status":"trial_ready","state":"running", … ,"preview_url":"https://preview-3ebf7a73da7ee112.up.railway.app","human_claim_url":"https://railway.com/ssh-signup?code=gTTu_…","build_expires_at":"2026-09-27T03:52:19Z"}
stderr: Pseudo-terminal will not be allocated because stdin is not a terminal.
EXIT=0
```

**c) `ssh -T … railway.new 'echo T_OK; hostname'`** → 命令输出在 stdout，manifest 在 stderr（EXIT=0）。
**d) `ssh … railway.new -- hostname`** → 正常工作，stdout=`fvm-b1389c15-…`，EXIT=0（`--` 被 ssh 吃掉，不会当成远端命令的一部分）。
**e) `ssh -tt … railway.new </dev/null`** → stdout/stderr 都是空的，只有 `Connection to railway.new closed.`，EXIT=0（没有本地 tty 时强开 pty 没意义）。

**manifest 出现的规律（重要）**：同一个 box 上，带命令的连接里 manifest 只在前 3 次出现（第 1、2、3 次），13 分钟后再连就没有了；而**不带命令的连接每次都打印 JSON 到 stdout**。所以「稳定拿 JSON」= 用不带命令的连接。

**JSON 结构**（`status` 为 `trial_starting` 或 `trial_ready`）：

| 字段 | 含义 |
|---|---|
| `status` | `trial_starting` / `trial_ready` /（失败时）`refused` |
| `state` | `starting` / `running` |
| `description` | 一句人话说明 |
| `usage` | `{run, copyFiles, forwardPort, installDependencies, railwayCli}` 官方给的用法示例 |
| `preview_url` | `https://preview-<hash>.up.railway.app`（只有创建它的 IP 能访问，直到被 claim） |
| `human_claim_url` | 给人点去认领的链接（带一次性 code） |
| `build_expires_at` | 构建窗口截止时间（UTC，ISO8601，= 创建时间 + 60 分钟） |

注意：manifest 里 **没有** 主机名/用户名/端口字段；这些要登进 VM 取（`hostname`、`whoami`、`$PORT`=8080、`$RAILWAY_PREVIEW_URL`、`$RAILWAY_TRIAL_EXPIRES_AT`）。

### 2. 非交互执行命令、退出码、同一 key 是否回同一台

```
$ ssh … railway.new 'exit 7' ; echo $?        → EXIT=7
$ ssh … railway.new 'false'                   → EXIT=1
$ ssh … railway.new 'nosuchcmd_xyz'           → EXIT=127, stderr: /usr/bin/bash: line 1: nosuchcmd_xyz: command not found
```

远端退出码原样传回（127 也是远端的，不是本地 ssh 的）。

```
whoami=root host=fvm-b1389c15-3c15-4814-ad1b-ee67433fe54a-1-0 home=/root pwd=/ shell=/bin/bash
PATH=/root/.local/bin:/root/.opencode/bin:/root/.grok/bin:/root/.local/share/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

工具可用性（非交互 SSH，PATH 里已有 mise shims）：

```
python3    /root/.local/share/mise/shims/python3    Python 3.14.7
node       /root/.local/share/mise/shims/node       v24.21.0
npm        /root/.local/share/mise/shims/npm
git        /usr/bin/git                             git 2.53.0
curl       /usr/bin/curl                            curl 8.18.0
claude     /root/.local/bin/claude
codex      /root/.local/share/mise/shims/codex
opencode   /root/.opencode/bin/opencode
agent      /root/.local/bin/agent
railway    /usr/local/bin/railway                   railway 5.62.1
pi / mise / gh / jq / setsid / pkill / timeout / uv / rg / fd / pnpm / yarn / sqlite3 / docker 都在
playwright MISSING   chromium MISSING
```

同一把 key 再连 → 同一台（写文件再读回）：

```
$ ssh … railway.new 'echo marker-$$-$(date +%s) > /app/rw_marker.txt; cat /app/rw_marker.txt'
marker-66110-1790477556
# 之后的新连接
$ ssh … railway.new 'cat /app/rw_marker.txt; hostname'
marker-66110-1790477556
fvm-b1389c15-3c15-4814-ad1b-ee67433fe54a-1-0
```

连接开销实测 `time ssh … 'true'`：**2.41s / 2.27s / 2.47s**（含 TCP+认证+proxy 分配，不是 1.4s；1.4s 应指机器就绪时间）。

### 3. 文件传输

三种都可用（同一 key、同一选项）：

```
$ scp … local_upload.txt railway.new:/app/local_upload.txt     → EXIT=0（stdout/stderr 都空）
$ ssh … railway.new 'cat /app/local_upload.txt'                → hello-railway-1790477614
$ ssh … railway.new 'echo from-vm-… > /app/remote_file.txt'
$ scp … railway.new:/app/remote_file.txt ./downloaded.txt      → EXIT=0，内容正确
$ ssh … railway.new 'cat > /app/via_stdin.txt' < local_upload.txt   → EXIT=0
$ sftp -b sftp.batch … railway.new                             → put / ls / bye 全部成功，EXIT=0
```

`sftp` 用的批处理文件：`put local_upload.txt /app/via_sftp.txt` + `ls -l …` + `bye`。

### 4. 断线后后台进程是否存活

```
$ ssh … railway.new 'nohup sleep 300 > /tmp/x.log 2>&1 & echo "started pid=$!"; sleep 1; pgrep -af "sleep 300"'
started pid=833
833 sleep 300
# 断开后新连接
$ ssh … railway.new 'pgrep -af "sleep 300"; ps -o pid,ppid,stat,cmd -p 833'
833 sleep 300
    PID    PPID STAT CMD
    833       1 S    sleep 300        ← 已被 init 收养，活着
```

分离式长任务 + 退出码落盘（推荐写法）：

```
$ ssh … railway.new 'bash /app/run_job.sh'      # run_job.sh: setsid -f bash -c 'bash /app/job.sh >/app/job.log 2>&1; echo $? >/app/job.exit'
launched
# 断开后轮询
$ ssh … railway.new '[ -f /app/job.exit ] && echo "exit=$(cat /app/job.exit)" || echo "still running"; cat /app/job.log'
exit=3
job start 02:55:45
job done 02:55:51
```

本地 ssh 被 `kill -9`（模拟 MaiWork 超时掐连接）之后：普通前台命令 `sleep 120` **仍在**（PID 1166），`setsid -f` 的 `sleep 150` 也在（PID 1170/1171）。结论：远端进程不会随连接消失，但前台命令的 stdout 已经没了，**必须重定向到文件**。

### 5. 资源、出网、包管理、剩余时间

```
nproc=2
               total        used        free      shared  buff/cache   available
Mem:            2262         378        1798           0        255         1883
Swap:            511           0         511
/dev/disk/by-id/packstore-rootfs   30G  4.2G   26G  15% /
PRETTY_NAME="Ubuntu 26.04.1 LTS"    内核 6.18.46-railway  x86_64
date: Sun Sep 27 02:54:10 UTC 2026（UTC 时钟）
```

出网：

```
pypi.org HTTP 200 in 0.055352s
github.com HTTP 200
curl https://api.ipify.org → 152.55.177.190（IPv4 出网）
```

包管理：

```
$ python3 -m pip install -q six && python3 -c "import six;print(six.__version__)"   → six OK 1.17.0
$ npm install lodash → added 1 package in 746ms ; lodash OK 4.18.1
```

剩余时间（三种来源全部一致）：

```
$ ssh … railway.new 'echo $RAILWAY_TRIAL_EXPIRES_AT; railway-trial-status'
2026-09-27T03:52:19.167Z
{"left":"…Trial VM · {countdown} left to build · $3.00 of $3.00 AI budget left…","right":"…/status to claim…","countdown_to":"2026-09-27T03:52:19.167Z","url":"https://railway.com/ssh-signup?code=gTTu_…"}

$ ssh … railway.new 'curl -fsS -H "Authorization: Bearer $AI_AGENT_KEY" "$AI_GATEWAY_URL/status"'
{"trial":true,"build_expires_at":"2026-09-27T03:52:19.167Z","claim_expires_at":"2026-09-28T02:52:19.167Z","claim_url":"https://railway.com/ssh-signup?code=gTTu_…","budget":{"used_microdollars":0,"total_microdollars":3000000}}
```

VM 内的环境变量（只列名字，密钥值没有打印过）：
`RAILWAY_PREVIEW_URL`、`RAILWAY_PUBLIC_DOMAIN`、`RAILWAY_PUBLIC_DOMAIN_8080`、`RAILWAY_TRIAL_EXPIRES_AT`、`RAILWAY_FACTORY_VM_ID`、`RAILWAY_FACTORY_VM_INCARNATION`、`PORT=8080`、`AI_GATEWAY_URL=https://backboard.railway.com/dev-studio/llm`、`AI_AGENT_KEY`、`AI_API_KEY`、`RAILWAY_TOKEN`（后三者是凭据，只以变量引用，未回显）。
`/usr/local/bin/railway-trial-status` 是打印状态行的工具，输出 `{"left","right","countdown_to","url"}`。

### 6. 同一 key 能否有多台 VM / 怎么释放

- **同一把 key 从两个不同 IP 连，进的是同一台机器**：把服务器上那把临时 key 拷回本机，从本机（另一个出口 IP）连接，仍然回到服务器的 box `fvm-58da4c16-9932-4d8f-9e68-f7d5abcdb1e3-1-0`，`/app/server_marker.txt` 内容也在。→ **一把 key = 一台 box，与来源 IP 无关。**
- 想在同一个 IP 上用**新 key** 拿第二台 → 被拒（见第 7 条），所以我们无法在同一 IP 上同时跑两台。
- **释放/销毁：没有**。`railway delete` 是删项目（要账号），`railway ca delete` / `railway sandbox destroy` 是账号里的 Cloud Agents / Sandbox 功能；VM 里 `destroy/release/poweroff/shutdown/reboot/systemctl` 全部 MISSING（PID 1 是 `/rwinit/init`）。官方文档只给了两条路：claim（要账号）或等过期。→ **MaiWork 只能「不再使用 + 等过期」**，不能主动销毁。

### 7. 拿不到 VM 时返回什么（这次实测到了）

在已有一台活跃 box 的情况下，用新 key 再要一台，返回（`stdout` 一行 JSON，**EXIT=13**，stderr 只有 ssh 的 pty 提示）：

```json
{"status":"refused","human_signup_url":"https://railway.com/ssh-signup?code=KYxOWlkDViidIJBzO50Prw","poll_url":"https://backboard.railway.com/ssh-signup/poll?code=KYxOWlkDViidIJBzO50Prw","description":"Anonymous visitors are limited. Sign up to keep building."}
```

同一时刻，**已经拿到的那台 box 仍然可用**（`ssh … railway.new 'hostname; echo alive'` → EXIT=0）。重试两次都是同一份 JSON、同一 code（不是随机抖动）。注意这句话和文档 FAQ 里那句 `"Anonymous trials are temporarily disabled. Try again shortly, or sign up to keep building."` 措辞不同；因为当时两个出口 IP 各有一台活跃 box，**无法区分**这是「每个 IP 同时只允许 1 台未认领 box」还是「全局高峰限流」。

### 8. 服务器（Debian 13）复测

```
服务器 ssh: OpenSSH_10.0p2 Debian-7+deb13u4
$ ssh-keygen -t ed25519 -N "" -f /tmp/rw-test/id
$ ssh -o IdentitiesOnly=yes -i /tmp/rw-test/id … railway.new </dev/null
→ {"status":"trial_starting", … "preview_url":"https://preview-588d887f0722c531.up.railway.app","human_claim_url":"https://railway.com/ssh-signup?code=8CrVTmtqfFZt_-A19q50hQ","build_expires_at":"2026-09-27T03:56:22Z"}
$ ssh … railway.new 'echo HELLO_FROM_SERVER_VM; hostname'  → HELLO_FROM_SERVER_VM / fvm-58da4c16-…
   第 2 次连接 stderr 出现完整 manifest（status=trial_ready）
$ ssh … railway.new 'exit 7' → EXIT=7
$ ssh … railway.new 'cat /app/server_marker.txt' → 内容一致（同一台）
$ nproc=2 / Mem 2262MB / disk 30G / Ubuntu 26.04.1 LTS ；pypi HTTP 200 0.013s ；pip install six → OK
$ scp 上传/下载都 EXIT=0；nohup sleep 200 存活；setsid -f 分离 OK
```

结论：**服务器（Debian）上完全可用**，行为与本机一致。

### 9. 清理

```
本机：rm -rf $TMPDIR/rw-test   → ls: No such file or directory（已确认）
服务器：cd /tmp && rm -rf /tmp/rw-test → ls: cannot access '/tmp/rw-test': No such file or directory
验证：grep -c railway.new ~/.ssh/known_hosts → 0（没有污染 ~/.ssh）
```

### 10. 其他实测到的坑

- 非交互会话的包装是 `/usr/bin/bash -c 'if [ -f /etc/environment ]; then set -a; . /etc/environment; set +a; fi; <你的命令>'`，所以 PATH 里有 mise shims（python3/node/npm 直接可用）。
- 并发会话可用：两条 `sleep 3` 同时跑，墙钟 5s（串行应是 ~11s，含各自 2.4s 建连）。
- 大输出不截断：`seq 1 400000 | tr '\n' x` 收满 2,688,895 字节。
- 脚本可走 stdin 执行，不必传文件：`ssh … railway.new 'python3 -' < script.py` → `stdin-script ok fvm-…`。
- 前台长命令不受限：`sleep 45` 正常返回（墙钟 48s）。
- preview URL 只有创建它的 IP 能访问：从本机 `curl -o /dev/null -w %{http_code}` → HTTP 200（占位页）。
- `build_expires_at`（manifest）与 `RAILWAY_TRIAL_EXPIRES_AT` 精度不同（前者整秒、后者带毫秒），值一致；claim 之后 `RAILWAY_TRIAL_EXPIRES_AT` 不会清空，官方说以 `/status` 为准。

---

## 三、MaiWork 调用建议

### 1. 获取一台 VM（无人值守）

```bash
KEYDIR=/var/lib/maiwork/rw/<jobid>          # 每个 job 一把新 key
mkdir -p "$KEYDIR" && chmod 700 "$KEYDIR"
ssh-keygen -t ed25519 -N "" -f "$KEYDIR/id" -q

# 第一次连接：不带命令，JSON 落在 stdout；同时完成建机器
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i "$KEYDIR/id" \
    -o UserKnownHostsFile="$KEYDIR/known_hosts" -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout=25 railway.new < /dev/null > "$KEYDIR/manifest.json" 2> "$KEYDIR/first.err"
```

- `key 就是身份`：**每台机一把 key**；key 丢了就找不回那台机（要重新拿一台）。key 文件权限 600，绝不进日志/仓库。
- 返回码 13 → 没拿到（见下面失败处理）；返回码 0 且 stdout 是 JSON → 拿到了。
- 建机器和第一条命令可以在同一次连接里跑（实测 `trial_starting` 时已经能执行命令），但为了可靠解析，建议**先建后跑**。

### 2. 执行命令 + 拿退出码

```bash
ssh … railway.new 'cd /app && <命令>' ; rc=$?     # rc 就是远端退出码
```
- 0/1/127 都是真实退出码，可直接当验收依据。
- 长任务不要占着连接：用 `setsid` 分离 + 退出码/日志落盘，然后轮询。

```bash
# 启动（立刻返回）
ssh … railway.new 'rm -f /app/job.log /app/job.exit; setsid -f bash -c "bash /app/job.sh >/app/job.log 2>&1; echo \$? >/app/job.exit"'
# 轮询
ssh … railway.new '[ -f /app/job.exit ] && echo "exit=$(cat /app/job.exit)" || echo RUNNING; tail -c 4000 /app/job.log'
```
- 客户端超时/断线不会杀掉远端进程（实测连 `kill -9` 本地 ssh 也没杀），但前台命令的 stdout 会丢，所以**永远重定向到文件**。
- 单条连接建连约 2.3–2.5s，轮询不要太密（≥2s）。

### 3. 传文件

```bash
scp -o BatchMode=yes -o IdentitiesOnly=yes -i "$KEYDIR/id" -o UserKnownHostsFile=… -o StrictHostKeyChecking=accept-new \
    "$local" railway.new:/app/
scp … railway.new:/app/result.json "$localdir/"          # 取回
ssh … railway.new 'cat > /app/x' < localfile             # 或用 stdin 重定向
ssh … railway.new 'python3 -' < script.py                # 直接跑本地脚本
```
`sftp -b` 批处理也可用（适合多文件）。

### 4. 解析 JSON（三种形态）

统一先 `json.loads(line)`，再按 `status` 分派：

1. **创建/无命令连接** → stdout 一行 JSON（stdout 干净，stderr 可能有 ssh 的 pty 提示，忽略）。`status` ∈ `trial_starting` / `trial_ready` / `refused`。
2. **带命令连接** → stdout 是命令输出；manifest 只在 box 的**前 ~3 次**连接出现在 stderr，**不可依赖**。要元数据就用第 1 种方式（或进 VM 查）。
3. **失败/被拒** → `status=refused`，字段是 `human_signup_url` / `poll_url` / `description`（**没有** `build_expires_at`——用它判断是否成功）。

VM 内想要精确状态：

```bash
ssh … railway.new 'curl -fsS -H "Authorization: Bearer $AI_AGENT_KEY" "$AI_GATEWAY_URL/status"'
# {"trial":true,"build_expires_at":…,"claim_expires_at":…,"claim_url":…,"budget":{…}}
```
（`$AI_AGENT_KEY` 是 VM 内的环境变量，只在 VM 里用，不要回传到日志里。）

### 5. 判断剩余时间

优先级从高到低：

1. VM 内 `$AI_GATEWAY_URL/status` 的 `build_expires_at`（官方权威）；
2. 创建时 manifest 的 `build_expires_at`（= 创建时间 + 60 分钟）；
3. VM 内 `$RAILWAY_TRIAL_EXPIRES_AT` / `railway-trial-status` 的 `countdown_to`（值一致，但 claim 后不清空，只能参考）。

计算：`remaining = build_expires_at - now(UTC)`；建议 `remaining < 5min` 就不要再派新活，直接把结论/产物取回，之后换新 key 重新拿机器。VM 内时钟是 UTC，`date -u` 可直接用。

### 6. 失败处理矩阵

| 现象 | 含义 | 处理 |
|---|---|---|
| EXIT=13 + `{"status":"refused", …}` | 现在拿不到新机（限流/每 IP 已有一台） | 按 JSON 里的 `poll_url` 语义**退避重试**（30–120s，最多几次），不要狂连；文档说这条不计入每日额度。仍失败就报「暂时拿不到一次性 VM」，退回本机/服务器执行 |
| EXIT=13，stdout 不是 JSON | 同上的变体/网关抖动 | 保留 stderr 原文，退避重试 |
| EXIT=255 | ssh 自身错误（网络、认证、host key） | 当普通网络故障重试；检查 key 路径权限、known_hosts 写权限 |
| EXIT=127/1/其它 | 远端命令的真实退出码 | 按命令语义处理，不要当成拿机器失败 |
| 连接成功但 stdout 空、无 JSON | 用了带命令的连接且 manifest 已不再打印 | 改用「不带命令的连接」取元数据 |
| 建机器成功但从没读到 `preview_url` | manifest 丢了 | 进 VM 读 `$RAILWAY_PREVIEW_URL`，或重新不带命令连一次 |

其他注意：

- **不要 claim**（要账号）；未认领的机器免费。
- **一个 IP 同时只有一台能干活**（实测同 IP 新 key 被拒），所以 MaiWork 别指望在同一台服务器上并行开多台一次性 VM；要并行就换出口 IP。
- **60 分钟到点后 VM 会拒绝新工作**（官方文档），MaiWork 的任务要按 60 分钟切分或提前收尾。
- preview URL 只有创建它的 IP 能访问；如果要让用户看结果，得把结果文件取回或走 claim。
- 首次连接建议 `ConnectTimeout=25`、加 `-o ServerAliveInterval=15 -o ServerAliveCountMax=4`；一条连接建连 ~2.5s，别用默认几秒的超时。

---

## 四、没测到 / 不确定的（不编造）

1. **构建窗口到点后的确切表现**（连接是报错、还是重新给一台）没测：需要等 60 分钟，实测期间没等。
2. `refused` 到底是「每 IP 同时 1 台未认领 box」还是「全局高峰限流」——因为两个出口 IP 当时都各有一台活跃 box，无法区分；文档里那句 `Anonymous trials are temporarily disabled` 也没能主动复现。
3. **主动释放/销毁**：官方没有匿名 box 的 destroy；VM 里也没有 poweroff/shutdown/systemctl。我们**没有**尝试 `kill -1` / sysrq 之类强行关机（不解决问题，而且怕触发第 4 台 VM，超出「最多 3 台」的约束）。
4. `scp -r` 传目录、`rsync` 未测。
5. 服务器上没有再复测「同一把 key 多台 VM」（同一 key 与 IP 无关已在本机侧证明）。
6. Chromium/Playwright：文档说有，但 `playwright`/`chromium` 不在非交互 PATH 上（可能在别的位置或首次用时下载），未深入验证。
