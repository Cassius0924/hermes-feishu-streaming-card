from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Sequence

from .install.detect import detect_hermes
from .install.recovery import plan_recovery
from .maintenance_store import ArtifactMetadata


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


def _git_operation_incomplete(root: Path) -> bool:
    git_dir = root / ".git"
    return any(
        (git_dir / name).exists() or (git_dir / name).is_symlink()
        for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply")
    )


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
