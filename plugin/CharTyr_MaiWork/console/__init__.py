"""MaiWork 网页（console）：server.py 是 HTTP 层，views.py 拼页面数据，static/ 是前端。"""

from .server import ConsoleServer, create_app
from . import views

__all__ = ["ConsoleServer", "create_app", "views"]
