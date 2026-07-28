from __future__ import annotations

import hashlib
import hmac
from ipaddress import ip_address
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Callable


EVENT_TIMESTAMP_HEADER = "X-HFC-Event-Timestamp"
EVENT_NONCE_HEADER = "X-HFC-Event-Nonce"
EVENT_SIGNATURE_HEADER = "X-HFC-Event-Signature"
POLICY_TIMESTAMP_HEADER = "X-HFC-Policy-Timestamp"
POLICY_NONCE_HEADER = "X-HFC-Policy-Nonce"
POLICY_SIGNATURE_HEADER = "X-HFC-Policy-Signature"

_ROOT_SECRET_BYTES = 32
_PROOF_MAX_AGE_SECONDS = 30
_POLICY_PROOF_MAX_AGE_SECONDS = 5
_EVENT_MAX_NONCES = 16_384
_POLICY_MAX_NONCES = 512


class EventAuthenticationError(ValueError):
    pass


class PolicyAuthenticationError(ValueError):
    pass


def sign_event_request(
    secret: bytes,
    body: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    return _sign_domain_request(
        secret,
        body,
        domain="hfc-event-v1",
        timestamp_header=EVENT_TIMESTAMP_HEADER,
        nonce_header=EVENT_NONCE_HEADER,
        signature_header=EVENT_SIGNATURE_HEADER,
        timestamp=timestamp,
        nonce=nonce,
        label="event",
    )


def sign_policy_request(
    secret: bytes,
    body: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    return _sign_domain_request(
        secret,
        body,
        domain="hfc-policy-v1",
        timestamp_header=POLICY_TIMESTAMP_HEADER,
        nonce_header=POLICY_NONCE_HEADER,
        signature_header=POLICY_SIGNATURE_HEADER,
        timestamp=timestamp,
        nonce=nonce,
        label="policy",
    )


class EventProofVerifier:
    def __init__(
        self,
        secret: bytes,
        *,
        now: Callable[[], float] = time.time,
        max_nonces: int = _EVENT_MAX_NONCES,
    ):
        self._verifier = _DomainProofVerifier(
            secret,
            domain="hfc-event-v1",
            timestamp_header=EVENT_TIMESTAMP_HEADER,
            nonce_header=EVENT_NONCE_HEADER,
            signature_header=EVENT_SIGNATURE_HEADER,
            max_age_seconds=_PROOF_MAX_AGE_SECONDS,
            error_type=EventAuthenticationError,
            now=now,
            max_nonces=max_nonces,
        )

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        self._verifier.verify(headers, body)


class PolicyProofVerifier:
    def __init__(
        self,
        secret: bytes,
        *,
        now: Callable[[], float] = time.time,
        max_nonces: int = _POLICY_MAX_NONCES,
    ):
        self._verifier = _DomainProofVerifier(
            secret,
            domain="hfc-policy-v1",
            timestamp_header=POLICY_TIMESTAMP_HEADER,
            nonce_header=POLICY_NONCE_HEADER,
            signature_header=POLICY_SIGNATURE_HEADER,
            max_age_seconds=_POLICY_PROOF_MAX_AGE_SECONDS,
            error_type=PolicyAuthenticationError,
            now=now,
            max_nonces=max_nonces,
        )

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        self._verifier.verify(headers, body)


def _sign_domain_request(
    secret: bytes,
    body: bytes,
    *,
    domain: str,
    timestamp_header: str,
    nonce_header: str,
    signature_header: str,
    timestamp: int | None,
    nonce: str | None,
    label: str,
) -> dict[str, str]:
    _validate_secret(secret)
    if not isinstance(body, bytes):
        raise ValueError(f"{label} request body must be bytes")
    signed_at = int(time.time()) if timestamp is None else timestamp
    request_nonce = secrets.token_urlsafe(18) if nonce is None else nonce
    if (
        isinstance(signed_at, bool)
        or not isinstance(signed_at, int)
        or not isinstance(request_nonce, str)
        or not 16 <= len(request_nonce) <= 128
    ):
        raise ValueError(f"{label} proof metadata is invalid")
    signature = hmac.new(
        secret,
        _domain_signing_input(domain, signed_at, request_nonce, _body_hash(body)),
        hashlib.sha256,
    ).hexdigest()
    return {
        timestamp_header: str(signed_at),
        nonce_header: request_nonce,
        signature_header: signature,
    }


class _DomainProofVerifier:
    def __init__(
        self,
        secret: bytes,
        *,
        domain: str,
        timestamp_header: str,
        nonce_header: str,
        signature_header: str,
        max_age_seconds: int,
        error_type: type[ValueError],
        now: Callable[[], float],
        max_nonces: int,
    ) -> None:
        _validate_secret(secret)
        if max_nonces < 1:
            raise ValueError("max_nonces must be positive")
        self._secret = secret
        self._domain = domain
        self._timestamp_header = timestamp_header
        self._nonce_header = nonce_header
        self._signature_header = signature_header
        self._max_age_seconds = max_age_seconds
        self._error_type = error_type
        self._now = now
        self._max_nonces = max_nonces
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        if not isinstance(body, bytes):
            raise self._error_type(f"invalid {self._proof_label()} proof")
        timestamp_text = _header_value(headers, self._timestamp_header)
        nonce = _header_value(headers, self._nonce_header)
        signature = _header_value(headers, self._signature_header)
        try:
            timestamp = int(timestamp_text) if timestamp_text is not None else None
        except (TypeError, ValueError):
            timestamp = None
        if (
            timestamp is None
            or isinstance(timestamp, bool)
            or not isinstance(nonce, str)
            or not 16 <= len(nonce) <= 128
            or not isinstance(signature, str)
            or len(signature) != 64
        ):
            raise self._error_type(f"invalid {self._proof_label()} proof")
        now = self._now()
        if abs(now - timestamp) > self._max_age_seconds:
            raise self._error_type(f"{self._proof_label()} proof expired")
        expected = hmac.new(
            self._secret,
            _domain_signing_input(
                self._domain,
                timestamp,
                nonce,
                _body_hash(body),
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise self._error_type(f"invalid {self._proof_label()} proof")
        with self._lock:
            self._prune_nonces_locked(now)
            if nonce in self._nonces:
                raise self._error_type(f"{self._proof_label()} proof replayed")
            if len(self._nonces) >= self._max_nonces:
                raise self._error_type(
                    f"{self._proof_label()} proof verifier overloaded"
                )
            self._nonces[nonce] = timestamp + self._max_age_seconds

    def _prune_nonces_locked(self, now: float) -> None:
        for nonce, expires_at in list(self._nonces.items()):
            if expires_at < now:
                self._nonces.pop(nonce, None)

    def _proof_label(self) -> str:
        return self._domain.removeprefix("hfc-").removesuffix("-v1")


def is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) != _ROOT_SECRET_BYTES:
        raise ValueError("event transport root is invalid")


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _event_signing_input(timestamp: int, nonce: str, body_hash: str) -> bytes:
    return _domain_signing_input("hfc-event-v1", timestamp, nonce, body_hash)


def _domain_signing_input(
    domain: str,
    timestamp: int,
    nonce: str,
    body_hash: str,
) -> bytes:
    return f"{domain}\0{timestamp}\0{nonce}\0{body_hash}".encode("utf-8")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    normalized = name.lower()
    for key, candidate in headers.items():
        if str(key).lower() == normalized:
            return candidate
    return None
