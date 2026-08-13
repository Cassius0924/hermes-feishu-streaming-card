from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Thread, current_thread

import pytest

from hermes_feishu_card.hermes_plugin_runtime import (
    IngressBinding,
    IngressBindingRegistry,
    TurnEventCoordinator,
    TurnState,
    register_callbacks,
    reset_plugin_runtime_state,
)
from tests.fixtures.hermes_v020_plugin_api import PluginContext


def binding(
    generation="generation-a",
    *,
    profile_id="default",
    profile_source="fallback_default",
    session_id="session-1",
    gateway_session_key="gateway-session-1",
    expires_at=200.0,
):
    return IngressBinding(
        profile_id=profile_id,
        profile_source=profile_source,
        session_id=session_id,
        gateway_session_key=gateway_session_key,
        generation=generation,
        chat_id="oc_1",
        incoming_message_id="om_1",
        reply_to_message_id="om_1",
        thread_id="",
        expires_at=expires_at,
    )


class AcceptedDict(dict):
    pass


class PretendsDelivered:
    def __eq__(self, other):
        return other == "delivered"


class PretendsKey:
    def __init__(self, target):
        self.target = target

    def __hash__(self):
        return hash(self.target)

    def __eq__(self, other):
        return other == self.target


class StringSubclass(str):
    pass


class FloatSubclass(float):
    pass


class IntSubclass(int):
    pass


def test_new_generation_replaces_old_ingress_binding():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding("generation-a")) is True
    assert registry.bind(binding("generation-b")) is True
    assert registry.claim("default", "session-1", "generation-a", "turn-old") is None
    turn = registry.claim("default", "session-1", "generation-b", "turn-new")
    assert turn is not None
    assert turn.turn_id == "turn-new"


def test_claim_requires_exact_unique_binding_and_never_uses_recent_chat():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.claim("default", "missing", "generation-a", "turn-1") is None
    assert registry.bind(binding()) is True
    assert registry.claim("default", "session-1", "other", "turn-1") is None
    assert registry.claim("default", "session-1", "generation-a", "") is None
    assert registry.claim("default", "session-1", "generation-a", "turn-1") is not None
    assert registry.claim("default", "session-1", "generation-a", "turn-2") is None


def test_official_pre_llm_claims_only_one_unambiguous_agent_session():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    turn = registry.claim_unique_session("session-1", "turn-1")
    assert turn is not None
    assert turn.turn_id == "turn-1"
    assert turn.ingress.gateway_session_key == "gateway-session-1"
    assert turn.ingress.profile_source == "fallback_default"
    assert registry.claim_unique_session("session-1", "turn-2") is None


@pytest.mark.parametrize("profile_source", ("env", "locals", "hermes_home", "fallback_default"))
def test_bind_and_unique_claim_preserve_allowed_profile_source(profile_source):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding(profile_source=profile_source)) is True
    turn = registry.claim_unique_session("session-1", "turn-1")
    assert turn is not None
    assert turn.ingress.profile_source == profile_source


def test_official_pre_llm_refuses_ambiguity_without_consuming_either_binding():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding(profile_id="profile-a")) is True
    assert registry.bind(binding(profile_id="profile-b")) is True
    assert registry.claim_unique_session("session-1", "turn-1") is None
    assert registry.claim("profile-a", "session-1", "generation-a", "turn-a") is not None
    assert registry.claim("profile-b", "session-1", "generation-a", "turn-b") is not None


def test_expiry_resolves_ambiguous_agent_session_before_unique_claim():
    now = [100.0]
    registry = IngressBindingRegistry(now=lambda: now[0])
    assert registry.bind(binding(profile_id="profile-a", expires_at=101.0)) is True
    assert registry.bind(binding(profile_id="profile-b", expires_at=200.0)) is True
    assert registry.claim_unique_session("session-1", "turn-early") is None
    now[0] = 101.0
    turn = registry.claim_unique_session("session-1", "turn-after-expiry")
    assert turn is not None
    assert turn.ingress.profile_id == "profile-b"


def test_concurrent_unique_session_claims_consume_exactly_once():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    barrier = Barrier(16)

    def claim(index):
        barrier.wait()
        return registry.claim_unique_session("session-1", f"turn-{index}")

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(claim, range(16)))

    claimed = [turn for turn in results if turn is not None]
    assert len(claimed) == 1
    assert claimed[0].turn_id.startswith("turn-")


@pytest.mark.parametrize(
    ("session_id", "turn_id"),
    (
        ("", "turn-1"),
        ("  ", "turn-1"),
        (StringSubclass("session-1"), "turn-1"),
        (1, "turn-1"),
        ("session-1", ""),
        ("session-1", StringSubclass("turn-1")),
        ("session-1", None),
    ),
)
def test_unique_session_claim_rejects_nonordinary_or_blank_identities_without_consuming(
    session_id, turn_id
):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    assert registry.claim_unique_session(session_id, turn_id) is None
    assert registry.claim_unique_session("session-1", "turn-valid") is not None


@pytest.mark.parametrize("invalid_value", ("", StringSubclass("session-1"), None))
def test_invalid_unique_session_claim_still_prunes_expired_bindings(invalid_value):
    now = [100.0]
    registry = IngressBindingRegistry(now=lambda: now[0])
    assert registry.bind(binding(expires_at=101.0)) is True
    now[0] = 101.0
    assert registry.claim_unique_session(invalid_value, "turn-1") is None
    now[0] = 100.0
    assert registry.claim_unique_session("session-1", "turn-valid") is None


def test_started_transitions_only_on_explicit_sidecar_acceptance():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    registry.bind(binding())
    turn = registry.claim("default", "session-1", "generation-a", "turn-1")
    assert turn.state is TurnState.PENDING_START
    assert turn.record_started_result({"ok": True, "applied": False}) is TurnState.NATIVE_BYPASS
    assert turn.record_started_result({"ok": True, "applied": True}) is TurnState.NATIVE_BYPASS


def test_started_requires_boolean_true_not_integer_values():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    for result in (
        {"ok": 1, "applied": True},
        {"ok": True, "applied": 1},
    ):
        registry.bind(binding())
        turn = registry.claim("default", "session-1", "generation-a", "turn-1")
        assert turn.record_started_result(result) is TurnState.NATIVE_BYPASS


def test_started_accepts_exact_delivered_sidecar_response():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    turn = registry.claim("default", "session-1", "generation-a", "turn-1")
    result = {
        "ok": True,
        "applied": True,
        "delivery": {"outcome": "delivered"},
    }
    assert turn.record_started_result(result) is TurnState.CARD_ACTIVE


@pytest.mark.parametrize(
    "result",
    (
        AcceptedDict(ok=True, applied=True),
        AcceptedDict(
            ok=True,
            applied=True,
            delivery={"outcome": "delivered"},
        ),
        {
            "ok": True,
            "applied": True,
            "delivery": AcceptedDict(outcome="delivered"),
        },
        {
            "ok": True,
            "applied": True,
            "delivery": {"outcome": "delivered", "extra": False},
        },
        {"ok": True, "applied": True, "delivery": {}},
        {"ok": True, "applied": True, "delivery": {"outcome": 1}},
        {
            "ok": True,
            "applied": True,
            "delivery": {"outcome": "unknown"},
        },
        {
            "ok": True,
            "applied": True,
            "delivery": {"outcome": PretendsDelivered()},
        },
        {PretendsKey("ok"): True, "applied": True},
        {"ok": True, PretendsKey("applied"): True},
        {
            "ok": True,
            "applied": True,
            PretendsKey("delivery"): {"outcome": "delivered"},
        },
        {
            "ok": True,
            "applied": True,
            "delivery": {PretendsKey("outcome"): "delivered"},
        },
        {"ok": True, "applied": True, "extra": False},
        {
            "ok": True,
            "applied": True,
            "delivery": {"outcome": "delivered"},
            "extra": False,
        },
    ),
    ids=(
        "top-level-dict-subclass-two-key",
        "top-level-dict-subclass-with-delivery",
        "delivery-dict-subclass",
        "delivery-extra-key",
        "delivery-missing-outcome",
        "delivery-non-string-outcome",
        "delivery-non-delivered-outcome",
        "delivery-equality-spoofed-outcome",
        "top-level-spoofed-ok-key",
        "top-level-spoofed-applied-key",
        "top-level-spoofed-delivery-key",
        "delivery-spoofed-outcome-key",
        "top-level-unknown-key",
        "top-level-unknown-key-with-delivery",
    ),
)
def test_started_rejects_non_allowlisted_sidecar_responses(result):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    turn = registry.claim("default", "session-1", "generation-a", "turn-1")
    assert turn.record_started_result(result) is TurnState.NATIVE_BYPASS


def test_card_active_turn_becomes_terminal_once_and_rejects_late_events():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    registry.bind(binding())
    turn = registry.claim("default", "session-1", "generation-a", "turn-1")
    assert turn.record_started_result({"ok": True, "applied": True}) is TurnState.CARD_ACTIVE
    assert turn.finish() is True
    assert turn.finish() is False
    assert turn.state is TurnState.TERMINAL
    assert turn.accepts_observer_events is False
    assert turn.record_started_result({"ok": True, "applied": True}) is TurnState.TERMINAL


def test_finish_is_atomic_when_many_threads_finish_one_active_turn():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    turn = registry.claim("default", "session-1", "generation-a", "turn-1")
    assert turn.record_started_result({"ok": True, "applied": True}) is TurnState.CARD_ACTIVE
    barrier = Barrier(16)

    def finish_at_once():
        barrier.wait()
        return turn.finish()

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda _: finish_at_once(), range(16)))

    assert results.count(True) == 1
    assert turn.state is TurnState.TERMINAL


def test_started_result_racing_finish_cannot_reopen_terminal():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    turn = registry.claim("default", "session-1", "generation-a", "turn-1")
    barrier = Barrier(2)

    def record_started():
        barrier.wait()
        return turn.record_started_result({"ok": True, "applied": True})

    def finish():
        barrier.wait()
        return turn.finish()

    with ThreadPoolExecutor(max_workers=2) as executor:
        started = executor.submit(record_started)
        finished = executor.submit(finish)
        started.result()
        assert finished.result() is True

    assert turn.state is TurnState.TERMINAL


def test_bind_rejects_blank_identity_and_expired_bindings():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding(profile_id="")) is False
    assert registry.bind(binding(generation="  ")) is False
    assert registry.bind(binding(expires_at=100.0)) is False
    assert registry.claim("default", "session-1", "generation-a", "turn-1") is None


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("profile_id", ""),
        ("session_id", "  "),
        ("gateway_session_key", ""),
        ("generation", StringSubclass("generation-a")),
        ("chat_id", StringSubclass("oc_1")),
        ("incoming_message_id", 1),
        ("reply_to_message_id", None),
        ("thread_id", StringSubclass("")),
    ),
)
def test_bind_rejects_blank_nonstring_or_string_subclass_identity_fields(
    field_name, invalid_value
):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(replace(binding(), **{field_name: invalid_value})) is False


@pytest.mark.parametrize(
    "profile_source",
    (
        "",
        "unknown",
        StringSubclass("env"),
        "sanitized_env",
        "sanitized_locals",
        "sanitized_hermes_home",
        "sanitized_fallback_default",
    ),
)
def test_bind_rejects_unverified_profile_sources_without_consuming_valid_binding(
    profile_source
):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    assert registry.bind(
        binding(generation="generation-b", profile_source=profile_source)
    ) is False
    turn = registry.claim_unique_session("session-1", "turn-1")
    assert turn is not None
    assert turn.ingress.profile_source == "fallback_default"


def test_invalid_profile_source_bind_still_prunes_expired_bindings():
    now = [100.0]
    registry = IngressBindingRegistry(now=lambda: now[0])
    assert registry.bind(binding(expires_at=101.0)) is True
    now[0] = 101.0
    assert registry.bind(binding(profile_source="sanitized_env")) is False
    now[0] = 100.0
    assert registry.claim_unique_session("session-1", "turn-1") is None


@pytest.mark.parametrize("expires_at", (True, float("nan"), float("inf"), -float("inf"), "200", None))
def test_bind_rejects_non_numeric_or_nonfinite_expiry(expires_at):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding(expires_at=expires_at)) is False


@pytest.mark.parametrize(
    "expires_at",
    (FloatSubclass(200.0), IntSubclass(200), 10**10000),
    ids=("float-subclass", "int-subclass", "huge-int"),
)
def test_bind_rejects_numeric_subclasses_and_overflowing_expiry(expires_at):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding(expires_at=expires_at)) is False


def test_invalid_bind_still_prunes_expired_bindings():
    now = [100.0]
    registry = IngressBindingRegistry(now=lambda: now[0])
    assert registry.bind(binding(expires_at=101.0)) is True
    now[0] = 101.0
    assert registry.bind(binding(gateway_session_key="")) is False
    now[0] = 100.0
    assert registry.claim("default", "session-1", "generation-a", "turn-1") is None


def test_bind_rejects_ingress_binding_subclasses():
    class DerivedIngressBinding(IngressBinding):
        pass

    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(DerivedIngressBinding(**binding().__dict__)) is False


@pytest.mark.parametrize(
    ("profile_id", "session_id", "generation", "turn_id"),
    (
        (StringSubclass("default"), "session-1", "generation-a", "turn-1"),
        ("default", StringSubclass("session-1"), "generation-a", "turn-1"),
        ("default", "session-1", StringSubclass("generation-a"), "turn-1"),
        ("default", "session-1", "generation-a", StringSubclass("turn-1")),
    ),
)
def test_explicit_claim_rejects_string_subclasses_without_consuming(
    profile_id, session_id, generation, turn_id
):
    registry = IngressBindingRegistry(now=lambda: 100.0)
    assert registry.bind(binding()) is True
    assert registry.claim(profile_id, session_id, generation, turn_id) is None
    assert registry.claim("default", "session-1", "generation-a", "turn-valid") is not None


def test_invalid_explicit_claim_still_prunes_expired_bindings():
    now = [100.0]
    registry = IngressBindingRegistry(now=lambda: now[0])
    assert registry.bind(binding(expires_at=101.0)) is True
    now[0] = 101.0
    assert registry.claim("", "session-1", "generation-a", "turn-1") is None
    now[0] = 100.0
    assert registry.claim("default", "session-1", "generation-a", "turn-valid") is None


def test_expired_binding_is_pruned_before_claim():
    now = [100.0]
    registry = IngressBindingRegistry(now=lambda: now[0])
    assert registry.bind(binding(expires_at=101.0)) is True
    now[0] = 101.0
    assert registry.claim("default", "session-1", "generation-a", "turn-1") is None


def test_registry_evicts_oldest_binding_when_capacity_is_exceeded():
    registry = IngressBindingRegistry(now=lambda: 100.0)
    for index in range(1025):
        assert registry.bind(binding(profile_id=f"profile-{index}")) is True
    assert registry.claim("profile-0", "session-1", "generation-a", "turn-0") is None
    assert registry.claim("profile-1", "session-1", "generation-a", "turn-1") is not None
    assert registry.claim("profile-1024", "session-1", "generation-a", "turn-last") is not None


def test_reset_clears_module_state_without_breaking_callback_registration():
    reset_plugin_runtime_state()
    context = PluginContext()
    assert register_callbacks(context) is None
    assert set(context.registered) == {
        "pre_llm_call", "post_llm_call", "on_session_end",
        "on_session_reset", "on_session_finalize", "pre_tool_call",
        "post_tool_call", "pre_approval_request", "post_approval_response",
        "subagent_start", "subagent_stop",
    }
    assert reset_plugin_runtime_state() is None


def test_event_ids_match_the_public_deterministic_contract():
    coordinator = TurnEventCoordinator("turn-1", max_pending=2)
    assert coordinator.event_id("started") == "turn:turn-1:started"
    assert coordinator.event_id("tool", item_id="call-1", phase="started") == "tool:turn-1:call-1:started"
    assert coordinator.event_id("approval", item_id="fp-1", phase="terminal") == "approval:turn-1:fp-1:terminal"
    assert coordinator.event_id("subagent", item_id="child-1", phase="started") == "subagent:turn-1:child-1:started"
    assert coordinator.event_id("completed") == "turn:turn-1:completed"
    assert coordinator.event_id("failed") == "turn:turn-1:failed"


def test_event_identity_rejects_blank_or_invalid_public_values():
    import pytest

    with pytest.raises(ValueError, match="turn"):
        TurnEventCoordinator("  ")
    coordinator = TurnEventCoordinator("turn-1")
    with pytest.raises(ValueError, match="identity"):
        coordinator.event_id("unknown")
    with pytest.raises(ValueError, match="identity"):
        coordinator.event_id("tool", phase="started")
    with pytest.raises(ValueError, match="phase"):
        coordinator.event_id("tool", item_id="call-1", phase="updated")


def test_plugin_and_patch_share_one_monotonic_sequence():
    coordinator = TurnEventCoordinator("turn-1", max_pending=4)
    assert coordinator.next_sequence("plugin") == 0
    assert coordinator.next_sequence("patch") == 1
    assert coordinator.next_sequence("plugin") == 2


def test_unknown_producer_is_rejected_at_sequence_and_submission_boundaries():
    import pytest

    coordinator = TurnEventCoordinator("turn-1")
    with pytest.raises(ValueError, match="producer"):
        coordinator.next_sequence("unknown")
    with pytest.raises(ValueError, match="producer"):
        coordinator.submit_observer({"event": "tool.updated"}, producer="unknown")


def test_nonpositive_max_pending_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="max_pending"):
        TurnEventCoordinator("turn-1", max_pending=0)
    with pytest.raises(ValueError, match="max_pending"):
        TurnEventCoordinator("turn-1", max_pending=-1)


def test_terminal_barrier_rejects_late_items_and_terminal_is_after_accepted_items():
    coordinator = TurnEventCoordinator("turn-1", max_pending=4, start_worker=False)
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is True
    assert coordinator.submit_observer({"event": "subagent.updated"}, producer="plugin") is True
    barrier = coordinator.close_terminal_barrier()
    assert barrier == 1
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is False
    assert coordinator.next_terminal_sequence() == 2


def test_terminal_sequence_is_reused_without_advancing_shared_sequence_on_retry():
    coordinator = TurnEventCoordinator("turn-1", start_worker=False)
    coordinator.close_terminal_barrier()
    assert coordinator.next_terminal_sequence() == 0
    assert coordinator.next_terminal_sequence() == 0
    assert coordinator.next_sequence("patch") == 1


def test_concurrent_terminal_sequence_retries_all_reuse_one_value():
    coordinator = TurnEventCoordinator("turn-1", start_worker=False)
    coordinator.close_terminal_barrier()
    start = Barrier(8)

    def get_terminal_sequence():
        start.wait()
        return coordinator.next_terminal_sequence()

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(lambda _: get_terminal_sequence(), range(8)))

    assert sequences == [0] * 8
    assert coordinator.next_sequence("plugin") == 1


def test_queue_full_drops_observer_work_without_raising_and_can_leave_sequence_gap():
    coordinator = TurnEventCoordinator("turn-1", max_pending=1, start_worker=False)
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is True
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is False
    assert coordinator.next_sequence("patch") == 2


def test_worker_delivery_exception_always_marks_work_done_and_drain_returns():
    delivery_started = Event()

    def fail_delivery(_event):
        delivery_started.set()
        raise RuntimeError("delivery failed")

    coordinator = TurnEventCoordinator("turn-1", deliver=fail_delivery)
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is True
    assert delivery_started.wait(timeout=0.5)
    coordinator.drain_before_terminal(timeout_seconds=0.5)
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is False


def test_drain_timeout_closes_admission_without_waiting_for_an_unstarted_worker():
    coordinator = TurnEventCoordinator("turn-1", start_worker=False)
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is True
    coordinator.drain_before_terminal(timeout_seconds=0)
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is False


def test_drain_race_never_returns_after_accepting_undrained_observer_work():
    import queue

    class DrainGateQueue(queue.Queue):
        def __init__(self):
            super().__init__(maxsize=1)
            self.drain_checked_empty = Event()
            self.release_drain_check = Event()
            self._first_drain_check = True

        @property
        def unfinished_tasks(self):
            if current_thread().name == "drainer" and self._first_drain_check:
                self._first_drain_check = False
                self.drain_checked_empty.set()
                assert self.release_drain_check.wait(timeout=0.5)
            return self._unfinished_tasks

        @unfinished_tasks.setter
        def unfinished_tasks(self, value):
            self._unfinished_tasks = value

    coordinator = TurnEventCoordinator("turn-1", max_pending=1, start_worker=False)
    gate = DrainGateQueue()
    coordinator._queue = gate
    drain_returned = Event()
    submit_finished = Event()
    result = []

    def drain():
        coordinator.drain_before_terminal(timeout_seconds=0)
        drain_returned.set()

    def submit():
        result.append(coordinator.submit_observer({"event": "tool.updated"}, producer="plugin"))
        submit_finished.set()

    drainer = Thread(target=drain, name="drainer")
    drainer.start()
    assert gate.drain_checked_empty.wait(timeout=0.5)
    submitter = Thread(target=submit, name="submitter")
    submitter.start()
    accepted_before_drain_close = submit_finished.wait(timeout=0.5)
    gate.release_drain_check.set()
    drainer.join(timeout=0.5)
    submitter.join(timeout=0.5)
    assert drain_returned.is_set()
    assert submit_finished.is_set()
    assert not (accepted_before_drain_close and gate.unfinished_tasks)
    assert result == [False]


def test_concurrent_barrier_and_submit_never_accept_an_event_after_barrier():
    coordinator = TurnEventCoordinator("turn-1", max_pending=4, start_worker=False)
    start = Barrier(2)

    def submit():
        start.wait()
        return coordinator.submit_observer({"event": "tool.updated"}, producer="plugin")

    def close_barrier():
        start.wait()
        return coordinator.close_terminal_barrier()

    with ThreadPoolExecutor(max_workers=2) as executor:
        submit_future = executor.submit(submit)
        barrier_future = executor.submit(close_barrier)
        accepted = submit_future.result()
        barrier = barrier_future.result()

    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is False
    assert (barrier == 0) is accepted


def test_close_is_idempotent_and_does_not_wait_for_a_blocked_daemon_delivery():
    delivery_started = Event()
    release_delivery = Event()

    def block_delivery(_event):
        delivery_started.set()
        assert release_delivery.wait(timeout=0.5)

    coordinator = TurnEventCoordinator("turn-1", deliver=block_delivery)
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is True
    assert delivery_started.wait(timeout=0.5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(coordinator.close).result(timeout=0.25) is None
    assert coordinator.close() is None
    assert coordinator.submit_observer({"event": "tool.updated"}, producer="plugin") is False
    release_delivery.set()
    coordinator.drain_before_terminal(timeout_seconds=0.5)
