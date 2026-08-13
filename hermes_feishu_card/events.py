from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict

from .status import normalize_display_status

SUPPORTED_EVENTS = {
    "message.started",
    "thinking.delta",
    "tool.updated",
    "answer.delta",
    "message.completed",
    "message.failed",
    "system.notice",
    "interaction.requested",
    "interaction.completed",
    "interaction.failed",
    "subagent.updated",
}

_EVENT_IDENTITY_MAX_CHARS = 256
_EVENT_PRODUCERS = {"plugin", "patch", "legacy-patch"}
_EVENT_PHASES = {"started", "terminal", "update"}


class EventValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SidecarEvent:
    schema_version: str
    event: str
    conversation_id: str
    message_id: str
    chat_id: str
    platform: str
    sequence: int
    created_at: float
    data: Dict[str, Any]
    thread_id: str = ""
    turn_id: str = ""
    event_id: str = ""
    producer: str = ""
    phase: str = ""

    @property
    def canonical_turn_id(self) -> str:
        return self.turn_id or self.message_id

    @property
    def display_status(self) -> str:
        return normalize_display_status(self.data.get("display_status"))

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SidecarEvent":
        if not isinstance(payload, dict):
            raise EventValidationError("payload must be an object")

        required = (
            "schema_version",
            "event",
            "conversation_id",
            "message_id",
            "chat_id",
            "platform",
            "sequence",
            "created_at",
            "data",
        )
        for key in required:
            if key not in payload:
                raise EventValidationError(f"missing required field: {key}")
        if payload["schema_version"] != "1":
            raise EventValidationError("unsupported schema_version")
        event = payload["event"]
        if not isinstance(event, str) or not event.strip():
            raise EventValidationError("event must be a non-empty string")
        if event not in SUPPORTED_EVENTS:
            raise EventValidationError(f"unknown event: {event}")
        if payload["platform"] != "feishu":
            raise EventValidationError("platform must be feishu")
        if (
            isinstance(payload["sequence"], bool)
            or not isinstance(payload["sequence"], int)
            or payload["sequence"] < 0
        ):
            raise EventValidationError("sequence must be a non-negative integer")
        for key in ("conversation_id", "message_id", "chat_id"):
            value = payload[key]
            if not isinstance(value, str) or not value.strip():
                raise EventValidationError(f"{key} must be a non-empty string")
        try:
            created_at = float(payload["created_at"])
        except (TypeError, ValueError) as exc:
            raise EventValidationError("created_at must be a number") from exc
        if not math.isfinite(created_at):
            raise EventValidationError("created_at must be finite")
        data = payload["data"]
        if not isinstance(data, dict):
            raise EventValidationError("data must be an object")
        thread_id = payload.get("thread_id", "")
        if thread_id is None:
            thread_id = ""
        if not isinstance(thread_id, str):
            raise EventValidationError("thread_id must be a string")
        turn_id = payload.get("turn_id", "")
        if not isinstance(turn_id, str):
            raise EventValidationError("turn_id must be a string")
        identity: dict[str, str] = {}
        for key in ("event_id", "producer", "phase"):
            value = payload.get(key, "")
            if type(value) is not str:
                raise EventValidationError(f"{key} must be an ordinary string")
            canonical_value = value.strip()
            if len(canonical_value) > _EVENT_IDENTITY_MAX_CHARS:
                raise EventValidationError(f"{key} must be at most 256 characters")
            identity[key] = canonical_value
        if identity["producer"] and identity["producer"] not in _EVENT_PRODUCERS:
            raise EventValidationError("unknown producer")
        if identity["phase"] and identity["phase"] not in _EVENT_PHASES:
            raise EventValidationError("unknown phase")
        stripped_turn_id = turn_id.strip()
        identity_present = any(identity.values())
        if identity_present and not all(
            (
                identity["event_id"],
                identity["producer"],
                identity["phase"],
                stripped_turn_id,
            )
        ):
            raise EventValidationError("partial event identity")
        return cls(
            schema_version=payload["schema_version"],
            event=event,
            conversation_id=payload["conversation_id"],
            message_id=payload["message_id"],
            chat_id=payload["chat_id"],
            thread_id=thread_id.strip(),
            platform=payload["platform"],
            sequence=payload["sequence"],
            created_at=created_at,
            data=data,
            turn_id=stripped_turn_id,
            event_id=identity["event_id"],
            producer=identity["producer"],
            phase=identity["phase"],
        )
