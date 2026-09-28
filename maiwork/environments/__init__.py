"""执行环境（docs/02-设计.md §9、docs/07-代码接口.md §11.1 / §11.1b）。

local（本机执行环境）在这里导出；railway.new 一次性 VM 见 environments/railway.py，
app 用全路径取（`_import_m2_class("environments.railway", "RailwayEnv")`）。ssh 还没做。
"""

from __future__ import annotations

from . import capability
from .local import LocalEnv, RunResult

__all__ = ["LocalEnv", "RunResult", "capability"]
