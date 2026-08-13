import importlib
import sys

from hermes_feishu_card import hermes_plugin
from tests.fixtures.hermes_v020_plugin_api import PluginContext


EXPECTED_HOOKS = {
    "pre_llm_call", "post_llm_call", "on_session_end",
    "on_session_reset", "on_session_finalize", "pre_tool_call",
    "post_tool_call", "pre_approval_request", "post_approval_response",
    "subagent_start", "subagent_stop",
}


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


def test_register_registers_exactly_the_verified_v020_official_hook_names():
    context = PluginContext()
    assert hermes_plugin.register(context) is None
    assert set(context.registered) == EXPECTED_HOOKS


def test_register_context_matches_v020_and_has_no_valid_hooks_attribute():
    context = PluginContext()
    assert hermes_plugin.register(context) is None
    assert not hasattr(context, "VALID_HOOKS")


def test_one_host_registration_error_does_not_abort_later_hooks():
    context = PluginContext(reject_hooks={"post_llm_call"})
    assert hermes_plugin.register(context) is None
    assert "post_llm_call" not in context.registered
    assert set(context.registered) == EXPECTED_HOOKS - {"post_llm_call"}


def test_registered_callback_returns_none_when_runtime_callback_raises(monkeypatch):
    context = PluginContext()
    hermes_plugin.register(context)
    monkeypatch.setattr(
        "hermes_feishu_card.hermes_plugin_runtime.handle_pre_llm_call",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bridge failure")),
    )
    assert context.registered["pre_llm_call"](turn_id="turn-1") is None
