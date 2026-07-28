from __future__ import annotations

from pathlib import Path
import threading
from typing import Any, Callable

from .install.detect import detect_hermes
from .install.integrity import execute_integrity_repair, plan_integrity_repair
from .runtime_control import RuntimeIntegritySupervisor


class RuntimeIntegrityCoordinator:
    """Coordinate strict hook recovery without controlling Gateway lifecycle."""

    def __init__(
        self,
        *,
        mode: str,
        hermes_root: str | Path,
        supervisor: RuntimeIntegritySupervisor,
        detector: Callable[[str | Path], Any] = detect_hermes,
        planner: Callable[[Any], Any] = plan_integrity_repair,
        executor: Callable[..., Any] = execute_integrity_repair,
    ):
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"safe", "notify", "off"}:
            raise ValueError("integrity mode is invalid")
        self.mode = normalized_mode
        self.hermes_root = Path(hermes_root).expanduser()
        self.supervisor = supervisor
        self._detector = detector
        self._planner = planner
        self._executor = executor
        self._lock = threading.Lock()
        self._last_evidence = ""
        self._last_refusal_evidence = ""
        self._last_status = "idle"
        self._last_reason = ""
        self._repair_attempts = 0
        self._repair_successes = 0
        self._repair_refusals = 0

    def check_once(self) -> dict[str, Any]:
        with self._lock:
            return self._check_once_locked()

    def _check_once_locked(self) -> dict[str, Any]:
        if self.mode == "off":
            return self._record("disabled", "integrity_disabled", attempted=False)
        readiness = self.supervisor.snapshot()
        readiness_status = str(readiness.get("status") or "")
        readiness_reason = str(readiness.get("reason") or "")
        if readiness_status == "ready" or readiness_reason == "runtime_ready":
            return self._record("ready", "runtime_ready", attempted=False)
        if readiness_reason == "gateway_restart_required":
            return self._record(
                "restart_required",
                "gateway_restart_required",
                attempted=False,
            )
        if readiness_reason == "manual_review_required":
            return self._record(
                "manual_review_required",
                "manual_review_required",
                attempted=False,
            )

        try:
            detection = self._detector(self.hermes_root)
            plan = self._planner(detection)
        except Exception:
            self._repair_refusals += 1
            self.supervisor.mark_manual_review_required()
            return self._record(
                "manual_review_required",
                "integrity_evidence_unavailable",
                attempted=False,
            )

        if plan.state == "installed":
            self.supervisor.mark_restart_required()
            return self._record(
                "restart_required",
                "gateway_restart_required",
                attempted=False,
            )
        if not plan.executable:
            refusal_evidence = str(getattr(plan, "fingerprint", ""))
            if refusal_evidence and refusal_evidence == self._last_refusal_evidence:
                return self._record(
                    "manual_review_required",
                    str(getattr(plan, "reason", "integrity_evidence_invalid")),
                    attempted=False,
                )
            self._last_refusal_evidence = refusal_evidence
            self._repair_refusals += 1
            self.supervisor.mark_manual_review_required()
            return self._record(
                "manual_review_required",
                str(getattr(plan, "reason", "integrity_evidence_invalid")),
                attempted=False,
            )
        if self.mode == "notify":
            self._last_evidence = str(plan.fingerprint)
            return self._record(
                "repair_available",
                str(plan.reason),
                attempted=False,
            )
        if str(plan.fingerprint) == self._last_evidence:
            return self._record("deduplicated", str(plan.reason), attempted=False)

        self._last_evidence = str(plan.fingerprint)
        self._repair_attempts += 1
        try:
            result = self._executor(
                detection,
                expected_fingerprint=str(plan.fingerprint),
            )
        except Exception:
            self._repair_refusals += 1
            self.supervisor.mark_manual_review_required()
            return self._record(
                "manual_review_required",
                "integrity_repair_refused",
                attempted=True,
            )
        if getattr(result, "restart_required", False):
            self.supervisor.mark_restart_required()
        self._repair_successes += 1
        return self._record("repaired", "gateway_restart_required", attempted=True)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                "last_status": self._last_status,
                "last_reason": self._last_reason,
                "repair_attempts": self._repair_attempts,
                "repair_successes": self._repair_successes,
                "repair_refusals": self._repair_refusals,
            }

    def _record(
        self,
        status: str,
        reason: str,
        *,
        attempted: bool,
    ) -> dict[str, Any]:
        self._last_status = status
        self._last_reason = reason
        return {"status": status, "reason": reason, "attempted": attempted}
