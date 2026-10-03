"""端点自定义头值也须进入工具/群可见输出的统一遮罩。"""
from types import SimpleNamespace

from CharTyr_MaiWork.maiwork.app import MaiWorkApp


def test_endpoint_headers_in_global_secret_mask():
    app = MaiWorkApp.__new__(MaiWorkApp)
    app.store = None
    app.search = None
    app._settings = SimpleNamespace(
        models=SimpleNamespace(api_key=""), jev=SimpleNamespace(api_key=""),
        console=SimpleNamespace(password=""), extensions=SimpleNamespace(mcp=()),
        endpoints=(SimpleNamespace(api_key="endpoint-key", headers={"X-Route": "private-route-value"}),),
    )
    assert "private-route-value" in app._known_secrets()
