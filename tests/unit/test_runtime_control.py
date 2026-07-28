from __future__ import annotations

import json
import os

import pytest

from hermes_feishu_card import hook_runtime
from hermes_feishu_card import runtime_control
from hermes_feishu_card.event_auth import sign_event_request
from hermes_feishu_card.runtime_control import (
    RUNTIME_HOOK_GENERATION,
    RuntimeControlEmitter,
    RuntimeControlEvent,
    RuntimeControlValidationError,
    RuntimeIntegritySupervisor,
    RuntimeProofVerifier,
    runtime_events_url,
    sign_runtime_request,
)


def _payload(**changes):
    payload = {
        "schema_version": "1",
        "event": "runtime.hello",
        "runtime_id": "runtime-1234567890",
        "sequence": 1,
        "created_at": 100.0,
        "hook_generation": RUNTIME_HOOK_GENERATION,
        "package_version": "4.1.0",
    }
    payload.update(changes)
    return payload


def test_runtime_control_event_accepts_only_bounded_safe_fields():
    event = RuntimeControlEvent.from_dict(_payload())

    assert event.event == "runtime.hello"
    assert event.sequence == 1

    for changes in (
        {"event": "message.completed"},
        {"sequence": -1},
        {"runtime_id": "short"},
        {"package_version": "v" * 129},
        {"local_path": "/private/secret"},
    ):
        with pytest.raises(RuntimeControlValidationError):
            RuntimeControlEvent.from_dict(_payload(**changes))


def test_runtime_proof_binds_body_rejects_replay_and_is_domain_separated():
    secret = b"r" * 32
    body = json.dumps(_payload(), sort_keys=True).encode()
    headers = sign_runtime_request(
        secret,
        body,
        timestamp=100,
        nonce="nonce-1234567890",
    )
    verifier = RuntimeProofVerifier(secret, now=lambda: 100.0)

    verifier.verify(headers, body)
    with pytest.raises(RuntimeControlValidationError, match="replayed"):
        verifier.verify(headers, body)

    event_headers = sign_event_request(
        secret,
        body,
        timestamp=100,
        nonce="event-nonce-123456",
    )
    with pytest.raises(RuntimeControlValidationError, match="invalid"):
        RuntimeProofVerifier(secret, now=lambda: 100.0).verify(event_headers, body)


def test_runtime_proof_rejects_expired_and_wrong_body():
    secret = b"r" * 32
    body = b'{}'
    headers = sign_runtime_request(
        secret,
        body,
        timestamp=100,
        nonce="nonce-1234567890",
    )

    with pytest.raises(RuntimeControlValidationError, match="expired"):
        RuntimeProofVerifier(secret, now=lambda: 106.0).verify(headers, body)
    with pytest.raises(RuntimeControlValidationError, match="invalid"):
        RuntimeProofVerifier(secret, now=lambda: 100.0).verify(headers, b'{"x":1}')


def test_runtime_events_url_replaces_events_path_without_leaking_query():
    assert (
        runtime_events_url("http://127.0.0.1:18765/events?token=ignored")
        == "http://127.0.0.1:18765/runtime/events"
    )


def test_emitter_rereads_transport_secret_and_increments_sequence():
    secrets = iter((b"a" * 32, b"b" * 32))
    calls = []
    clock = iter((100.0, 101.0))
    emitter = RuntimeControlEmitter(
        event_url="http://127.0.0.1:18765/events",
        hook_generation=RUNTIME_HOOK_GENERATION,
        package_version="4.1.0",
        runtime_id="runtime-1234567890",
        now=lambda: next(clock),
        secret_reader=lambda: next(secrets),
        poster=lambda url, body, headers, timeout: calls.append(
            (url, json.loads(body), headers, timeout)
        )
        or True,
    )

    assert emitter.emit_once("runtime.hello") is True
    assert emitter.emit_once("runtime.heartbeat") is True

    assert [call[1]["sequence"] for call in calls] == [1, 2]
    assert [call[1]["event"] for call in calls] == [
        "runtime.hello",
        "runtime.heartbeat",
    ]
    assert calls[0][2] != calls[1][2]
    assert all(call[0].endswith("/runtime/events") for call in calls)


def test_emitter_fails_open_when_secret_or_post_is_unavailable():
    missing = RuntimeControlEmitter(
        event_url="http://127.0.0.1:18765/events",
        hook_generation=RUNTIME_HOOK_GENERATION,
        package_version="4.1.0",
        secret_reader=lambda: None,
    )
    failing = RuntimeControlEmitter(
        event_url="http://127.0.0.1:18765/events",
        hook_generation=RUNTIME_HOOK_GENERATION,
        package_version="4.1.0",
        secret_reader=lambda: b"r" * 32,
        poster=lambda *_args: (_ for _ in ()).throw(OSError("offline")),
    )

    assert missing.emit_once("runtime.hello") is False
    assert failing.emit_once("runtime.hello") is False


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1:18765/runtime/events",
        "http://localhost:18765/runtime/events",
        "http://[::1]:18765/runtime/events",
    ),
)
def test_runtime_control_post_bypasses_environment_proxy_for_loopback(
    monkeypatch,
    url,
):
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"{}"

    class NoProxyOpener:
        def open(self, req, timeout):
            calls.append((req.full_url, timeout))
            return Response()

    monkeypatch.setattr(runtime_control, "_NO_PROXY_OPENER", NoProxyOpener())
    monkeypatch.setattr(
        runtime_control.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("loopback runtime control used the system proxy path")
        ),
    )

    assert runtime_control._post_runtime_request(url, b"{}", {}, 1.0) is True
    assert calls == [(url, 1.0)]


def test_runtime_control_post_preserves_system_proxy_path_for_remote_endpoint(
    monkeypatch,
):
    calls = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b""

    class FailingNoProxyOpener:
        def open(self, *_args, **_kwargs):
            raise AssertionError("remote runtime control bypassed the system proxy")

    monkeypatch.setattr(
        runtime_control,
        "_NO_PROXY_OPENER",
        FailingNoProxyOpener(),
    )
    monkeypatch.setattr(
        runtime_control.request,
        "urlopen",
        lambda req, timeout: calls.append((req.full_url, timeout)) or Response(),
    )

    assert runtime_control._post_runtime_request(
        "https://sidecar.example/runtime/events",
        b"{}",
        {},
        1.0,
    ) is True
    assert calls == [("https://sidecar.example/runtime/events", 1.0)]


def test_supervisor_has_independent_liveness_readiness_state_machine():
    clock = [0.0]
    supervisor = RuntimeIntegritySupervisor(
        mode="notify",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: clock[0],
        startup_grace_seconds=30.0,
        stale_after_seconds=45.0,
    )

    assert supervisor.snapshot() == {
        "status": "starting",
        "reason": "runtime_heartbeat_waiting",
        "integrity_mode": "notify",
        "runtime_seen": False,
        "generation_match": False,
        "restart_required": False,
        "last_seen_age_seconds": None,
    }

    clock[0] = 31.0
    assert supervisor.snapshot()["reason"] == "runtime_heartbeat_missing"

    supervisor.record(RuntimeControlEvent.from_dict(_payload(created_at=31.0)))
    assert supervisor.snapshot()["status"] == "ready"
    assert supervisor.snapshot()["generation_match"] is True

    clock[0] = 77.0
    assert supervisor.snapshot()["reason"] == "runtime_heartbeat_stale"


def test_supervisor_requires_matching_generation_and_can_mark_restart_required():
    supervisor = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 100.0,
    )
    supervisor.record(
        RuntimeControlEvent.from_dict(
            _payload(hook_generation="older-hook", package_version="4.0.21")
        )
    )
    assert supervisor.snapshot()["reason"] == "gateway_restart_required"

    supervisor.mark_restart_required()
    supervisor.record(
        RuntimeControlEvent.from_dict(
            _payload(runtime_id="runtime-restarted-123", created_at=101.0)
        )
    )
    assert supervisor.snapshot()["status"] == "ready"
    assert supervisor.snapshot()["restart_required"] is False


def test_restart_fence_survives_sidecar_restart_until_different_matching_hello(
    tmp_path,
):
    state_root = tmp_path / "private-state"
    old_runtime_id = "runtime-before-repair-123"
    new_runtime_id = "runtime-after-repair-456"
    first = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 100.0,
        state_directory=state_root,
    )
    assert first.record(
        RuntimeControlEvent.from_dict(
            _payload(runtime_id=old_runtime_id, created_at=99.0)
        )
    )
    first.mark_restart_required()

    persisted = (state_root / "runtime-integrity-fence.json").read_text()
    assert old_runtime_id not in persisted
    assert new_runtime_id not in persisted
    assert os.stat(state_root).st_mode & 0o777 == 0o700
    assert os.stat(state_root / "runtime-integrity-fence.json").st_mode & 0o777 == 0o600

    restarted = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 101.0,
        state_directory=state_root,
    )
    assert restarted.snapshot()["reason"] == "gateway_restart_required"

    assert restarted.record(
        RuntimeControlEvent.from_dict(
            _payload(
                event="runtime.heartbeat",
                runtime_id=old_runtime_id,
                sequence=2,
                created_at=101.0,
            )
        )
    )
    assert restarted.snapshot()["reason"] == "gateway_restart_required"
    assert restarted.record(
        RuntimeControlEvent.from_dict(
            _payload(
                runtime_id=old_runtime_id,
                sequence=3,
                created_at=102.0,
            )
        )
    )
    assert restarted.snapshot()["reason"] == "gateway_restart_required"

    assert restarted.record(
        RuntimeControlEvent.from_dict(
            _payload(
                event="runtime.heartbeat",
                runtime_id=new_runtime_id,
                sequence=1,
                created_at=103.0,
            )
        )
    )
    assert restarted.snapshot()["reason"] == "gateway_restart_required"
    assert restarted.record(
        RuntimeControlEvent.from_dict(
            _payload(
                runtime_id=new_runtime_id,
                sequence=2,
                created_at=104.0,
            )
        )
    )
    assert restarted.snapshot()["status"] == "ready"

    reloaded = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 105.0,
        state_directory=state_root,
    )
    assert reloaded.snapshot()["restart_required"] is False


def test_restart_fence_without_pre_repair_runtime_requires_manual_resolution(
    tmp_path,
):
    state_root = tmp_path / "private-state"
    first = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 100.0,
        state_directory=state_root,
    )

    first.mark_restart_required()

    assert first.snapshot()["reason"] == "manual_review_required"
    restarted = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 101.0,
        state_directory=state_root,
    )
    assert restarted.record(
        RuntimeControlEvent.from_dict(
            _payload(runtime_id="runtime-delayed-old-123", created_at=102.0)
        )
    )
    snapshot = restarted.snapshot()
    assert snapshot["reason"] == "manual_review_required"
    assert snapshot["restart_required"] is True


def test_manual_review_fence_survives_restart_and_matching_new_runtime_hello(
    tmp_path,
):
    state_root = tmp_path / "private-state"
    first = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 100.0,
        state_directory=state_root,
    )
    first.mark_manual_review_required()

    restarted = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 101.0,
        state_directory=state_root,
    )
    assert restarted.snapshot()["reason"] == "manual_review_required"
    assert restarted.record(
        RuntimeControlEvent.from_dict(
            _payload(runtime_id="runtime-after-review-789", created_at=102.0)
        )
    )
    assert restarted.snapshot()["reason"] == "manual_review_required"


def test_fence_state_lstat_permission_error_fails_closed(monkeypatch, tmp_path):
    state_root = tmp_path / "private-state"
    state_root.mkdir(mode=0o700)
    real_lstat = runtime_control.os.lstat

    def guarded_lstat(path):
        if runtime_control.Path(path).name == "runtime-integrity-fence.json":
            raise PermissionError("denied")
        return real_lstat(path)

    monkeypatch.setattr(runtime_control.os, "lstat", guarded_lstat)

    supervisor = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        state_directory=state_root,
    )

    assert supervisor.snapshot()["reason"] == "manual_review_required"


def test_fence_load_fails_closed_when_private_root_is_swapped_during_read(
    monkeypatch,
    tmp_path,
):
    state_root = tmp_path / "private-state"
    replacement_root = tmp_path / "replacement-private-state"
    displaced_root = tmp_path / "displaced-private-state"

    def write_state(root, *, restart_required, runtime_hash):
        root.mkdir(mode=0o700)
        path = root / "runtime-integrity-fence.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "restart_required": restart_required,
                    "manual_review_required": False,
                    "pre_repair_runtime_hash": runtime_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    write_state(state_root, restart_required=False, runtime_hash="")
    write_state(
        replacement_root,
        restart_required=True,
        runtime_hash="a" * 64,
    )
    real_fdopen = runtime_control.os.fdopen
    swapped = []

    class SwappingHandle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, *args, **kwargs):
            raw = self.handle.read(*args, **kwargs)
            state_root.rename(displaced_root)
            replacement_root.rename(state_root)
            swapped.append(True)
            return raw

    def swapping_fdopen(*args, **kwargs):
        return SwappingHandle(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(runtime_control.os, "fdopen", swapping_fdopen)

    supervisor = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        state_directory=state_root,
    )

    assert swapped == [True]
    assert supervisor.snapshot()["reason"] == "manual_review_required"
    assert json.loads(
        (state_root / "runtime-integrity-fence.json").read_text(encoding="utf-8")
    )["restart_required"] is True


def test_atomic_fence_write_never_replaces_or_unlinks_through_swapped_root(
    monkeypatch,
    tmp_path,
):
    state_root = tmp_path / "private-state"
    displaced_root = tmp_path / "displaced-private-state"
    state_root.mkdir(mode=0o700)
    real_replace = runtime_control.os.replace
    decoys = []

    def swap_root_then_replace(source, destination, *args, **kwargs):
        state_root.rename(displaced_root)
        state_root.mkdir(mode=0o700)
        decoy = state_root / runtime_control.Path(source).name
        decoy.write_text("user-owned", encoding="utf-8")
        decoys.append(decoy)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(runtime_control.os, "replace", swap_root_then_replace)
    supervisor = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        state_directory=state_root,
    )

    supervisor.mark_restart_required()

    assert supervisor.snapshot()["reason"] == "manual_review_required"
    assert len(decoys) == 1
    assert decoys[0].read_text(encoding="utf-8") == "user-owned"


def test_atomic_fence_write_fails_closed_if_root_loses_private_mode(
    monkeypatch,
    tmp_path,
):
    state_root = tmp_path / "private-state"
    state_root.mkdir(mode=0o700)
    real_replace = runtime_control.os.replace

    def expose_root_then_replace(source, destination, *args, **kwargs):
        state_root.chmod(0o755)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(runtime_control.os, "replace", expose_root_then_replace)
    supervisor = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        state_directory=state_root,
    )

    supervisor.mark_restart_required()

    assert supervisor.snapshot()["reason"] == "manual_review_required"


def test_matching_runtime_hello_does_not_clear_manual_review_requirement():
    supervisor = RuntimeIntegritySupervisor(
        mode="safe",
        expected_hook_generation=RUNTIME_HOOK_GENERATION,
        expected_package_version="4.1.0",
        now=lambda: 100.0,
    )
    supervisor.mark_manual_review_required()

    assert supervisor.record(
        RuntimeControlEvent.from_dict(
            _payload(runtime_id="runtime-reviewed-123", created_at=101.0)
        )
    )

    snapshot = supervisor.snapshot()
    assert snapshot["status"] == "degraded"
    assert snapshot["reason"] == "manual_review_required"


def test_supervisor_off_mode_is_disabled_even_without_runtime():
    supervisor = RuntimeIntegritySupervisor(mode="off")

    assert supervisor.snapshot()["status"] == "disabled"
    assert supervisor.snapshot()["reason"] == "integrity_disabled"


def test_existing_startup_adapter_call_starts_runtime_control_without_new_patch(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        hook_runtime,
        "load_runtime_config",
        lambda: type(
            "Config",
            (),
            {
                "enabled": True,
                "event_url": "http://127.0.0.1:18765/events",
            },
        )(),
    )
    monkeypatch.setattr(
        hook_runtime,
        "start_runtime_control",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    assert (
        hook_runtime.install_feishu_command_card_adapter_methods(
            type("Runner", (), {"adapters": {}})()
        )
        is False
    )

    assert calls == [
        {
            "event_url": "http://127.0.0.1:18765/events",
            "package_version": hook_runtime.__version__,
        }
    ]
