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
    assert "docker compose -f docker-compose.smoke.yml logs" in text
    assert "--no-color setup sidecar gateway" in text
    assert "--abort-on-container-exit" not in text


def test_release_assets_workflow_supports_manual_package_dry_run():
    workflow = ROOT / ".github" / "workflows" / "release-assets.yml"

    text = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "inputs:" in text
    assert "tag_ref:" in text
    assert "Build install packages" in text
    assert "gh release upload" in text
    assert "install-docker.sh" in text
    assert "docker-compose.example.yml" in text


def test_release_workflow_packages_only_after_reusable_test_gate():
    text = (ROOT / ".github" / "workflows" / "release-assets.yml").read_text(
        encoding="utf-8"
    )

    assert "resolve-release:" in text
    assert "release-gate:" in text
    assert "uses: ./.github/workflows/tests.yml" in text
    assert "needs: [resolve-release, release-gate]" in text
    assert "checkout_ref: ${{ needs.resolve-release.outputs.commit }}" in text


def test_release_write_permission_is_scoped_to_package_job():
    text = (ROOT / ".github" / "workflows" / "release-assets.yml").read_text(
        encoding="utf-8"
    )
    prefix, package = text.split("  package:", 1)

    assert "permissions:\n  contents: read" in prefix
    assert "contents: write" not in prefix
    assert "permissions:\n      contents: write" in package


def test_reusable_ci_checks_out_requested_ref_in_every_job():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
    )
    checkout_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses") == "actions/checkout@v4"
    ]

    assert checkout_steps
    assert all(
        step.get("with", {}).get("ref")
        == "${{ inputs.checkout_ref || github.sha }}"
        for step in checkout_steps
    )


def test_reusable_ci_runs_powershell_installer_tests():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )

    assert "Test Windows installer contract" in text
    assert (
        'python -m pytest tests/unit/test_install_scripts.py -k "powershell" -q'
        in text
    )
    assert "gh release upload" not in text
    assert "gh release create" not in text


def test_release_package_reruns_full_gate_after_exact_checkout():
    text = (ROOT / ".github" / "workflows" / "release-assets.yml").read_text(
        encoding="utf-8"
    )
    package = text.split("  package:", 1)[1]

    checkout_index = package.index(
        "ref: ${{ needs.resolve-release.outputs.commit }}"
    )
    verifier_positions = []
    start = 0
    needle = "python scripts/verify_release.py"
    while True:
        position = package.find(needle, start)
        if position < 0:
            break
        verifier_positions.append(position)
        start = position + len(needle)
    build_index = package.index("Build install packages")
    upload_index = min(
        package.index("gh release upload"),
        package.index("gh release create"),
    )

    assert len(verifier_positions) == 2
    assert checkout_index < verifier_positions[0] < build_index
    assert build_index < verifier_positions[1] < upload_index
    assert package.count("--require-main-ancestor") == 2


def test_release_dispatch_accepts_only_full_semver_tag_ref():
    text = (ROOT / ".github" / "workflows" / "release-assets.yml").read_text(
        encoding="utf-8"
    )

    assert "tag_ref:" in text
    assert "^refs/tags/v[0-9]+\\.[0-9]+\\.[0-9]+$" in text
    assert 'tag="${tag_ref#refs/tags/}"' in text


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
    assert setup["environment"]["HFC_INSTALL_SOURCE"] == "/tmp/hfc-install-source"
    assert "mkdir -p /tmp/hfc-install-source" in setup_command
    assert "cp /src/pyproject.toml /src/README.md /src/LICENSE" in setup_command
    assert "cp -R /src/hermes_feishu_card" in setup_command
    assert ".:/src:ro" in setup["volumes"]
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
