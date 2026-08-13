from __future__ import annotations

from collections.abc import Callable
from typing import Any


OFFICIAL_HOOKS = (
    "pre_llm_call", "post_llm_call", "on_session_end",
    "on_session_reset", "on_session_finalize", "pre_tool_call",
    "post_tool_call", "pre_approval_request", "post_approval_response",
    "subagent_start", "subagent_stop",
)


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
