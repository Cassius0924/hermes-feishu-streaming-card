from __future__ import annotations

from collections.abc import Callable, Iterable


class PluginContext:
    def __init__(self, valid_hooks: Iterable[str]):
        self.VALID_HOOKS = frozenset(valid_hooks)
        self.registered: dict[str, Callable] = {}

    def register_hook(self, name: str, callback: Callable) -> None:
        if name not in self.VALID_HOOKS:
            raise ValueError(f"unsupported hook: {name}")
        self.registered[name] = callback
