import importlib
import sys


def test_plugin_import_does_not_import_hermes_or_runtime_bridge():
    sys.modules.pop("hermes_feishu_card.hermes_plugin", None)
    before = set(sys.modules)
    module = importlib.import_module("hermes_feishu_card.hermes_plugin")
    imported = set(sys.modules) - before
    assert callable(module.register)
    assert not any(
        name == "hermes_cli" or name.startswith("hermes_cli.")
        for name in imported
    )
    assert "hermes_feishu_card.hermes_plugin_runtime" not in imported


def test_register_is_fail_open_until_runtime_bridge_is_available(monkeypatch):
    module = importlib.import_module("hermes_feishu_card.hermes_plugin")
    real_import = importlib.import_module

    def unavailable(name, package=None):
        if name == ".hermes_plugin_runtime":
            raise ImportError("runtime unavailable")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", unavailable)
    assert module.register(object()) is None


def test_register_lazily_delegates_to_runtime_bridge(monkeypatch):
    module = importlib.import_module("hermes_feishu_card.hermes_plugin")
    requested = []
    received = []
    ctx = object()

    class Runtime:
        @staticmethod
        def register_callbacks(callback_ctx):
            received.append(callback_ctx)

    def runtime_import(name, package=None):
        requested.append((name, package))
        assert name == ".hermes_plugin_runtime"
        assert package == "hermes_feishu_card"
        return Runtime

    monkeypatch.setattr(importlib, "import_module", runtime_import)

    assert module.register(ctx) is None
    assert received == [ctx]
    assert requested == [(".hermes_plugin_runtime", "hermes_feishu_card")]
