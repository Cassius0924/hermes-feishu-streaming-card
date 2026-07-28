from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_STATE_DIR = Path.home() / ".hermes_feishu_card"
PIDFILE_NAME = "sidecar.pid"
LOGFILE_NAME = "sidecar.log"
SYSTEMD_UNIT_NAME = "hermes-feishu-card-sidecar.service"
SERVICE_MANAGER_VALUES = frozenset(
    {"auto", "systemd-user", "systemd-system", "detached"}
)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def process_token_hash(token: str | None) -> str:
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def status_sidecar(config: dict[str, dict[str, Any]]) -> dict[str, Any]:
    record = read_pid_record()
    if record is not None and not _record_identity_valid(record):
        record = None
    pid = record["pid"] if record is not None else None
    health = fetch_health(config)
    if (
        record is not None
        and _record_manager(record) in {"systemd-user", "systemd-system"}
        and health is not None
        and _record_matches_health(record, health)
    ):
        pid = health["process_pid"]
    running = health is not None
    return {
        "running": running,
        "pid": pid,
        "health": health,
        "pid_running": pid_is_running(pid) if pid is not None else False,
        "manager": _record_manager(record) if record is not None else "unknown",
        "unit": record.get("unit", "") if record is not None else "",
    }


def start_sidecar(
    config_path: str | Path,
    config: dict[str, dict[str, Any]],
    *,
    env_file: str | Path | None = None,
) -> str:
    selected_manager, manager_error = _select_service_manager(config)
    if manager_error:
        return manager_error
    state_error = _prepare_private_state_dir()
    if state_error:
        return state_error
    health = fetch_health(config)
    record = read_pid_record()
    record_path = pid_path()
    try:
        record_file_exists = record_path.exists() or record_path.is_symlink()
    except OSError:
        return "failed: pidfile state could not be inspected; start refused"
    if health is not None:
        if record is None or not _record_identity_valid(record):
            return (
                "failed: running sidecar has no verified pidfile; "
                "manager transition refused"
            )
        if not _record_matches_health(record, health):
            return "failed: running sidecar identity mismatch; migration refused"
        if _record_manager(record) == selected_manager:
            return "already running"
        if not _stop_owned_record(record):
            return "failed: owned sidecar could not be stopped for manager migration"
        clear_pid()
    elif record is None and record_file_exists:
        return "failed: invalid pidfile exists; start refused"
    elif record is not None:
        if _record_manager(record) != "detached" or pid_is_running(record["pid"]):
            return "failed: owned sidecar health is unavailable; start refused"
        clear_pid()

    token = secrets.token_hex(16)
    command = _sidecar_command(config_path, env_file=env_file, token=token)

    if selected_manager in {"systemd-user", "systemd-system"}:
        unit = _expected_unit(selected_manager)
        if selected_manager == "systemd-user":
            started = _start_systemd_user_sidecar(command)
            failure = "failed: systemd user service could not be started"
        else:
            started = _start_systemd_system_sidecar(command, unit)
            failure = (
                "failed: systemd system service could not be started; "
                "verify explicit caller permission"
            )
        if not started:
            return failure
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            health = fetch_health(config)
            if (
                health is not None
                and _health_matches_token(health, token)
            ):
                try:
                    write_pid_record(
                        health["process_pid"],
                        token,
                        manager=selected_manager,
                        unit=unit,
                    )
                except (OSError, ValueError) as exc:
                    _stop_systemd_sidecar(selected_manager, unit)
                    return f"failed: pidfile could not be written: {exc.__class__.__name__}"
                return "started"
            time.sleep(0.1)
        _stop_systemd_sidecar(selected_manager, unit)
        clear_pid()
        return "failed: health check timed out"

    log_handle = log_path().open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()

    try:
        write_pid_record(process.pid, token)
    except (OSError, ValueError) as exc:
        stop_pid(process.pid)
        return f"failed: pidfile could not be written: {exc.__class__.__name__}"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            clear_pid()
            return f"failed: process exited with {process.returncode}"
        health = fetch_health(config)
        detached_record = {
            "pid": process.pid,
            "token": token,
            "manager": "detached",
        }
        if health is not None and _record_matches_health(detached_record, health):
            return "started"
        time.sleep(0.1)

    stop_pid(process.pid)
    clear_pid()
    return "failed: health check timed out"


def stop_sidecar(config: dict[str, dict[str, Any]]) -> str:
    record = read_pid_record()
    if record is None:
        if fetch_health(config) is not None:
            return "failed: running sidecar has no pidfile"
        return "not running"

    if not _record_identity_valid(record):
        return "failed: pidfile manager identity mismatch"
    pid = record["pid"]
    manager = _record_manager(record)
    health = fetch_health(config)
    if manager in {"systemd-user", "systemd-system"}:
        if health is None or not _record_matches_health(record, health):
            return "failed: pidfile identity mismatch"
        unit = str(record["unit"])
        if not _stop_systemd_sidecar(manager, unit):
            label = "user" if manager == "systemd-user" else "system"
            return f"failed: systemd {label} service could not be stopped"
        clear_pid()
        return "stopped"
    if health is None:
        if pid_is_running(pid):
            return "failed: pidfile identity mismatch"
        clear_pid()
        return "not running"
    if not _record_matches_health(record, health):
        return "failed: pidfile identity mismatch"

    if pid_is_running(pid):
        stop_pid(pid)
    clear_pid()
    return "stopped"


def fetch_health(config: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    server = config["server"]
    host = str(server["host"])
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    url = f"http://{url_host}:{server['port']}/health"
    try:
        with _open_health_url(url, timeout=0.4) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if isinstance(payload, dict) and payload.get("status") in {"healthy", "degraded"}:
        return payload
    return None


def _open_health_url(url: str, timeout: float):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return _NO_PROXY_OPENER.open(urllib.request.Request(url), timeout=timeout)
    return urllib.request.urlopen(url, timeout=timeout)


def read_pid() -> int | None:
    record = read_pid_record()
    return record["pid"] if record is not None else None


def read_pid_record() -> dict[str, Any] | None:
    path = pid_path()
    if path.is_symlink():
        return None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            text = handle.read(4097)
    except (OSError, UnicodeError):
        return None
    if len(text) > 4096:
        return None
    text = text.strip()
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    token = record.get("token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(token, str) or not token:
        return None
    manager = record.get("manager", "detached")
    result = {"pid": pid, "token": token, "manager": manager}
    unit = record.get("unit")
    if unit is not None:
        result["unit"] = unit
    return result if _record_identity_valid(result) else None


def write_pid_record(
    pid: int,
    token: str,
    *,
    manager: str = "detached",
    unit: str = "",
) -> None:
    payload: dict[str, Any] = {"pid": pid, "token": token, "manager": manager}
    if unit:
        payload["unit"] = unit
    if not _record_identity_valid(payload):
        raise ValueError("invalid pidfile manager identity")
    path = pid_path()
    if path.is_symlink():
        raise ValueError("pidfile must not be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise
        with handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ValueError("pidfile must not be a symbolic link")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sidecar_command(
    config_path: str | Path,
    *,
    env_file: str | Path | None,
    token: str,
) -> list[str]:
    resolved_config = Path(config_path).expanduser().resolve(strict=False)
    command = [
        sys.executable,
        "-m",
        "hermes_feishu_card.runner",
        "--config",
        str(resolved_config),
    ]
    if env_file is not None:
        resolved_env = Path(env_file).expanduser().resolve(strict=False)
        command.extend(("--env-file", str(resolved_env)))
    command.extend(("--token", token))
    return command


def _prepare_private_state_dir() -> str:
    private_state_dir = state_dir()
    try:
        if private_state_dir.is_symlink():
            return "failed: state directory must not be a symbolic link"
        private_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if private_state_dir.is_symlink() or not private_state_dir.is_dir():
            return "failed: state directory is not a private directory"
        private_state_dir.chmod(0o700)
    except OSError:
        return "failed: state directory could not be prepared"
    return ""


def _record_manager(record: dict[str, Any]) -> str:
    manager = record.get("manager", "detached")
    return manager if isinstance(manager, str) else ""


def _expected_unit(manager: str) -> str:
    if manager == "systemd-user":
        return SYSTEMD_UNIT_NAME
    if manager == "systemd-system":
        return _systemd_system_unit_name()
    return ""


def _record_identity_valid(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    token = record.get("token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not isinstance(token, str) or not token:
        return False
    manager = _record_manager(record)
    unit = record.get("unit", "")
    if manager == "detached":
        return unit in {"", None}
    if manager not in {"systemd-user", "systemd-system"}:
        return False
    return isinstance(unit, str) and unit == _expected_unit(manager)


def _record_matches_health(
    record: dict[str, Any], health: dict[str, Any]
) -> bool:
    if health.get("process_token_hash") != process_token_hash(record.get("token")):
        return False
    health_pid = health.get("process_pid")
    if not isinstance(health_pid, int) or isinstance(health_pid, bool) or health_pid <= 0:
        return False
    if _record_manager(record) == "detached":
        return health_pid == record.get("pid")
    return True


def _health_matches_token(health: dict[str, Any], token: str) -> bool:
    health_pid = health.get("process_pid")
    return (
        health.get("process_token_hash") == process_token_hash(token)
        and isinstance(health_pid, int)
        and not isinstance(health_pid, bool)
        and health_pid > 0
    )


def _stop_owned_record(record: dict[str, Any]) -> bool:
    manager = _record_manager(record)
    if manager == "detached":
        # The caller has already matched PID + token against live health.
        # stop_pid() is itself tolerant of a process exiting between checks.
        stop_pid(record["pid"])
        return True
    return _stop_systemd_sidecar(manager, str(record["unit"]))


def _stop_systemd_sidecar(manager: str, unit: str) -> bool:
    if manager == "systemd-user":
        return _stop_systemd_user_sidecar(unit)
    if manager == "systemd-system":
        return _stop_systemd_system_sidecar(unit)
    return False


def _systemd_system_unit_name() -> str:
    scope = f"{os.getuid()}:{state_dir().expanduser().resolve(strict=False)}"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
    return f"hermes-feishu-card-sidecar-{os.getuid()}-{digest}.service"


def _systemd_user_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _systemd_system_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--system", "--no-ask-password", "show-environment"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _select_service_manager(
    config: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    service = config.get("service", {})
    requested = service.get("manager", "auto") if isinstance(service, dict) else "auto"
    if requested not in SERVICE_MANAGER_VALUES:
        return "", "failed: invalid service.manager"
    if requested == "auto":
        return ("systemd-user", "") if _systemd_user_available() else ("detached", "")
    if requested == "detached":
        return "detached", ""
    if requested == "systemd-user":
        if _systemd_user_available():
            return requested, ""
        return (
            "",
            "failed: service.manager=systemd-user is unavailable; "
            "start the user systemd manager or select detached",
        )
    if _systemd_system_available():
        return requested, ""
    return (
        "",
        "failed: service.manager=systemd-system is unavailable; "
        "it requires Linux with systemd-run and systemctl",
    )


def _start_systemd_user_sidecar(command: list[str]) -> bool:
    log_file = log_path()
    try:
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                f"--unit={SYSTEMD_UNIT_NAME}",
                "--collect",
                _systemd_state_environment_arg(),
                "--property=Type=exec",
                "--property=Restart=on-failure",
                "--property=RestartSec=2s",
                f"--property=StandardOutput=append:{log_file}",
                f"--property=StandardError=append:{log_file}",
                "--",
                *command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _start_systemd_system_sidecar(command: list[str], unit: str) -> bool:
    if unit != _systemd_system_unit_name():
        return False
    log_file = log_path()
    try:
        result = subprocess.run(
            [
                "systemd-run",
                "--system",
                "--no-ask-password",
                f"--unit={unit}",
                "--collect",
                _systemd_state_environment_arg(),
                f"--uid={os.getuid()}",
                f"--gid={os.getgid()}",
                "--property=Type=exec",
                "--property=Restart=on-failure",
                "--property=RestartSec=2s",
                "--property=UMask=0077",
                "--property=NoNewPrivileges=yes",
                f"--property=StandardOutput=append:{log_file}",
                f"--property=StandardError=append:{log_file}",
                "--",
                *command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _systemd_state_environment_arg() -> str:
    return f"--setenv=HERMES_FEISHU_CARD_STATE_DIR={state_dir().expanduser()}"


def _stop_systemd_user_sidecar(unit: str) -> bool:
    if unit != SYSTEMD_UNIT_NAME:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _stop_systemd_system_sidecar(unit: str) -> bool:
    if unit != _systemd_system_unit_name():
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--system", "--no-ask-password", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def clear_pid() -> None:
    pid_path().unlink(missing_ok=True)


def pid_is_running(pid: int) -> bool:
    if sys.platform == "win32":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_pid(pid: int) -> None:
    if sys.platform == "win32":
        _stop_pid_windows(pid)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not pid_is_running(pid):
                return
            time.sleep(0.05)


def _pid_is_running_windows(pid: int) -> bool:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        process_handle = kernel32.OpenProcess(0x1000, False, pid)
        if process_handle:
            kernel32.CloseHandle(process_handle)
            return True
        return False
    except Exception:
        return False


def _stop_pid_windows(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return
        time.sleep(0.05)


def pid_path() -> Path:
    return state_dir() / PIDFILE_NAME


def log_path() -> Path:
    return state_dir() / LOGFILE_NAME


def state_dir() -> Path:
    configured = os.environ.get("HERMES_FEISHU_CARD_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_STATE_DIR
