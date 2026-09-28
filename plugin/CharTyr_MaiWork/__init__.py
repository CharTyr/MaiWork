"""MaiWork：MaiBot 旁边的群聊生产力管线。create_plugin 在 plugin.py 里。"""

try:
    from .plugin import PLUGIN_ID, PLUGIN_VERSION, MaiWorkPlugin

    __all__ = ["PLUGIN_ID", "PLUGIN_VERSION", "MaiWorkPlugin"]
except Exception:  # 模块化加载（如本地单测直接 import 子模块）时包入口失败不拖后腿
    __all__ = []
