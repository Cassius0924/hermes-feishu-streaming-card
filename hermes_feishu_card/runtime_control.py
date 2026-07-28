from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib import parse, request

from .operations_transport import read_transport_root_secret


RUNTIME_TIMESTAMP_HEADER = "X-HFC-Runtime-Timestamp"
RUNTIME_NONCE_HEADER = "X-HFC-Runtime-Nonce"
RUNTIME_SIGNATURE_HEADER = "X-HFC-Runtime-Signature"
RUNTIME_HOOK_GENERATION = "hfc-runtime-control-v1"

_ROOT_SECRET_BYTES = 32
_PROOF_MAX_AGE_SECONDS = 5
_MAX_NONCES = 512
_RUNTIME_EVENTS = frozenset({"runtime.hello", "runtime.heartbeat"})
_RUNTIME_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event",
        "runtime_id",
        "sequence",
        "created_at",
        "hook_generation",
        "package_version",
    }
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


class RuntimeControlValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeControlEvent:
    schema_version: str
    event: str
    runtime_id: str
    sequence: int
    created_at: float
    hook_generation: str
    package_version: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuntimeControlEvent":
        if not isinstance(payload, Mapping) or set(payload) != _RUNTIME_EVENT_FIELDS:
            raise RuntimeControlValidationError("invalid runtime control event")
        schema_version = payload.get("schema_version")
        event = payload.get("event")
        runtime_id = payload.get("runtime_id")
        sequence = payload.get("sequence")
        created_at = payload.get("created_at")
        hook_generation = payload.get("hook_generation")
        package_version = payload.get("package_version")
        if schema_version != "1" or event not in _RUNTIME_EVENTS:
            raise RuntimeControlValidationError("invalid runtime control event")
        if (
            not isinstance(runtime_id, str)
            or not 16 <= len(runtime_id) <= 128
            or _SAFE_ID_RE.fullmatch(runtime_id) is None
        ):
            raise RuntimeControlValidationError("invalid runtime control event")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise RuntimeControlValidationError("invalid runtime control event")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or float(created_at) < 0
        ):
            raise RuntimeControlValidationError("invalid runtime control event")
        for value in (hook_generation, package_version):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
            ):
                raise RuntimeControlValidationError("invalid runtime control event")
        return cls(
            schema_version="1",
            event=event,
            runtime_id=runtime_id,
            sequence=sequence,
            created_at=float(created_at),
            hook_generation=hook_generation,
            package_version=package_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "runtime_id": self.runtime_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "hook_generation": self.hook_generation,
            "package_version": self.package_version,
        }


def sign_runtime_request(
    secret: bytes,
    body: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    _validate_secret(secret)
    if not isinstance(body, bytes):
        raise ValueError("runtime request body must be bytes")
    signed_at = int(time.time()) if timestamp is None else timestamp
    request_nonce = secrets.token_urlsafe(18) if nonce is None else nonce
    if (
        isinstance(signed_at, bool)
        or not isinstance(signed_at, int)
        or not isinstance(request_nonce, str)
        or not 16 <= len(request_nonce) <= 128
    ):
        raise ValueError("runtime proof metadata is invalid")
    signature = hmac.new(
        secret,
        _runtime_signing_input(signed_at, request_nonce, _body_hash(body)),
        hashlib.sha256,
    ).hexdigest()
    return {
        RUNTIME_TIMESTAMP_HEADER: str(signed_at),
        RUNTIME_NONCE_HEADER: request_nonce,
        RUNTIME_SIGNATURE_HEADER: signature,
    }


class RuntimeProofVerifier:
    def __init__(
        self,
        secret: bytes,
        *,
        now: Callable[[], float] = time.time,
        max_nonces: int = _MAX_NONCES,
    ):
        _validate_secret(secret)
        if max_nonces < 1:
            raise ValueError("max_nonces must be positive")
        self._secret = secret
        self._now = now
        self._max_nonces = max_nonces
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        if not isinstance(body, bytes):
            raise RuntimeControlValidationError("invalid runtime proof")
        timestamp_text = _header_value(headers, RUNTIME_TIMESTAMP_HEADER)
        nonce = _header_value(headers, RUNTIME_NONCE_HEADER)
        signature = _header_value(headers, RUNTIME_SIGNATURE_HEADER)
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
            raise RuntimeControlValidationError("invalid runtime proof")

        now = self._now()
        if abs(now - timestamp) > _PROOF_MAX_AGE_SECONDS:
            raise RuntimeControlValidationError("runtime proof expired")
        expected = hmac.new(
            self._secret,
            _runtime_signing_input(timestamp, nonce, _body_hash(body)),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise RuntimeControlValidationError("invalid runtime proof")

        with self._lock:
            self._prune_nonces_locked(now)
            if nonce in self._nonces:
                raise RuntimeControlValidationError("runtime proof replayed")
            if len(self._nonces) >= self._max_nonces:
                raise RuntimeControlValidationError("runtime proof verifier overloaded")
            self._nonces[nonce] = timestamp + _PROOF_MAX_AGE_SECONDS

    def _prune_nonces_locked(self, now: float) -> None:
        for nonce, expires_at in list(self._nonces.items()):
            if expires_at < now:
                self._nonces.pop(nonce, None)


def runtime_events_url(event_url: str) -> str:
    parsed = parse.urlsplit(str(event_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("runtime event URL is invalid")
    path = parsed.path.rstrip("/")
    if path.endswith("/events"):
        path = path[: -len("/events")]
    return parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{path}/runtime/events", "", "")
    )


class RuntimeControlEmitter:
    def __init__(
        self,
        *,
        event_url: str,
        hook_generation: str,
        package_version: str,
        runtime_id: str | None = None,
        now: Callable[[], float] = time.time,
        secret_reader: Callable[[], bytes | None] = read_transport_root_secret,
        poster: Callable[[str, bytes, dict[str, str], float], bool] | None = None,
        timeout_seconds: float = 1.0,
    ):
        self.runtime_url = runtime_events_url(event_url)
        self.hook_generation = _bounded_text(hook_generation, "hook generation")
        self.package_version = _bounded_text(package_version, "package version")
        self.runtime_id = runtime_id or f"runtime-{secrets.token_urlsafe(18)}"
        if (
            not 16 <= len(self.runtime_id) <= 128
            or _SAFE_ID_RE.fullmatch(self.runtime_id) is None
        ):
            raise ValueError("runtime id is invalid")
        if timeout_seconds <= 0 or timeout_seconds > 5:
            raise ValueError("runtime timeout is invalid")
        self._now = now
        self._secret_reader = secret_reader
        self._poster = poster or _post_runtime_request
        self._timeout_seconds = timeout_seconds
        self._sequence = 0
        self._lock = threading.Lock()

    def emit_once(self, event_name: str) -> bool:
        if event_name not in _RUNTIME_EVENTS:
            return False
        try:
            with self._lock:
                self._sequence += 1
                sequence = self._sequence
            created_at = float(self._now())
            event = RuntimeControlEvent.from_dict(
                {
                    "schema_version": "1",
                    "event": event_name,
                    "runtime_id": self.runtime_id,
                    "sequence": sequence,
                    "created_at": created_at,
                    "hook_generation": self.hook_generation,
                    "package_version": self.package_version,
                }
            )
            body = json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            secret = self._secret_reader()
            if secret is None:
                return False
            headers = {"Content-Type": "application/json"}
            headers.update(sign_runtime_request(secret, body, timestamp=int(created_at)))
            return bool(
                self._poster(
                    self.runtime_url,
                    body,
                    headers,
                    self._timeout_seconds,
                )
            )
        except Exception:
            return False

    def run(self, stop_event: threading.Event, interval_seconds: float = 15.0) -> None:
        self.emit_once("runtime.hello")
        while not stop_event.wait(interval_seconds):
            self.emit_once("runtime.heartbeat")


class RuntimeIntegritySupervisor:
    def __init__(
        self,
        *,
        mode: str,
        expected_hook_generation: str = RUNTIME_HOOK_GENERATION,
        expected_package_version: str = "",
        now: Callable[[], float] = time.monotonic,
        startup_grace_seconds: float = 30.0,
        stale_after_seconds: float = 45.0,
    ):
        mode = str(mode or "").strip().lower()
        if mode not in {"safe", "notify", "off"}:
            raise ValueError("integrity mode is invalid")
        if startup_grace_seconds < 0 or stale_after_seconds <= 0:
            raise ValueError("runtime readiness timing is invalid")
        self.mode = mode
        self.expected_hook_generation = expected_hook_generation
        self.expected_package_version = expected_package_version
        self._now = now
        self._started_at = now()
        self._startup_grace_seconds = startup_grace_seconds
        self._stale_after_seconds = stale_after_seconds
        self._last_seen_at: float | None = None
        self._runtime_id = ""
        self._last_sequence = 0
        self._generation_match = False
        self._restart_required = False
        self._manual_review_required = False
        self._control_auth_unavailable = False
        self._lock = threading.Lock()

    def record(self, event: RuntimeControlEvent) -> bool:
        if not isinstance(event, RuntimeControlEvent) or self.mode == "off":
            return False
        now = self._now()
        with self._lock:
            if event.runtime_id == self._runtime_id and event.sequence <= self._last_sequence:
                return False
            self._runtime_id = event.runtime_id
            self._last_sequence = event.sequence
            self._last_seen_at = now
            self._generation_match = bool(
                event.hook_generation == self.expected_hook_generation
                and (
                    not self.expected_package_version
                    or event.package_version == self.expected_package_version
                )
            )
            if event.event == "runtime.hello" and self._generation_match:
                self._restart_required = False
                self._manual_review_required = False
        return True

    def mark_restart_required(self) -> None:
        with self._lock:
            self._restart_required = True

    def mark_manual_review_required(self) -> None:
        with self._lock:
            self._manual_review_required = True

    def mark_control_auth_unavailable(self) -> None:
        with self._lock:
            self._control_auth_unavailable = True

    def snapshot(self) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            last_seen_at = self._last_seen_at
            generation_match = self._generation_match
            restart_required = self._restart_required
            manual_review_required = self._manual_review_required
            control_auth_unavailable = self._control_auth_unavailable

        if self.mode == "off":
            status = "disabled"
            reason = "integrity_disabled"
        elif control_auth_unavailable:
            status = "degraded"
            reason = "control_auth_unavailable"
        elif manual_review_required:
            status = "degraded"
            reason = "manual_review_required"
        elif last_seen_at is None:
            status = "starting" if now - self._started_at <= self._startup_grace_seconds else "degraded"
            reason = (
                "runtime_heartbeat_waiting"
                if status == "starting"
                else "runtime_heartbeat_missing"
            )
        elif now - last_seen_at > self._stale_after_seconds:
            status = "degraded"
            reason = "runtime_heartbeat_stale"
        elif restart_required or not generation_match:
            status = "degraded"
            reason = "gateway_restart_required"
            restart_required = True
        else:
            status = "ready"
            reason = "runtime_ready"

        age = None if last_seen_at is None else max(0, int(now - last_seen_at))
        return {
            "status": status,
            "reason": reason,
            "integrity_mode": self.mode,
            "runtime_seen": last_seen_at is not None,
            "generation_match": generation_match,
            "restart_required": restart_required,
            "last_seen_age_seconds": age,
        }


_CONTROL_LOCK = threading.Lock()
_CONTROL_EMITTER: RuntimeControlEmitter | None = None
_CONTROL_STOP: threading.Event | None = None
_CONTROL_THREAD: threading.Thread | None = None


def start_runtime_control(
    *,
    event_url: str,
    package_version: str,
    hook_generation: str = RUNTIME_HOOK_GENERATION,
    interval_seconds: float = 15.0,
) -> bool:
    global _CONTROL_EMITTER, _CONTROL_STOP, _CONTROL_THREAD
    try:
        if interval_seconds <= 0:
            return False
        with _CONTROL_LOCK:
            if _CONTROL_THREAD is not None and _CONTROL_THREAD.is_alive():
                return True
            emitter = RuntimeControlEmitter(
                event_url=event_url,
                hook_generation=hook_generation,
                package_version=package_version,
            )
            stop_event = threading.Event()
            thread = threading.Thread(
                target=emitter.run,
                args=(stop_event, interval_seconds),
                name="hfc-runtime-control",
                daemon=True,
            )
            _CONTROL_EMITTER = emitter
            _CONTROL_STOP = stop_event
            _CONTROL_THREAD = thread
            thread.start()
        return True
    except Exception:
        return False


def reset_runtime_control_for_tests() -> None:
    global _CONTROL_EMITTER, _CONTROL_STOP, _CONTROL_THREAD
    with _CONTROL_LOCK:
        stop_event = _CONTROL_STOP
        thread = _CONTROL_THREAD
        _CONTROL_EMITTER = None
        _CONTROL_STOP = None
        _CONTROL_THREAD = None
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)


def _post_runtime_request(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
) -> bool:
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as response:
        response.read(4096)
        return 200 <= int(getattr(response, "status", 0)) < 300


def _bounded_text(value: str, label: str) -> str:
    normalized = str(value or "")
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _validate_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) != _ROOT_SECRET_BYTES:
        raise ValueError("runtime transport root is invalid")


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _runtime_signing_input(timestamp: int, nonce: str, body_hash: str) -> bytes:
    return f"hfc-runtime-v1\0{timestamp}\0{nonce}\0{body_hash}".encode("utf-8")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    normalized = name.lower()
    for key, candidate in headers.items():
        if str(key).lower() == normalized:
            return candidate
    return None
