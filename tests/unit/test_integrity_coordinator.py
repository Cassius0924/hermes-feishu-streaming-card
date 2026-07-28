from __future__ import annotations

from types import SimpleNamespace

from hermes_feishu_card.integrity import (
    RuntimeIntegrityCoordinator,
    build_runtime_integrity_fence_binding,
    sanitize_integrity_snapshot,
)
from hermes_feishu_card.runtime_control import (
    RUNTIME_HOOK_GENERATION,
    RuntimeControlEvent,
    RuntimeIntegritySupervisor,
)


class FakeSupervisor:
    def __init__(self, reason="runtime_heartbeat_missing"):
        self.reason = reason
        self.restart_required = 0
        self.manual_review_required = 0
        self.bindings = []

    def snapshot(self):
        return {"status": "degraded", "reason": self.reason}

    def mark_restart_required(self, *, binding=None):
        self.restart_required += 1
        self.bindings.append(binding)

    def mark_manual_review_required(self, *, binding=None):
        self.manual_review_required += 1
        self.bindings.append(binding)


def _plan(*, executable=True, fingerprint="evidence-1", state="stale_unpatched"):
    return SimpleNamespace(
        executable=executable,
        fingerprint=fingerprint,
        state=state,
        reason="verified_git_upgrade" if executable else "git_target_modified",
    )


def test_fence_binding_is_domain_separated_and_contains_no_target_path(tmp_path):
    hermes_root = tmp_path / "private-hermes-target"
    hermes_root.mkdir()

    first = build_runtime_integrity_fence_binding(hermes_root, "a" * 64)
    second = build_runtime_integrity_fence_binding(hermes_root, "b" * 64)

    assert len(first.target_identity) == 64
    assert len(first.plan_fingerprint) == 64
    assert first.target_identity != first.plan_fingerprint
    assert first.target_identity == second.target_identity
    assert first.plan_fingerprint != second.plan_fingerprint
    assert str(hermes_root) not in repr(first)


def test_coordinator_binds_new_fence_to_target_and_exact_plan(tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    supervisor = FakeSupervisor()
    plan = _plan(executable=False, fingerprint="c" * 64)
    coordinator = RuntimeIntegrityCoordinator(
        mode="safe",
        hermes_root=hermes_root,
        supervisor=supervisor,
        detector=lambda _root: object(),
        planner=lambda _detection: plan,
    )

    result = coordinator.check_once()

    assert result["status"] == "manual_review_required"
    assert supervisor.bindings == [
        build_runtime_integrity_fence_binding(hermes_root, plan.fingerprint)
    ]


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


def test_installed_plan_waits_for_first_heartbeat_without_persisting_fence(
    tmp_path,
):
    for grace_seconds, current_time, readiness_reason in (
        (30.0, 0.0, "runtime_heartbeat_waiting"),
        (0.0, 1.0, "runtime_heartbeat_missing"),
    ):
        clock = [0.0]
        state_root = tmp_path / readiness_reason
        supervisor = RuntimeIntegritySupervisor(
            mode="safe",
            expected_package_version="4.1.1",
            now=lambda: clock[0],
            startup_grace_seconds=grace_seconds,
            state_directory=state_root,
        )
        clock[0] = current_time
        assert supervisor.snapshot()["reason"] == readiness_reason
        coordinator = RuntimeIntegrityCoordinator(
            mode="safe",
            hermes_root="/sanitized-in-test",
            supervisor=supervisor,
            detector=lambda _root: object(),
            planner=lambda _detection: _plan(
                executable=False,
                state="installed",
            ),
            executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("must not execute")
            ),
        )

        result = coordinator.check_once()

        assert result == {
            "status": "idle",
            "reason": "recovery_not_required",
            "attempted": False,
        }
        readiness = supervisor.snapshot()
        assert readiness["reason"] == readiness_reason
        assert readiness["restart_required"] is False
        assert not (state_root / "runtime-integrity-fence.json").exists()

        assert supervisor.record(
            RuntimeControlEvent.from_dict(
                {
                    "schema_version": "1",
                    "event": "runtime.hello",
                    "runtime_id": f"runtime-after-{readiness_reason}",
                    "sequence": 1,
                    "created_at": clock[0],
                    "hook_generation": RUNTIME_HOOK_GENERATION,
                    "package_version": "4.1.1",
                }
            )
        )
        assert coordinator.check_once() == {
            "status": "ready",
            "reason": "runtime_ready",
            "attempted": False,
        }
        assert not (state_root / "runtime-integrity-fence.json").exists()


def test_missing_control_auth_never_triggers_source_inspection_or_repair():
    calls = []
    coordinator = RuntimeIntegrityCoordinator(
        mode="safe",
        hermes_root="/sanitized-in-test",
        supervisor=FakeSupervisor(reason="control_auth_unavailable"),
        detector=lambda _root: calls.append("detect"),
        executor=lambda *_args, **_kwargs: calls.append("execute"),
    )

    result = coordinator.check_once()

    assert result == {
        "status": "manual_review_required",
        "reason": "control_auth_unavailable",
        "attempted": False,
    }
    assert calls == []


def test_integrity_snapshot_sanitizer_allows_only_bounded_operator_fields():
    sanitized = sanitize_integrity_snapshot(
        {
            "mode": "notify",
            "last_status": "repair_available",
            "last_reason": "verified_git_upgrade",
            "repair_attempts": 2,
            "repair_successes": 1,
            "repair_refusals": 3,
            "private_path": "/private/secret",
        }
    )

    assert sanitized == {
        "mode": "notify",
        "last_status": "repair_available",
        "last_reason": "verified_git_upgrade",
        "repair_attempts": 2,
        "repair_successes": 1,
        "repair_refusals": 3,
    }
    assert sanitize_integrity_snapshot(
        {
            "mode": "unsafe",
            "last_status": "/private/secret",
            "last_reason": "token=secret",
            "repair_attempts": -1,
        }
    ) == {
        "mode": "unknown",
        "last_status": "unknown",
        "last_reason": "unknown",
        "repair_attempts": 0,
        "repair_successes": 0,
        "repair_refusals": 0,
    }
