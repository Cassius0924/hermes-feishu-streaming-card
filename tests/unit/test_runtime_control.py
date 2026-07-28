from __future__ import annotations

import json

import pytest

from hermes_feishu_card import hook_runtime
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
