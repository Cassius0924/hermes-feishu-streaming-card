from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from math import isfinite
import queue
import re
import threading
from threading import Lock, RLock
from time import monotonic, time
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
    profile_source: str
    session_id: str
    gateway_session_key: str
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
            if self._is_accepted_started_result(result):
                self._state = TurnState.CARD_ACTIVE
            else:
                self._state = TurnState.NATIVE_BYPASS
            return self._state

    @staticmethod
    def _is_accepted_started_result(result: object) -> bool:
        if type(result) is not dict:
            return False
        if not all(type(key) is str for key in result):
            return False
        keys = set(result)
        if keys not in (
            {"ok", "applied"},
            {"ok", "applied", "delivery"},
        ):
            return False
        if result["ok"] is not True or result["applied"] is not True:
            return False
        if "delivery" not in result:
            return True
        delivery = result["delivery"]
        if type(delivery) is not dict:
            return False
        if not all(type(key) is str for key in delivery):
            return False
        if set(delivery) != {"outcome"}:
            return False
        outcome = delivery["outcome"]
        return type(outcome) is str and outcome == "delivered"

    def finish(self) -> bool:
        with self._lock:
            if self._state is TurnState.TERMINAL:
                return False
            self._state = TurnState.TERMINAL
            return True


@dataclass(frozen=True)
class ObserverEvent:
    sequence: int
    producer: str
    payload: dict[str, object]


class TurnEventCoordinator:
    """Allocate one turn-local sequence and bound asynchronous observer work."""

    _PRODUCERS = frozenset({"plugin", "patch", "legacy-patch"})
    _WORKER_POLL_SECONDS = 0.05
    _CLOSE_JOIN_SECONDS = 0.1

    def __init__(
        self,
        turn_id: str,
        *,
        max_pending: int = 64,
        deliver: Callable[[ObserverEvent], None] | None = None,
        start_worker: bool = True,
    ) -> None:
        if not self._is_nonblank(turn_id):
            raise ValueError("turn_id must be nonblank")
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending <= 0:
            raise ValueError("max_pending must be a positive integer")
        self.turn_id = turn_id
        self._next = 0
        self._barrier: int | None = None
        self._terminal_sequence: int | None = None
        self._closed = False
        self._lock = Lock()
        self._queue: queue.Queue[ObserverEvent] = queue.Queue(maxsize=max_pending)
        self._deliver = deliver or (lambda event: None)
        self._worker: threading.Thread | None = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._deliver_observer_events,
                name="hfc-turn-observer",
                daemon=True,
            )
            self._worker.start()

    def next_sequence(self, producer: str) -> int:
        self._validate_producer(producer)
        with self._lock:
            value, self._next = self._next, self._next + 1
            return value

    def close_terminal_barrier(self) -> int:
        with self._lock:
            if self._barrier is None:
                self._barrier = self._next - 1
            return self._barrier

    def next_terminal_sequence(self) -> int:
        with self._lock:
            if self._barrier is None:
                raise ValueError("terminal barrier is not closed")
            if self._terminal_sequence is not None:
                return self._terminal_sequence
            value, self._next = self._next, self._next + 1
            self._terminal_sequence = value
            self._closed = True
            return value

    def event_id(self, kind: str, *, item_id: str = "", phase: str = "") -> str:
        if kind in {"started", "completed", "failed"}:
            return f"turn:{self.turn_id}:{kind}"
        if kind not in {"tool", "approval", "subagent"} or not self._is_nonblank(item_id):
            raise ValueError("invalid event identity")
        if phase not in {"started", "terminal"}:
            raise ValueError("invalid event phase")
        return f"{kind}:{self.turn_id}:{item_id}:{phase}"

    def submit_observer(self, payload: dict[str, object], *, producer: str) -> bool:
        self._validate_producer(producer)
        with self._lock:
            if self._closed or self._barrier is not None:
                return False
            sequence, self._next = self._next, self._next + 1
            event = ObserverEvent(sequence, producer, dict(payload, sequence=sequence))
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                return False
            return True

    def drain_before_terminal(self, timeout_seconds: float) -> None:
        deadline = monotonic() + max(0.0, timeout_seconds)
        with self._lock:
            self._closed = True
        while self._queue.unfinished_tasks and monotonic() < deadline:
            threading.Event().wait(0.001)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self._CLOSE_JOIN_SECONDS)

    def _deliver_observer_events(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=self._WORKER_POLL_SECONDS)
            except queue.Empty:
                with self._lock:
                    if self._closed:
                        return
                continue
            try:
                self._deliver(event)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    @staticmethod
    def _is_nonblank(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @classmethod
    def _validate_producer(cls, producer: object) -> None:
        if producer not in cls._PRODUCERS:
            raise ValueError("unknown producer")


class IngressBindingRegistry:
    """A bounded, one-shot registry for Feishu ingress bindings."""

    _MAX_BINDINGS = 1024
    _PROFILE_SOURCES = frozenset({"env", "locals", "hermes_home", "fallback_default"})

    def __init__(
        self,
        now: Callable[[], float] = time,
        *,
        lock: RLock | None = None,
    ) -> None:
        self._now = now
        self._bindings: OrderedDict[tuple[str, str, str], IngressBinding] = OrderedDict()
        self._lock = lock or RLock()

    def bind(self, binding: IngressBinding) -> bool:
        with self._lock:
            now = self._now()
            self._prune_expired(now)
            if not self._is_valid_binding(binding):
                return False
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
        with self._lock:
            self._prune_expired(self._now())
            if not all(
                self._is_exact_nonblank(value)
                for value in (profile_id, session_id, generation, turn_id)
            ):
                return None
            binding = self._bindings.pop((profile_id, session_id, generation), None)
            if binding is None:
                return None
            return TurnBinding(ingress=binding, turn_id=turn_id)

    def claim_unique_session(self, session_id: str, turn_id: str) -> TurnBinding | None:
        with self._lock:
            self._prune_expired(self._now())
            if not all(self._is_exact_nonblank(value) for value in (session_id, turn_id)):
                return None
            candidates = [
                (key, binding)
                for key, binding in self._bindings.items()
                if binding.session_id == session_id
            ]
            if len(candidates) != 1:
                return None
            key, binding = candidates[0]
            del self._bindings[key]
            return TurnBinding(ingress=binding, turn_id=turn_id)

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()

    def remove_session(self, session_id: object) -> None:
        with self._lock:
            self._prune_expired(self._now())
            if not self._is_exact_nonblank(session_id):
                return
            for key, binding in tuple(self._bindings.items()):
                if binding.session_id == session_id:
                    del self._bindings[key]

    @staticmethod
    def _is_exact_nonblank(value: object) -> bool:
        return type(value) is str and bool(value.strip())

    @classmethod
    def _is_valid_binding(cls, binding: object) -> bool:
        if type(binding) is not IngressBinding:
            return False
        if not all(
            cls._is_exact_nonblank(value)
            for value in (
                binding.profile_id,
                binding.session_id,
                binding.gateway_session_key,
                binding.generation,
                binding.chat_id,
                binding.incoming_message_id,
                binding.reply_to_message_id,
            )
        ):
            return False
        if (
            type(binding.profile_source) is not str
            or binding.profile_source not in cls._PROFILE_SOURCES
        ):
            return False
        if type(binding.thread_id) is not str:
            return False
        expires_at = binding.expires_at
        if type(expires_at) not in (int, float):
            return False
        try:
            return isfinite(expires_at)
        except (OverflowError, TypeError, ValueError):
            return False

    def _prune_expired(self, now: float) -> None:
        for key, binding in tuple(self._bindings.items()):
            if binding.expires_at <= now:
                del self._bindings[key]


@dataclass(frozen=True, repr=False)
class PendingApproval:
    session_key: str
    turn_id: str
    tool_call_id: str
    command_fingerprint: str
    surface: str
    interaction_id: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class _AnswerEntry:
    answer: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class _TerminalRecord:
    payload: dict[str, object]
    response: dict[str, object] | None
    expires_at: float


class PluginRuntime:
    """Bounded official-hook coordinator.

    Lock order is runtime lock -> registry/turn/coordinator locks.  Transport,
    coordinator drain, and coordinator close always run after releasing the
    runtime lock, so callbacks never wait while blocking cross-map cleanup.
    """

    _MAX_ENTRIES = 1024
    _ANSWER_TTL_SECONDS = 30.0
    _STATE_TTL_SECONDS = 300.0
    _NATIVE_HANDOFF_PROTOCOL = "hfc-native-handoff-v2"
    _NATIVE_HANDOFF_MAX_FUTURE_SECONDS = 3630.0
    _HANDOFF_ID_RE = re.compile(r"[0-9a-f]{64}")
    _UUID_SEED_RE = re.compile(r"[0-9a-f]{32}")

    def __init__(
        self,
        *,
        post: Callable[[dict[str, object], float], object],
        now: Callable[[], float] = time,
        observer_timeout_seconds: float = 0.8,
        terminal_timeout_seconds: float = 10.0,
        max_pending_observers: int = 64,
    ) -> None:
        if not callable(post) or not callable(now):
            raise ValueError("post and now must be callable")
        if (
            type(max_pending_observers) is not int
            or max_pending_observers <= 0
            or max_pending_observers > self._MAX_ENTRIES
        ):
            raise ValueError("max_pending_observers is invalid")
        self._post = post
        self._now = now
        self._observer_timeout_seconds = max(0.0, float(observer_timeout_seconds))
        self._terminal_timeout_seconds = max(0.0, float(terminal_timeout_seconds))
        self._max_pending_observers = max_pending_observers
        self._lock = RLock()
        self._registry = IngressBindingRegistry(now=now, lock=self._lock)
        self._turns: OrderedDict[str, TurnBinding] = OrderedDict()
        self._coordinators: OrderedDict[str, TurnEventCoordinator] = OrderedDict()
        self._answers: OrderedDict[str, _AnswerEntry] = OrderedDict()
        self._terminal_records: OrderedDict[str, _TerminalRecord] = OrderedDict()
        self._dispositions: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._pending_approvals: OrderedDict[
            tuple[str, str, str, str, str], PendingApproval
        ] = OrderedDict()
        self._claimed_approvals: set[tuple[str, str, str, str, str]] = set()
        self._subagents: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
        self._terminal_owners: OrderedDict[str, object] = OrderedDict()

    def bind_ingress_from_values(
        self,
        profile_id: object,
        profile_source: object,
        session_id: object,
        gateway_session_key: object,
        generation: object,
        chat_id: object,
        incoming_message_id: object,
        reply_to_message_id: object,
        thread_id: object,
    ) -> bool:
        binding = IngressBinding(
            profile_id=profile_id,
            profile_source=profile_source,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            generation=generation,
            chat_id=chat_id,
            incoming_message_id=incoming_message_id,
            reply_to_message_id=reply_to_message_id,
            thread_id=thread_id,
            expires_at=self._now() + self._STATE_TTL_SECONDS,
        )
        with self._lock:
            return self._registry.bind(binding)

    def turn_state(self, turn_id: object) -> TurnState | None:
        with self._lock:
            self._expire_locked(self._now())
            turn = self._turns.get(turn_id) if self._exact_nonblank(turn_id) else None
        return turn.state if turn is not None else None

    def handle_pre_llm_call(self, **kwargs: object) -> None:
        session_id = kwargs.get("session_id")
        turn_id = kwargs.get("turn_id")
        if type(kwargs.get("platform")) is not str or kwargs.get("platform") != "feishu":
            return None
        if not all(self._exact_nonblank(value) for value in (session_id, turn_id)):
            return None
        coordinator = TurnEventCoordinator(
            turn_id,
            max_pending=self._max_pending_observers,
            deliver=self._deliver_observer,
        )
        turn: TurnBinding | None = None
        admitted = False
        evicted_coordinators: list[TurnEventCoordinator] = []
        with self._lock:
            self._expire_locked(self._now())
            if turn_id not in self._turns:
                admitted, evicted_coordinators = self._make_turn_room_locked()
            if admitted:
                turn = self._registry.claim_unique_session(session_id, turn_id)
                if turn is not None:
                    self._turns[turn_id] = turn
                    self._coordinators[turn_id] = coordinator
                else:
                    admitted = False
        for evicted in evicted_coordinators:
            evicted.close()
        if not admitted or turn is None:
            coordinator.close()
            return None
        with self._lock:
            cleaned_before_transport = self._turns.get(turn_id) is not turn
        if cleaned_before_transport:
            coordinator.close()
            return None
        payload = self._base_payload(
            turn,
            sequence=coordinator.next_sequence("plugin"),
            created_at=self._now(),
        )
        payload.update(
            event="message.started",
            event_id=coordinator.event_id("started"),
            phase="started",
            data={
                "profile_id": turn.ingress.profile_id,
                "profile_source": turn.ingress.profile_source,
                "reply_to_message_id": turn.ingress.reply_to_message_id,
            },
        )
        result = self._post_retry_unknown(
            payload,
            self._observer_timeout_seconds,
            self._is_exact_started_response,
        )
        turn.record_started_result(result)
        return None

    def handle_post_llm_call(self, **kwargs: object) -> None:
        turn_id = kwargs.get("turn_id")
        answer = kwargs.get("assistant_response")
        if not self._exact_nonblank(turn_id) or type(answer) is not str:
            return None
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            turn = self._turns.get(turn_id)
            if turn is None or turn.state is TurnState.TERMINAL:
                return None
            self._answers[turn_id] = _AnswerEntry(answer, now + self._ANSWER_TTL_SECONDS)
            self._answers.move_to_end(turn_id)
            self._trim_locked(self._answers)
        return None

    def handle_on_session_end(self, **kwargs: object) -> None:
        turn_id = kwargs.get("turn_id")
        if not self._exact_nonblank(turn_id):
            with self._lock:
                self._expire_locked(self._now())
            return None
        completed = kwargs.get("completed")
        failed = kwargs.get("failed")
        interrupted = kwargs.get("interrupted")
        flags_exact = all(type(value) is bool for value in (completed, failed, interrupted))
        coordinator: TurnEventCoordinator | None = None
        turn: TurnBinding | None = None
        owner_token: object | None = None
        answer: str | None = None
        terminal_kind: str | None = None
        with self._lock:
            now = self._now()
            self._expire_locked(now)
            turn = self._turns.get(turn_id)
            if turn is None or turn.state is TurnState.TERMINAL or turn_id in self._terminal_owners:
                return None
            entry = self._answers.get(turn_id)
            if entry is not None:
                answer = entry.answer
            if flags_exact and (failed is True or interrupted is True):
                terminal_kind = "failed"
            elif (
                flags_exact
                and completed is True
                and failed is False
                and interrupted is False
                and answer is not None
            ):
                terminal_kind = "completed"
            else:
                cleanup_coordinator = self._cleanup_turn_locked(
                    turn_id, keep_disposition=False
                )
                turn.finish()
                coordinator = cleanup_coordinator
                terminal_kind = None
            if terminal_kind is not None:
                owner_token = object()
                self._terminal_owners[turn_id] = owner_token
                self._terminal_owners.move_to_end(turn_id)
                coordinator = self._coordinators.get(turn_id)
        if terminal_kind is None:
            if coordinator is not None:
                coordinator.close()
            return None
        if coordinator is not None and turn.state is TurnState.CARD_ACTIVE:
            coordinator.close_terminal_barrier()
            coordinator.drain_before_terminal(self._observer_timeout_seconds)
            sequence = coordinator.next_terminal_sequence()
        elif coordinator is not None:
            coordinator.close_terminal_barrier()
            sequence = coordinator.next_terminal_sequence()
        else:
            sequence = 1
        created_at = self._now()
        payload = self._base_payload(turn, sequence=sequence, created_at=created_at)
        payload.update(
            event=f"message.{terminal_kind}",
            event_id=f"turn:{turn_id}:{terminal_kind}",
            phase="terminal",
            data=(
                {"answer": answer}
                if terminal_kind == "completed"
                else {
                    "error": "消息处理失败",
                    "turn_exit_reason": self._classify_exit_reason(
                        kwargs.get("turn_exit_reason"), interrupted is True
                    ),
                }
            ),
        )
        validation_time = self._now()
        response = self._post_retry_unknown(
            payload,
            self._terminal_timeout_seconds,
            lambda value: self._valid_terminal_response(value, now=validation_time)
            is not None,
        )
        valid_response = self._valid_terminal_response(response, now=validation_time)
        with self._lock:
            now = self._now()
            still_owner = self._terminal_owners.get(turn_id) is owner_token
            if still_owner:
                self._terminal_owners.pop(turn_id, None)
            if still_owner:
                payload_copy = deepcopy(payload)
                response_copy = deepcopy(valid_response)
                record = _TerminalRecord(
                    payload_copy, response_copy, now + self._STATE_TTL_SECONDS
                )
                self._terminal_records[turn_id] = record
                self._terminal_records.move_to_end(turn_id)
                if (
                    response_copy is not None
                    and payload_copy.get("event") == "message.completed"
                    and response_copy == {"ok": True, "applied": True}
                ):
                    self._dispositions[turn_id] = deepcopy(response_copy)
                    self._dispositions.move_to_end(turn_id)
                self._cleanup_turn_locked(turn_id, keep_disposition=True)
                self._trim_locked(self._terminal_records)
                self._trim_locked(self._dispositions)
        turn.finish()
        if coordinator is not None:
            coordinator.close()
        return None

    def handle_on_session_reset(self, **kwargs: object) -> None:
        self._cleanup_session(kwargs.get("old_session_id"))
        return None

    def handle_on_session_finalize(self, **kwargs: object) -> None:
        self._cleanup_session(kwargs.get("session_id"))
        return None

    def handle_pre_tool_call(self, **kwargs: object) -> None:
        self._submit_tool(kwargs, pending=True)
        return None

    def handle_post_tool_call(self, **kwargs: object) -> None:
        self._submit_tool(kwargs, pending=False)
        return None

    def handle_pre_approval_request(self, **kwargs: object) -> None:
        values = self._approval_values(kwargs)
        if values is None:
            return None
        session_key, turn_id, tool_call_id, surface, fingerprint = values
        interaction_id = f"approval:{turn_id}:{tool_call_id}:{fingerprint[:16]}"
        pending = PendingApproval(
            session_key=session_key,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            command_fingerprint=fingerprint,
            surface=surface,
            interaction_id=interaction_id,
            expires_at=self._now() + self._STATE_TTL_SECONDS,
        )
        key = (session_key, turn_id, tool_call_id, surface, fingerprint)
        with self._lock:
            self._expire_locked(self._now())
            if key in self._pending_approvals:
                return None
            coordinator = self._card_active_coordinator_locked(turn_id)
            if coordinator is None:
                return None
            self._pending_approvals[key] = pending
            self._pending_approvals.move_to_end(key)
            self._trim_pending_approvals_locked()
        payload = self._observer_payload(
            turn_id,
            event="interaction.requested",
            event_id=(
                f"approval:{turn_id}:{tool_call_id}:{fingerprint}:requested"
            ),
            phase="started",
            data={
                "interaction_id": interaction_id,
                "kind": "approval",
                "prompt": "需要授权后继续执行",
                "allow_custom_input": False,
                "options": [
                    {"label": "允许一次", "value": "once", "style": "primary"},
                    {"label": "本会话允许", "value": "session"},
                    {"label": "始终允许", "value": "always"},
                    {"label": "拒绝", "value": "deny", "style": "danger"},
                ],
            },
        )
        if payload is None or not coordinator.submit_observer(payload, producer="plugin"):
            with self._lock:
                self._pending_approvals.pop(key, None)
        return None

    def handle_post_approval_response(self, **kwargs: object) -> None:
        values = self._approval_values(kwargs)
        if values is None:
            return None
        choice = kwargs.get("choice")
        if type(choice) is not str:
            return None
        choice = choice.strip().lower()
        if choice not in {"once", "session", "always", "deny", "timeout"}:
            return None
        session_key, turn_id, tool_call_id, surface, fingerprint = values
        key = (session_key, turn_id, tool_call_id, surface, fingerprint)
        with self._lock:
            self._expire_locked(self._now())
            pending = self._pending_approvals.pop(key, None)
            self._claimed_approvals.discard(key)
            coordinator = self._card_active_coordinator_locked(turn_id)
        if pending is None or coordinator is None:
            return None
        event = "interaction.completed" if choice != "timeout" else "interaction.failed"
        data: dict[str, object] = {"interaction_id": pending.interaction_id}
        if choice == "timeout":
            data["error"] = "交互已过期"
        else:
            data["choice"] = choice
        payload = self._observer_payload(
            turn_id,
            event=event,
            event_id=f"approval:{turn_id}:{tool_call_id}:{fingerprint}:terminal",
            phase="terminal",
            data=data,
        )
        if payload is not None:
            coordinator.submit_observer(payload, producer="plugin")
        return None

    def take_pending_approval(
        self,
        session_key: object,
        turn_id: object,
        tool_call_id: object,
        command: object,
        surface: object,
    ) -> PendingApproval | None:
        values = self._approval_values(
            {
                "session_key": session_key,
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "command": command,
                "surface": surface,
            }
        )
        if values is None:
            return None
        key = values
        with self._lock:
            self._expire_locked(self._now())
            pending = self._pending_approvals.get(key)
            if pending is None or key in self._claimed_approvals:
                return None
            self._claimed_approvals.add(key)
            return pending

    def handle_subagent_start(self, **kwargs: object) -> None:
        turn_id = kwargs.get("parent_turn_id")
        child_session_id = kwargs.get("child_session_id")
        child_id = kwargs.get("child_subagent_id")
        if not self._exact_nonblank(child_id):
            child_id = child_session_id
        if not all(self._exact_nonblank(value) for value in (turn_id, child_session_id, child_id)):
            return None
        with self._lock:
            self._expire_locked(self._now())
            coordinator = self._card_active_coordinator_locked(turn_id)
            if coordinator is None:
                return None
            key = (turn_id, child_session_id)
            self._subagents[key] = (child_id, self._now() + self._STATE_TTL_SECONDS)
            self._subagents.move_to_end(key)
            self._trim_locked(self._subagents)
        data: dict[str, object] = {"child_id": child_id, "status": "queued"}
        role = self._preview(kwargs.get("child_role"))
        goal = self._preview(kwargs.get("child_goal"))
        if role:
            data["role"] = role
        if goal:
            data["goal_preview"] = goal
        self._submit_subagent(coordinator, turn_id, child_id, "started", data)
        return None

    def handle_subagent_stop(self, **kwargs: object) -> None:
        turn_id = kwargs.get("parent_turn_id")
        child_session_id = kwargs.get("child_session_id")
        if not all(self._exact_nonblank(value) for value in (turn_id, child_session_id)):
            return None
        with self._lock:
            self._expire_locked(self._now())
            coordinator = self._card_active_coordinator_locked(turn_id)
            identity = self._subagents.pop((turn_id, child_session_id), None)
        if coordinator is None or identity is None:
            return None
        child_id = identity[0]
        status = kwargs.get("child_status")
        safe_status = self._subagent_status(status)
        data: dict[str, object] = {"child_id": child_id, "status": safe_status}
        role = self._preview(kwargs.get("child_role"))
        summary = self._preview(kwargs.get("child_summary"))
        duration = self._safe_duration(kwargs.get("duration_ms"))
        if role:
            data["role"] = role
        if summary:
            data["summary_preview"] = summary
        if duration is not None:
            data["duration_ms"] = duration
        self._submit_subagent(coordinator, turn_id, child_id, "terminal", data)
        return None

    def take_terminal_disposition(self, turn_id: object) -> dict[str, object] | None:
        if not self._exact_nonblank(turn_id):
            return None
        with self._lock:
            self._expire_locked(self._now())
            record = self._terminal_records.get(turn_id)
            disposition = self._dispositions.get(turn_id)
            if (
                record is None
                or record.payload.get("event") != "message.completed"
                or disposition != {"ok": True, "applied": True}
            ):
                return None
            self._terminal_records.pop(turn_id, None)
            self._dispositions.pop(turn_id, None)
            return deepcopy(disposition)

    def take_terminal_record(self, turn_id: object) -> dict[str, object] | None:
        if not self._exact_nonblank(turn_id):
            return None
        with self._lock:
            self._expire_locked(self._now())
            record = self._terminal_records.pop(turn_id, None)
            self._dispositions.pop(turn_id, None)
            if record is None:
                return None
            return {
                "payload": deepcopy(record.payload),
                "response": deepcopy(record.response),
            }

    def drain_observers(self, timeout_seconds: float) -> None:
        with self._lock:
            coordinators = tuple(self._coordinators.values())
        for coordinator in coordinators:
            coordinator.drain_before_terminal(timeout_seconds)
        return None

    def close(self) -> None:
        with self._lock:
            coordinators = tuple(self._coordinators.values())
            self._turns.clear()
            self._coordinators.clear()
            self._answers.clear()
            self._terminal_records.clear()
            self._dispositions.clear()
            self._pending_approvals.clear()
            self._claimed_approvals.clear()
            self._subagents.clear()
            self._terminal_owners.clear()
            self._registry.clear()
        for coordinator in coordinators:
            coordinator.close()
        return None

    def _deliver_observer(self, event: ObserverEvent) -> None:
        try:
            self._post(event.payload, self._observer_timeout_seconds)
        except Exception:
            pass

    def _submit_tool(self, kwargs: dict[str, object], *, pending: bool) -> None:
        turn_id = kwargs.get("turn_id")
        tool_call_id = kwargs.get("tool_call_id")
        tool_name = kwargs.get("tool_name")
        if not all(self._exact_nonblank(value) for value in (turn_id, tool_call_id, tool_name)):
            return
        with self._lock:
            self._expire_locked(self._now())
            coordinator = self._card_active_coordinator_locked(turn_id)
        if coordinator is None:
            return
        status = "pending" if pending else self._tool_status(kwargs.get("status"))
        data: dict[str, object] = {
            "tool_id": tool_call_id,
            "name": self._preview(tool_name),
            "status": status,
        }
        duration = self._safe_duration(kwargs.get("duration_ms"))
        if not pending and duration is not None:
            data["duration_ms"] = duration
        phase = "started" if pending else "terminal"
        payload = self._observer_payload(
            turn_id,
            event="tool.updated",
            event_id=coordinator.event_id("tool", item_id=tool_call_id, phase=phase),
            phase=phase,
            data=data,
        )
        if payload is not None:
            coordinator.submit_observer(payload, producer="plugin")

    def _submit_subagent(
        self,
        coordinator: TurnEventCoordinator,
        turn_id: str,
        child_id: str,
        phase: str,
        data: dict[str, object],
    ) -> None:
        payload = self._observer_payload(
            turn_id,
            event="subagent.updated",
            event_id=coordinator.event_id("subagent", item_id=child_id, phase=phase),
            phase=phase,
            data=data,
        )
        if payload is not None:
            coordinator.submit_observer(payload, producer="plugin")

    def _observer_payload(
        self,
        turn_id: str,
        *,
        event: str,
        event_id: str,
        phase: str,
        data: dict[str, object],
    ) -> dict[str, object] | None:
        with self._lock:
            turn = self._turns.get(turn_id)
        if turn is None or not turn.accepts_observer_events:
            return None
        payload = self._base_payload(turn, sequence=0, created_at=self._now())
        payload.update(event=event, event_id=event_id, phase=phase, data=data)
        return payload

    def _approval_values(
        self, kwargs: dict[str, object]
    ) -> tuple[str, str, str, str, str] | None:
        session_key = kwargs.get("session_key")
        turn_id = kwargs.get("turn_id")
        tool_call_id = kwargs.get("tool_call_id")
        command = kwargs.get("command")
        surface = kwargs.get("surface")
        if not all(
            self._exact_nonblank(value)
            for value in (session_key, turn_id, tool_call_id, command, surface)
        ):
            return None
        if surface != "gateway":
            return None
        normalized = " ".join(command.split())
        fingerprint = sha256(normalized.encode("utf-8")).hexdigest()
        with self._lock:
            turn = self._turns.get(turn_id)
            valid_turn = bool(
                turn is not None
                and turn.accepts_observer_events
                and turn.ingress.gateway_session_key == session_key
            )
        if not valid_turn:
            return None
        return session_key, turn_id, tool_call_id, surface, fingerprint

    def _card_active_coordinator_locked(
        self, turn_id: str
    ) -> TurnEventCoordinator | None:
        turn = self._turns.get(turn_id)
        if turn is None or not turn.accepts_observer_events:
            return None
        return self._coordinators.get(turn_id)

    @staticmethod
    def _preview(value: object) -> str:
        if type(value) is not str:
            return ""
        return " ".join(value.split())[:240]

    @staticmethod
    def _safe_duration(value: object) -> int | float | None:
        if type(value) not in (int, float):
            return None
        try:
            if value < 0 or not isfinite(value):
                return None
        except (OverflowError, TypeError, ValueError):
            return None
        return value

    @staticmethod
    def _tool_status(value: object) -> str:
        if type(value) is not str:
            return "failed"
        return {
            "ok": "completed",
            "error": "failed",
            "blocked": "blocked",
            "timeout": "timeout",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(value.strip().lower(), "failed")

    @staticmethod
    def _subagent_status(value: object) -> str:
        if type(value) is not str:
            return "failed"
        return {
            "queued": "queued",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "interrupted": "interrupted",
            "timeout": "failed",
        }.get(value.strip().lower(), "failed")

    def _post_retry_unknown(
        self,
        payload: dict[str, object],
        timeout: float,
        is_explicit: Callable[[object], bool],
    ) -> object:
        result: object = None
        for _attempt in range(2):
            try:
                result = self._post(payload, timeout)
            except Exception:
                result = None
            if is_explicit(result):
                return result
        return result

    @classmethod
    def _is_exact_started_response(cls, value: object) -> bool:
        if type(value) is not dict or not all(type(key) is str for key in value):
            return False
        keys = set(value)
        if keys == {"ok", "applied"}:
            return value.get("ok") is True and value.get("applied") is True
        if keys == {"ok", "applied", "delivery"}:
            delivery = value["delivery"]
            return (
                value.get("ok") is True
                and value.get("applied") is True
                and type(delivery) is dict
                and all(type(key) is str for key in delivery)
                and set(delivery) == {"outcome"}
                and type(delivery["outcome"]) is str
                and delivery["outcome"] == "delivered"
            )
        if keys == {"ok", "applied", "disposition"}:
            return (
                value.get("ok") is True
                and value.get("applied") is False
                and type(value.get("disposition")) is str
                and value.get("disposition") == "native"
            )
        return False

    @classmethod
    def _valid_terminal_response(
        cls,
        value: object,
        *,
        now: float,
    ) -> dict[str, object] | None:
        if type(value) is not dict or not all(type(key) is str for key in value):
            return None
        keys = set(value)
        if keys == {"ok", "applied"}:
            if value.get("ok") is not True or value.get("applied") is not True:
                return None
            return deepcopy(value)
        if keys not in (
            {"ok", "applied", "disposition"},
            {"ok", "applied", "disposition", "native_handoff"},
        ):
            return None
        if (
            value.get("ok") is not True
            or value.get("applied") is not False
            or type(value.get("disposition")) is not str
            or value.get("disposition") != "native"
        ):
            return None
        if "native_handoff" in value:
            descriptor = value["native_handoff"]
            if not cls._valid_native_handoff_descriptor(descriptor, now=now):
                return None
        return deepcopy(value)

    @classmethod
    def _valid_native_handoff_descriptor(cls, value: object, *, now: float) -> bool:
        if type(value) is not dict or not all(type(key) is str for key in value):
            return False
        if set(value) != {"protocol", "id", "uuid_seed", "expires_at"}:
            return False
        protocol = value["protocol"]
        handoff_id = value["id"]
        uuid_seed = value["uuid_seed"]
        expires_at = value["expires_at"]
        if type(protocol) is not str or protocol != cls._NATIVE_HANDOFF_PROTOCOL:
            return False
        if type(handoff_id) is not str or cls._HANDOFF_ID_RE.fullmatch(handoff_id) is None:
            return False
        if type(uuid_seed) is not str or cls._UUID_SEED_RE.fullmatch(uuid_seed) is None:
            return False
        if type(expires_at) not in (int, float):
            return False
        try:
            return (
                isfinite(expires_at)
                and now < expires_at <= now + cls._NATIVE_HANDOFF_MAX_FUTURE_SECONDS
            )
        except (OverflowError, TypeError, ValueError):
            return False

    def _base_payload(
        self, turn: TurnBinding, *, sequence: int, created_at: float
    ) -> dict[str, object]:
        ingress = turn.ingress
        return {
            "schema_version": "1",
            "event": "",
            "conversation_id": ingress.thread_id or ingress.chat_id,
            "message_id": ingress.incoming_message_id,
            "chat_id": ingress.chat_id,
            "thread_id": ingress.thread_id,
            "platform": "feishu",
            "turn_id": turn.turn_id,
            "sequence": sequence,
            "created_at": created_at,
            "event_id": "",
            "producer": "plugin",
            "phase": "",
            "data": {},
        }

    def _cleanup_session(self, session_id: object) -> None:
        if not self._exact_nonblank(session_id):
            with self._lock:
                self._expire_locked(self._now())
            return
        with self._lock:
            self._expire_locked(self._now())
            self._registry.remove_session(session_id)
            turn_ids = [
                turn_id
                for turn_id, turn in self._turns.items()
                if turn.ingress.session_id == session_id
            ]
            coordinators = [self._coordinators.get(turn_id) for turn_id in turn_ids]
            for turn_id in turn_ids:
                turn = self._turns.get(turn_id)
                self._cleanup_turn_locked(turn_id, keep_disposition=False)
                if turn is not None:
                    turn.finish()
        for coordinator in coordinators:
            if coordinator is not None:
                coordinator.close()

    def _cleanup_turn_locked(
        self, turn_id: str, *, keep_disposition: bool
    ) -> TurnEventCoordinator | None:
        self._turns.pop(turn_id, None)
        coordinator = self._coordinators.pop(turn_id, None)
        self._answers.pop(turn_id, None)
        self._terminal_owners.pop(turn_id, None)
        for key in tuple(self._pending_approvals):
            if key[1] == turn_id:
                del self._pending_approvals[key]
                self._claimed_approvals.discard(key)
        for key in tuple(self._subagents):
            if key[0] == turn_id:
                del self._subagents[key]
        if not keep_disposition:
            self._terminal_records.pop(turn_id, None)
            self._dispositions.pop(turn_id, None)
        return coordinator

    def _expire_locked(self, now: float) -> None:
        for turn_id, answer in tuple(self._answers.items()):
            if answer.expires_at <= now:
                del self._answers[turn_id]
        for turn_id, record in tuple(self._terminal_records.items()):
            if record.expires_at <= now:
                del self._terminal_records[turn_id]
                self._dispositions.pop(turn_id, None)
        for key, pending in tuple(self._pending_approvals.items()):
            if pending.expires_at <= now:
                del self._pending_approvals[key]
                self._claimed_approvals.discard(key)
        for key, (_child_id, expires_at) in tuple(self._subagents.items()):
            if expires_at <= now:
                del self._subagents[key]

    @classmethod
    def _trim_locked(cls, mapping: OrderedDict) -> None:
        while len(mapping) > cls._MAX_ENTRIES:
            mapping.popitem(last=False)

    def _trim_pending_approvals_locked(self) -> None:
        while len(self._pending_approvals) > self._MAX_ENTRIES:
            key, _pending = self._pending_approvals.popitem(last=False)
            self._claimed_approvals.discard(key)

    def _make_turn_room_locked(self) -> tuple[bool, list[TurnEventCoordinator]]:
        evicted: list[TurnEventCoordinator] = []
        while len(self._turns) >= self._MAX_ENTRIES:
            victim = next(
                (
                    turn_id
                    for turn_id in self._turns
                    if turn_id not in self._terminal_owners
                ),
                None,
            )
            if victim is None:
                return False, evicted
            coordinator = self._cleanup_turn_locked(victim, keep_disposition=False)
            if coordinator is not None:
                evicted.append(coordinator)
        return True, evicted

    @staticmethod
    def _exact_nonblank(value: object) -> bool:
        return type(value) is str and bool(value.strip())

    @staticmethod
    def _classify_exit_reason(value: object, interrupted: bool) -> str:
        if interrupted:
            return "interrupted"
        if type(value) is not str:
            return "failed"
        text = value.strip().lower()
        if "timeout" in text or "timed_out" in text:
            return "timeout"
        if "budget" in text or "max_iterations" in text:
            return "budget_exhausted"
        if "error" in text or "failed" in text or "retries_exhausted" in text:
            return "runtime_error"
        return "failed"


_ACTIVE_RUNTIME: PluginRuntime | None = None
_ACTIVE_RUNTIME_LOCK = RLock()
_ingress_registry = IngressBindingRegistry()


def configure_plugin_runtime(runtime: PluginRuntime | None) -> None:
    global _ACTIVE_RUNTIME
    with _ACTIVE_RUNTIME_LOCK:
        old_runtime = _ACTIVE_RUNTIME
        _ACTIVE_RUNTIME = runtime
    if old_runtime is not None and old_runtime is not runtime:
        old_runtime.close()
    return None


def reset_plugin_runtime_state() -> None:
    configure_plugin_runtime(None)
    _ingress_registry.clear()
    return None


def _no_op(**kwargs: Any) -> None:
    return None


def _dispatch(method_name: str, **kwargs: Any) -> None:
    with _ACTIVE_RUNTIME_LOCK:
        runtime = _ACTIVE_RUNTIME
    if runtime is not None:
        getattr(runtime, method_name)(**kwargs)
    return None


def handle_pre_llm_call(**kwargs: Any) -> None:
    return _dispatch("handle_pre_llm_call", **kwargs)


def handle_post_llm_call(**kwargs: Any) -> None:
    return _dispatch("handle_post_llm_call", **kwargs)


def handle_on_session_end(**kwargs: Any) -> None:
    return _dispatch("handle_on_session_end", **kwargs)


def handle_on_session_reset(**kwargs: Any) -> None:
    return _dispatch("handle_on_session_reset", **kwargs)


def handle_on_session_finalize(**kwargs: Any) -> None:
    return _dispatch("handle_on_session_finalize", **kwargs)


def handle_pre_tool_call(**kwargs: Any) -> None:
    return _dispatch("handle_pre_tool_call", **kwargs)


def handle_post_tool_call(**kwargs: Any) -> None:
    return _dispatch("handle_post_tool_call", **kwargs)


def handle_pre_approval_request(**kwargs: Any) -> None:
    return _dispatch("handle_pre_approval_request", **kwargs)


def handle_post_approval_response(**kwargs: Any) -> None:
    return _dispatch("handle_post_approval_response", **kwargs)


def handle_subagent_start(**kwargs: Any) -> None:
    return _dispatch("handle_subagent_start", **kwargs)


def handle_subagent_stop(**kwargs: Any) -> None:
    return _dispatch("handle_subagent_stop", **kwargs)


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
    for name, handler_name in HOOK_HANDLERS.items():
        try:
            ctx.register_hook(name, _callback(handler_name))
        except Exception:
            continue
    return None
