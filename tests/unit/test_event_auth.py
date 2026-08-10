from __future__ import annotations

import pytest

from hermes_feishu_card import event_auth as event_auth_module
from hermes_feishu_card.event_auth import (
    EventAuthenticationError,
    EventProofVerifier,
    NativeHandoffAckAuthenticationError,
    NativeHandoffAckProofVerifier,
    NativeHandoffRecoveryAuthenticationError,
    NativeHandoffRecoveryProofVerifier,
    PolicyAuthenticationError,
    PolicyProofVerifier,
    sign_event_request,
    sign_native_handoff_ack_request,
    sign_native_handoff_recovery_request,
    sign_policy_request,
)


def test_policy_proof_binds_body_and_rejects_replay():
    secret = b"p" * 32
    body = b'{"schema_version":"1","chat_id":"chat-a"}'
    headers = sign_policy_request(
        secret,
        body,
        timestamp=100,
        nonce="policy-nonce-0001",
    )
    verifier = PolicyProofVerifier(secret, now=lambda: 100.0)

    verifier.verify(headers, body)

    with pytest.raises(PolicyAuthenticationError, match="replayed"):
        verifier.verify(headers, body)
    with pytest.raises(PolicyAuthenticationError, match="invalid"):
        PolicyProofVerifier(secret, now=lambda: 100.0).verify(headers, body + b" ")


def test_policy_proof_expires_after_five_seconds():
    secret = b"p" * 32
    body = b"{}"
    headers = sign_policy_request(
        secret,
        body,
        timestamp=100,
        nonce="policy-nonce-0002",
    )

    PolicyProofVerifier(secret, now=lambda: 105.0).verify(headers, body)
    with pytest.raises(PolicyAuthenticationError, match="expired"):
        PolicyProofVerifier(secret, now=lambda: 106.0).verify(headers, body)


def test_event_and_policy_proofs_are_domain_separated():
    secret = b"p" * 32
    body = b"{}"
    event_headers = sign_event_request(
        secret,
        body,
        timestamp=100,
        nonce="domain-nonce-0001",
    )
    policy_headers = sign_policy_request(
        secret,
        body,
        timestamp=100,
        nonce="domain-nonce-0001",
    )

    with pytest.raises(PolicyAuthenticationError):
        PolicyProofVerifier(secret, now=lambda: 100.0).verify(
            {
                "X-HFC-Policy-Timestamp": event_headers["X-HFC-Event-Timestamp"],
                "X-HFC-Policy-Nonce": event_headers["X-HFC-Event-Nonce"],
                "X-HFC-Policy-Signature": event_headers["X-HFC-Event-Signature"],
            },
            body,
        )
    with pytest.raises(EventAuthenticationError):
        EventProofVerifier(secret, now=lambda: 100.0).verify(
            {
                "X-HFC-Event-Timestamp": policy_headers["X-HFC-Policy-Timestamp"],
                "X-HFC-Event-Nonce": policy_headers["X-HFC-Policy-Nonce"],
                "X-HFC-Event-Signature": policy_headers["X-HFC-Policy-Signature"],
            },
            body,
        )


def test_native_handoff_ack_proof_is_body_bound_replay_safe_and_domain_separated():
    secret = b"a" * 32
    body = b'{"protocol":"hfc-native-handoff-v2"}'
    headers = sign_native_handoff_ack_request(
        secret,
        body,
        timestamp=100,
        nonce="native-ack-nonce-0001",
    )
    verifier = NativeHandoffAckProofVerifier(secret, now=lambda: 100.0)

    verifier.verify(headers, body)
    with pytest.raises(NativeHandoffAckAuthenticationError, match="replayed"):
        verifier.verify(headers, body)
    with pytest.raises(NativeHandoffAckAuthenticationError, match="invalid"):
        NativeHandoffAckProofVerifier(secret, now=lambda: 100.0).verify(
            headers,
            body + b" ",
        )


def test_native_handoff_recovery_proof_is_separate_from_ack_domain():
    secret = b"r" * 32
    body = b'{"protocol":"hfc-native-handoff-recovery-v2"}'
    headers = sign_native_handoff_recovery_request(
        secret,
        body,
        timestamp=100,
        nonce="native-recovery-nonce-0001",
    )
    verifier = NativeHandoffRecoveryProofVerifier(secret, now=lambda: 100.0)

    verifier.verify(headers, body)
    with pytest.raises(NativeHandoffRecoveryAuthenticationError, match="replayed"):
        verifier.verify(headers, body)

    ack_headers = sign_native_handoff_ack_request(
        secret,
        body,
        timestamp=100,
        nonce="native-recovery-nonce-0002",
    )
    with pytest.raises(NativeHandoffRecoveryAuthenticationError, match="invalid"):
        NativeHandoffRecoveryProofVerifier(secret, now=lambda: 100.0).verify(
            {
                "X-HFC-Native-Recovery-Timestamp": ack_headers[
                    "X-HFC-Native-Ack-Timestamp"
                ],
                "X-HFC-Native-Recovery-Nonce": ack_headers[
                    "X-HFC-Native-Ack-Nonce"
                ],
                "X-HFC-Native-Recovery-Signature": ack_headers[
                    "X-HFC-Native-Ack-Signature"
                ],
            },
            body,
        )

    event_headers = sign_event_request(
        secret,
        body,
        timestamp=100,
        nonce="native-ack-nonce-0002",
    )
    with pytest.raises(NativeHandoffAckAuthenticationError, match="invalid"):
        NativeHandoffAckProofVerifier(secret, now=lambda: 100.0).verify(
            {
                "X-HFC-Native-Ack-Timestamp": event_headers["X-HFC-Event-Timestamp"],
                "X-HFC-Native-Ack-Nonce": event_headers["X-HFC-Event-Nonce"],
                "X-HFC-Native-Ack-Signature": event_headers["X-HFC-Event-Signature"],
            },
            body,
        )


@pytest.mark.parametrize("secret", [b"", b"short", b"x" * 31, b"x" * 33])
def test_policy_proof_refuses_missing_or_invalid_private_root(secret):
    with pytest.raises(ValueError, match="transport root is invalid"):
        sign_policy_request(secret, b"{}")
    with pytest.raises(ValueError, match="transport root is invalid"):
        PolicyProofVerifier(secret)


def test_default_event_nonce_capacity_handles_expected_parallel_streams():
    secret = b"e" * 32
    body = b'{}'
    verifier = EventProofVerifier(secret, now=lambda: 100.0)

    for index in range(4097):
        verifier.verify(
            sign_event_request(
                secret,
                body,
                timestamp=100,
                nonce=f"event-nonce-{index:08d}",
            ),
            body,
        )


def test_event_nonce_capacity_remains_fail_closed_when_explicitly_full():
    secret = b"e" * 32
    body = b"{}"
    verifier = EventProofVerifier(secret, now=lambda: 100.0, max_nonces=2)

    for index in range(2):
        verifier.verify(
            sign_event_request(
                secret,
                body,
                timestamp=100,
                nonce=f"small-event-nonce-{index:04d}",
            ),
            body,
        )

    with pytest.raises(EventAuthenticationError, match="overloaded"):
        verifier.verify(
            sign_event_request(
                secret,
                body,
                timestamp=100,
                nonce="small-event-nonce-0002",
            ),
            body,
        )


def test_sidecar_request_proof_binds_method_path_and_body_and_rejects_replay():
    signer = getattr(event_auth_module, "sign_sidecar_request", None)
    verifier_type = getattr(event_auth_module, "SidecarRequestProofVerifier", None)
    error_type = getattr(
        event_auth_module,
        "SidecarRequestAuthenticationError",
        ValueError,
    )

    assert callable(signer)
    assert verifier_type is not None

    secret = b"s" * 32
    body = b'{"choice":"once"}'
    headers = signer(
        secret,
        "POST",
        "/card/actions",
        body,
        timestamp=100,
        nonce="sidecar-request-nonce-0001",
    )
    verifier = verifier_type(secret, now=lambda: 100.0)

    verifier.verify(headers, "POST", "/card/actions", body)
    with pytest.raises(error_type, match="replayed"):
        verifier.verify(headers, "POST", "/card/actions", body)
    with pytest.raises(error_type, match="invalid"):
        verifier_type(secret, now=lambda: 100.0).verify(
            headers,
            "GET",
            "/card/actions",
            body,
        )
    with pytest.raises(error_type, match="invalid"):
        verifier_type(secret, now=lambda: 100.0).verify(
            headers,
            "POST",
            "/interactions/approval-1",
            body,
        )
    with pytest.raises(error_type, match="invalid"):
        verifier_type(secret, now=lambda: 100.0).verify(
            headers,
            "POST",
            "/card/actions",
            body + b" ",
        )


def test_sidecar_request_proof_is_domain_separated_and_expires():
    signer = getattr(event_auth_module, "sign_sidecar_request", None)
    verifier_type = getattr(event_auth_module, "SidecarRequestProofVerifier", None)
    error_type = getattr(
        event_auth_module,
        "SidecarRequestAuthenticationError",
        ValueError,
    )

    assert callable(signer)
    assert verifier_type is not None

    secret = b"s" * 32
    body = b""
    headers = signer(
        secret,
        "GET",
        "/messages/message-1/summary/",
        body,
        timestamp=100,
        nonce="sidecar-request-nonce-0002",
    )

    verifier_type(secret, now=lambda: 130.0).verify(
        headers,
        "GET",
        "/messages/message-1/summary",
        body,
    )
    with pytest.raises(error_type, match="expired"):
        verifier_type(secret, now=lambda: 131.0).verify(
            headers,
            "GET",
            "/messages/message-1/summary",
            body,
        )

    event_headers = sign_event_request(
        secret,
        body,
        timestamp=100,
        nonce="sidecar-request-nonce-0003",
    )
    remapped_headers = {
        "X-HFC-Sidecar-Timestamp": event_headers["X-HFC-Event-Timestamp"],
        "X-HFC-Sidecar-Nonce": event_headers["X-HFC-Event-Nonce"],
        "X-HFC-Sidecar-Signature": event_headers["X-HFC-Event-Signature"],
    }
    with pytest.raises(error_type, match="invalid"):
        verifier_type(secret, now=lambda: 100.0).verify(
            remapped_headers,
            "GET",
            "/messages/message-1/summary",
            body,
        )
