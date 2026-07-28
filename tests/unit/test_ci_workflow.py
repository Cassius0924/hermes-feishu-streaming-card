from __future__ import annotations

from pathlib import Path

import yaml


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
    assert "--detach --wait --wait-timeout 180" in text
    assert "docker compose -f docker-compose.smoke.yml run" in text
    assert "--rm --no-deps probe" in text
    assert "--abort-on-container-exit" not in text


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


def test_docker_compose_runtime_smoke_uses_published_install_topology():
    source = (ROOT / "docker-compose.smoke.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(source)
    services = compose["services"]

    assert set(services) == {"setup", "sidecar", "gateway", "probe"}
    setup = services["setup"]
    sidecar = services["sidecar"]
    gateway = services["gateway"]
    probe = services["probe"]

    assert setup["user"] == "0:0"
    for service in (sidecar, gateway, probe):
        assert service["user"] == "65532:65532"
        assert service.get("privileged") is not True

    setup_command = "\n".join(setup["command"])
    assert "/src/install-docker.sh" in setup_command
    assert "tests/fixtures/hermes_v2026_4_23" in setup_command
    assert setup["environment"]["HFC_INSTALL_SOURCE"] == "/src"
    assert setup["environment"]["HFC_TEST_NOOP_DELIVERY"] == "1"
    assert setup["environment"]["HFC_SKIP_START"] == "1"
    assert sidecar["depends_on"]["setup"]["condition"] == "service_completed_successfully"
    assert gateway["depends_on"]["sidecar"]["condition"] == "service_healthy"
    assert probe["depends_on"]["gateway"]["condition"] == "service_healthy"

    for service in services.values():
        volumes = service["volumes"]
        assert any("hermes-runtime:/opt/hermes" in volume for volume in volumes)
        assert any("hermes-data:/opt/data" in volume for volume in volumes)
        assert any("hfc-smoke-state:/opt/hfc-state" in volume for volume in volumes)

    assert "privileged:" not in source
    assert "systemd" not in source.lower()


def test_docker_compose_runtime_smoke_executes_patched_gateway_hook():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.smoke.yml").read_text(encoding="utf-8")
    )
    config = yaml.safe_load(
        (ROOT / "tests" / "fixtures" / "docker-smoke-config.yaml").read_text(
            encoding="utf-8"
        )
    )
    fixture = (
        ROOT / "tests" / "fixtures" / "docker_gateway_smoke.py"
    ).read_text(encoding="utf-8")
    gateway = compose["services"]["gateway"]
    probe = compose["services"]["probe"]
    probe_command = "\n".join(probe["command"])

    assert "docker_gateway_smoke.py" in "\n".join(gateway["command"])
    assert "healthcheck" in gateway
    assert "HERMES_FEISHU_CARD_PATCH_BEGIN" in fixture
    assert "_handle_message_with_agent" in fixture
    assert "gateway-result.json" in fixture
    assert 'health["metrics"]["runtime_control_events_received"] >= 1' in probe_command
    assert 'health["metrics"]["runtime_control_events_accepted"] >= 1' in probe_command
    assert 'health["metrics"]["events_received"] >= 1' in probe_command
    assert 'health["metrics"]["event_auth_rejections"] == 0' in probe_command
    assert 'health["readiness"]["status"] == "ready"' in probe_command
    assert 'receipt["patched_events_before_direct"] >= 1' in probe_command
    assert 'receipt["event_response"]["disposition"] == "native"' in probe_command
    assert "sign_event_request" not in probe_command
    assert config["feishu"] == {"app_id": "", "app_secret": ""}
    assert config["integrity"] == {"mode": "safe"}
