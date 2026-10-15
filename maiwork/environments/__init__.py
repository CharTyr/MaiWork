"""执行环境（docs/02-设计.md §9、docs/07-代码接口.md §11.1 / §11.1b / §11.1d / §11.1e）。

local（本机执行环境）在这里导出。一次性 VM 现在有第二家（Freestyle），平台预设表在
environments/vm_platforms.py；app 用 `vm_platforms.make_env` 建**用户选中的那一家**
（不再写死导入 RailwayEnv）。专用 SSH 机器见 environments.ssh（SshEnv）。
"""

from __future__ import annotations

from . import capability, vm_platforms
from .local import LocalEnv, RunResult

__all__ = ["LocalEnv", "RunResult", "capability", "vm_platforms"]
