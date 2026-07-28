from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Iterator

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - unavailable on Windows.
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - unavailable on POSIX.
    _msvcrt = None


NATIVE_HANDOFF_STATE_NAME = "native-handoffs.json"
NATIVE_HANDOFF_LOCK_NAME = "native-handoffs.lock"
DEFAULT_MAX_RECORDS = 512
DEFAULT_MAX_FILE_BYTES = 256 * 1024
_IDENTITY_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_STATES = frozenset({"pending", "committed", "no_card", "lifecycle"})
_ACTIVE_STATES = frozenset({"pending", "lifecycle"})
_MAX_FEISHU_MESSAGE_ID_CHARS = 512
_MAX_BOT_ID_CHARS = 256
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


class NativeHandoffStoreError(OSError):
    """The private handoff state could not be read or updated safely."""


@dataclass(frozen=True)
class NativeHandoffRecord:
    state: str
    feishu_message_id: str
    bot_id: str
    created_at: float
    updated_at: float
    event_created_at: float


def handoff_identity_key(
    *,
    profile_id: str,
    chat_id: str,
    conversation_id: str,
    message_id: str,
) -> str:
    """Return a stable opaque key without retaining any routing identifiers."""

    identity = json.dumps(
        {
            "profile_id": str(profile_id or ""),
            "chat_id": str(chat_id or ""),
            "conversation_id": str(conversation_id or ""),
            "message_id": str(message_id or ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"hfc-native-handoff-v1\0" + identity).hexdigest()


class NativeHandoffStore:
    def __init__(
        self,
        root: str | Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        now: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or max_file_bytes < 1024
        ):
            raise ValueError("max_file_bytes must be at least 1024")
        expanded_root = Path(root).expanduser()
        self.root = (
            expanded_root
            if expanded_root.is_absolute()
            else (Path.cwd() / expanded_root).absolute()
        )
        self.path = self.root / NATIVE_HANDOFF_STATE_NAME
        self.lock_path = self.root / NATIVE_HANDOFF_LOCK_NAME
        self.max_records = max_records
        self.max_file_bytes = max_file_bytes
        self._now = now
        self._lock = threading.RLock()

    def get(self, identity_key: str) -> NativeHandoffRecord | None:
        _validate_identity_key(identity_key)
        with self._lock:
            with self._persistent_lock():
                return self._load_records().get(identity_key)

    def begin(
        self,
        identity_key: str,
        *,
        feishu_message_id: str,
        bot_id: str,
        event_created_at: float,
    ) -> tuple[NativeHandoffRecord, bool]:
        _validate_identity_key(identity_key)
        message_id = _bounded_identifier(
            feishu_message_id,
            name="feishu_message_id",
            max_chars=_MAX_FEISHU_MESSAGE_ID_CHARS,
            required=True,
        )
        bounded_bot_id = _bounded_identifier(
            bot_id,
            name="bot_id",
            max_chars=_MAX_BOT_ID_CHARS,
            required=False,
        )
        return self._begin(
            identity_key,
            state="pending",
            feishu_message_id=message_id,
            bot_id=bounded_bot_id,
            event_created_at=_finite_timestamp(event_created_at),
        )

    def begin_no_card(
        self, identity_key: str, *, event_created_at: float
    ) -> tuple[NativeHandoffRecord, bool]:
        _validate_identity_key(identity_key)
        return self._begin(
            identity_key,
            state="no_card",
            feishu_message_id="",
            bot_id="",
            event_created_at=_finite_timestamp(event_created_at),
        )

    def mark_committed(
        self,
        identity_key: str,
        *,
        expected_record: NativeHandoffRecord | None = None,
    ) -> NativeHandoffRecord | None:
        _validate_identity_key(identity_key)
        with self._lock:
            with self._persistent_lock():
                records = self._load_records()
                current = records.get(identity_key)
                if current is None:
                    return None
                if expected_record is not None and current != expected_record:
                    # A newer lifecycle may have reused the same stable key
                    # while an older asynchronous PATCH was still in flight.
                    # Never let that stale completion commit the new record.
                    return current
                if current.state in {"committed", "no_card", "lifecycle"}:
                    return current
                updated = replace(
                    current,
                    state="committed",
                    updated_at=self._timestamp(),
                )
                pending = dict(records)
                pending[identity_key] = updated
                self._write_records(pending, protected_key=identity_key)
                return updated

    def clear(self, identity_key: str) -> bool:
        _validate_identity_key(identity_key)
        with self._lock:
            with self._persistent_lock():
                records = self._load_records()
                if identity_key not in records:
                    return False
                pending = dict(records)
                pending.pop(identity_key, None)
                self._write_records(pending)
                return True

    def prepare_lifecycle(self, identity_key: str, *, event_created_at: float) -> str:
        """Clear a tombstone only for a strictly newer lifecycle event."""

        _validate_identity_key(identity_key)
        lifecycle_at = _finite_timestamp(event_created_at)
        with self._lock:
            with self._persistent_lock():
                records = self._load_records()
                current = records.get(identity_key)
                if current is None:
                    return "absent"
                if lifecycle_at <= current.event_created_at:
                    return "stale"
                timestamp = self._timestamp()
                lifecycle_floor = NativeHandoffRecord(
                    state="lifecycle",
                    feishu_message_id="",
                    bot_id="",
                    created_at=timestamp,
                    updated_at=timestamp,
                    event_created_at=lifecycle_at,
                )
                pending = dict(records)
                pending[identity_key] = lifecycle_floor
                self._write_records(pending, protected_key=identity_key)
                return "cleared"

    def _begin(
        self,
        identity_key: str,
        *,
        state: str,
        feishu_message_id: str,
        bot_id: str,
        event_created_at: float,
    ) -> tuple[NativeHandoffRecord, bool]:
        with self._lock:
            with self._persistent_lock():
                records = self._load_records()
                current = records.get(identity_key)
                if current is not None:
                    if (
                        current.state != "lifecycle"
                        or event_created_at < current.event_created_at
                    ):
                        return current, False
                timestamp = self._timestamp()
                record = NativeHandoffRecord(
                    state=state,
                    feishu_message_id=feishu_message_id,
                    bot_id=bot_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                    event_created_at=event_created_at,
                )
                pending = dict(records)
                pending[identity_key] = record
                self._write_records(pending, protected_key=identity_key)
                return record, True

    def _timestamp(self) -> float:
        try:
            value = float(self._now())
        except (TypeError, ValueError, OverflowError) as exc:
            raise NativeHandoffStoreError("handoff state clock is invalid") from exc
        if not math.isfinite(value) or value < 0:
            raise NativeHandoffStoreError("handoff state clock is invalid")
        return value

    @contextmanager
    def _persistent_lock(self) -> Iterator[None]:
        _prepare_private_root(self.root)
        local_lock = _process_lock_for(self.lock_path)
        with local_lock:
            descriptor = _open_private_lock_file(self.root, self.lock_path)
            locked = False
            try:
                _acquire_persistent_lock(descriptor)
                locked = True
                yield
            finally:
                try:
                    if locked:
                        _release_persistent_lock(descriptor)
                finally:
                    os.close(descriptor)

    def _load_records(self) -> dict[str, NativeHandoffRecord]:
        if _lstat(self.path) is None:
            return {}
        raw = _read_private_file(self.root, self.path, self.max_file_bytes)
        return _decode_records(raw, self.max_records)

    def _write_records(
        self,
        records: dict[str, NativeHandoffRecord],
        *,
        protected_key: str = "",
    ) -> None:
        pending = dict(records)
        _validate_existing_private_file(self.root, self.path)
        self._evict_to_count(pending, protected_key=protected_key)
        payload = self._serialized_payload(pending)
        while len(payload) > self.max_file_bytes and len(pending) > 1:
            if not self._evict_one(pending, protected_key=protected_key):
                break
            payload = self._serialized_payload(pending)
        if len(payload) > self.max_file_bytes:
            raise NativeHandoffStoreError("handoff state exceeds bounded file size")
        _atomic_write_private(self.root, self.path, payload)

    def _serialized_payload(
        self, values: dict[str, NativeHandoffRecord]
    ) -> bytes:
        records = {
            key: {
                "state": record.state,
                "feishu_message_id": record.feishu_message_id,
                "bot_id": record.bot_id,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "event_created_at": record.event_created_at,
            }
            for key, record in sorted(values.items())
        }
        return (
            json.dumps(
                {"version": 1, "records": records},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def _evict_to_count(
        self,
        records: dict[str, NativeHandoffRecord],
        *,
        protected_key: str,
    ) -> None:
        while len(records) > self.max_records:
            if not self._evict_one(records, protected_key=protected_key):
                raise NativeHandoffStoreError("handoff state cannot be bounded")

    def _evict_one(
        self,
        records: dict[str, NativeHandoffRecord],
        *,
        protected_key: str,
    ) -> bool:
        candidates = [
            (key, record)
            for key, record in records.items()
            if key != protected_key and record.state not in _ACTIVE_STATES
        ]
        if not candidates:
            return False
        key, _ = min(
            candidates,
            key=lambda item: (
                item[1].updated_at,
                item[0],
            ),
        )
        records.pop(key, None)
        return True


def _decode_records(raw: bytes, max_records: int) -> dict[str, NativeHandoffRecord]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise NativeHandoffStoreError("handoff state is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise NativeHandoffStoreError("handoff state is invalid")
    values = payload.get("records")
    if not isinstance(values, dict):
        raise NativeHandoffStoreError("handoff state is invalid")
    decoded: dict[str, NativeHandoffRecord] = {}
    for key, value in values.items():
        try:
            _validate_identity_key(key)
            decoded[key] = _decode_record(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise NativeHandoffStoreError("handoff state is invalid") from exc
    if len(decoded) > max_records:
        active_count = sum(
            record.state in _ACTIVE_STATES for record in decoded.values()
        )
        if active_count > max_records:
            raise NativeHandoffStoreError(
                "handoff state has too many active records"
            )
        ordered = sorted(
            decoded.items(),
            key=lambda item: (
                item[1].state in _ACTIVE_STATES,
                item[1].updated_at,
                item[0],
            ),
            reverse=True,
        )
        decoded = dict(ordered[:max_records])
    return decoded


def _decode_record(value: Any) -> NativeHandoffRecord:
    if not isinstance(value, dict):
        raise ValueError("record must be an object")
    state = value.get("state")
    if state not in _VALID_STATES:
        raise ValueError("invalid record state")
    message_id = _bounded_identifier(
        value.get("feishu_message_id"),
        name="feishu_message_id",
        max_chars=_MAX_FEISHU_MESSAGE_ID_CHARS,
        required=state in {"pending", "committed"},
    )
    bot_id = _bounded_identifier(
        value.get("bot_id"),
        name="bot_id",
        max_chars=_MAX_BOT_ID_CHARS,
        required=False,
    )
    if state in {"no_card", "lifecycle"} and (message_id or bot_id):
        raise ValueError("record must not contain delivery identifiers")
    created_at = _finite_timestamp(value.get("created_at"))
    updated_at = _finite_timestamp(value.get("updated_at"))
    # Early development builds wrote version-1 records before this field was
    # added.  The private file is migrated in place on the next mutation; using
    # the durable write timestamp is the safest lifecycle-ordering fallback.
    event_created_at = _finite_timestamp(value.get("event_created_at", created_at))
    if updated_at < created_at:
        raise ValueError("record timestamps are invalid")
    return NativeHandoffRecord(
        state=state,
        feishu_message_id=message_id,
        bot_id=bot_id,
        created_at=created_at,
        updated_at=updated_at,
        event_created_at=event_created_at,
    )


def _validate_identity_key(value: Any) -> None:
    if not isinstance(value, str) or _IDENTITY_KEY_RE.fullmatch(value) is None:
        raise ValueError("identity key must be a SHA-256 digest")


def _bounded_identifier(
    value: Any,
    *,
    name: str,
    max_chars: int,
    required: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > max_chars or any(ord(char) < 0x20 for char in normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _finite_timestamp(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("timestamp is invalid")
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timestamp is invalid") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("timestamp is invalid")
    return timestamp


def _prepare_private_root(root: Path) -> None:
    root = root.absolute()
    _reject_symlink_components(root, allow_missing=True)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise NativeHandoffStoreError("handoff state directory could not be prepared") from exc
    _reject_symlink_components(root, allow_missing=False)
    metadata = _lstat(root)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise NativeHandoffStoreError("handoff state directory is invalid")
    _require_current_owner(metadata, "handoff state directory")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o700:
        try:
            root.chmod(0o700)
        except OSError as exc:
            raise NativeHandoffStoreError("handoff state directory permissions are invalid") from exc
        metadata = _lstat(root)
        if metadata is None or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise NativeHandoffStoreError("handoff state directory permissions are invalid")


def _reject_symlink_components(path: Path, *, allow_missing: bool) -> None:
    absolute = path.absolute()
    parts = absolute.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        metadata = _lstat(current)
        if metadata is None:
            if allow_missing:
                return
            raise NativeHandoffStoreError("handoff state directory is missing")
        if stat.S_ISLNK(metadata.st_mode):
            raise NativeHandoffStoreError("handoff state path contains a symbolic link")
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise NativeHandoffStoreError("handoff state parent is not a directory")


def _validate_existing_private_file(root: Path, path: Path) -> None:
    metadata = _lstat(path)
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise NativeHandoffStoreError("handoff state file must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise NativeHandoffStoreError("handoff state file is invalid")
    _require_current_owner(metadata, "handoff state file")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise NativeHandoffStoreError("handoff state file permissions must be 0600")
    root_metadata = _lstat(root)
    if root_metadata is None or not stat.S_ISDIR(root_metadata.st_mode):
        raise NativeHandoffStoreError("handoff state directory is invalid")


def _process_lock_for(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _open_private_lock_file(root: Path, path: Path) -> int:
    _validate_existing_private_file(root, path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise NativeHandoffStoreError("handoff state lock could not be opened") from exc
    try:
        opened = os.fstat(descriptor)
        current = _lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or current is None
            or stat.S_ISLNK(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise NativeHandoffStoreError("handoff state lock is invalid")
        _require_current_owner(opened, "handoff state lock")
        if os.name != "nt":
            if stat.S_IMODE(opened.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise NativeHandoffStoreError(
                    "handoff state lock permissions must be 0600"
                )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_persistent_lock(descriptor: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(descriptor, _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:
        # msvcrt locks a byte range from the current file position.  Ensure a
        # real byte exists so independent Windows processes contend on the
        # same range, then use its blocking lock mode.
        if os.fstat(descriptor).st_size < 1:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_LOCK, 1)
        return
    raise NativeHandoffStoreError("persistent file locking is unavailable")


def _release_persistent_lock(descriptor: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
        return
    raise NativeHandoffStoreError("persistent file locking is unavailable")


def _read_private_file(root: Path, path: Path, max_file_bytes: int) -> bytes:
    _validate_existing_private_file(root, path)
    before = _lstat(path)
    if before is None:
        return b""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise NativeHandoffStoreError("handoff state file could not be opened") from exc
    try:
        opened = os.fstat(descriptor)
        current = _lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or current is None
            or before.st_dev != opened.st_dev
            or before.st_ino != opened.st_ino
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise NativeHandoffStoreError("handoff state file changed while opening")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(max_file_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > max_file_bytes:
        raise NativeHandoffStoreError("handoff state exceeds bounded file size")
    return raw


def _atomic_write_private(root: Path, path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(root)
    )
    temporary = Path(temporary_name)
    try:
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "wb")
        except Exception:
            os.close(descriptor)
            raise
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_existing_private_file(root, path)
        os.replace(temporary, path)
        if os.name != "nt":
            current = _lstat(path)
            if current is None or stat.S_IMODE(current.st_mode) != 0o600:
                path.chmod(0o600)
        _fsync_directory(root)
    except NativeHandoffStoreError:
        raise
    except OSError as exc:
        raise NativeHandoffStoreError("handoff state could not be written atomically") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _require_current_owner(metadata: os.stat_result, label: str) -> None:
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and metadata.st_uid != getuid():
        raise NativeHandoffStoreError(f"{label} is not owned by the current user")


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NativeHandoffStoreError("handoff state path could not be inspected") from exc
