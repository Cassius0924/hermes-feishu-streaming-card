from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hermes_v2026_4_23"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _run_cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "hermes_feishu_card.cli", *args],
        check=False,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )


def _legacy_git_install(tmp_path: Path):
    root = tmp_path / "hermes"
    shutil.copytree(FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "HFC Test")
    _git(root, "config", "user.email", "hfc@example.invalid")
    _git(root, "add", "gateway/run.py")
    _git(root, "commit", "-qm", "initial Hermes")
    installed = _run_cli("install", "--hermes-dir", str(root), "--yes")
    assert installed.returncode == 0, installed.stderr
    manifest_path = root / ".hermes_feishu_card_manifest"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["integrity"]["version"] == 2
    manifest.pop("integrity", None)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    return root, manifest_path


def test_integrity_migrate_safe_preserves_yaml_and_updates_private_env(tmp_path):
    root, manifest_path = _legacy_git_install(tmp_path)
    config = tmp_path / "config.yaml"
    original_config = "# keep this comment\nserver:\n  port: 8765\n"
    config.write_text(original_config, encoding="utf-8")

    result = _run_cli(
        "integrity",
        "migrate-safe",
        "--config",
        str(config),
        "--hermes-dir",
        str(root),
        "--yes",
    )

    assert result.returncode == 0, result.stderr
    assert "integrity mode: safe" in result.stdout
    assert config.read_text(encoding="utf-8") == original_config
    assert (
        "HERMES_FEISHU_CARD_INTEGRITY_MODE=safe"
        in (tmp_path / ".env").read_text(encoding="utf-8")
    )
    assert json.loads(manifest_path.read_text())["integrity"]["version"] == 2
    assert str(root) not in result.stdout


def test_integrity_migrate_safe_refuses_user_edits_without_changing_env(tmp_path):
    root, _manifest_path = _legacy_git_install(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("server: {}\n", encoding="utf-8")
    run_py = root / "gateway" / "run.py"
    run_py.write_text(run_py.read_text(encoding="utf-8") + "# user edit\n")

    result = _run_cli(
        "integrity",
        "migrate-safe",
        "--config",
        str(config),
        "--hermes-dir",
        str(root),
        "--yes",
    )

    assert result.returncode == 1
    assert "error:" in result.stderr
    assert not (tmp_path / ".env").exists()
