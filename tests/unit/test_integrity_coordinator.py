from __future__ import annotations

from types import SimpleNamespace

from hermes_feishu_card.integrity import RuntimeIntegrityCoordinator


class FakeSupervisor:
    def __init__(self, reason="runtime_heartbeat_missing"):
        self.reason = reason
        self.restart_required = 0
        self.manual_review_required = 0

    def snapshot(self):
        return {"status": "degraded", "reason": self.reason}

    def mark_restart_required(self):
        self.restart_required += 1

    def mark_manual_review_required(self):
        self.manual_review_required += 1


def _plan(*, executable=True, fingerprint="evidence-1", state="stale_unpatched"):
    return SimpleNamespace(
        executable=executable,
        fingerprint=fingerprint,
        state=state,
        reason="verified_git_upgrade" if executable else "git_target_modified",
    )


def test_notify_mode_reports_verified_repair_without_mutating():
    supervisor = FakeSupervisor()
    executed = []
    coordinator = RuntimeIntegrityCoordinator(
        mode="notify",
        hermes_root="/sanitized-in-test",
        supervisor=supervisor,
        detector=lambda _root: object(),
        planner=lambda _detection: _plan(),
        executor=lambda *_args, **_kwargs: executed.append(True),
    )

    result = coordinator.check_once()

    assert result == {
        "status": "repair_available",
        "reason": "verified_git_upgrade",
        "attempted": False,
    }
    assert executed == []
    assert supervisor.restart_required == 0


def test_safe_mode_executes_once_per_evidence_and_never_restarts_gateway():
    supervisor = FakeSupervisor()
    executed = []

    def execute(_detection, *, expected_fingerprint):
        executed.append(expected_fingerprint)
        return SimpleNamespace(status="repaired", restart_required=True)

    coordinator = RuntimeIntegrityCoordinator(
        mode="safe",
        hermes_root="/sanitized-in-test",
        supervisor=supervisor,
        detector=lambda _root: object(),
        planner=lambda _detection: _plan(),
        executor=execute,
    )

    first = coordinator.check_once()
    second = coordinator.check_once()

    assert first["status"] == "repaired"
    assert second["status"] == "deduplicated"
    assert executed == ["evidence-1"]
    assert supervisor.restart_required == 1
    assert coordinator.snapshot()["repair_attempts"] == 1
    assert coordinator.snapshot()["repair_successes"] == 1


def test_ambiguous_stale_state_requires_manual_review_without_mutating():
    supervisor = FakeSupervisor()
    coordinator = RuntimeIntegrityCoordinator(
        mode="safe",
        hermes_root="/sanitized-in-test",
        supervisor=supervisor,
        detector=lambda _root: object(),
        planner=lambda _detection: _plan(executable=False),
        executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not execute")
        ),
    )

    result = coordinator.check_once()

    assert result["status"] == "manual_review_required"
    assert supervisor.manual_review_required == 1
    assert coordinator.snapshot()["repair_refusals"] == 1


def test_ambiguous_evidence_is_deduplicated_instead_of_spamming_refusals():
    supervisor = FakeSupervisor()
    coordinator = RuntimeIntegrityCoordinator(
        mode="safe",
        hermes_root="/sanitized-in-test",
        supervisor=supervisor,
        detector=lambda _root: object(),
        planner=lambda _detection: _plan(
            executable=False,
            fingerprint="ambiguous-evidence",
        ),
    )

    first = coordinator.check_once()
    second = coordinator.check_once()

    assert first["status"] == "manual_review_required"
    assert second["status"] == "manual_review_required"
    assert coordinator.snapshot()["repair_refusals"] == 1
    assert supervisor.manual_review_required == 1


def test_ready_runtime_and_off_mode_do_not_inspect_or_mutate_source():
    calls = []
    for mode, reason in (("safe", "runtime_ready"), ("off", "runtime_heartbeat_missing")):
        coordinator = RuntimeIntegrityCoordinator(
            mode=mode,
            hermes_root="/sanitized-in-test",
            supervisor=FakeSupervisor(reason=reason),
            detector=lambda _root: calls.append(True),
        )
        assert coordinator.check_once()["status"] in {"ready", "disabled"}

    assert calls == []
