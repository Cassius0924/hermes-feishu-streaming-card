from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock, RLock
from time import time
from typing import Any


OFFICIAL_HOOKS = (
    "pre_llm_call", "post_llm_call", "on_session_end",
    "on_session_reset", "on_session_finalize", "pre_tool_call",
    "post_tool_call", "pre_approval_request", "post_approval_response",
    "subagent_start", "subagent_stop",
)


class TurnState(str, Enum):
    PENDING_START = "pending-start"
    CARD_ACTIVE = "card-active"
    NATIVE_BYPASS = "native-bypass"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class IngressBinding:
    profile_id: str
    session_id: str
    generation: str
    chat_id: str
    incoming_message_id: str
    reply_to_message_id: str
    thread_id: str
    expires_at: float


@dataclass
class TurnBinding:
    ingress: IngressBinding
    turn_id: str
    _state: TurnState = field(default=TurnState.PENDING_START, init=False, repr=False, compare=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    @property
    def state(self) -> TurnState:
        with self._lock:
            return self._state

    @property
    def accepts_observer_events(self) -> bool:
        with self._lock:
            return self._state is TurnState.CARD_ACTIVE

    def record_started_result(self, result: object) -> TurnState:
        with self._lock:
            if self._state is not TurnState.PENDING_START:
                return self._state
            if (
                type(result) is dict
                and set(result) == {"ok", "applied"}
                and result["ok"] is True
                and result["applied"] is True
            ):
                self._state = TurnState.CARD_ACTIVE
            else:
                self._state = TurnState.NATIVE_BYPASS
            return self._state

    def finish(self) -> bool:
        with self._lock:
            if self._state is TurnState.TERMINAL:
                return False
            self._state = TurnState.TERMINAL
            return True


class IngressBindingRegistry:
    """A bounded, one-shot registry for Feishu ingress bindings."""

    _MAX_BINDINGS = 1024

    def __init__(self, now: Callable[[], float] = time) -> None:
        self._now = now
        self._bindings: OrderedDict[tuple[str, str, str], IngressBinding] = OrderedDict()
        self._lock = RLock()

    def bind(self, binding: IngressBinding) -> bool:
        if not self._is_valid_binding(binding):
            return False
        with self._lock:
            now = self._now()
            self._prune_expired(now)
            if binding.expires_at <= now:
                return False
            pair = (binding.profile_id, binding.session_id)
            for key in tuple(self._bindings):
                if key[:2] == pair:
                    del self._bindings[key]
            key = (*pair, binding.generation)
            self._bindings[key] = binding
            while len(self._bindings) > self._MAX_BINDINGS:
                self._bindings.popitem(last=False)
            return True

    def claim(
        self, profile_id: str, session_id: str, generation: str, turn_id: str
    ) -> TurnBinding | None:
        if not all(self._is_nonblank(value) for value in (profile_id, session_id, generation, turn_id)):
            return None
        with self._lock:
            self._prune_expired(self._now())
            binding = self._bindings.pop((profile_id, session_id, generation), None)
            if binding is None:
                return None
            return TurnBinding(ingress=binding, turn_id=turn_id)

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()

    @staticmethod
    def _is_nonblank(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @classmethod
    def _is_valid_binding(cls, binding: object) -> bool:
        if not isinstance(binding, IngressBinding):
            return False
        return all(
            cls._is_nonblank(value)
            for value in (
                binding.profile_id,
                binding.session_id,
                binding.generation,
                binding.chat_id,
                binding.incoming_message_id,
                binding.reply_to_message_id,
            )
        )

    def _prune_expired(self, now: float) -> None:
        for key, binding in tuple(self._bindings.items()):
            if binding.expires_at <= now:
                del self._bindings[key]


_ingress_registry = IngressBindingRegistry()


def reset_plugin_runtime_state() -> None:
    _ingress_registry.clear()
    return None


def _no_op(**kwargs: Any) -> None:
    return None


def handle_pre_llm_call(**kwargs: Any) -> None:
    return None


def handle_post_llm_call(**kwargs: Any) -> None:
    return None


def handle_on_session_end(**kwargs: Any) -> None:
    return None


def handle_on_session_reset(**kwargs: Any) -> None:
    return None


def handle_on_session_finalize(**kwargs: Any) -> None:
    return None


def handle_pre_tool_call(**kwargs: Any) -> None:
    return None


def handle_post_tool_call(**kwargs: Any) -> None:
    return None


def handle_pre_approval_request(**kwargs: Any) -> None:
    return None


def handle_post_approval_response(**kwargs: Any) -> None:
    return None


def handle_subagent_start(**kwargs: Any) -> None:
    return None


def handle_subagent_stop(**kwargs: Any) -> None:
    return None


HOOK_HANDLERS = {
    "pre_llm_call": "handle_pre_llm_call",
    "post_llm_call": "handle_post_llm_call",
    "on_session_end": "handle_on_session_end",
    "on_session_reset": "handle_on_session_reset",
    "on_session_finalize": "handle_on_session_finalize",
    "pre_tool_call": "handle_pre_tool_call",
    "post_tool_call": "handle_post_tool_call",
    "pre_approval_request": "handle_pre_approval_request",
    "post_approval_response": "handle_post_approval_response",
    "subagent_start": "handle_subagent_start",
    "subagent_stop": "handle_subagent_stop",
}


def _callback(handler_name: str) -> Callable[..., None]:
    def invoke(**kwargs: Any) -> None:
        try:
            globals()[handler_name](**kwargs)
        except Exception:
            return None
        return None

    return invoke


def register_callbacks(ctx: Any) -> None:
    valid = set(getattr(ctx, "VALID_HOOKS", ()))
    for name, handler_name in HOOK_HANDLERS.items():
        if name in valid:
            ctx.register_hook(name, _callback(handler_name))
    return None
