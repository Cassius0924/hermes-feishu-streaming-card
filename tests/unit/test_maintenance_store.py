import json
import os
from pathlib import Path
import stat
import zipfile

import pytest

from hermes_feishu_card.maintenance_store import (
    ArtifactMetadata,
    MaintenanceRefused,
    acquire_update_lock,
    create_job,
    file_sha256,
    load_job,
    load_verified_artifact,
    maintenance_paths,
    prune_jobs,
    stage_wheel_artifact,
    transition_job,
)


def _write_wheel(
    path: Path,
    *,
    distribution: str = "hermes-feishu-streaming-card",
    version: str = "4.2.0",
) -> Path:
    dist_info = f"hermes_feishu_streaming_card-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            "\n".join(
                [
                    "Metadata-Version: 2.1",
                    f"Name: {distribution}",
                    f"Version: {version}",
                    "",
                ]
            ),
        )
        archive.writestr(
            "hermes_feishu_card/__init__.py",
            f'__version__ = "{version}"\n',
        )
    return path


@pytest.fixture
def wheel_file(tmp_path):
    return _write_wheel(tmp_path / "hfc.whl")


@pytest.fixture
def verified_artifact(tmp_path, wheel_file):
    return stage_wheel_artifact(
        maintenance_paths(tmp_path / "state"),
        wheel_file,
        expected_version="4.2.0",
        source_kind="installer_spec",
        now=lambda: 100.0,
    )


def _create_job(tmp_path: Path, artifact: ArtifactMetadata, **overrides):
    values = {
        "hermes_root": tmp_path / "hermes",
        "config_path": tmp_path / "config.yaml",
        "env_file": None,
        "profile_id": "default",
        "chat_id": "oc_private",
        "card_message_id": "om_card",
        "operator_hash": "sha256:operator",
        "pre_update_version": "0.19.1",
        "pre_update_head": "abc123",
        "target_fingerprint": "target-1",
        "artifact": artifact,
    }
    values.update(overrides)
    return create_job(
        maintenance_paths(tmp_path / "state"),
        now=lambda: 100.0,
        **values,
    )


def test_stage_wheel_records_exact_version_hash_and_private_modes(
    tmp_path, wheel_file
):
    paths = maintenance_paths(tmp_path / "state")

    metadata = stage_wheel_artifact(
        paths,
        wheel_file,
        expected_version="4.2.0",
        source_kind="installer_spec",
        now=lambda: 100.0,
    )

    assert metadata.version == "4.2.0"
    assert metadata.distribution == "hermes-feishu-streaming-card"
    assert metadata.sha256 == file_sha256(metadata.wheel_path)
    assert metadata.created_at == 100.0
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    if os.name != "nt":
        assert stat.S_IMODE(metadata.wheel_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(metadata.metadata_path.stat().st_mode) == 0o600


def test_load_verified_artifact_rejects_hash_drift(tmp_path, wheel_file):
    paths = maintenance_paths(tmp_path / "state")
    metadata = stage_wheel_artifact(
        paths,
        wheel_file,
        expected_version="4.2.0",
    )
    metadata.wheel_path.write_bytes(metadata.wheel_path.read_bytes() + b"tamper")

    with pytest.raises(MaintenanceRefused, match="artifact hash mismatch"):
        load_verified_artifact(paths, expected_version="4.2.0")


def test_stage_wheel_rejects_wrong_distribution_or_version(tmp_path):
    paths = maintenance_paths(tmp_path / "state")
    wrong_name = _write_wheel(
        tmp_path / "wrong-name.whl",
        distribution="other-package",
    )
    with pytest.raises(MaintenanceRefused, match="artifact distribution mismatch"):
        stage_wheel_artifact(paths, wrong_name, expected_version="4.2.0")

    wrong_version = _write_wheel(tmp_path / "wrong-version.whl", version="4.1.4")
    with pytest.raises(MaintenanceRefused, match="artifact version mismatch"):
        stage_wheel_artifact(paths, wrong_version, expected_version="4.2.0")


def test_transition_job_is_atomic_and_compare_and_swap(tmp_path, verified_artifact):
    job = _create_job(tmp_path, verified_artifact)

    updated = transition_job(
        job.path,
        expected_phase="locking",
        phase="draining",
        now=lambda: 110.0,
    )

    assert updated.phase == "draining"
    assert updated.updated_at == 110.0
    assert updated.attempts == {"draining": 1}
    with pytest.raises(MaintenanceRefused, match="job phase changed"):
        transition_job(
            job.path,
            expected_phase="locking",
            phase="failed",
        )


def test_job_round_trip_omits_secrets_and_raw_output(tmp_path, verified_artifact):
    env_file = tmp_path / ".env"
    job = _create_job(tmp_path, verified_artifact, env_file=env_file)

    loaded = load_job(job.path)
    payload = json.loads(job.path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload).lower()

    assert loaded == job
    assert loaded.env_file == env_file.resolve(strict=False)
    assert "app_secret" not in serialized
    assert "tenant_token" not in serialized
    assert "transport_secret" not in serialized
    assert "raw_output" not in payload
    assert payload["schema_version"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(job.path.stat().st_mode) == 0o600


def test_load_job_rejects_symlink_and_unknown_result_key(tmp_path, verified_artifact):
    job = _create_job(tmp_path, verified_artifact)
    symlink = job.path.with_name("linked.json")
    try:
        symlink.symlink_to(job.path)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(MaintenanceRefused, match="job path must not be a symlink"):
        load_job(symlink)

    payload = json.loads(job.path.read_text(encoding="utf-8"))
    payload["result"] = {"raw_output": "secret"}
    job.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(MaintenanceRefused, match="unsafe job result key"):
        load_job(job.path)


def test_update_lock_is_exclusive(tmp_path):
    paths = maintenance_paths(tmp_path / "state")

    with acquire_update_lock(paths, job_id="job-1"):
        with pytest.raises(MaintenanceRefused, match="update already in progress"):
            with acquire_update_lock(paths, job_id="job-2"):
                raise AssertionError("lock must not be acquired twice")


def test_prune_jobs_keeps_five_recent_and_removes_jobs_older_than_seven_days(
    tmp_path, verified_artifact
):
    paths = maintenance_paths(tmp_path / "state")
    jobs = []
    for index in range(7):
        job = create_job(
            paths,
            hermes_root=tmp_path / "hermes",
            config_path=tmp_path / "config.yaml",
            env_file=None,
            profile_id="default",
            chat_id=f"oc_{index}",
            card_message_id=f"om_{index}",
            operator_hash="sha256:operator",
            pre_update_version="0.19.1",
            pre_update_head=f"head-{index}",
            target_fingerprint=f"target-{index}",
            artifact=verified_artifact,
            job_id=f"job-{index}",
            now=lambda index=index: float(index * 100),
        )
        jobs.append(
            transition_job(
                job.path,
                expected_phase="locking",
                phase="failed",
                result={
                    "error_code": "test_failure",
                    "recovery_boundary": "no_mutation",
                },
                now=lambda index=index: float(index * 100),
            )
        )

    prune_jobs(paths, now=700.0, max_terminal=5, max_age_seconds=10_000.0)

    assert not jobs[0].path.exists()
    assert not jobs[1].path.exists()
    assert all(job.path.exists() for job in jobs[2:])

    prune_jobs(paths, now=700.0 + 604_801.0, max_terminal=5)

    assert list(paths.jobs.glob("*.json")) == []
