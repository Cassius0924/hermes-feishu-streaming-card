from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from hermes_feishu_card.hermes_plugin_runtime import (
    IngressBinding,
    IngressBindingRegistry,
    TurnEventCoordinator,
    TurnState,
    register_callbacks,
    reset_plugin_runtime_state,
)
from tests.fixtures.hermes_v020_plugin_api import PluginContext


def binding(generation="generation-a", *, expires_at=200.0, profile_id="default"):
    return IngressBinding(
        profile_id=profile_id,
        session_id="session-1",
        generation=generation,
        chat_id="oc_1",
        incoming_message_id="om_1",
        reply_to_message_id="om_1",
        thread_id="",
        expires_at=expires_at,
    )


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


def test_started_rejects_dict_subclasses_and_extra_fields():
    class AcceptedDict(dict):
        pass

    registry = IngressBindingRegistry(now=lambda: 100.0)
    for result in (
        AcceptedDict(ok=True, applied=True),
        {"ok": True, "applied": True, "extra": False},
    ):
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
    context = PluginContext(valid_hooks={"pre_llm_call"})
    assert register_callbacks(context) is None
    assert set(context.registered) == {"pre_llm_call"}
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
