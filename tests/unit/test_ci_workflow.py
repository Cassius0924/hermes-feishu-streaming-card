from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_github_actions_runs_full_pytest_matrix():
    workflow = ROOT / ".github" / "workflows" / "tests.yml"

    text = workflow.read_text(encoding="utf-8")

    assert "pull_request:" in text
    assert "push:" in text
    assert 'python-version: ["3.9", "3.12"]' in text
    assert 'python -m pip install -e ".[test]"' in text
    assert "python -m pytest -q" in text
    assert "powershell-installer:" in text
    assert "runs-on: windows-latest" in text
    assert "ParseFile" in text
    assert "install.ps1" in text
    assert "docker-compose-runtime-smoke:" in text
    assert "docker compose -f docker-compose.example.yml config --quiet" in text
    assert "docker compose -f docker-compose.smoke.yml up" in text
    assert "--abort-on-container-exit" in text
    assert "--exit-code-from probe" in text


def test_release_assets_workflow_supports_manual_package_dry_run():
    workflow = ROOT / ".github" / "workflows" / "release-assets.yml"

    text = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "inputs:" in text
    assert "tag:" in text
    assert "Build install packages" in text
    assert "gh release upload" in text
    assert "install-docker.sh" in text
    assert "docker-compose.example.yml" in text


def test_docker_compose_example_documents_container_paths():
    compose = (ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")

    assert "image: your-hermes-image:latest" in compose
    assert "/opt/hermes" in compose
    assert "/opt/data" in compose
    assert "FEISHU_APP_ID" in compose
    assert "FEISHU_APP_SECRET" in compose
    assert "install-docker.sh" in compose


def test_docker_compose_runtime_smoke_bootstraps_before_unprivileged_sidecar():
    compose = (ROOT / "docker-compose.smoke.yml").read_text(encoding="utf-8")
    config = (
        ROOT / "tests" / "fixtures" / "docker-smoke-config.yaml"
    ).read_text(encoding="utf-8")

    assert "python:3.12-slim" in compose
    assert "setup:" in compose
    assert "sidecar:" in compose
    assert "probe:" in compose
    assert "condition: service_completed_successfully" in compose
    assert "condition: service_healthy" in compose
    assert 'user: "65532:65532"' in compose
    assert "python -m venv /venv" in compose
    assert "- /venv/bin/python" in compose
    assert "- hermes_feishu_card.runner" in compose
    assert "python -m pip install" not in compose
    assert "chmod 700 /state" not in compose
    assert "HERMES_FEISHU_CARD_STATE_DIR" in compose
    assert "privileged:" not in compose
    assert "systemd" not in compose.lower()
    assert "allow_non_loopback: true" in config
    assert "manager: detached" in config


def test_docker_compose_runtime_smoke_sends_authenticated_event_from_shared_state():
    compose = (ROOT / "docker-compose.smoke.yml").read_text(encoding="utf-8")
    config = (
        ROOT / "tests" / "fixtures" / "docker-smoke-config.yaml"
    ).read_text(encoding="utf-8")

    assert "read_transport_root_secret" in compose
    assert "sign_event_request" in compose
    assert 'opener.open(request, timeout=5)' in compose
    assert 'assert result["disposition"] == "native"' in compose
    assert 'assert health["metrics"]["events_received"] == 1' in compose
    assert 'assert health["metrics"]["event_auth_rejections"] == 0' in compose
    assert "hfc-smoke-runtime:/venv" in compose
    assert "hfc-smoke-state:/state:ro" in compose
    assert "native_chats:" in config
    assert "smoke-native-chat" in config
