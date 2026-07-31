from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from typing import Callable, Iterator, Mapping
import zipfile

from .process import state_dir


ARTIFACT_SCHEMA_VERSION = 1
JOB_SCHEMA_VERSION = 1
ARTIFACT_METADATA_NAME = "artifact.json"
UPDATE_PHASES = frozenset(
    {
        "locking",
        "draining",
        "restoring_hooks",
        "updating_hermes",
        "reinstalling_hfc",
        "starting_services",
        "verifying",
        "succeeded",
        "failed",
        "cancelled",
    }
)
TERMINAL_UPDATE_PHASES = frozenset({"succeeded", "failed", "cancelled"})
_SAFE_RESULT_KEYS = frozenset(
    {
        "actual_head",
        "actual_version",
        "card_delivery",
        "error_code",
        "hermes_head",
        "hermes_version",
        "hfc_version",
        "import_origin",
        "message",
        "recovery_boundary",
        "service_status",
        "status",
    }
)
_MAX_STRING_CHARS = 4096
_EXPECTED_DISTRIBUTION = "hermes-feishu-streaming-card"


class MaintenanceRefused(ValueError):
    """Raised when maintenance evidence is incomplete or unsafe."""


@dataclass(frozen=True)
class MaintenancePaths:
    root: Path
    runtime: Path
    artifacts: Path
    jobs: Path
    lock: Path


@dataclass(frozen=True)
class ArtifactMetadata:
    schema_version: int
    distribution: str
    version: str
    sha256: str
    wheel_path: Path
    metadata_path: Path
    source_kind: str
    created_at: float


@dataclass(frozen=True)
class UpdateJob:
    schema_version: int
    job_id: str
    path: Path
    phase: str
    hermes_root: Path
    config_path: Path
    env_file: Path | None
    profile_id: str
    chat_id: str
    card_message_id: str
    operator_hash: str
    pre_update_version: str
    pre_update_head: str
    target_fingerprint: str
    artifact_version: str
    artifact_sha256: str
    artifact_path: Path
    attempts: dict[str, int]
    created_at: float
    updated_at: float
    result: dict[str, object]
    bot_id: str = "default"


def maintenance_paths(root: Path | None = None) -> MaintenancePaths:
    selected = (
        Path(root).expanduser()
        if root is not None
        else state_dir().expanduser() / "maintenance"
    ).resolve(strict=False)
    return MaintenancePaths(
        root=selected,
        runtime=selected / "runtime",
        artifacts=selected / "artifacts",
        jobs=selected / "jobs",
        lock=selected / "update.lock",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_wheel_artifact(
    paths: MaintenancePaths,
    wheel_path: Path,
    *,
    expected_version: str,
    source_kind: str = "unknown",
    now: Callable[[], float] = time.time,
) -> ArtifactMetadata:
    _prepare_paths(paths)
    source = Path(wheel_path).expanduser()
    if source.is_symlink():
        raise MaintenanceRefused("artifact wheel must not be a symlink")
    if not source.is_file():
        raise MaintenanceRefused("artifact wheel is missing")
    distribution, version = _wheel_identity(source)
    if _normalized_distribution(distribution) != _normalized_distribution(
        _EXPECTED_DISTRIBUTION
    ):
        raise MaintenanceRefused("artifact distribution mismatch")
    if version != _bounded_string(expected_version, "artifact expected version"):
        raise MaintenanceRefused("artifact version mismatch")
    safe_source_kind = _bounded_string(source_kind, "artifact source kind")
    destination = paths.artifacts / source.name
    _atomic_copy(source, destination)
    digest = file_sha256(destination)
    created_at = float(now())
    metadata_path = paths.artifacts / ARTIFACT_METADATA_NAME
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "distribution": _EXPECTED_DISTRIBUTION,
        "version": version,
        "sha256": digest,
        "wheel_filename": destination.name,
        "source_kind": safe_source_kind,
        "created_at": created_at,
    }
    _atomic_write_json(metadata_path, payload)
    return ArtifactMetadata(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        distribution=_EXPECTED_DISTRIBUTION,
        version=version,
        sha256=digest,
        wheel_path=destination.resolve(strict=False),
        metadata_path=metadata_path.resolve(strict=False),
        source_kind=safe_source_kind,
        created_at=created_at,
    )


def load_verified_artifact(
    paths: MaintenancePaths,
    *,
    expected_version: str | None = None,
) -> ArtifactMetadata:
    _prepare_paths(paths)
    metadata_path = paths.artifacts / ARTIFACT_METADATA_NAME
    payload = _load_json_file(metadata_path, label="artifact metadata")
    if payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise MaintenanceRefused("artifact metadata schema unsupported")
    distribution = _required_string(payload, "distribution", "artifact metadata")
    version = _required_string(payload, "version", "artifact metadata")
    digest = _required_string(payload, "sha256", "artifact metadata")
    wheel_filename = _required_string(
        payload, "wheel_filename", "artifact metadata"
    )
    source_kind = _required_string(payload, "source_kind", "artifact metadata")
    created_at = _safe_timestamp(payload.get("created_at"), "artifact metadata")
    if Path(wheel_filename).name != wheel_filename:
        raise MaintenanceRefused("artifact wheel filename is invalid")
    if _normalized_distribution(distribution) != _normalized_distribution(
        _EXPECTED_DISTRIBUTION
    ):
        raise MaintenanceRefused("artifact distribution mismatch")
    if expected_version is not None and version != expected_version:
        raise MaintenanceRefused("artifact version mismatch")
    wheel_path = paths.artifacts / wheel_filename
    if wheel_path.is_symlink() or not wheel_path.is_file():
        raise MaintenanceRefused("artifact wheel is missing")
    if file_sha256(wheel_path) != digest:
        raise MaintenanceRefused("artifact hash mismatch")
    wheel_distribution, wheel_version = _wheel_identity(wheel_path)
    if _normalized_distribution(wheel_distribution) != _normalized_distribution(
        distribution
    ):
        raise MaintenanceRefused("artifact distribution mismatch")
    if wheel_version != version:
        raise MaintenanceRefused("artifact version mismatch")
    return ArtifactMetadata(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        distribution=distribution,
        version=version,
        sha256=digest,
        wheel_path=wheel_path.resolve(strict=False),
        metadata_path=metadata_path.resolve(strict=False),
        source_kind=source_kind,
        created_at=created_at,
    )


def create_job(
    paths: MaintenancePaths,
    *,
    hermes_root: Path,
    config_path: Path,
    env_file: Path | None,
    profile_id: str,
    chat_id: str,
    card_message_id: str,
    operator_hash: str,
    pre_update_version: str,
    pre_update_head: str,
    target_fingerprint: str,
    artifact: ArtifactMetadata,
    bot_id: str = "default",
    job_id: str | None = None,
    now: Callable[[], float] = time.time,
) -> UpdateJob:
    _prepare_paths(paths)
    verified_artifact = load_verified_artifact(
        paths, expected_version=artifact.version
    )
    if verified_artifact.sha256 != artifact.sha256:
        raise MaintenanceRefused("artifact hash mismatch")
    selected_job_id = (
        _bounded_string(job_id, "job id")
        if job_id is not None
        else secrets.token_urlsafe(18)
    )
    if not all(char.isalnum() or char in {"-", "_"} for char in selected_job_id):
        raise MaintenanceRefused("job id is invalid")
    job_path = paths.jobs / f"{selected_job_id}.json"
    if job_path.exists() or job_path.is_symlink():
        raise MaintenanceRefused("job id collision")
    timestamp = float(now())
    job = UpdateJob(
        schema_version=JOB_SCHEMA_VERSION,
        job_id=selected_job_id,
        path=job_path.resolve(strict=False),
        phase="locking",
        hermes_root=_absolute_path(hermes_root, "Hermes root"),
        config_path=_absolute_path(config_path, "config path"),
        env_file=(
            _absolute_path(env_file, "env file") if env_file is not None else None
        ),
        profile_id=_bounded_string(profile_id, "profile id"),
        chat_id=_bounded_string(chat_id, "chat id"),
        card_message_id=_bounded_string(card_message_id, "card message id"),
        operator_hash=_bounded_string(operator_hash, "operator hash"),
        pre_update_version=_bounded_string(
            pre_update_version, "pre-update version"
        ),
        pre_update_head=_bounded_string(pre_update_head, "pre-update head"),
        target_fingerprint=_bounded_string(
            target_fingerprint, "target fingerprint"
        ),
        artifact_version=verified_artifact.version,
        artifact_sha256=verified_artifact.sha256,
        artifact_path=verified_artifact.wheel_path,
        attempts={},
        created_at=timestamp,
        updated_at=timestamp,
        result={},
        bot_id=_bounded_string(bot_id, "bot id"),
    )
    _atomic_write_json(job.path, _job_payload(job))
    return job


def load_job(path: Path, *, require_private: bool = True) -> UpdateJob:
    selected = Path(path).expanduser()
    if selected.is_symlink():
        raise MaintenanceRefused("job path must not be a symlink")
    if not selected.is_file():
        raise MaintenanceRefused("job file is missing")
    if require_private:
        _require_private_file(selected, "job file")
    payload = _load_json_file(selected, label="job")
    expected_keys = {
        "schema_version",
        "job_id",
        "phase",
        "hermes_root",
        "config_path",
        "env_file",
        "profile_id",
        "chat_id",
        "card_message_id",
        "operator_hash",
        "pre_update_version",
        "pre_update_head",
        "target_fingerprint",
        "artifact_version",
        "artifact_sha256",
        "artifact_path",
        "attempts",
        "created_at",
        "updated_at",
        "result",
        "bot_id",
    }
    if set(payload) != expected_keys:
        raise MaintenanceRefused("job schema fields are invalid")
    if payload.get("schema_version") != JOB_SCHEMA_VERSION:
        raise MaintenanceRefused("job schema unsupported")
    job_id = _required_string(payload, "job_id", "job")
    if selected.name != f"{job_id}.json":
        raise MaintenanceRefused("job path does not match job id")
    phase = _required_string(payload, "phase", "job")
    if phase not in UPDATE_PHASES:
        raise MaintenanceRefused("job phase is invalid")
    attempts = _safe_attempts(payload.get("attempts"))
    result = _safe_result(payload.get("result"))
    env_value = payload.get("env_file")
    if env_value is not None and not isinstance(env_value, str):
        raise MaintenanceRefused("job env file is invalid")
    return UpdateJob(
        schema_version=JOB_SCHEMA_VERSION,
        job_id=job_id,
        path=selected.resolve(strict=False),
        phase=phase,
        hermes_root=_serialized_absolute_path(payload, "hermes_root"),
        config_path=_serialized_absolute_path(payload, "config_path"),
        env_file=(
            _absolute_path(Path(env_value), "env file") if env_value else None
        ),
        profile_id=_required_string(payload, "profile_id", "job"),
        chat_id=_required_string(payload, "chat_id", "job"),
        card_message_id=_required_string(payload, "card_message_id", "job"),
        operator_hash=_required_string(payload, "operator_hash", "job"),
        pre_update_version=_required_string(
            payload, "pre_update_version", "job"
        ),
        pre_update_head=_required_string(payload, "pre_update_head", "job"),
        target_fingerprint=_required_string(
            payload, "target_fingerprint", "job"
        ),
        artifact_version=_required_string(payload, "artifact_version", "job"),
        artifact_sha256=_required_string(payload, "artifact_sha256", "job"),
        artifact_path=_serialized_absolute_path(payload, "artifact_path"),
        attempts=attempts,
        created_at=_safe_timestamp(payload.get("created_at"), "job"),
        updated_at=_safe_timestamp(payload.get("updated_at"), "job"),
        result=result,
        bot_id=_required_string(payload, "bot_id", "job"),
    )


def transition_job(
    path: Path,
    *,
    expected_phase: str,
    phase: str,
    result: Mapping[str, object] | None = None,
    now: Callable[[], float] = time.time,
) -> UpdateJob:
    if phase not in UPDATE_PHASES:
        raise MaintenanceRefused("job phase is invalid")
    current = load_job(path)
    if current.phase != expected_phase:
        raise MaintenanceRefused("job phase changed")
    attempts = dict(current.attempts)
    attempts[phase] = attempts.get(phase, 0) + 1
    safe_result = _safe_result(dict(result) if result is not None else current.result)
    updated = UpdateJob(
        **{
            **current.__dict__,
            "phase": phase,
            "attempts": attempts,
            "updated_at": float(now()),
            "result": safe_result,
        }
    )
    latest = load_job(path)
    if latest.phase != expected_phase or latest.updated_at != current.updated_at:
        raise MaintenanceRefused("job phase changed")
    _atomic_write_json(current.path, _job_payload(updated))
    return updated


@contextmanager
def acquire_update_lock(
    paths: MaintenancePaths,
    *,
    job_id: str,
) -> Iterator[Path]:
    _prepare_paths(paths)
    safe_job_id = _bounded_string(job_id, "job id")
    descriptor = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MaintenanceRefused("update already in progress") from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise MaintenanceRefused("update already in progress") from exc
        os.fchmod(descriptor, 0o600) if hasattr(os, "fchmod") else None
        os.ftruncate(descriptor, 0)
        os.write(descriptor, (safe_job_id + "\n").encode("utf-8"))
        os.fsync(descriptor)
        yield paths.lock
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def prune_jobs(
    paths: MaintenancePaths,
    *,
    now: float | None = None,
    max_terminal: int = 5,
    max_age_seconds: float = 7 * 24 * 60 * 60,
) -> None:
    _prepare_paths(paths)
    if max_terminal < 0:
        raise ValueError("max_terminal must be non-negative")
    current_time = time.time() if now is None else float(now)
    terminal: list[UpdateJob] = []
    for path in paths.jobs.glob("*.json"):
        try:
            job = load_job(path)
        except MaintenanceRefused:
            continue
        if job.phase in TERMINAL_UPDATE_PHASES:
            terminal.append(job)
    terminal.sort(key=lambda item: (item.updated_at, item.job_id), reverse=True)
    retained = 0
    for job in terminal:
        expired = current_time - job.updated_at > max_age_seconds
        over_capacity = retained >= max_terminal
        if expired or over_capacity:
            _unlink_regular_job(job.path)
        else:
            retained += 1


def _prepare_paths(paths: MaintenancePaths) -> None:
    for directory in (paths.root, paths.runtime, paths.artifacts, paths.jobs):
        if directory.is_symlink():
            raise MaintenanceRefused("maintenance directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise MaintenanceRefused("maintenance path is not a directory")
        try:
            directory.chmod(0o700)
        except OSError as exc:
            raise MaintenanceRefused(
                "maintenance directory permissions could not be secured"
            ) from exc


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
                and "/" not in name[: -len("/METADATA")].split(".dist-info", 1)[0]
            ]
            if len(candidates) != 1:
                raise MaintenanceRefused("artifact metadata is invalid")
            contents = archive.read(candidates[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise MaintenanceRefused("artifact wheel is invalid") from exc
    metadata = Parser().parsestr(contents)
    distribution = str(metadata.get("Name") or "").strip()
    version = str(metadata.get("Version") or "").strip()
    if not distribution or not version:
        raise MaintenanceRefused("artifact metadata is invalid")
    return distribution, version


def _normalized_distribution(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(".", "-")


def _atomic_copy(source: Path, destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            if hasattr(os, "fchmod"):
                os.fchmod(writer.fileno(), 0o600)
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if destination.is_symlink():
            raise MaintenanceRefused("artifact destination must not be a symlink")
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.is_symlink():
        raise MaintenanceRefused("maintenance file must not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise MaintenanceRefused("maintenance file must not be a symlink")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json_file(path: Path, *, label: str) -> dict[str, object]:
    if path.is_symlink():
        raise MaintenanceRefused(f"{label} must not be a symlink")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaintenanceRefused(f"{label} is invalid") from exc
    if not isinstance(data, dict):
        raise MaintenanceRefused(f"{label} is invalid")
    return data


def _require_private_file(path: Path, label: str) -> None:
    if os.name == "nt":
        return
    try:
        info = path.stat()
    except OSError as exc:
        raise MaintenanceRefused(f"{label} is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise MaintenanceRefused(f"{label} must be a regular file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise MaintenanceRefused(f"{label} permissions are not private")
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        raise MaintenanceRefused(f"{label} owner is invalid")


def _required_string(
    payload: Mapping[str, object],
    key: str,
    label: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MaintenanceRefused(f"{label} {key} is invalid")
    return _bounded_string(value, f"{label} {key}")


def _bounded_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise MaintenanceRefused(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_STRING_CHARS or "\x00" in normalized:
        raise MaintenanceRefused(f"{label} is invalid")
    return normalized


def _absolute_path(value: Path, label: str) -> Path:
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        selected = selected.resolve(strict=False)
    resolved = selected.resolve(strict=False)
    if "\x00" in str(resolved):
        raise MaintenanceRefused(f"{label} is invalid")
    return resolved


def _serialized_absolute_path(
    payload: Mapping[str, object],
    key: str,
) -> Path:
    value = _required_string(payload, key, "job")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise MaintenanceRefused(f"job {key} must be absolute")
    return path.resolve(strict=False)


def _safe_timestamp(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaintenanceRefused(f"{label} timestamp is invalid")
    timestamp = float(value)
    if timestamp < 0 or timestamp == float("inf") or timestamp != timestamp:
        raise MaintenanceRefused(f"{label} timestamp is invalid")
    return timestamp


def _safe_attempts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise MaintenanceRefused("job attempts are invalid")
    attempts: dict[str, int] = {}
    for key, count in value.items():
        if key not in UPDATE_PHASES:
            raise MaintenanceRefused("job attempts are invalid")
        if isinstance(count, bool) or not isinstance(count, int) or not (0 <= count <= 9):
            raise MaintenanceRefused("job attempts are invalid")
        attempts[key] = count
    return attempts


def _safe_result(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MaintenanceRefused("job result is invalid")
    result: dict[str, object] = {}
    for key, item in value.items():
        if key not in _SAFE_RESULT_KEYS:
            raise MaintenanceRefused("unsafe job result key")
        if isinstance(item, str):
            result[key] = _bounded_string(item, f"job result {key}")
        elif isinstance(item, bool) or item is None:
            result[key] = item
        elif isinstance(item, int) and not isinstance(item, bool):
            result[key] = item
        else:
            raise MaintenanceRefused("job result value is invalid")
    return result


def _job_payload(job: UpdateJob) -> dict[str, object]:
    return {
        "schema_version": job.schema_version,
        "job_id": job.job_id,
        "phase": job.phase,
        "hermes_root": str(job.hermes_root),
        "config_path": str(job.config_path),
        "env_file": str(job.env_file) if job.env_file is not None else None,
        "profile_id": job.profile_id,
        "chat_id": job.chat_id,
        "card_message_id": job.card_message_id,
        "operator_hash": job.operator_hash,
        "pre_update_version": job.pre_update_version,
        "pre_update_head": job.pre_update_head,
        "target_fingerprint": job.target_fingerprint,
        "artifact_version": job.artifact_version,
        "artifact_sha256": job.artifact_sha256,
        "artifact_path": str(job.artifact_path),
        "attempts": dict(job.attempts),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "result": dict(job.result),
        "bot_id": job.bot_id,
    }


def _unlink_regular_job(path: Path) -> None:
    if path.is_symlink():
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        return
