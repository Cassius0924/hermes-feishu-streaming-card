from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from hermes_feishu_card.install import detect
from hermes_feishu_card.install import native_hooks
from hermes_feishu_card.install.native_hooks import (
    FIXED_TAG_COMMIT,
    FIXED_TAG_PROVENANCE_PATH,
    FIXED_TAG_PROVENANCE_SHA256,
    HFC_REGISTERED_HOOKS,
    FixedTagNativeHookProvenance,
    NativeHookAnchorProvenance,
    NativeHookSourceProvenance,
    NativeCapabilityStatus,
    NativeHookCapabilityProbe,
    NativeHookSourceDigest,
    load_fixed_tag_native_hook_provenance,
    probe_native_hook_capabilities,
    verify_provenance_slices,
)
from hermes_feishu_card.integration import (
    HYBRID_REQUIRED_PATCH_GROUPS,
    PatchCapabilities,
    select_integration_mode,
)


_SOURCE_BY_TARGET = {
    "plugin_manager": '''
import importlib.metadata

def _get_enabled_plugins():
    config = load_config()
    plugins_cfg = config.get("plugins")
    if not isinstance(plugins_cfg, dict) or "enabled" not in plugins_cfg:
        return None
    enabled = plugins_cfg.get("enabled")
    if not isinstance(enabled, list):
        return None
    return set(enabled)

class PluginContext:
    def register_hook(self, hook_name, callback):
        self._manager._hooks.setdefault(hook_name, []).append(callback)

class PluginManager:
    def _scan_entry_points(self):
        eps = importlib.metadata.entry_points()
        group_eps = eps.select(group="hermes_agent.plugins")
        return [PluginManifest(name=ep.name, source="entrypoint", path=ep.value, key=ep.name) for ep in group_eps]

    def _discover_and_load_inner(self):
        manifests = self._scan_entry_points()
        enabled = _get_enabled_plugins()
        for manifest in manifests:
            lookup_key = manifest.key or manifest.name
            is_enabled = enabled is not None and (lookup_key in enabled or manifest.name in enabled)
            if not is_enabled:
                continue
            self._load_plugin(manifest)

    def _load_entrypoint_module(self, manifest):
        eps = importlib.metadata.entry_points()
        for ep in eps.select(group="hermes_agent.plugins"):
            if ep.name == manifest.name:
                return ep.load()
        raise ImportError

    def _load_plugin(self, manifest):
        module = self._load_entrypoint_module(manifest)
        register_fn = getattr(module, "register", None)
        ctx = PluginContext(manifest, self)
        register_fn(ctx)

    def invoke_hook(self, hook_name, **kwargs):
        callbacks = self._hooks.get(hook_name, [])
        results = []
        for cb in callbacks:
            try:
                ret = cb(**kwargs)
                if ret is not None:
                    results.append(ret)
            except Exception:
                pass
        return results

def _get_pre_tool_call_directive_details(tool_name, args, task_id="", session_id="", tool_call_id="", turn_id="", api_request_id="", middleware_trace=None):
    return invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, task_id=task_id,
        session_id=session_id, tool_call_id=tool_call_id, turn_id=turn_id,
        api_request_id=api_request_id, middleware_trace=middleware_trace,
    )
''',
    "turn_context": '''
def prepare_turn(agent, effective_task_id, turn_id, original_user_message, messages, conversation_history):
    agent._current_turn_id = turn_id
    results = invoke_hook(
        "pre_llm_call", session_id=agent.session_id, task_id=effective_task_id,
        turn_id=turn_id, user_message=original_user_message,
        conversation_history=list(messages), is_first_turn=(not bool(conversation_history)),
        model=agent.model, platform=agent.platform, parent_session_id="", sender_id="",
    )
    return results
''',
    "turn_finalizer": '''
def finalize(agent, effective_task_id, turn_id, original_user_message, final_response, messages, completed, failed, interrupted, turn_exit_reason):
    if final_response and not interrupted:
        invoke_hook(
            "post_llm_call", session_id=agent.session_id, task_id=effective_task_id,
            turn_id=turn_id, user_message=original_user_message,
            assistant_response=final_response, conversation_history=list(messages),
            model=agent.model, platform=agent.platform,
        )
    result = {"completed": completed, "failed": failed, "interrupted": interrupted}
    invoke_hook(
        "on_session_end", session_id=agent.session_id, task_id=effective_task_id,
        turn_id=turn_id, completed=completed, failed=failed, interrupted=interrupted,
        turn_exit_reason=turn_exit_reason, model=agent.model, platform=agent.platform,
    )
    return result
''',
    "tool_hooks": '''
def _emit_post_tool_call_hook(function_name, function_args, result, task_id, session_id, tool_call_id, turn_id, api_request_id, duration_ms, status, error_type, error_message, middleware_trace):
    invoke_hook(
        "post_tool_call", tool_name=function_name, args=function_args, result=result,
        task_id=task_id, session_id=session_id, tool_call_id=tool_call_id,
        turn_id=turn_id, api_request_id=api_request_id, duration_ms=duration_ms,
        status=status, error_type=error_type, error_message=error_message,
        middleware_trace=list(middleware_trace),
    )

def handle_function_call(function_name, function_args, task_id, session_id, tool_call_id, turn_id, api_request_id):
    block_message = resolve_pre_tool_block(
        function_name, function_args, task_id=task_id, session_id=session_id,
        tool_call_id=tool_call_id, turn_id=turn_id, api_request_id=api_request_id,
        middleware_trace=[],
    )
    tokens = set_current_observability_context(turn_id=turn_id, tool_call_id=tool_call_id)
    try:
        result = registry.dispatch(function_name, function_args, task_id=task_id, session_id=session_id)
    finally:
        reset_current_observability_context(tokens)
    _emit_post_tool_call_hook(
        function_name=function_name, function_args=function_args, result=result,
        task_id=task_id, session_id=session_id, tool_call_id=tool_call_id,
        turn_id=turn_id, api_request_id=api_request_id, duration_ms=1,
        status="ok", error_type=None, error_message=None, middleware_trace=[],
    )
    return result
''',
    "approval": '''
import contextvars
_approval_turn_id = contextvars.ContextVar("approval_turn_id", default="")
_approval_tool_call_id = contextvars.ContextVar("approval_tool_call_id", default="")

def _fire_approval_hook(hook_name, **kwargs):
    kwargs.setdefault("turn_id", _approval_turn_id.get())
    kwargs.setdefault("tool_call_id", _approval_tool_call_id.get())
    invoke_hook(hook_name, **kwargs)

def _await_gateway_decision(session_key, notify_cb, approval_data):
    entry = _ApprovalEntry(approval_data)
    with _lock:
        _gateway_queues.setdefault(session_key, []).append(entry)
    _fire_approval_hook("pre_approval_request", command="", description="", pattern_key="", pattern_keys=[], session_key=session_key, surface="gateway")
    notify_cb(approval_data)
    resolved = entry.event.wait(timeout=1)
    _fire_approval_hook("post_approval_response", command="", description="", pattern_key="", pattern_keys=[], session_key=session_key, surface="gateway", choice=entry.result)
    return {"resolved": resolved, "choice": entry.result}
''',
    "subagent": '''
def create_child(parent_agent, child, parent_subagent_id, subagent_id, effective_role, goal):
    child._parent_turn_id = parent_agent._current_turn_id
    invoke_hook(
        "subagent_start", parent_session_id=parent_agent.session_id,
        parent_turn_id=parent_agent._current_turn_id,
        parent_subagent_id=parent_subagent_id, child_session_id=child.session_id,
        child_subagent_id=subagent_id, child_role=effective_role, child_goal=goal,
    )
    return child

def finish_children(parent_agent, entry, child, child_role):
    invoke_hook(
        "subagent_stop", parent_session_id=parent_agent.session_id,
        parent_turn_id=parent_agent._current_turn_id,
        child_session_id=child.session_id, child_role=child_role,
        child_summary=entry.get("summary"), child_status=entry.get("status"),
        tool_call_history=[], duration_ms=1,
    )
''',
    "gateway": '''
def dispatch(event, gateway, session_store):
    results = invoke_hook("pre_gateway_dispatch", event=event, gateway=gateway, session_store=session_store)
    if not authenticated(event):
        return None
    return run_agent(event)
''',
    "cron": "def deliver_cron(result):\n    return send(result)\n",
    "base": "def send_native(message):\n    return send(message)\n",
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


FIXED_SOURCE_ROOT = Path("/private/tmp/hermes-agent-v2026.8.3-v430-audit")


def _wheel_digest(data: bytes) -> str:
    import base64

    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(data).digest()
    ).rstrip(b"=").decode("ascii")


def _build_regular_hfc_wheel(output_dir: Path) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    package_root = repo_root / "hermes_feishu_card"
    version_match = re.search(
        r'(?m)^version = "([0-9]+(?:\.[0-9]+)+)"$',
        (repo_root / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert version_match is not None
    version = version_match.group(1)
    dist_info = f"hermes_feishu_streaming_card-{version}.dist-info"
    members = {}
    for path in sorted(package_root.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
            members[path.relative_to(repo_root).as_posix()] = path.read_bytes()
    members[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        "Name: hermes-feishu-streaming-card\n"
        f"Version: {version}\n"
        "Requires-Python: >=3.9\n\n"
    ).encode("ascii")
    members[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: hfc-fixed-tag-test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n\n"
    ).encode("ascii")
    members[f"{dist_info}/entry_points.txt"] = (
        "[console_scripts]\n"
        "hermes-feishu-card = hermes_feishu_card.cli:main\n\n"
        "[hermes_agent.plugins]\n"
        "hermes-feishu-card = hermes_feishu_card.hermes_plugin\n"
    ).encode("ascii")
    record_path = f"{dist_info}/RECORD"
    record_lines = [
        f"{name},{_wheel_digest(data)},{len(data)}\n"
        for name, data in sorted(members.items())
    ]
    record_lines.append(f"{record_path},,\n")
    members[record_path] = "".join(record_lines).encode("utf-8")
    wheel = output_dir / f"hermes_feishu_streaming_card-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)
    return wheel


@pytest.fixture(scope="module")
def installed_fixed_runtime(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("fixed-runtime")
    wheel = _build_regular_hfc_wheel(root)
    runtime = root / "runtime"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(runtime)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    python = runtime / "bin" / "python"
    import yaml

    purelib = runtime / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    shutil.copytree(Path(yaml.__file__).resolve().parent, purelib / "yaml")
    subprocess.run(
        [
            str(python), "-m", "pip", "install", "--no-deps", "--no-index",
            str(wheel),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"HOME": str(root), "PYTHONNOUSERSITE": "1"},
    )
    return python


def test_fixed_tag_provenance_is_one_canonical_manifest_with_24_slices():
    raw = FIXED_TAG_PROVENANCE_PATH.read_bytes()
    provenance = load_fixed_tag_native_hook_provenance(FIXED_TAG_PROVENANCE_PATH)

    assert _sha256(raw) == FIXED_TAG_PROVENANCE_SHA256
    assert provenance.commit == FIXED_TAG_COMMIT
    assert len(provenance.sources) == 9
    assert sum(len(source.anchors) for source in provenance.sources) == 24
    assert all(source.anchors for source in provenance.sources)
    assert verify_provenance_slices(
        provenance, fixture_root=FIXED_TAG_PROVENANCE_PATH.parent
    ) is True


def test_real_regular_wheel_plugin_manager_probe_reports_exactly_four_capabilities(
    installed_fixed_runtime,
):
    assert FIXED_SOURCE_ROOT.is_dir()
    result = probe_native_hook_capabilities(
        FIXED_SOURCE_ROOT,
        expected_commit=FIXED_TAG_COMMIT,
        runtime_python=installed_fixed_runtime,
    )

    expected = {
        "turn_start",
        "turn_terminal_result",
        "stable_tool_lifecycle",
        "approval_observe",
    }
    assert result.reason_code == "verified"
    assert result.capabilities.available == expected
    assert result.plugin_evidence_sha256.startswith("sha256:")
    assert {status.name for status in result.statuses if status.available} == expected
    subagent = next(
        status for status in result.statuses if status.name == "subagent_lifecycle"
    )
    assert subagent.reason_code == "callsite_contract_mismatch"
    assert len(result.source_digests) == 9

    decision = select_integration_mode(
        result.capabilities,
        PatchCapabilities.from_names(HYBRID_REQUIRED_PATCH_GROUPS),
    )
    assert decision.mode is None


def test_detector_wrapper_uses_runtime_probe_without_version_guessing(
    installed_fixed_runtime,
):
    result = detect.detect_native_hook_capabilities(
        FIXED_SOURCE_ROOT,
        expected_commit=FIXED_TAG_COMMIT,
        runtime_python=installed_fixed_runtime,
    )
    assert result.capabilities.available == {
        "turn_start", "turn_terminal_result", "stable_tool_lifecycle",
        "approval_observe",
    }


def test_review_attack_caller_forged_evidence_and_manifest_are_not_public_inputs():
    parameters = inspect.signature(probe_native_hook_capabilities).parameters
    assert set(parameters) == {"hermes_root", "expected_commit", "runtime_python"}
    assert "PluginManagerSubprocessEvidence" not in vars(native_hooks)


def test_review_attack_zero_anchor_rehashed_manifest_cannot_substitute(
    tmp_path, monkeypatch,
):
    payload = json.loads(FIXED_TAG_PROVENANCE_PATH.read_text(encoding="utf-8"))
    for item in payload["files"]:
        item["anchors"] = []
    substitute = tmp_path / "provenance.json"
    substitute.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(native_hooks, "FIXED_TAG_PROVENANCE_PATH", substitute)

    result = probe_native_hook_capabilities(
        FIXED_SOURCE_ROOT,
        expected_commit=FIXED_TAG_COMMIT,
        runtime_python=tmp_path / "missing-python",
    )

    assert result.capabilities.available == frozenset()
    assert result.reason_code == "provenance_invalid"


def _payload_template(runtime: Path, hermes_root: Path) -> dict[str, object]:
    version = [3, 12, 0]
    prefix = runtime.parent.parent
    purelib = prefix / "lib" / "python3.12" / "site-packages"
    payload = {
        "schema": 1,
        "python_version": version,
        "executable": str(runtime),
        "prefix": str(prefix),
        "base_prefix": "/base",
        "purelib": str(purelib),
        "platlib": str(purelib),
        "manager_origin": str(hermes_root / "hermes_cli" / "plugins.py"),
        "distribution_name": "hermes-feishu-streaming-card",
        "distribution_version": "4.2.12",
        "distribution_metadata_path": str(
            purelib / "hermes_feishu_streaming_card-4.2.12.dist-info"
        ),
        "record_path": str(
            purelib / "hermes_feishu_streaming_card-4.2.12.dist-info" / "RECORD"
        ),
        "record_sha256": "sha256:" + "0" * 64,
        "entrypoint_group": "hermes_agent.plugins",
        "entrypoint_key": "hermes-feishu-card",
        "entrypoint_value": "hermes_feishu_card.hermes_plugin",
        "entrypoint_origin": str(purelib / "hermes_feishu_card" / "hermes_plugin.py"),
        "package_origin": str(purelib / "hermes_feishu_card" / "__init__.py"),
        "enabled_config": ["hermes-feishu-card"],
        "matching_entrypoint_count": 1,
        "matching_discovered_count": 1,
        "matching_enabled_count": 1,
        "matching_loaded_count": 1,
        "registered_hooks": sorted(HFC_REGISTERED_HOOKS),
    }
    payload["attestation_sha256"] = _sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    )
    return payload


def test_review_attack_entrypoint_equality_spoof_is_rejected(tmp_path):
    class EqualitySpoof(str):
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

    runtime = tmp_path / "runtime" / "bin" / "python"
    payload = _payload_template(runtime, tmp_path)
    payload["entrypoint_group"] = EqualitySpoof("attacker.group")

    assert native_hooks._validate_plugin_manager_payload(
        payload, runtime_python=runtime, hermes_root=tmp_path
    ) == "plugin_evidence_invalid"


def test_review_attack_duplicate_child_json_keys_are_rejected():
    with pytest.raises(ValueError, match="object"):
        native_hooks._decode_canonical_json_object(b'{"a":1,"a":1}\n')


def test_review_attack_subagent_cross_turn_parent_is_unavailable():
    check = native_hooks._probe_subagent_lifecycle(_SOURCE_BY_TARGET)
    assert check.available is False
    assert check.reason_code == "callsite_contract_mismatch"


def test_review_attack_ancestor_symlink_is_rejected(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    child = real_parent / "child"
    child.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(OSError):
        native_hooks._open_absolute_directory(alias / "child")


def test_review_attack_parent_swap_is_detected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    parent = root / "parent"
    replacement = root / "replacement"
    parent.mkdir(parents=True)
    replacement.mkdir()
    (parent / "source.py").write_text("value = 1\n", encoding="utf-8")
    (replacement / "source.py").write_text("value = 1\n", encoding="utf-8")
    root_descriptor = native_hooks._open_absolute_directory(root)
    real_open = native_hooks.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == "parent" and not swapped:
            swapped = True
            parent.rename(root / "old-parent")
            replacement.rename(parent)
        return descriptor

    monkeypatch.setattr(native_hooks.os, "open", swapping_open)
    try:
        with pytest.raises(ValueError, match="identity"):
            native_hooks._read_bound_relative_file(
                root_descriptor, "parent/source.py", 1024
            )
    finally:
        os.close(root_descriptor)
    assert swapped is True


def test_review_attack_symlink_hardlink_and_oversize_source_are_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    original = root / "original.py"
    original.write_bytes(b"x")
    symlink = root / "symlink.py"
    symlink.symlink_to(original.name)
    hardlink = root / "hardlink.py"
    hardlink.hardlink_to(original)
    oversized = root / "oversized.py"
    oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    descriptor = native_hooks._open_absolute_directory(root)
    try:
        for relative in ("symlink.py", "hardlink.py", "oversized.py"):
            with pytest.raises((OSError, ValueError)):
                native_hooks._read_bound_relative_file(
                    descriptor, relative, 2 * 1024 * 1024
                )
    finally:
        os.close(descriptor)


def test_review_attack_bool_lines_and_malformed_primitives_are_rejected():
    with pytest.raises(ValueError, match="lines"):
        NativeHookAnchorProvenance(
            name="attack",
            line_start=True,
            line_end=True,
            slice_path="slices/attack.py",
            slice_sha256="sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError, match="boolean"):
        NativeCapabilityStatus(
            name="turn_start",
            available=1,
            reason_code="verified",
            callsite_signature="sha256:" + "0" * 64,
        )


def test_review_attack_commit_mismatch_reports_observed_commit(tmp_path, monkeypatch):
    observed = "1" * 40
    monkeypatch.setattr(
        native_hooks, "_git_source_state", lambda _root: (observed, True)
    )

    result = probe_native_hook_capabilities(
        tmp_path,
        expected_commit=FIXED_TAG_COMMIT,
        runtime_python=tmp_path / "missing-python",
    )

    assert result.reason_code == "source_commit_mismatch"
    assert result.source_commit == observed


def test_dirty_fixed_source_checkout_closes_all_capabilities(tmp_path, monkeypatch):
    monkeypatch.setattr(
        native_hooks,
        "_git_source_state",
        lambda _root: (FIXED_TAG_COMMIT, False),
    )
    result = probe_native_hook_capabilities(
        tmp_path,
        expected_commit=FIXED_TAG_COMMIT,
        runtime_python=tmp_path / "missing-python",
    )
    assert result.reason_code == "source_dirty"
    assert result.capabilities.available == frozenset()


def test_forged_subprocess_output_cannot_open_capabilities(
    installed_fixed_runtime, monkeypatch,
):
    payload = _payload_template(installed_fixed_runtime, FIXED_SOURCE_ROOT)
    payload["matching_loaded_count"] = True
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    monkeypatch.setattr(
        native_hooks.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=raw, stderr=b""
        ),
    )

    evidence, reason = native_hooks._produce_plugin_manager_evidence(
        FIXED_SOURCE_ROOT, installed_fixed_runtime
    )

    assert evidence is None
    assert reason == "plugin_evidence_invalid"


def test_dataclasses_reject_duplicate_targets_path_aliases_and_bad_digest():
    source = NativeHookSourceProvenance(
        target="plugin_manager",
        relative_path="hermes_cli/plugins.py",
        sha256="sha256:" + "0" * 64,
    )
    with pytest.raises(ValueError, match="targets"):
        FixedTagNativeHookProvenance(commit="0" * 40, sources=(source, source))
    with pytest.raises(ValueError, match="path"):
        NativeHookSourceProvenance(
            target="plugin_manager",
            relative_path="hermes_cli/../plugins.py",
            sha256="sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError, match="digest"):
        NativeHookSourceDigest(target="approval", sha256="not-a-digest")
