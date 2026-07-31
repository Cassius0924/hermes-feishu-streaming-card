from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Sequence

from .install.detect import detect_hermes
from .install.recovery import plan_recovery
from .maintenance_store import (
    ArtifactMetadata,
    MaintenanceRefused,
    UpdateJob,
    acquire_update_lock,
    file_sha256,
    load_job,
    load_verified_artifact,
    maintenance_paths,
    transition_job,
)


UPDATE_CHECK_TIMEOUT_SECONDS = 60.0
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|password|authorization)\s*=\s*\S+"
)
_UNMERGED_CODES = frozenset(
    {
        "DD",
        "AU",
        "UD",
        "UA",
        "DU",
        "AA",
        "UU",
    }
)
_RUNTIME_IMPORT_PROBE = (
    "import json, pathlib, hermes_feishu_card; "
    "print(json.dumps({'version': hermes_feishu_card.__version__, "
    "'location': str(pathlib.Path(hermes_feishu_card.__file__).resolve())}))"
)
_TERMINAL_PHASES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class UpdateInspection:
    ready: bool
    reason_code: str
    current_version: str
    current_head: str
    target_summary: str
    target_fingerprint: str
    hfc_version: str
    artifact_sha256: str
    active_sessions: int
    requires_drain: bool
    hook_state: str
    hook_fingerprint: str
    maintenance_ready: bool
    changed_paths: tuple[str, ...]
    created_at: float

    @property
    def fingerprint(self) -> str:
        return inspection_fingerprint(self)


CommandRunner = Callable[[Sequence[str], float], CommandResult]
HealthFetcher = Callable[[], dict[str, object] | None]
JobPublisher = Callable[[UpdateJob], bool]


def inspect_update(
    *,
    hermes_root: Path,
    artifact: ArtifactMetadata,
    installed_hfc_version: str,
    active_sessions: int,
    run: CommandRunner | None = None,
    now: Callable[[], float] = time.time,
) -> UpdateInspection:
    root = Path(hermes_root).expanduser().resolve(strict=False)
    runner = run or run_command
    created_at = float(now())
    hfc_version = str(installed_hfc_version or "").strip()
    session_count = max(0, int(active_sessions))

    def result(
        ready: bool,
        reason_code: str,
        *,
        current_version: str = "",
        current_head: str = "",
        target_summary: str = "",
        target_fingerprint: str = "",
        hook_state: str = "",
        hook_fingerprint: str = "",
        changed_paths: tuple[str, ...] = (),
    ) -> UpdateInspection:
        return UpdateInspection(
            ready=ready,
            reason_code=reason_code,
            current_version=_safe_short(current_version, 80),
            current_head=_safe_short(current_head, 80),
            target_summary=_safe_short(target_summary, 240),
            target_fingerprint=_safe_fingerprint(target_fingerprint),
            hfc_version=_safe_short(hfc_version, 80),
            artifact_sha256=_safe_fingerprint(artifact.sha256),
            active_sessions=session_count,
            requires_drain=session_count > 0,
            hook_state=_safe_short(hook_state, 80),
            hook_fingerprint=_safe_fingerprint(hook_fingerprint),
            maintenance_ready=artifact.version == hfc_version,
            changed_paths=tuple(_safe_relative_path(item) for item in changed_paths),
            created_at=created_at,
        )

    if not hfc_version or artifact.version != hfc_version:
        return result(False, "artifact_version_mismatch")
    if not _is_sha256(artifact.sha256):
        return result(False, "artifact_hash_invalid")

    try:
        detection = detect_hermes(root)
    except Exception:
        return result(False, "hermes_detection_failed")
    version = str(getattr(detection, "version", "") or "")
    if not bool(getattr(detection, "supported", False)) or str(
        getattr(detection, "compatibility", "")
    ) != "full":
        return result(
            False,
            "hermes_not_fully_supported",
            current_version=version,
        )

    if _git_operation_incomplete(root):
        return result(
            False,
            "git_operation_incomplete",
            current_version=version,
        )

    try:
        recovery = plan_recovery(detection)
    except Exception:
        return result(
            False,
            "hook_evidence_unavailable",
            current_version=version,
        )
    hook_state = str(getattr(recovery, "state", "") or "")
    hook_fingerprint = str(getattr(recovery, "fingerprint", "") or "")
    findings = tuple(getattr(recovery, "findings", ()) or ())
    if (
        hook_state != "installed"
        or tuple(getattr(recovery, "actions", ()) or ())
        or any(str(getattr(item, "severity", "")) == "error" for item in findings)
    ):
        return result(
            False,
            "hook_state_unverified",
            current_version=version,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
        )

    head_result = runner(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        20.0,
    )
    if head_result.timed_out or head_result.returncode != 0:
        return result(
            False,
            "git_head_unavailable",
            current_version=version,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
        )
    current_head = _first_line(head_result.stdout)
    if not current_head:
        return result(
            False,
            "git_head_unavailable",
            current_version=version,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
        )

    status_result = runner(
        (
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        ),
        20.0,
    )
    if status_result.timed_out or status_result.returncode != 0:
        return result(
            False,
            "git_status_unavailable",
            current_version=version,
            current_head=current_head,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
        )
    status_entries = _parse_porcelain(status_result.stdout)
    if any(code in _UNMERGED_CODES or "U" in code for code, _path in status_entries):
        return result(
            False,
            "git_operation_incomplete",
            current_version=version,
            current_head=current_head,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
        )
    owned_paths = _owned_hook_paths(root, detection)
    unrelated = tuple(
        sorted(
            path
            for _code, path in status_entries
            if path not in owned_paths
        )
    )
    if unrelated:
        return result(
            False,
            "unrelated_tracked_changes",
            current_version=version,
            current_head=current_head,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
            changed_paths=unrelated,
        )

    update_result = runner(
        ("hermes", "update", "--check"),
        UPDATE_CHECK_TIMEOUT_SECONDS,
    )
    if update_result.timed_out:
        return result(
            False,
            "update_check_timeout",
            current_version=version,
            current_head=current_head,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
        )
    if update_result.returncode != 0:
        return result(
            False,
            "update_check_failed",
            current_version=version,
            current_head=current_head,
            hook_state=hook_state,
            hook_fingerprint=hook_fingerprint,
        )
    normalized_target = _safe_short(update_result.stdout, 240)
    target_fingerprint = hashlib.sha256(
        _normalized_output(update_result.stdout).encode("utf-8")
    ).hexdigest()
    return result(
        True,
        "ready",
        current_version=version,
        current_head=current_head,
        target_summary=normalized_target,
        target_fingerprint=target_fingerprint,
        hook_state=hook_state,
        hook_fingerprint=hook_fingerprint,
    )


def inspection_fingerprint(inspection: UpdateInspection) -> str:
    payload = {
        "ready": inspection.ready,
        "reason_code": inspection.reason_code,
        "current_version": inspection.current_version,
        "current_head": inspection.current_head,
        "target_fingerprint": inspection.target_fingerprint,
        "hfc_version": inspection.hfc_version,
        "artifact_sha256": inspection.artifact_sha256,
        "hook_state": inspection.hook_state,
        "hook_fingerprint": inspection.hook_fingerprint,
        "maintenance_ready": inspection.maintenance_ready,
        "changed_paths": list(inspection.changed_paths),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    normalized = tuple(str(value) for value in argv)
    try:
        completed = subprocess.run(
            list(normalized),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(normalized, -1, "", "", timed_out=True)
    except (OSError, ValueError):
        return CommandResult(normalized, -1, "", "")
    return CommandResult(
        argv=normalized,
        returncode=int(completed.returncode),
        stdout=str(completed.stdout or ""),
        stderr=str(completed.stderr or ""),
    )


def detect_runtime_python(hermes_root: Path) -> Path | None:
    root = Path(hermes_root).expanduser()
    candidates = (
        root / "venv" / "bin" / "python",
        root / "venv" / "bin" / "python3",
        root / ".venv" / "bin" / "python",
        root / ".venv" / "bin" / "python3",
        root / "venv" / "Scripts" / "python.exe",
        root / ".venv" / "Scripts" / "python.exe",
        root / "gateway" / "venv" / "bin" / "python",
        root / "gateway" / "venv" / "bin" / "python3",
        root / "gateway" / ".venv" / "bin" / "python",
        root / "gateway" / ".venv" / "bin" / "python3",
        root / "gateway" / "venv" / "Scripts" / "python.exe",
        root / "gateway" / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_symlink():
            continue
        if candidate.is_file():
            return candidate.resolve(strict=False)
    return None


def run_job(
    job_path: Path,
    *,
    run: CommandRunner | None = None,
    fetch_health: HealthFetcher | None = None,
    publish: JobPublisher | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    maintenance_python: Path | None = None,
    drain_timeout_seconds: float = 180.0,
    update_timeout_seconds: float = 3600.0,
) -> UpdateJob:
    runner = run or run_command
    health_fetcher = fetch_health or (lambda: None)
    publisher = publish or (lambda current: True)
    selected_python = Path(maintenance_python or sys.executable).resolve(
        strict=False
    )
    current = load_job(job_path)
    if current.phase in _TERMINAL_PHASES:
        return current
    paths = maintenance_paths(current.path.parent.parent)
    with acquire_update_lock(paths, job_id=current.job_id):
        current = load_job(current.path)
        if current.phase in _TERMINAL_PHASES:
            return current
        try:
            artifact = load_verified_artifact(
                paths,
                expected_version=current.artifact_version,
            )
        except MaintenanceRefused:
            return _fail_job(
                current,
                "artifact_verification_failed",
                "no_mutation",
                publisher,
            )
        if (
            artifact.sha256 != current.artifact_sha256
            or artifact.wheel_path != current.artifact_path
            or file_sha256(current.artifact_path) != current.artifact_sha256
        ):
            return _fail_job(
                current,
                "artifact_verification_failed",
                "no_mutation",
                publisher,
            )

        if current.phase == "locking":
            if not _publish_job(publisher, current):
                return _fail_job(
                    current,
                    "card_update_failed",
                    "no_mutation",
                    publisher=None,
                )
            health = _safe_health(health_fetcher)
            active_sessions = _active_maintenance_sessions(health)
            inspection = inspect_update(
                hermes_root=current.hermes_root,
                artifact=artifact,
                installed_hfc_version=current.artifact_version,
                active_sessions=active_sessions,
                run=runner,
            )
            if (
                not inspection.ready
                or inspection.current_head != current.pre_update_head
                or inspection.target_fingerprint != current.target_fingerprint
                or inspection.hfc_version != current.artifact_version
                or inspection.artifact_sha256 != current.artifact_sha256
            ):
                return _fail_job(
                    current,
                    "preflight_evidence_changed",
                    "no_mutation",
                    publisher,
                )
            current = _advance_job(
                current,
                "draining",
                publisher,
                require_delivery=True,
            )
            if current.phase == "failed":
                return current

        if current.phase == "draining":
            if not _wait_for_drain(
                health_fetcher,
                timeout_seconds=drain_timeout_seconds,
                sleep=sleep,
                monotonic=monotonic,
            ):
                return _fail_job(
                    current,
                    "active_sessions_timeout",
                    "no_mutation",
                    publisher,
                )
            current = _advance_job(
                current,
                "restoring_hooks",
                publisher,
                require_delivery=True,
            )
            if current.phase == "failed":
                return current

        if current.phase == "restoring_hooks":
            stop = runner(
                tuple(
                    _cli_command(
                        selected_python,
                        "stop",
                        config=current.config_path,
                        env_file=current.env_file,
                    )
                ),
                60.0,
            )
            if stop.timed_out or stop.returncode != 0:
                return _fail_job(
                    current,
                    "sidecar_stop_failed",
                    "old_hfc_or_manual",
                    publisher,
                )
            restore = runner(
                (
                    str(selected_python),
                    "-I",
                    "-m",
                    "hermes_feishu_card.cli",
                    "restore",
                    "--hermes-dir",
                    str(current.hermes_root),
                    "--yes",
                ),
                120.0,
            )
            if restore.timed_out or restore.returncode != 0:
                return _fail_job(
                    current,
                    "hook_restore_failed",
                    "old_hfc_or_manual",
                    publisher,
                )
            clean_status = _git_status(current.hermes_root, runner)
            if clean_status is None or clean_status:
                return _fail_job(
                    current,
                    "tracked_changes_after_restore",
                    "old_hfc_or_manual",
                    publisher,
                )
            current = _advance_job(
                current,
                "updating_hermes",
                publisher,
                require_delivery=False,
            )

        if current.phase == "updating_hermes":
            actual_head = _git_head(current.hermes_root, runner)
            if not actual_head:
                return _fail_job(
                    current,
                    "git_head_unavailable",
                    "updater_result_classified",
                    publisher,
                )
            if actual_head == current.pre_update_head:
                updated = runner(
                    ("hermes", "update", "--yes"),
                    update_timeout_seconds,
                )
                if updated.timed_out:
                    return _fail_job(
                        current,
                        "hermes_update_timeout",
                        "updater_result_classified",
                        publisher,
                    )
                if updated.returncode != 0:
                    restored = _restore_old_hfc_after_update_failure(
                        current,
                        runner,
                    )
                    return _fail_job(
                        current,
                        "hermes_update_failed",
                        (
                            "old_hfc_restored"
                            if restored
                            else "updater_result_classified"
                        ),
                        publisher,
                    )
                actual_head = _git_head(current.hermes_root, runner)
                if not actual_head:
                    return _fail_job(
                        current,
                        "post_update_head_unavailable",
                        "updater_result_classified",
                        publisher,
                    )
            current = _advance_job(
                current,
                "reinstalling_hfc",
                publisher,
                require_delivery=False,
            )

        if current.phase == "reinstalling_hfc":
            post_detection = _supported_full_detection(current.hermes_root)
            if post_detection is None or _update_marker_present(current.hermes_root):
                return _fail_job(
                    current,
                    "post_update_hermes_invalid",
                    "native_hermes",
                    publisher,
                )
            post_status = _git_status(current.hermes_root, runner)
            if post_status is None or post_status:
                return _fail_job(
                    current,
                    "post_update_tracked_changes",
                    "native_hermes",
                    publisher,
                )
            runtime_python = detect_runtime_python(current.hermes_root)
            if runtime_python is None:
                return _fail_job(
                    current,
                    "hermes_runtime_missing",
                    "native_hermes",
                    publisher,
                )
            installed = runner(
                (
                    str(runtime_python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--force-reinstall",
                    str(current.artifact_path),
                ),
                300.0,
            )
            if installed.timed_out or installed.returncode != 0:
                return _fail_job(
                    current,
                    "hfc_wheel_install_failed",
                    "native_hermes",
                    publisher,
                )
            install_hook = runner(
                (
                    str(runtime_python),
                    "-I",
                    "-m",
                    "hermes_feishu_card.cli",
                    "install",
                    "--hermes-dir",
                    str(current.hermes_root),
                    "--yes",
                ),
                180.0,
            )
            if install_hook.timed_out or install_hook.returncode != 0:
                return _fail_job(
                    current,
                    "hfc_hook_install_failed",
                    "native_hermes",
                    publisher,
                )
            if not _installed_hook_verified(current.hermes_root):
                return _fail_job(
                    current,
                    "hfc_hook_verification_failed",
                    "native_hermes",
                    publisher,
                )
            current = _advance_job(
                current,
                "starting_services",
                publisher,
                require_delivery=False,
            )

        if current.phase == "starting_services":
            runtime_python = detect_runtime_python(current.hermes_root)
            if runtime_python is None:
                return _fail_job(
                    current,
                    "hermes_runtime_missing",
                    "service_recovery",
                    publisher,
                )
            start = runner(
                tuple(
                    _cli_command(
                        runtime_python,
                        "start",
                        config=current.config_path,
                        env_file=current.env_file,
                        hermes_root=current.hermes_root,
                    )
                ),
                120.0,
            )
            if start.timed_out or start.returncode != 0:
                return _fail_job(
                    current,
                    "sidecar_start_failed",
                    "service_recovery",
                    publisher,
                )
            restart = runner(
                ("hermes", "gateway", "restart"),
                120.0,
            )
            if restart.timed_out or restart.returncode != 0:
                return _fail_job(
                    current,
                    "gateway_restart_failed",
                    "service_recovery",
                    publisher,
                )
            current = _advance_job(
                current,
                "verifying",
                publisher,
                require_delivery=False,
            )

        if current.phase == "verifying":
            runtime_python = detect_runtime_python(current.hermes_root)
            if runtime_python is None:
                return _fail_job(
                    current,
                    "hermes_runtime_missing",
                    "service_recovery",
                    publisher,
                )
            import_result = runner(
                (str(runtime_python), "-I", "-c", _RUNTIME_IMPORT_PROBE),
                30.0,
            )
            import_ok, import_origin = _verified_import(
                import_result,
                runtime_python,
                expected_version=current.artifact_version,
            )
            if not import_ok:
                return _fail_job(
                    current,
                    "runtime_import_verification_failed",
                    "service_recovery",
                    publisher,
                )
            health = _wait_for_ready_health(
                health_fetcher,
                sleep=sleep,
                monotonic=monotonic,
            )
            if health is None:
                return _fail_job(
                    current,
                    "runtime_readiness_timeout",
                    "service_recovery",
                    publisher,
                )
            detection = _supported_full_detection(current.hermes_root)
            if detection is None or not _installed_hook_verified(
                current.hermes_root
            ):
                return _fail_job(
                    current,
                    "final_hook_verification_failed",
                    "service_recovery",
                    publisher,
                )
            actual_head = _git_head(current.hermes_root, runner)
            result = {
                "hermes_version": str(getattr(detection, "version", "") or "unknown"),
                "hermes_head": actual_head or "unknown",
                "hfc_version": current.artifact_version,
                "import_origin": import_origin,
                "service_status": "ready",
                "status": "succeeded",
            }
            current = transition_job(
                current.path,
                expected_phase="verifying",
                phase="succeeded",
                result=result,
            )
            _publish_job(publisher, current)
        return current


def _git_operation_incomplete(root: Path) -> bool:
    git_dir = root / ".git"
    return any(
        (git_dir / name).exists() or (git_dir / name).is_symlink()
        for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply")
    )


def _publish_job(publisher: JobPublisher, job: UpdateJob) -> bool:
    try:
        return publisher(job) is True
    except Exception:
        return False


def _advance_job(
    current: UpdateJob,
    phase: str,
    publisher: JobPublisher,
    *,
    require_delivery: bool,
) -> UpdateJob:
    advanced = transition_job(
        current.path,
        expected_phase=current.phase,
        phase=phase,
    )
    delivered = _publish_job(publisher, advanced)
    if require_delivery and not delivered:
        return _fail_job(
            advanced,
            "card_update_failed",
            "no_mutation",
            publisher=None,
        )
    return advanced


def _fail_job(
    current: UpdateJob,
    error_code: str,
    recovery_boundary: str,
    publisher: JobPublisher | None,
) -> UpdateJob:
    if current.phase in _TERMINAL_PHASES:
        return current
    try:
        failed = transition_job(
            current.path,
            expected_phase=current.phase,
            phase="failed",
            result={
                "error_code": error_code,
                "recovery_boundary": recovery_boundary,
                "status": "failed",
            },
        )
    except MaintenanceRefused:
        return load_job(current.path)
    if publisher is not None:
        _publish_job(publisher, failed)
    return failed


def _safe_health(fetch: HealthFetcher) -> dict[str, object]:
    try:
        value = fetch()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _active_maintenance_sessions(health: dict[str, object]) -> int:
    value = health.get("maintenance_active_sessions", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _wait_for_drain(
    fetch: HealthFetcher,
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> bool:
    deadline = monotonic() + max(0.0, timeout_seconds)
    while True:
        health = _safe_health(fetch)
        if health and _active_maintenance_sessions(health) == 0:
            return True
        if monotonic() >= deadline:
            return False
        sleep(min(1.0, max(0.0, deadline - monotonic())))


def _wait_for_ready_health(
    fetch: HealthFetcher,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout_seconds: float = 120.0,
) -> dict[str, object] | None:
    deadline = monotonic() + timeout_seconds
    while True:
        health = _safe_health(fetch)
        readiness = health.get("readiness")
        ready = (
            health.get("status") == "healthy"
            and isinstance(readiness, dict)
            and readiness.get("status") == "ready"
            and readiness.get("runtime_ready") is True
        )
        if ready:
            return health
        if monotonic() >= deadline:
            return None
        sleep(min(1.0, max(0.0, deadline - monotonic())))


def _cli_command(
    python: Path,
    command: str,
    *,
    config: Path,
    env_file: Path | None,
    hermes_root: Path | None = None,
) -> list[str]:
    argv = [
        str(python),
        "-I",
        "-m",
        "hermes_feishu_card.cli",
        command,
        "--config",
        str(config),
    ]
    if env_file is not None:
        argv.extend(("--env-file", str(env_file)))
    if hermes_root is not None:
        argv.extend(("--hermes-dir", str(hermes_root)))
    return argv


def _git_head(root: Path, run: CommandRunner) -> str:
    result = run(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        20.0,
    )
    if result.timed_out or result.returncode != 0:
        return ""
    return _first_line(result.stdout)


def _git_status(root: Path, run: CommandRunner) -> tuple[str, ...] | None:
    result = run(
        (
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        ),
        20.0,
    )
    if result.timed_out or result.returncode != 0:
        return None
    return tuple(
        path
        for _code, path in _parse_porcelain(result.stdout)
        if path
    )


def _supported_full_detection(root: Path) -> object | None:
    try:
        detection = detect_hermes(root)
    except Exception:
        return None
    if not bool(getattr(detection, "supported", False)):
        return None
    if str(getattr(detection, "compatibility", "")) != "full":
        return None
    return detection


def _installed_hook_verified(root: Path) -> bool:
    detection = _supported_full_detection(root)
    if detection is None:
        return False
    try:
        plan = plan_recovery(detection)
    except Exception:
        return False
    findings = tuple(getattr(plan, "findings", ()) or ())
    return (
        str(getattr(plan, "state", "")) == "installed"
        and not tuple(getattr(plan, "actions", ()) or ())
        and not any(
            str(getattr(item, "severity", "")) == "error"
            for item in findings
        )
    )


def _update_marker_present(root: Path) -> bool:
    candidates = (
        root / ".update_pending.json",
        root / ".hermes_update_pending.json",
    )
    return any(path.exists() or path.is_symlink() for path in candidates)


def _verified_import(
    result: CommandResult,
    runtime_python: Path,
    *,
    expected_version: str,
) -> tuple[bool, str]:
    if result.timed_out or result.returncode != 0:
        return False, ""
    try:
        payload = json.loads(str(result.stdout or "").strip())
    except json.JSONDecodeError:
        return False, ""
    if not isinstance(payload, dict):
        return False, ""
    if str(payload.get("version") or "") != expected_version:
        return False, ""
    location_text = str(payload.get("location") or "").strip()
    if not location_text:
        return False, ""
    location = Path(location_text).resolve(strict=False)
    try:
        runtime_root = runtime_python.resolve(strict=False).parent.parent
        location.relative_to(runtime_root)
    except ValueError:
        return False, ""
    if "site-packages" not in location.parts:
        return False, ""
    return True, "site-packages"


def _restore_old_hfc_after_update_failure(
    job: UpdateJob,
    run: CommandRunner,
) -> bool:
    if _git_head(job.hermes_root, run) != job.pre_update_head:
        return False
    if _supported_full_detection(job.hermes_root) is None:
        return False
    status = _git_status(job.hermes_root, run)
    if status is None or status:
        return False
    runtime_python = detect_runtime_python(job.hermes_root)
    if runtime_python is None:
        return False
    install_package = run(
        (
            str(runtime_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            str(job.artifact_path),
        ),
        300.0,
    )
    if install_package.timed_out or install_package.returncode != 0:
        return False
    install_hook = run(
        (
            str(runtime_python),
            "-I",
            "-m",
            "hermes_feishu_card.cli",
            "install",
            "--hermes-dir",
            str(job.hermes_root),
            "--yes",
        ),
        180.0,
    )
    if install_hook.timed_out or install_hook.returncode != 0:
        return False
    if not _installed_hook_verified(job.hermes_root):
        return False
    start = run(
        tuple(
            _cli_command(
                runtime_python,
                "start",
                config=job.config_path,
                env_file=job.env_file,
                hermes_root=job.hermes_root,
            )
        ),
        120.0,
    )
    if start.timed_out or start.returncode != 0:
        return False
    restart = run(("hermes", "gateway", "restart"), 120.0)
    return not restart.timed_out and restart.returncode == 0


def _owned_hook_paths(root: Path, detection: object) -> frozenset[str]:
    owned: set[str] = set()
    for attribute, exists_attribute in (
        ("run_py", "run_py_exists"),
        ("cron_py", "cron_py_exists"),
        ("base_py", "base_py_exists"),
    ):
        path = getattr(detection, attribute, None)
        exists = getattr(detection, exists_attribute, True)
        if path is None or exists is False:
            continue
        try:
            owned.add(Path(path).resolve(strict=False).relative_to(root).as_posix())
        except ValueError:
            continue
    return frozenset(owned)


def _parse_porcelain(output: str) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for raw_line in str(output or "").splitlines():
        if len(raw_line) < 4:
            continue
        code = raw_line[:2]
        path = raw_line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        safe_path = _safe_relative_path(path)
        if safe_path:
            entries.append((code, safe_path))
    return tuple(entries)


def _normalized_output(value: object) -> str:
    text = _ANSI_RE.sub("", str(value or ""))
    text = _CONTROL_RE.sub(" ", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return " ".join(text.split()).strip()


def _safe_short(value: object, maximum: int) -> str:
    normalized = _normalized_output(value)
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1].rstrip() + "…"


def _first_line(value: object) -> str:
    text = _normalized_output(value)
    return text.split(" ", 1)[0] if text else ""


def _safe_fingerprint(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if _is_sha256(text) else ""


def _is_sha256(value: object) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _safe_relative_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or "\x00" in text:
        return ""
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)[:512]
