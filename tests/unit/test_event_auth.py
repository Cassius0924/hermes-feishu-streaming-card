from __future__ import annotations

import pytest

from hermes_feishu_card.event_auth import (
    EventAuthenticationError,
    EventProofVerifier,
    PolicyAuthenticationError,
    PolicyProofVerifier,
    sign_event_request,
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


@pytest.mark.parametrize("secret", [b"", b"short", b"x" * 31, b"x" * 33])
def test_policy_proof_refuses_missing_or_invalid_private_root(secret):
    with pytest.raises(ValueError, match="transport root is invalid"):
        sign_policy_request(secret, b"{}")
    with pytest.raises(ValueError, match="transport root is invalid"):
        PolicyProofVerifier(secret)
