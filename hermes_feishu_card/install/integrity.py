from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable

from .detect import HermesDetection
from .patcher import apply_cron_patch, apply_patch, remove_cron_patch, remove_patch
from .recovery import (
    BACKUP_SUFFIX,
    MANIFEST_NAME,
    RecoveryPlan,
    _root_lock,
    plan_recovery,
)


INTEGRITY_MANIFEST_VERSION = 2
_GIT_HASH_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class IntegrityRepairRefused(ValueError):
    pass


@dataclass(frozen=True)
class IntegrityRepairPlan:
    state: str
    executable: bool
    fingerprint: str
    reason: str
    recovery_plan: RecoveryPlan


@dataclass(frozen=True)
class IntegrityRepairResult:
    status: str
    restart_required: bool
    plan: IntegrityRepairPlan


def build_integrity_provenance(
    root: str | Path,
    *,
    run_py: str | Path,
    run_source: str,
    cron_py: str | Path | None = None,
    cron_source: str | None = None,
) -> dict[str, Any]:
    root_path = _exact_git_root(Path(root))
    head = _git_head(root_path)
    run_relative = _relative_regular_path(root_path, Path(run_py))
    if _git_blob(root_path, head, run_relative) != run_source:
        raise IntegrityRepairRefused("gateway source does not match Git HEAD")
    provenance: dict[str, Any] = {
        "version": INTEGRITY_MANIFEST_VERSION,
        "git_head": head,
        "run_blob_sha256": _text_sha256(run_source),
    }
    if cron_py is not None:
        if cron_source is None:
            raise IntegrityRepairRefused("cron provenance is incomplete")
        cron_relative = _relative_regular_path(root_path, Path(cron_py))
        if _git_blob(root_path, head, cron_relative) != cron_source:
            raise IntegrityRepairRefused("cron source does not match Git HEAD")
        provenance["cron_blob_sha256"] = _text_sha256(cron_source)
    return provenance


def plan_integrity_repair(detection: HermesDetection) -> IntegrityRepairPlan:
    base_plan = plan_recovery(detection, accept_hermes_upgrade=True)
    cron_py = _active_cron_py(detection)
    evidence: dict[str, str] = {
        "base_fingerprint": base_plan.fingerprint,
        "state": base_plan.state,
    }
    reason = "recovery_not_required"
    executable = False

    manifest = _read_manifest(detection.root / MANIFEST_NAME)
    integrity = manifest.get("integrity") if manifest is not None else None
    if not _valid_integrity_manifest(integrity, cron_py is not None):
        reason = "integrity_migration_required"
        evidence["integrity"] = "missing_or_invalid"
        return _plan(base_plan, executable, reason, evidence)
    evidence["integrity"] = _canonical_hash(integrity)

    if base_plan.state != "stale_unpatched" or not base_plan.executable:
        reason = (
            "recovery_not_required"
            if base_plan.state in {"clean", "installed"}
            else "recovery_evidence_not_executable"
        )
        return _plan(base_plan, executable, reason, evidence)

    try:
        root = _exact_git_root(detection.root)
        old_head = str(integrity["git_head"])
        current_head = _git_head(root)
        evidence["current_head"] = current_head
        if not _is_ancestor(root, old_head, current_head):
            reason = "git_history_not_descendant"
            return _plan(base_plan, executable, reason, evidence)

        targets = [
            (
                detection.run_py,
                detection.run_py.with_name(
                    f"{detection.run_py.name}{BACKUP_SUFFIX}"
                ),
                str(integrity["run_blob_sha256"]),
            )
        ]
        if cron_py is not None:
            targets.append(
                (
                    cron_py,
                    cron_py.with_name(
                        f"{cron_py.name}{BACKUP_SUFFIX}"
                    ),
                    str(integrity["cron_blob_sha256"]),
                )
            )

        current_sources: list[str] = []
        for target, old_backup, old_blob_hash in targets:
            relative = _relative_regular_path(root, target)
            if old_backup.is_symlink() or not old_backup.is_file():
                reason = "owned_backup_invalid"
                return _plan(base_plan, executable, reason, evidence)
            old_blob = _git_blob(root, old_head, relative)
            if (
                _text_sha256(old_blob) != old_blob_hash
                or _text_sha256(_read_text(old_backup)) != old_blob_hash
            ):
                reason = "owned_backup_mismatch"
                return _plan(base_plan, executable, reason, evidence)
            if _git_target_status(root, relative):
                reason = "git_target_modified"
                return _plan(base_plan, executable, reason, evidence)
            current_source = _read_text(target)
            if current_source != _git_blob(root, current_head, relative):
                reason = "git_target_modified"
                return _plan(base_plan, executable, reason, evidence)
            current_sources.append(current_source)
            evidence[f"target_{len(current_sources)}"] = _text_sha256(current_source)

        _validate_reinstall_candidates(detection, current_sources)
    except IntegrityRepairRefused as exc:
        reason = _safe_reason(exc)
        return _plan(base_plan, executable, reason, evidence)

    executable = True
    reason = "verified_git_upgrade"
    return _plan(base_plan, executable, reason, evidence)


def execute_integrity_repair(
    detection: HermesDetection,
    *,
    expected_fingerprint: str,
) -> IntegrityRepairResult:
    with _root_lock(detection.root):
        fresh = plan_integrity_repair(detection)
        if fresh.fingerprint != expected_fingerprint:
            raise IntegrityRepairRefused("integrity evidence changed; rerun diagnosis")
        if not fresh.executable:
            raise IntegrityRepairRefused(
                f"integrity repair refused: {fresh.reason}"
            )

        run_source = _read_text(detection.run_py)
        cron_py = _active_cron_py(detection)
        cron_source = (
            _read_text(cron_py)
            if cron_py is not None
            else None
        )
        run_patched = apply_patch(
            run_source,
            strategy=detection.hook_strategy or "legacy_gateway_run",
        )
        cron_patched = (
            apply_cron_patch(cron_source)
            if cron_source is not None
            else None
        )
        _validate_reinstall_candidates(
            detection,
            [source for source in (run_source, cron_source) if source is not None],
        )

        run_backup = detection.run_py.with_name(
            f"{detection.run_py.name}{BACKUP_SUFFIX}"
        )
        changes: list[tuple[Path, str]] = [
            (detection.run_py, run_patched),
            (run_backup, run_source),
        ]
        cron_backup: Path | None = None
        if cron_py is not None and cron_source is not None and cron_patched is not None:
            cron_backup = cron_py.with_name(
                f"{cron_py.name}{BACKUP_SUFFIX}"
            )
            changes.extend(
                ((cron_py, cron_patched), (cron_backup, cron_source))
            )

        manifest = _install_manifest(
            detection,
            run_source=run_source,
            run_patched=run_patched,
            run_backup=run_backup,
            cron_source=cron_source,
            cron_patched=cron_patched,
            cron_backup=cron_backup,
        )
        changes.append(
            (
                detection.root / MANIFEST_NAME,
                json.dumps(manifest, sort_keys=True) + "\n",
            )
        )
        def validate_repair_snapshot() -> None:
            latest = plan_integrity_repair(detection)
            if latest.fingerprint != fresh.fingerprint or not latest.executable:
                raise IntegrityRepairRefused(
                    "integrity evidence changed; rerun diagnosis"
                )
            if _read_text(detection.run_py) != run_source:
                raise IntegrityRepairRefused(
                    "integrity evidence changed; rerun diagnosis"
                )
            if cron_py is not None and _read_text(cron_py) != cron_source:
                raise IntegrityRepairRefused(
                    "integrity evidence changed; rerun diagnosis"
                )

        def validate_committed_state() -> None:
            installed = plan_recovery(detection)
            if installed.state != "installed" or any(
                finding.severity == "error" for finding in installed.findings
            ):
                raise IntegrityRepairRefused(
                    "integrity repair validation failed after commit"
                )

        _atomic_replace_many(
            changes,
            pre_commit_validate=validate_repair_snapshot,
            validate=validate_committed_state,
        )
        return IntegrityRepairResult(
            status="repaired",
            restart_required=True,
            plan=fresh,
        )


def migrate_integrity_manifest(detection: HermesDetection) -> dict[str, Any]:
    with _root_lock(detection.root):
        installed = plan_recovery(detection)
        if installed.state != "installed" or any(
            finding.severity == "error" for finding in installed.findings
        ):
            raise IntegrityRepairRefused(
                "integrity migration requires a healthy installed hook"
            )
        manifest_path = detection.root / MANIFEST_NAME
        manifest = _read_manifest(manifest_path)
        if manifest is None:
            raise IntegrityRepairRefused("integrity migration requires a manifest")
        if detection.run_py.is_symlink():
            raise IntegrityRepairRefused("gateway source must be a regular file")
        run_current = _read_text(detection.run_py)
        run_source = remove_patch(run_current)
        if run_source == run_current or apply_patch(
            run_source,
            strategy=detection.hook_strategy or "legacy_gateway_run",
        ) != run_current:
            raise IntegrityRepairRefused("gateway hook is not reversible")
        run_backup = detection.run_py.with_name(
            f"{detection.run_py.name}{BACKUP_SUFFIX}"
        )
        if run_backup.is_symlink() or _read_text(run_backup) != run_source:
            raise IntegrityRepairRefused("gateway backup is not verified")

        cron_source = None
        cron_py = _active_cron_py(detection)
        if cron_py is not None:
            if cron_py.is_symlink():
                raise IntegrityRepairRefused("cron source must be a regular file")
            cron_current = _read_text(cron_py)
            cron_source = remove_cron_patch(cron_current)
            if cron_source == cron_current or apply_cron_patch(cron_source) != cron_current:
                raise IntegrityRepairRefused("cron hook is not reversible")
            cron_backup = cron_py.with_name(
                f"{cron_py.name}{BACKUP_SUFFIX}"
            )
            if cron_backup.is_symlink() or _read_text(cron_backup) != cron_source:
                raise IntegrityRepairRefused("cron backup is not verified")

        provenance = build_integrity_provenance(
            detection.root,
            run_py=detection.run_py,
            run_source=run_source,
            cron_py=cron_py,
            cron_source=cron_source,
        )
        manifest["integrity"] = provenance
        _atomic_replace_many(
            [(manifest_path, json.dumps(manifest, sort_keys=True) + "\n")]
        )
        return provenance


def _install_manifest(
    detection: HermesDetection,
    *,
    run_source: str,
    run_patched: str,
    run_backup: Path,
    cron_source: str | None,
    cron_patched: str | None,
    cron_backup: Path | None,
) -> dict[str, Any]:
    cron_py = _active_cron_py(detection)
    manifest: dict[str, Any] = {
        "run_py": detection.run_py.relative_to(detection.root).as_posix(),
        "patched_sha256": _text_sha256(run_patched),
        "backup": run_backup.relative_to(detection.root).as_posix(),
        "backup_sha256": _text_sha256(run_source),
    }
    if (
        cron_py is not None
        and cron_source is not None
        and cron_patched is not None
        and cron_backup is not None
    ):
        manifest.update(
            {
                "cron_py": cron_py.relative_to(detection.root).as_posix(),
                "cron_patched_sha256": _text_sha256(cron_patched),
                "cron_backup": cron_backup.relative_to(detection.root).as_posix(),
                "cron_backup_sha256": _text_sha256(cron_source),
            }
        )
    manifest["integrity"] = build_integrity_provenance(
        detection.root,
        run_py=detection.run_py,
        run_source=run_source,
        cron_py=cron_py,
        cron_source=cron_source,
    )
    return manifest


def _validate_reinstall_candidates(
    detection: HermesDetection, sources: list[str]
) -> None:
    if not detection.supported or not sources:
        raise IntegrityRepairRefused("unsupported_anchors")
    run_source = sources[0]
    try:
        ast.parse(run_source)
        run_patched = apply_patch(
            run_source,
            strategy=detection.hook_strategy or "legacy_gateway_run",
        )
        ast.parse(run_patched)
        if remove_patch(run_patched) != run_source:
            raise ValueError("gateway roundtrip failed")
        if _active_cron_py(detection) is not None:
            if len(sources) != 2:
                raise ValueError("cron source missing")
            cron_source = sources[1]
            ast.parse(cron_source)
            cron_patched = apply_cron_patch(cron_source)
            ast.parse(cron_patched)
            if remove_cron_patch(cron_patched) != cron_source:
                raise ValueError("cron roundtrip failed")
    except (SyntaxError, ValueError) as exc:
        raise IntegrityRepairRefused("unsupported_anchors") from exc


def _valid_integrity_manifest(value: Any, has_cron: bool) -> bool:
    if not isinstance(value, dict):
        return False
    required = {"version", "git_head", "run_blob_sha256"}
    if has_cron:
        required.add("cron_blob_sha256")
    if not required.issubset(value):
        return False
    return bool(
        value.get("version") == INTEGRITY_MANIFEST_VERSION
        and isinstance(value.get("git_head"), str)
        and _GIT_HASH_RE.fullmatch(str(value["git_head"]))
        and _SHA256_RE.fullmatch(str(value["run_blob_sha256"]))
        and (
            not has_cron
            or _SHA256_RE.fullmatch(str(value["cron_blob_sha256"]))
        )
    )


def _plan(
    base_plan: RecoveryPlan,
    executable: bool,
    reason: str,
    evidence: dict[str, str],
) -> IntegrityRepairPlan:
    fingerprint = sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return IntegrityRepairPlan(
        state=base_plan.state,
        executable=executable,
        fingerprint=fingerprint,
        reason=reason,
        recovery_plan=base_plan,
    )


def _exact_git_root(root: Path) -> Path:
    resolved = root.resolve()
    result = _run_git(resolved, "rev-parse", "--show-toplevel")
    try:
        toplevel = Path(result.stdout.decode("utf-8").strip()).resolve()
    except UnicodeError as exc:
        raise IntegrityRepairRefused("git_root_invalid") from exc
    if toplevel != resolved:
        raise IntegrityRepairRefused("git_root_invalid")
    return resolved


def _git_head(root: Path) -> str:
    result = _run_git(root, "rev-parse", "HEAD")
    head = result.stdout.decode("ascii", errors="ignore").strip().lower()
    if _GIT_HASH_RE.fullmatch(head) is None:
        raise IntegrityRepairRefused("git_head_invalid")
    return head


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    if result.returncode not in {0, 1}:
        raise IntegrityRepairRefused("git_history_unavailable")
    return result.returncode == 0


def _git_blob(root: Path, revision: str, relative: str) -> str:
    result = _run_git(root, "show", f"{revision}:{relative}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError as exc:
        raise IntegrityRepairRefused("git_blob_invalid") from exc


def _git_target_status(root: Path, relative: str) -> str:
    result = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "--",
        relative,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrityRepairRefused("git_evidence_unavailable") from exc
    if result.returncode != 0:
        raise IntegrityRepairRefused("git_evidence_unavailable")
    return result


def _relative_regular_path(root: Path, path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise IntegrityRepairRefused("source_not_regular")
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise IntegrityRepairRefused("source_outside_root") from exc


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _read_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise IntegrityRepairRefused("source_not_regular")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise IntegrityRepairRefused("source_read_failed") from exc


def _atomic_replace_many(
    changes: list[tuple[Path, str]],
    *,
    pre_commit_validate: Callable[[], None] | None = None,
    validate: Callable[[], None] | None = None,
) -> None:
    staged: dict[Path, Path] = {}
    rollback: dict[Path, Path | None] = {}
    changed: list[Path] = []
    try:
        for target, contents in changes:
            if target.is_symlink():
                raise IntegrityRepairRefused("mutation target is a symlink")
            rollback[target] = (
                _stage_text(target, _read_text(target)) if target.exists() else None
            )
            staged[target] = _stage_text(target, contents)
        if pre_commit_validate is not None:
            pre_commit_validate()
        for target, _contents in changes:
            os.replace(staged[target], target)
            staged.pop(target, None)
            changed.append(target)
        if validate is not None:
            validate()
    except Exception as exc:
        rollback_error: Exception | None = None
        for target in reversed(changed):
            try:
                original = rollback.get(target)
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(original, target)
                    rollback[target] = None
            except OSError as rollback_exc:
                rollback_error = rollback_error or rollback_exc
        if rollback_error is not None:
            raise IntegrityRepairRefused(
                "integrity repair rollback failed; manual review required"
            ) from exc
        if isinstance(exc, IntegrityRepairRefused):
            raise
        raise IntegrityRepairRefused("integrity repair transaction failed") from exc
    finally:
        for path in list(staged.values()) + [
            item for item in rollback.values() if item is not None
        ]:
            path.unlink(missing_ok=True)


def _stage_text(target: Path, contents: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            os.chmod(staged, target.stat().st_mode)
        else:
            os.chmod(staged, 0o600)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _text_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _active_cron_py(detection: HermesDetection) -> Path | None:
    cron_py = detection.cron_py
    if cron_py is None or not getattr(detection, "cron_py_exists", cron_py.is_file()):
        return None
    return cron_py


def _canonical_hash(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _safe_reason(exc: IntegrityRepairRefused) -> str:
    reason = str(exc)
    allowed = {
        "git_root_invalid",
        "git_head_invalid",
        "git_history_unavailable",
        "git_evidence_unavailable",
        "git_blob_invalid",
        "source_not_regular",
        "source_outside_root",
        "source_read_failed",
        "unsupported_anchors",
    }
    return reason if reason in allowed else "integrity_evidence_invalid"
