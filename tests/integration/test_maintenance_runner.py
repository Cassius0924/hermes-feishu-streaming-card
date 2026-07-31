import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

import hermes_feishu_card.maintenance_runner as maintenance_runner_module
from hermes_feishu_card.maintenance_store import (
    create_job,
    load_active_drain_lease,
    load_job,
    maintenance_paths,
    reserve_drain_lease,
    stage_wheel_artifact,
    transition_job,
)
from hermes_feishu_card.maintenance_update import (
    CommandResult,
    run_job,
)


UPDATE_CHECK_TEXT = "3 updates available; target upstream/f3cda0ce"
TARGET_HEAD = "e" * 40
TARGET_FINGERPRINT = hashlib.sha256(
    ("origin/main\0" + TARGET_HEAD).encode()
).hexdigest()


def _write_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "hermes_feishu_streaming_card-4.2.0.dist-info/METADATA",
            "Metadata-Version: 2.1\n"
            "Name: hermes-feishu-streaming-card\n"
            "Version: 4.2.0\n",
        )
        archive.writestr(
            "hermes_feishu_card/__init__.py",
            '__version__ = "4.2.0"\n',
        )
    return path


@pytest.fixture
def maintenance_fixture(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / ".git").mkdir(parents=True)
    (root / "gateway" / "platforms").mkdir(parents=True)
    (root / "cron").mkdir()
    for relative in (
        "gateway/run.py",
        "gateway/platforms/base.py",
        "cron/scheduler.py",
    ):
        (root / relative).write_text("patched\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "server:\n  host: 127.0.0.1\n  port: 8765\n"
        "feishu:\n  app_id: test\n  app_secret: test\n",
        encoding="utf-8",
    )
    paths = maintenance_paths(tmp_path / "state" / "maintenance")
    wheel = _write_wheel(
        tmp_path / "hermes_feishu_streaming_card-4.2.0-py3-none-any.whl"
    )
    artifact = stage_wheel_artifact(
        paths,
        wheel,
        expected_version="4.2.0",
        now=lambda: 100.0,
    )
    job = create_job(
        paths,
        hermes_root=root,
        config_path=config,
        env_file=None,
        profile_id="default",
        chat_id="oc_private",
        card_message_id="om_card",
        operator_hash="sha256:operator",
        pre_update_version="0.19.1",
        pre_update_head="a" * 40,
        target_fingerprint=TARGET_FINGERPRINT,
        target_head=TARGET_HEAD,
        artifact=artifact,
        pre_sidecar_pid=111,
        pre_runtime_id_hash="1" * 64,
        pre_runtime_sequence=5,
        job_id="job-1",
        now=lambda: 100.0,
    )
    reserve_drain_lease(paths, owner_id=job.job_id)
    state = {"hook": "installed", "version": "0.19.1"}

    def detection(current_root):
        return SimpleNamespace(
            root=current_root,
            version=state["version"],
            supported=True,
            compatibility="full",
            run_py=current_root / "gateway" / "run.py",
            run_py_exists=True,
            cron_py=current_root / "cron" / "scheduler.py",
            cron_py_exists=True,
            base_py=current_root / "gateway" / "platforms" / "base.py",
            base_py_exists=True,
            base_required=True,
        )

    def recovery(_detection):
        return SimpleNamespace(
            state=state["hook"],
            actions=(),
            executable=False,
            fingerprint="d" * 64,
            findings=(),
        )

    monkeypatch.setattr(
        "hermes_feishu_card.maintenance_update.detect_hermes",
        detection,
    )
    monkeypatch.setattr(
        "hermes_feishu_card.maintenance_update.plan_recovery",
        recovery,
    )
    return SimpleNamespace(
        root=root,
        config=config,
        paths=paths,
        artifact=artifact,
        job=job,
        state=state,
    )


class CommandHarness:
    def __init__(self, fixture):
        self.fixture = fixture
        self.actual_head = "a" * 40
        self.git_status = (
            " M gateway/run.py\n"
            " M gateway/platforms/base.py\n"
            " M cron/scheduler.py\n"
        )
        self.mutations = []
        self.fail_at = ""
        self.hermes_update_calls = 0
        self.pinned_install_calls = 0
        self.runtime_python = (
            fixture.root
            / "venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        self.package_location = (
            fixture.root
            / "venv"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "hermes_feishu_card"
            / "__init__.py"
        )
        self.runtime_python.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_python.write_text("#!python\n", encoding="utf-8")
        self.runtime_python.chmod(0o700)
        self.package_location.parent.mkdir(parents=True, exist_ok=True)
        self.package_location.write_text(
            '__version__ = "4.2.0"\n',
            encoding="utf-8",
        )

    def __call__(self, argv, timeout):
        command = tuple(str(value) for value in argv)
        if (
            command[0] == str(self.runtime_python)
            and command[1:3] == ("-I", "-c")
            and "gateway.drain_control" in command[3]
        ):
            return CommandResult(command, 0, "", "")
        if command[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(command, 0, self.actual_head + "\n", "")
        if command[-3:] == ("rev-parse", "--verify", "origin/main"):
            return CommandResult(command, 0, TARGET_HEAD + "\n", "")
        if command[-4:] == ("fetch", "--quiet", "origin", "main"):
            return CommandResult(command, 0, "", "")
        if "merge-base" in command:
            return CommandResult(command, 0, "", "")
        if "log" in command and "--format=%h %s" in command:
            return CommandResult(command, 0, "e123456 target commit\n", "")
        if "status" in command and command[:2] == ("git", "-C"):
            return CommandResult(command, 0, self.git_status, "")
        if command == ("hermes", "update", "--check"):
            return CommandResult(command, 0, UPDATE_CHECK_TEXT + "\n", "")
        if command[-3:] == ("maintenance", "drain", "--status"):
            return CommandResult(command, 0, '{"active_sessions": 0}', "")
        if command == ("hermes", "gateway", "stop", "--all"):
            return self._mutation("gateway-stop", command)
        if "hermes_feishu_card.cli" in command and "stop" in command:
            return self._mutation("sidecar-stop", command)
        if "hermes_feishu_card.cli" in command and "restore" in command:
            result = self._mutation("hfc-restore", command)
            if result.returncode == 0:
                self.fixture.state["hook"] = "clean"
                self.git_status = ""
            return result
        if command == ("hermes", "update", "--yes"):
            self.hermes_update_calls += 1
            result = self._mutation("hermes-update", command)
            if result.returncode == 0:
                self.actual_head = "e" * 40
                self.fixture.state["version"] = "0.19.2"
                self.runtime_python.parent.mkdir(parents=True, exist_ok=True)
                self.runtime_python.write_text("#!python\n", encoding="utf-8")
                self.runtime_python.chmod(0o700)
                self.package_location.parent.mkdir(parents=True, exist_ok=True)
                self.package_location.write_text(
                    '__version__ = "4.2.0"\n',
                    encoding="utf-8",
                )
            return result
        if command[0] == str(self.runtime_python) and command[1:4] == (
            "-I",
            "-m",
            "pip",
        ):
            self.pinned_install_calls += 1
            return self._mutation("pinned-wheel-install", command)
        if (
            command[0] == str(self.runtime_python)
            and "hermes_feishu_card.cli" in command
            and "install" in command
        ):
            result = self._mutation("hfc-install", command)
            if result.returncode == 0:
                self.fixture.state["hook"] = "installed"
                self.git_status = (
                    " M gateway/run.py\n"
                    " M gateway/platforms/base.py\n"
                    " M cron/scheduler.py\n"
                )
            return result
        if (
            command[0] == str(self.runtime_python)
            and "hermes_feishu_card.cli" in command
            and "start" in command
        ):
            return self._mutation("sidecar-start", command)
        if command == ("hermes", "gateway", "restart"):
            return self._mutation("gateway-restart", command)
        if command[0] == str(self.runtime_python) and command[1:3] == ("-I", "-c"):
            return CommandResult(
                command,
                0,
                json.dumps(
                    {
                        "version": "4.2.0",
                        "location": str(self.package_location),
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    def _mutation(self, name, command):
        self.mutations.append(name)
        if self.fail_at == name:
            return CommandResult(command, 1, "", f"{name} failed")
        return CommandResult(command, 0, "", "")


class HealthHarness:
    def __init__(self, runtime_python):
        self.ready = True
        self.sequence = 0
        self.runtime_python = runtime_python

    def __call__(self):
        self.sequence += 1
        return {
            "status": "healthy" if self.ready else "degraded",
            "maintenance_active_sessions": 0,
            "gateway_active_sessions": 0,
            "maintenance_drain": {"active": True, "valid": True},
            "readiness": {
                "status": "ready" if self.ready else "degraded",
                "runtime_ready": self.ready,
                "last_sequence": self.sequence,
                "last_seen_age_seconds": 0,
                "runtime_id_hash": "2" * 64,
                "admission_draining": self.sequence <= 3,
                "active_work_count_complete": True,
                "drain_home_verified": True,
            },
            "package_version": "4.2.0",
            "process_pid": 222,
            "python_identity": _python_identity_for_fixture(self.runtime_python),
        }


def _python_identity_for_fixture(python_path):
    canonical = os.path.normcase(
        str(Path(python_path).parent.resolve(strict=False) / Path(python_path).name)
    )
    material = b"hermes-feishu-streaming-card:python-executable:v1\0" + os.fsencode(
        canonical
    )
    return f"python-sha256:{hashlib.sha256(material).hexdigest()}"


def test_supervisor_restores_updates_reinstalls_and_verifies(maintenance_fixture):
    commands = CommandHarness(maintenance_fixture)
    published = []

    result = run_job(
        maintenance_fixture.job.path,
        run=commands,
        fetch_health=HealthHarness(commands.runtime_python),
        publish=lambda current: published.append(current.phase) or True,
        sleep=lambda delay: None,
        maintenance_python=Path("/maintenance/bin/python"),
    )

    assert result.phase == "succeeded"
    assert commands.mutations == [
        "gateway-stop",
        "sidecar-stop",
        "hfc-restore",
        "hermes-update",
        "pinned-wheel-install",
        "hfc-install",
        "sidecar-start",
        "gateway-restart",
    ]
    assert result.result["hermes_version"] == "0.19.2"
    assert result.result["hfc_version"] == "4.2.0"
    assert result.result["import_origin"] == "site-packages"
    assert result.result["service_status"] == "ready"
    assert published == [
        "locking",
        "draining",
        "restoring_hooks",
        "updating_hermes",
        "reinstalling_hfc",
        "starting_services",
        "verifying",
        "succeeded",
    ]


def test_card_failure_before_mutation_aborts_without_commands(maintenance_fixture):
    commands = CommandHarness(maintenance_fixture)

    result = run_job(
        maintenance_fixture.job.path,
        run=commands,
        fetch_health=HealthHarness(commands.runtime_python),
        publish=lambda current: False,
        maintenance_python=Path("/maintenance/bin/python"),
    )

    assert result.phase == "failed"
    assert result.result["recovery_boundary"] == "no_mutation"
    assert commands.mutations == []


def test_official_update_failure_never_runs_custom_git_recovery(
    maintenance_fixture,
):
    commands = CommandHarness(maintenance_fixture)
    commands.fail_at = "hermes-update"

    result = run_job(
        maintenance_fixture.job.path,
        run=commands,
        fetch_health=HealthHarness(commands.runtime_python),
        publish=lambda current: True,
        maintenance_python=Path("/maintenance/bin/python"),
    )

    assert result.phase == "failed"
    assert result.result["error_code"] == "hermes_update_failed"
    assert result.result["recovery_boundary"] == "old_hfc_restored"
    assert commands.mutations == [
        "gateway-stop",
        "sidecar-stop",
        "hfc-restore",
        "hermes-update",
        "pinned-wheel-install",
        "hfc-install",
        "sidecar-start",
        "gateway-restart",
    ]
    assert all(
        command not in {"reset", "checkout", "stash"}
        for mutation in commands.mutations
        for command in mutation.split()
    )


def test_resume_after_completed_update_does_not_run_update_again(
    maintenance_fixture,
):
    commands = CommandHarness(maintenance_fixture)
    commands.actual_head = "e" * 40
    commands.fixture.state["version"] = "0.19.2"
    commands.fixture.state["hook"] = "clean"
    commands.git_status = ""
    commands.runtime_python.parent.mkdir(parents=True, exist_ok=True)
    commands.runtime_python.write_text("#!python\n", encoding="utf-8")
    commands.package_location.parent.mkdir(parents=True, exist_ok=True)
    commands.package_location.write_text('__version__="4.2.0"\n', encoding="utf-8")
    transition_job(
        maintenance_fixture.job.path,
        expected_phase="locking",
        phase="draining",
    )
    transition_job(
        maintenance_fixture.job.path,
        expected_phase="draining",
        phase="restoring_hooks",
    )
    transition_job(
        maintenance_fixture.job.path,
        expected_phase="restoring_hooks",
        phase="updating_hermes",
    )

    result = run_job(
        maintenance_fixture.job.path,
        run=commands,
        fetch_health=HealthHarness(commands.runtime_python),
        publish=lambda current: True,
        maintenance_python=Path("/maintenance/bin/python"),
    )

    assert result.phase == "succeeded"
    assert commands.hermes_update_calls == 0
    assert commands.pinned_install_calls == 1


def test_resume_rejects_foreign_post_update_head(maintenance_fixture):
    commands = CommandHarness(maintenance_fixture)
    commands.actual_head = "f" * 40
    commands.git_status = ""
    commands.fixture.state["hook"] = "clean"
    commands.fixture.state["version"] = "0.19.2"
    transition_job(
        maintenance_fixture.job.path,
        expected_phase="locking",
        phase="draining",
    )
    transition_job(
        maintenance_fixture.job.path,
        expected_phase="draining",
        phase="restoring_hooks",
    )
    transition_job(
        maintenance_fixture.job.path,
        expected_phase="restoring_hooks",
        phase="updating_hermes",
    )

    result = run_job(
        maintenance_fixture.job.path,
        run=commands,
        fetch_health=HealthHarness(commands.runtime_python),
        publish=lambda current: True,
        maintenance_python=Path("/maintenance/bin/python"),
    )

    assert result.phase == "failed"
    assert result.result["error_code"] == "post_update_target_mismatch"
    assert result.result["service_status"] == "ready"
    assert result.result["recovery_boundary"] == "new_hfc_restored"
    assert commands.hermes_update_calls == 0
    assert commands.pinned_install_calls == 1


def test_repeated_terminal_run_is_idempotent(maintenance_fixture):
    commands = CommandHarness(maintenance_fixture)
    completed = transition_job(
        maintenance_fixture.job.path,
        expected_phase="locking",
        phase="failed",
        result={
            "error_code": "preflight_failed",
            "recovery_boundary": "no_mutation",
        },
    )

    result = run_job(
        completed.path,
        run=commands,
        fetch_health=HealthHarness(commands.runtime_python),
        publish=lambda current: True,
        maintenance_python=Path("/maintenance/bin/python"),
    )

    assert result == load_job(completed.path)
    assert commands.mutations == []


def test_runner_initialization_failure_terminalizes_job_and_releases_fence(
    maintenance_fixture,
    monkeypatch,
):
    monkeypatch.setattr(
        maintenance_runner_module,
        "load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad config")),
    )
    monkeypatch.setattr(
        maintenance_runner_module,
        "set_gateway_external_drain",
        lambda *_args, **_kwargs: True,
    )

    code = maintenance_runner_module.main(
        ["--job", str(maintenance_fixture.job.path)]
    )

    failed = load_job(maintenance_fixture.job.path)
    assert code == 1
    assert failed.phase == "failed"
    assert failed.result["error_code"] == "runner_initialization_failed"
    assert load_active_drain_lease(maintenance_fixture.paths) is None
