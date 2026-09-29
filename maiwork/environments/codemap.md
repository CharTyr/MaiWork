# plugin/CharTyr_MaiWork/maiwork/environments/

执行环境模块。为 MaiWork 提供三种干活载体：本机（Local）、一次性 VM（Railway）、专用 SSH 机器（SshEnv）。本模块决定“活在哪跑、用什么用户身份、有什么资源限制”，并把工作区文件读写给隔离起来。

## Responsibility

这个目录解决一个核心问题：**子 agent 的命令和文件不能碰到 MaiBot 宿主、不能碰到别的群的工作区、不能超过资源限额**。

五个文件的职责：

| 文件 | 做什么 |
|------|--------|
| `capability.py` | 启动时探测一次本机能不能隔离跑命令，输出三种判定：`fixed` / `dynamic` / `stopped`。绝不真跑 systemd-run 或 useradd。 |
| `local.py` | 本机执行环境。两种模式：`systemd`（生产隔离）和 `direct`（本机开发测试用，无隔离）。提供 run / start / status / logs / stop / read_file / write_file / list_files。 |
| `ssh.py` | 专用 SSH 机器执行环境（用户自己的 VPS/VM）。acquire / run / put / get / release / check_all / status。 |
| `railway.py` | railway.new 一次性 VM 执行环境。acquire / run / put / get / release / usage_today / current_box。 |
| `__init__.py` | 导出 `LocalEnv`、`RunResult`、`capability`。`RailwayEnv`、`SshEnv` 由 app.py 用 `_import_m2_class` 懒加载，不走这里。 |

## Design

### 能力探测（capability.py）

**启动时跑一次，三种判定：**

```
fixed   → Linux + systemd-run + root + run_as 用户（默认 maiwork）存在
          → systemd-run --uid=maiwork 固定用户隔离（线上模式）
dynamic → Linux + systemd-run + root、但没有 maiwork 用户
          → DynamicUser=yes + 固定名 maiwork-sbx + StateDirectory，
            工作区实际落在 /var/lib/private/maiwork/workspaces/<名>
stopped → 其它所有情况（非 Linux、没 systemd、非 root）
          → 本机不能隔离跑命令；工作区由插件进程直接读写，
            要跑命令的活需交专用 SSH 机器或 Railway 一次性机器
```

**关键设计点：**

- 探测由 `app.py:1396 _detect_local_capability()` 调用，结果缓存在 `app.py:119 self.capability`。测试时通过 `capability_probe` 注入假判定。
- **绝不真跑 systemd-run / useradd**：`_probe()` 只检查 `sys.platform`、`os.geteuid`、`shutil.which("systemd-run")`、`pwd.getpwnam`，不执行任何命令。
- 工作区根按判定结果换：`fixed` → 尊重配置（线上 `/home/maiwork/workspaces`）；`dynamic` → 强制 `/var/lib/private/maiwork/workspaces`；`stopped` → 插件数据目录下的 `workspaces/`。见 `capability.py:139 resolve_workspace_root()`。

### 命令/进程安全边界（local.py）

**工作区访问**（所有文件操作的核心安全机制）：

- 从可信 `workspace_root` 锚定目录 fd，逐段 `O_NOFOLLOW + O_DIRECTORY` 打开，符号链接一律拒绝或跳过。
- `resolve()` 拒绝对路径、拒绝 `..`、realpath 之后必须仍留在工作区内（防符号链接逃逸）。
- `write_file` 用「临时文件 + 原子替换」：先在目录里建独立 inode，写完再 `os.replace` 到目标文件名；替换前再校验目标 inode 没变。永远不对旧 inode 做 `O_TRUNC` 或 `ftruncate`。
- `read_file` / `list_files` / `write_file` 都经 `_workspace_fd()` / `_parent_fd()` 上下文管理器，全程持有锚定 fd。

**命令执行（systemd 模式）**：

- 环境变量只给最小集（`PATH` + `HOME=工作区` + `LANG=C.UTF-8`），绝不继承插件进程的环境（防止密钥漏给子进程）。
- `run()`：一次性命令。`systemd-run --wait --collect --pipe` + `--uid=<run_as>` + `--setenv=HOME=ws` + `MemoryMax=<配置值，默认 512M>` + `RuntimeMaxSec=limit` + 一套沙箱 flag（`ProtectSystem=strict` + `ProtectHome=yes/read-only` + `PrivateTmp=yes` + `NoNewPrivileges=yes` + 工作区 BindPaths）。外再包 `asyncio.wait_for(limit+15s)` 兜底。
- `start()`：后台进程。`systemd-run --collect --quiet`（不带 `--wait --pipe`），输出重定向到工作区 `runtime/logs/<标签>.log`，进程收尾把自己的退出码写进 `runtime/logs/<标签>.exit`。`status()` 看 `systemctl show` 的 `ActiveState`，不活跃时退出码从 `.exit` 文件读。
- 判断超时/OOM：靠解析 stderr 里 `Finished with result:` 行（`oom-kill` → OOM，`timeout` → 超时），不靠退出码（都可能是 1）。
- `stop()`：`systemctl stop`，幂等。
- `close()`：收尾所有 direct 后台进程，杀进程、关日志句柄、取消 kill 定时器。插件热重载时由 `app._stop_stack` 调用。

**direct 模式**（仅本机开发测试，需 `MAIWORK_DEV_ALLOW_DIRECT=1` + 非 root）：

- 直接 `asyncio.create_subprocess_exec`，不经过 systemd、不通隔离。
- 后台进程登记在 `self._procs` 内存表里；日志和退出码同样落在 `runtime/logs/`（与 systemd 语义对齐，供测试用）。
- 生产路径（systemd 服务 / 容器 / root）即使配置 `direct` 也会被 `config.py:749` 强制回落 `systemd`。

### SSH/Railway 生命周期和回退

**SSH（ssh.py）——专用机器：**

- 密钥：插件自己生成一把 ed25519 key（`<data_dir>/ssh/id_ed25519`，目录 0700、私钥 600），公钥在网页给用户，用户手动加到目标机器的 `authorized_keys`。不读用户的 `~/.ssh`。
- `acquire(job_id, prefer)`：按主模型点名/配置顺序挑第一台「连得上 + 这会儿没活」的机器（同时只接 1 个任务），建立 `~/maiwork/<job_id>/` 工作目录。拿不到返回 None，原因进 `last_fail()`。
- `run(box, command)`：远端 `bash -lc` 跑（有 `timeout` 就用它限时），退出码原样回传。
- `put / get`：白名单校验相对路径，scp 传文件。
- `release(box)`：只释「忙」标记，远端目录留着（用户自己的机器）。
- 机器隔离由用户自己负责——这是专用机器，不是共享沙箱。

**Railway（railway.py）——一次性 VM：**

- 一把 key = 一台机器。acquire 时在 `<data_dir>/railway/<job_id>/` 生成 ed25519 key。
- 拿 manifest：不带命令，`ssh railway.new < /dev/null`，JSON 打在 stdout。`trial_starting / trial_ready` → 拿到了；`refused` / 退出码 13 → 拿不到。
- 限额：同一出口 IP 每天最多 3 台、同时只有 1 台能用。MaiWork 自己再收紧：`railway_daily_max`（默认 2）台/天，用量记 kv；同时只许 1 台（进程内锁 + kv `railway.active` 标记）。
- `run`：剩余时间 < timeout_s + 120 秒就拒（机器快到期）。
- **不能主动销毁**：官方没有销毁命令。release 只做「本地删 key + 释放锁」，等它 60 分钟到点被官方回收。

**回退逻辑（coordinator.py）**：

1. 主模型选 `env: "ssh"` → 申请专用机器；拿不到 → 改 `railway` / `local`；都拿不到且本机不能隔离 → 任务失败（不假装开工）。
2. 主模型选 `env: "railway"` → 申请一次性机器；拿不到 → 改 `ssh` / `local`。
3. 主模型选 `env: "local"` → 本机跑；`stopped` 判定且没远端机器 → 任务失败。
4. env 选 `ssh` 时工具集换成 `machine_*`（put_file/get_file/run_command）；选 `railway` 时换成 `vm_*`；本机时用 `run_command`/`start_process` 等本地工具。

### 集成名称与数据流

```
app.py 启动
  ├─ capability.probe() → Decision (fixed/dynamic/stopped)
  ├─ resolve_workspace_root() → 按模式换工作区根
  ├─ LocalEnv(get_settings, capability=decision)      ← 唯一经 __init__.py 导出
  ├─ RailwayEnv(get_settings, data_dir, store)        ← _import_m2_class 懒加载
  ├─ SshEnv(get_settings, data_dir)                   ← _import_m2_class 懒加载
  └─ SshEnv.ensure_key() → 生成 key 供网页展示

Coordinator.run_task() → _setup_exec_env()
  ├─ plan.env = "local" | "ssh" | "railway"
  ├─ "ssh"    → SshEnv.acquire(tid, prefer)  → SshBox
  │            → _run_job(tools=_remote_job_tools(..., box))
  │            → SshEnv.release(box)
  ├─ "railway" → RailwayEnv.acquire(tid)     → railway.Box
  │            → _run_job(tools=_remote_job_tools(..., box))
  │            → RailwayEnv.release(box)
  └─ "local"  → _run_job(tools=本机工具)
               → 本机工具调用 LocalEnv.run()/start()
```

**runner 契约**（三层统一）：`async runner(argv, *, cwd, env, timeout) → (code, out, err)`。测试注入假 runner。

## Flow

### 任务执行路径

```
主模型 plan
  ↓
Coordinator.run_task() → _setup_exec_env()
  ├─ plan.env == "ssh"    → SshBox
  │    → machine_run_command(box, cmd)
  │    → machine_put_file(...) / machine_get_file(...)
  │    → 完成 → SshEnv.release(box)
  ├─ plan.env == "railway" → railway.Box
  │    → vm_run_command(box, cmd)
  │    → vm_put_file(...) / vm_get_file(...)
  │    → 完成 → RailwayEnv.release(box)（本地删 key，远端等 60 分钟回收）
  └─ plan.env == "local"  → 本机工具调用 LocalEnv.run()/start()
       → 文件经 workspace_fd 锚定 fd 读写
       → 完成（无显式 release）
```

### 配置重载路径

`on_config_update` → `config.py:load_settings` → 配置变了 → `app._detect_local_capability` 重判并更新 `Decision`；现存执行环境对象未必重建，源码日志提示「判定结果变了需重启才完全生效」。

## macOS 限制（由 capability.py 判断逻辑推导，未在此任务运行实测）

| 条件 | 结果 |
|------|------|
| 非 Linux（macOS / Windows） | `is_linux` = False → `stopped` |
| 无 systemd | `has_systemd_run` = False → `stopped` |
| 非 root 运行 | `is_root` = False → `stopped` |

macOS 上在**生产配置**下本机不能隔离跑命令；工作区文件仍由插件进程直接读写（放数据目录下），命令活可选专用 SSH 机器或 Railway 一次性机器。仅在本地开发且显式启用 `MAIWORK_DEV_ALLOW_DIRECT=1`、非 root 时，`direct` 模式可无隔离跑命令，不能当作生产能力。本机文件读写（`write_file`、`list_files`、`read_file`、`resolve`）仍可使用。

## Integration

- 上层 [MaiWork 业务包](../codemap.md) 的 `app.py` 创建并传入 `LocalEnv`、`SshEnv`、`RailwayEnv`；`coordinator.py` 的 `_setup_exec_env()` 在任务开工前按 SSH → Railway → 本机等顺序选择，`_release_remote()` 在完成/失败时释放远端资源。
- `tools_exec.py` 的本机命令/进程与文件工具、`tools_ssh.py` 的 `machine_*`、`tools_railway.py` 的 `vm_*` 是给子 agent 的操作入口；环境模块本身不直接接收聊天消息。`config.py` 提供隔离、时限和机器清单，`store.py` 记录 Railway 用量。
- 控制台 [设置与健康视图](../console/codemap.md) 展示本机判定与远端机器状态；`direct` 只用于显式开发环境。机器的 SSH 隔离由用户自己承担；Railway 生命周期/回收说法来自代码引用的实测文档，本次只核源码、未连远端复测。
