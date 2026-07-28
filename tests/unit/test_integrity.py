from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from hermes_feishu_card import cli
import hermes_feishu_card.install.integrity as integrity_module
from hermes_feishu_card.install.detect import detect_hermes
from hermes_feishu_card.install.integrity import (
    IntegrityRepairRefused,
    _atomic_replace_many,
    build_integrity_provenance,
    execute_integrity_repair,
    migrate_integrity_manifest,
    plan_integrity_repair,
)
from hermes_feishu_card.install.patcher import (
    remove_base_patch,
    apply_cron_patch,
    apply_patch,
    remove_cron_patch,
    remove_patch,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hermes_v2026_4_23"
CRON_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "hermes_cron"
    / "scheduler.py"
)
EXACT_BASE_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "hermes_exact_base.py"
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_manifest(root, detection, run_source, cron_source, *, provenance=True):
    run_backup = detection.run_py.with_name("run.py.hermes_feishu_card.bak")
    cron_backup = detection.cron_py.with_name(
        "scheduler.py.hermes_feishu_card.bak"
    )
    run_patched = apply_patch(run_source, strategy=detection.hook_strategy)
    cron_patched = apply_cron_patch(cron_source)
    run_backup.write_text(run_source, encoding="utf-8")
    cron_backup.write_text(cron_source, encoding="utf-8")
    detection.run_py.write_text(run_patched, encoding="utf-8")
    detection.cron_py.write_text(cron_patched, encoding="utf-8")
    manifest = {
        "run_py": "gateway/run.py",
        "patched_sha256": sha256(run_patched.encode()).hexdigest(),
        "backup": "gateway/run.py.hermes_feishu_card.bak",
        "backup_sha256": sha256(run_source.encode()).hexdigest(),
        "cron_py": "cron/scheduler.py",
        "cron_patched_sha256": sha256(cron_patched.encode()).hexdigest(),
        "cron_backup": "cron/scheduler.py.hermes_feishu_card.bak",
        "cron_backup_sha256": sha256(cron_source.encode()).hexdigest(),
    }
    if provenance:
        manifest["integrity"] = build_integrity_provenance(
            root,
            run_py=detection.run_py,
            run_source=run_source,
            cron_py=detection.cron_py,
            cron_source=cron_source,
        )
    (root / ".hermes_feishu_card_manifest").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.fixture
def git_installed_state(tmp_path):
    root = tmp_path / "hermes"
    shutil.copytree(FIXTURE, root)
    (root / "cron").mkdir(exist_ok=True)
    shutil.copy2(CRON_FIXTURE, root / "cron" / "scheduler.py")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "HFC Test")
    _git(root, "config", "user.email", "hfc@example.invalid")
    _git(root, "add", "gateway/run.py", "cron/scheduler.py")
    _git(root, "commit", "-qm", "initial Hermes")
    detection = detect_hermes(root)
    run_source = detection.run_py.read_text(encoding="utf-8")
    cron_source = detection.cron_py.read_text(encoding="utf-8")
    _write_manifest(root, detection, run_source, cron_source)
    return root, detection, run_source, cron_source


def _commit_upstream_upgrade(root, detection, run_source, cron_source):
    upgraded_run = run_source + "\n# supported upstream gateway upgrade\n"
    upgraded_cron = cron_source + "\n# supported upstream cron upgrade\n"
    detection.run_py.write_text(upgraded_run, encoding="utf-8")
    detection.cron_py.write_text(upgraded_cron, encoding="utf-8")
    _git(root, "add", "gateway/run.py", "cron/scheduler.py")
    _git(root, "commit", "-qm", "upgrade Hermes")
    return upgraded_run, upgraded_cron


def test_integrity_plan_accepts_only_clean_descendant_git_upgrade(git_installed_state):
    root, detection, run_source, cron_source = git_installed_state
    upgraded_run, upgraded_cron = _commit_upstream_upgrade(
        root, detection, run_source, cron_source
    )

    plan = plan_integrity_repair(detect_hermes(root))

    assert plan.state == "stale_unpatched"
    assert plan.executable is True
    assert plan.reason == "verified_git_upgrade"
    assert plan.fingerprint
    assert upgraded_run.rstrip("\n") == _git(root, "show", "HEAD:gateway/run.py")
    assert upgraded_cron.rstrip("\n") == _git(root, "show", "HEAD:cron/scheduler.py")


def test_integrity_repair_tracks_exact_base_source_and_reinstalls_it(tmp_path):
    root = tmp_path / "hermes"
    shutil.copytree(FIXTURE, root)
    (root / "VERSION").write_text("v0.19.0\n", encoding="utf-8")
    base_py = root / "gateway" / "platforms" / "base.py"
    base_py.parent.mkdir(parents=True)
    shutil.copy2(EXACT_BASE_FIXTURE, base_py)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "HFC Test")
    _git(root, "config", "user.email", "hfc@example.invalid")
    _git(root, "add", "gateway/run.py", "gateway/platforms/base.py", "VERSION")
    _git(root, "commit", "-qm", "initial exact Hermes")
    assert cli.main(["install", "--hermes-dir", str(root), "--yes"]) == 0
    manifest = json.loads(
        (root / ".hermes_feishu_card_manifest").read_text(encoding="utf-8")
    )
    assert "base_blob_sha256" in manifest["integrity"]

    run_py = root / "gateway" / "run.py"
    upgraded_run = remove_patch(run_py.read_text(encoding="utf-8")) + "\n# upgrade\n"
    upgraded_base = (
        remove_base_patch(base_py.read_text(encoding="utf-8")) + "\n# upgrade\n"
    )
    run_py.write_text(upgraded_run, encoding="utf-8")
    base_py.write_text(upgraded_base, encoding="utf-8")
    _git(root, "add", "gateway/run.py", "gateway/platforms/base.py")
    _git(root, "commit", "-qm", "upgrade exact Hermes")
    detection = detect_hermes(root)
    plan = plan_integrity_repair(detection)

    assert plan.executable is True
    result = execute_integrity_repair(
        detection, expected_fingerprint=plan.fingerprint
    )

    assert result.status == "repaired"
    assert remove_patch(run_py.read_text(encoding="utf-8")) == upgraded_run
    assert remove_base_patch(base_py.read_text(encoding="utf-8")) == upgraded_base


def test_integrity_plan_refuses_legacy_manifest_until_explicit_migration(
    git_installed_state,
):
    root, detection, run_source, cron_source = git_installed_state
    manifest_path = root / ".hermes_feishu_card_manifest"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("integrity")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    _commit_upstream_upgrade(root, detection, run_source, cron_source)

    plan = plan_integrity_repair(detect_hermes(root))

    assert plan.executable is False
    assert plan.reason == "integrity_migration_required"


def test_integrity_plan_refuses_dirty_target_even_when_anchors_are_supported(
    git_installed_state,
):
    root, detection, run_source, cron_source = git_installed_state
    upgraded_run, _ = _commit_upstream_upgrade(root, detection, run_source, cron_source)
    detection.run_py.write_text(upgraded_run + "# local user edit\n", encoding="utf-8")

    plan = plan_integrity_repair(detect_hermes(root))

    assert plan.executable is False
    assert plan.reason == "git_target_modified"


def test_execute_integrity_repair_rechecks_and_atomically_reinstalls_current_hook(
    git_installed_state,
):
    root, detection, run_source, cron_source = git_installed_state
    upgraded_run, upgraded_cron = _commit_upstream_upgrade(
        root, detection, run_source, cron_source
    )
    detection = detect_hermes(root)
    plan = plan_integrity_repair(detection)

    result = execute_integrity_repair(
        detection, expected_fingerprint=plan.fingerprint
    )

    assert result.status == "repaired"
    assert result.restart_required is True
    assert remove_patch(detection.run_py.read_text(encoding="utf-8")) == upgraded_run
    assert (
        remove_cron_patch(detection.cron_py.read_text(encoding="utf-8"))
        == upgraded_cron
    )
    assert (
        detection.run_py.with_name("run.py.hermes_feishu_card.bak").read_text()
        == upgraded_run
    )
    manifest = json.loads(
        (root / ".hermes_feishu_card_manifest").read_text(encoding="utf-8")
    )
    assert manifest["integrity"]["version"] == 2
    assert manifest["integrity"]["git_head"] == _git(root, "rev-parse", "HEAD")


def test_execute_integrity_repair_refuses_changed_fingerprint(git_installed_state):
    root, detection, run_source, cron_source = git_installed_state
    upgraded_run, _ = _commit_upstream_upgrade(root, detection, run_source, cron_source)
    detection = detect_hermes(root)
    plan = plan_integrity_repair(detection)
    detection.run_py.write_text(upgraded_run + "# race\n", encoding="utf-8")

    with pytest.raises(IntegrityRepairRefused, match="evidence changed"):
        execute_integrity_repair(detection, expected_fingerprint=plan.fingerprint)


def test_execute_integrity_repair_rechecks_git_snapshot_immediately_before_write(
    git_installed_state, monkeypatch
):
    root, detection, run_source, cron_source = git_installed_state
    _commit_upstream_upgrade(root, detection, run_source, cron_source)
    detection = detect_hermes(root)
    plan = plan_integrity_repair(detection)
    tree = _git(root, "write-tree")
    unrelated = subprocess.run(
        ["git", "-C", str(root), "commit-tree", tree],
        check=True,
        capture_output=True,
        input="unrelated history\n",
        text=True,
    ).stdout.strip()
    tracked_paths = [
        detection.run_py,
        detection.cron_py,
        detection.run_py.with_name("run.py.hermes_feishu_card.bak"),
        detection.cron_py.with_name("scheduler.py.hermes_feishu_card.bak"),
        root / ".hermes_feishu_card_manifest",
    ]
    before = {path: path.read_bytes() for path in tracked_paths}
    original_install_manifest = integrity_module._install_manifest

    def switch_head_after_candidate_build(*args, **kwargs):
        manifest = original_install_manifest(*args, **kwargs)
        _git(root, "update-ref", "HEAD", unrelated)
        return manifest

    monkeypatch.setattr(
        integrity_module,
        "_install_manifest",
        switch_head_after_candidate_build,
    )

    with pytest.raises(IntegrityRepairRefused, match="evidence changed"):
        execute_integrity_repair(
            detection,
            expected_fingerprint=plan.fingerprint,
        )

    assert {path: path.read_bytes() for path in tracked_paths} == before


def test_migrate_integrity_manifest_requires_healthy_installed_git_state(
    git_installed_state,
):
    root, detection, _run_source, _cron_source = git_installed_state
    manifest_path = root / ".hermes_feishu_card_manifest"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("integrity")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")

    migrated = migrate_integrity_manifest(detection)

    assert migrated["version"] == 2
    assert json.loads(manifest_path.read_text())["integrity"] == migrated

    detection.run_py.write_text(
        detection.run_py.read_text(encoding="utf-8") + "# user edit\n"
    )
    with pytest.raises(IntegrityRepairRefused):
        migrate_integrity_manifest(detection)


def test_integrity_transaction_rolls_back_when_post_commit_validation_fails(tmp_path):
    existing = tmp_path / "existing.txt"
    created = tmp_path / "created.txt"
    existing.write_text("before\n", encoding="utf-8")

    def reject_after_commit():
        assert existing.read_text(encoding="utf-8") == "after\n"
        assert created.read_text(encoding="utf-8") == "created\n"
        raise IntegrityRepairRefused("post_commit_validation_failed")

    with pytest.raises(IntegrityRepairRefused, match="post_commit_validation_failed"):
        _atomic_replace_many(
            [(existing, "after\n"), (created, "created\n")],
            validate=reject_after_commit,
        )

    assert existing.read_text(encoding="utf-8") == "before\n"
    assert not created.exists()


def test_integrity_plan_refuses_non_descendant_history(git_installed_state):
    root, detection, run_source, cron_source = git_installed_state
    _git(root, "checkout", "--orphan", "rewound")
    detection.run_py.write_text(run_source + "\n# unrelated gateway history\n")
    detection.cron_py.write_text(cron_source + "\n# unrelated cron history\n")
    _git(root, "add", "gateway/run.py", "cron/scheduler.py")
    _git(root, "commit", "-qm", "unrelated Hermes history")

    plan = plan_integrity_repair(detect_hermes(root))

    assert plan.executable is False
    assert plan.reason == "git_history_not_descendant"


def test_integrity_plan_refuses_owned_backup_mismatch(git_installed_state):
    root, detection, run_source, cron_source = git_installed_state
    _commit_upstream_upgrade(root, detection, run_source, cron_source)
    detection.run_py.with_name("run.py.hermes_feishu_card.bak").write_text(
        run_source + "# tampered backup\n",
        encoding="utf-8",
    )

    plan = plan_integrity_repair(detect_hermes(root))

    assert plan.executable is False
    assert plan.reason in {"owned_backup_mismatch", "recovery_evidence_not_executable"}
