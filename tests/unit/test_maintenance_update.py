from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_feishu_card.maintenance_store import ArtifactMetadata
from hermes_feishu_card.maintenance_update import (
    CommandResult,
    inspect_update,
)


class CommandHarness:
    def __init__(self, hermes_root: Path):
        self.hermes_root = hermes_root
        self.commands = []
        self.git_head = "a" * 40 + "\n"
        self.git_status = (
            " M gateway/run.py\n"
            " M gateway/platforms/base.py\n"
            " M cron/scheduler.py\n"
        )
        self.update_result = CommandResult(
            argv=("hermes", "update", "--check"),
            returncode=0,
            stdout="3 updates available; target upstream/f3cda0ce\n",
            stderr="",
        )

    def __call__(self, argv, timeout):
        normalized = tuple(str(value) for value in argv)
        self.commands.append(normalized)
        if normalized[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(normalized, 0, self.git_head, "")
        if "status" in normalized:
            return CommandResult(normalized, 0, self.git_status, "")
        if normalized == ("hermes", "update", "--check"):
            return replace(self.update_result, argv=normalized)
        raise AssertionError(f"unexpected command: {normalized}")


@pytest.fixture
def clean_hermes(tmp_path):
    root = tmp_path / "hermes"
    (root / ".git").mkdir(parents=True)
    (root / "gateway" / "platforms").mkdir(parents=True)
    (root / "cron").mkdir()
    (root / "gateway" / "run.py").write_text("patched\n", encoding="utf-8")
    (root / "gateway" / "platforms" / "base.py").write_text(
        "patched\n", encoding="utf-8"
    )
    (root / "cron" / "scheduler.py").write_text("patched\n", encoding="utf-8")
    return root


@pytest.fixture
def artifact(tmp_path):
    wheel = tmp_path / "hfc.whl"
    wheel.write_bytes(b"wheel")
    return ArtifactMetadata(
        schema_version=1,
        distribution="hermes-feishu-streaming-card",
        version="4.2.0",
        sha256="b" * 64,
        wheel_path=wheel,
        metadata_path=tmp_path / "artifact.json",
        source_kind="installer_spec",
        created_at=100.0,
    )


@pytest.fixture(autouse=True)
def healthy_detection(monkeypatch, clean_hermes):
    detection = SimpleNamespace(
        root=clean_hermes,
        version="0.19.1",
        supported=True,
        compatibility="full",
        run_py=clean_hermes / "gateway" / "run.py",
        cron_py=clean_hermes / "cron" / "scheduler.py",
        cron_py_exists=True,
        base_py=clean_hermes / "gateway" / "platforms" / "base.py",
        base_py_exists=True,
        base_required=True,
    )
    recovery = SimpleNamespace(
        state="installed",
        actions=(),
        executable=False,
        fingerprint="recovery-fingerprint",
        findings=(),
    )
    monkeypatch.setattr(
        "hermes_feishu_card.maintenance_update.detect_hermes",
        lambda root: detection,
    )
    monkeypatch.setattr(
        "hermes_feishu_card.maintenance_update.plan_recovery",
        lambda current: recovery,
    )


def _inspect(clean_hermes, artifact, runner, *, active_sessions=0):
    return inspect_update(
        hermes_root=clean_hermes,
        artifact=artifact,
        installed_hfc_version="4.2.0",
        active_sessions=active_sessions,
        run=runner,
        now=lambda: 200.0,
    )


def test_inspect_update_runs_only_read_only_commands(
    clean_hermes, artifact
):
    runner = CommandHarness(clean_hermes)

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is True
    assert runner.commands == [
        ("git", "-C", str(clean_hermes), "rev-parse", "HEAD"),
        (
            "git",
            "-C",
            str(clean_hermes),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        ),
        ("hermes", "update", "--check"),
    ]
    assert inspection.current_version == "0.19.1"
    assert inspection.current_head == "a" * 40
    assert inspection.target_summary == "3 updates available; target upstream/f3cda0ce"
    assert inspection.hfc_version == "4.2.0"
    assert inspection.hook_state == "installed"
    assert inspection.maintenance_ready is True
    assert inspection.created_at == 200.0
    assert len(inspection.fingerprint) == 64


def test_inspect_update_refuses_unrelated_tracked_change(
    clean_hermes, artifact
):
    runner = CommandHarness(clean_hermes)
    runner.git_status += " M gateway/unrelated.py\n"

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is False
    assert inspection.reason_code == "unrelated_tracked_changes"
    assert inspection.changed_paths == ("gateway/unrelated.py",)
    assert ("hermes", "update", "--check") not in runner.commands


def test_inspect_update_allows_untracked_files(clean_hermes, artifact):
    (clean_hermes / "notes.local.md").write_text("keep", encoding="utf-8")
    runner = CommandHarness(clean_hermes)

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is True
    assert "notes.local.md" not in inspection.changed_paths


@pytest.mark.parametrize(
    "marker",
    [
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
    ],
)
def test_inspect_update_refuses_incomplete_git_operation(
    clean_hermes, artifact, marker
):
    marker_path = clean_hermes / ".git" / marker
    if "." not in marker:
        marker_path.mkdir()
    else:
        marker_path.write_text("pending\n", encoding="utf-8")
    runner = CommandHarness(clean_hermes)

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is False
    assert inspection.reason_code == "git_operation_incomplete"
    assert runner.commands == []


def test_inspect_update_refuses_unmerged_status(clean_hermes, artifact):
    runner = CommandHarness(clean_hermes)
    runner.git_status = "UU gateway/run.py\n"

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is False
    assert inspection.reason_code == "git_operation_incomplete"


def test_inspection_reports_active_work_without_mutating(
    clean_hermes, artifact
):
    runner = CommandHarness(clean_hermes)

    inspection = _inspect(
        clean_hermes,
        artifact,
        runner,
        active_sessions=3,
    )

    assert inspection.ready is True
    assert inspection.active_sessions == 3
    assert inspection.requires_drain is True


def test_update_check_timeout_is_not_ready(clean_hermes, artifact):
    runner = CommandHarness(clean_hermes)
    runner.update_result = CommandResult(
        ("hermes", "update", "--check"),
        -1,
        "",
        "",
        timed_out=True,
    )

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is False
    assert inspection.reason_code == "update_check_timeout"


def test_update_check_failure_is_sanitized_and_bounded(clean_hermes, artifact):
    runner = CommandHarness(clean_hermes)
    runner.update_result = CommandResult(
        ("hermes", "update", "--check"),
        2,
        "",
        "token=secret\n" + "x" * 1000,
    )

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is False
    assert inspection.reason_code == "update_check_failed"
    assert inspection.target_summary == ""
    assert "secret" not in repr(inspection)


def test_artifact_version_drift_blocks_before_commands(clean_hermes, artifact):
    runner = CommandHarness(clean_hermes)

    inspection = inspect_update(
        hermes_root=clean_hermes,
        artifact=replace(artifact, version="4.1.4"),
        installed_hfc_version="4.2.0",
        active_sessions=0,
        run=runner,
    )

    assert inspection.ready is False
    assert inspection.reason_code == "artifact_version_mismatch"
    assert runner.commands == []


def test_unsupported_or_partial_hermes_is_refused(
    clean_hermes, artifact, monkeypatch
):
    runner = CommandHarness(clean_hermes)
    monkeypatch.setattr(
        "hermes_feishu_card.maintenance_update.detect_hermes",
        lambda root: SimpleNamespace(
            root=root,
            version="0.19.1",
            supported=True,
            compatibility="partial",
        ),
    )

    inspection = _inspect(clean_hermes, artifact, runner)

    assert inspection.ready is False
    assert inspection.reason_code == "hermes_not_fully_supported"
    assert runner.commands == []
