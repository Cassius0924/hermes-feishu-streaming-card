from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_feishu_card.install import detect
from hermes_feishu_card.install import native_hooks
from hermes_feishu_card.install.native_hooks import (
    FIXED_TAG_COMMIT,
    FIXED_TAG_PROVENANCE_PATH,
    HFC_REGISTERED_HOOKS,
    FixedTagNativeHookProvenance,
    NativeHookAnchorProvenance,
    NativeHookSourceProvenance,
    NativeCapabilityStatus,
    NativeHookCapabilityProbe,
    NativeHookSourceDigest,
    PluginManagerSubprocessEvidence,
    load_fixed_tag_native_hook_provenance,
    probe_native_hook_capabilities,
    verify_provenance_slices,
)
from hermes_feishu_card.integration import (
    HYBRID_REQUIRED_NATIVE_CAPABILITIES,
    HYBRID_REQUIRED_PATCH_GROUPS,
    IntegrationMode,
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


def _commit_source_root(tmp_path: Path) -> tuple[Path, FixedTagNativeHookProvenance]:
    root = tmp_path / "hermes"
    relative_paths = {
        "plugin_manager": "hermes_cli/plugins.py",
        "turn_context": "agent/turn_context.py",
        "turn_finalizer": "agent/turn_finalizer.py",
        "tool_hooks": "model_tools.py",
        "approval": "tools/approval.py",
        "subagent": "tools/delegate_tool.py",
        "gateway": "gateway/run.py",
        "cron": "cron/scheduler.py",
        "base": "gateway/platforms/base.py",
    }
    sources = []
    for target, relative_path in relative_paths.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _SOURCE_BY_TARGET[target].lstrip().encode("utf-8")
        path.write_bytes(data)
        sources.append(
            NativeHookSourceProvenance(
                target=target,
                relative_path=relative_path,
                sha256=_sha256(data),
            )
        )
    return root, FixedTagNativeHookProvenance(
        commit=FIXED_TAG_COMMIT,
        sources=tuple(sources),
    )


@pytest.fixture(autouse=True)
def _fixed_source_commit(monkeypatch):
    real_git_head = native_hooks._git_head

    def git_head(root: Path) -> str:
        if root.name == "hermes" and root.parent.name.startswith("test_"):
            return FIXED_TAG_COMMIT
        return real_git_head(root)

    monkeypatch.setattr(native_hooks, "_git_head", git_head)


def _plugin_evidence(commit: str, **changes) -> PluginManagerSubprocessEvidence:
    values = {
        "source_commit": commit,
        "attestation_verified": True,
        "subprocess_completed": True,
        "runtime_binding_verified": True,
        "entrypoint_identity_verified": True,
        "plugins_enabled_exact": True,
        "registration_verified": True,
        "entrypoint_group": "hermes_agent.plugins",
        "entrypoint_key": "hermes-feishu-card",
        "entrypoint_value": "hermes_feishu_card.hermes_plugin",
        "distribution_name": "hermes-feishu-streaming-card",
        "matching_entrypoint_count": 1,
        "matching_enabled_count": 1,
        "registered_hooks": HFC_REGISTERED_HOOKS,
        "runtime_executable_sha256": "sha256:" + "1" * 64,
        "runtime_purelib_sha256": "sha256:" + "2" * 64,
        "entrypoint_origin_sha256": "sha256:" + "3" * 64,
        "attestation_sha256": "sha256:" + "4" * 64,
    }
    values.update(changes)
    return PluginManagerSubprocessEvidence(**values)


def test_fixed_tag_provenance_is_bound_to_exact_commit_hashes_and_slices():
    provenance = load_fixed_tag_native_hook_provenance(FIXED_TAG_PROVENANCE_PATH)

    assert provenance.commit == FIXED_TAG_COMMIT
    assert len(provenance.sources) == 9
    assert all(source.sha256.startswith("sha256:") for source in provenance.sources)
    assert verify_provenance_slices(
        provenance,
        fixture_root=FIXED_TAG_PROVENANCE_PATH.parent,
    ) is True


def test_exact_source_and_verified_plugin_manager_produce_only_lifecycle_capabilities(tmp_path):
    root, provenance = _commit_source_root(tmp_path)

    result = probe_native_hook_capabilities(
        root,
        expected_commit=provenance.commit,
        provenance=provenance,
        plugin_evidence=_plugin_evidence(provenance.commit),
    )

    assert result.capabilities.available == HYBRID_REQUIRED_NATIVE_CAPABILITIES
    assert {status.name for status in result.statuses if status.available} == {
        "turn_start",
        "turn_terminal_result",
        "stable_tool_lifecycle",
        "approval_observe",
        "subagent_lifecycle",
    }
    unavailable = {status.name: status.reason_code for status in result.statuses if not status.available}
    assert unavailable == {
        "authenticated_ingress": "authenticated_ingress_missing",
        "answer_delta": "answer_delta_missing",
        "thinking_delta": "thinking_delta_missing",
        "interaction_round_trip": "interaction_resolver_missing",
        "final_delivery_disposition": "terminal_consumer_missing",
        "command_platform_notice": "command_platform_notice_missing",
        "cron_delivery": "cron_hook_missing",
        "exact_native_delivery": "exact_native_delivery_missing",
    }
    assert all("/" not in status.reason_code for status in result.statuses)
    assert {item.target for item in result.source_digests} == set(_SOURCE_BY_TARGET)
    assert all(item.sha256.startswith("sha256:") for item in result.source_digests)

    decision = select_integration_mode(
        result.capabilities,
        PatchCapabilities.from_names(HYBRID_REQUIRED_PATCH_GROUPS),
    )
    assert decision.mode is IntegrationMode.HYBRID
    without_patches = select_integration_mode(
        result.capabilities,
        PatchCapabilities.from_names(()),
    )
    assert without_patches.mode is None


@pytest.mark.parametrize(
    ("changes", "reason_code"),
    [
        ({"attestation_verified": False}, "plugin_attestation_unverified"),
        ({"plugins_enabled_exact": False}, "plugin_not_enabled"),
        ({"matching_entrypoint_count": 2}, "entrypoint_ambiguous"),
        ({"registered_hooks": frozenset({"pre_llm_call"})}, "registration_incomplete"),
    ],
)
def test_unverified_or_ambiguous_plugin_evidence_closes_all_hook_capabilities(
    tmp_path, changes, reason_code,
):
    root, provenance = _commit_source_root(tmp_path)

    result = probe_native_hook_capabilities(
        root,
        expected_commit=provenance.commit,
        provenance=provenance,
        plugin_evidence=_plugin_evidence(provenance.commit, **changes),
    )

    assert result.capabilities.available == frozenset()
    assert result.reason_code == reason_code


def test_plugin_evidence_rejects_unstructured_identity_digest():
    with pytest.raises(ValueError, match="digest"):
        _plugin_evidence(FIXED_TAG_COMMIT, runtime_purelib_sha256="not-a-digest")


def test_missing_plugin_evidence_fails_closed(tmp_path):
    root, provenance = _commit_source_root(tmp_path)

    result = probe_native_hook_capabilities(
        root,
        expected_commit=provenance.commit,
        provenance=provenance,
        plugin_evidence=None,
    )

    assert result.capabilities.available == frozenset()
    assert result.reason_code == "plugin_evidence_missing"


def test_capability_status_is_closed_and_sanitized():
    with pytest.raises(ValueError, match="boolean"):
        NativeCapabilityStatus(
            name="turn_start",
            available="yes",
            reason_code="verified",
            callsite_signature="sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError, match="reason"):
        NativeCapabilityStatus(
            name="turn_start",
            available=False,
            reason_code="leaked/path",
            callsite_signature="sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError, match="reason"):
        NativeCapabilityStatus(
            name="turn_start",
            available=False,
            reason_code="future_unknown_reason",
            callsite_signature="sha256:" + "0" * 64,
        )


def test_probe_result_rejects_duplicate_statuses_and_invalid_source_digest():
    status = NativeCapabilityStatus(
        name="turn_start",
        available=False,
        reason_code="callsite_contract_mismatch",
        callsite_signature="sha256:" + "0" * 64,
    )
    with pytest.raises(ValueError, match="statuses"):
        NativeHookCapabilityProbe(
            capabilities=native_hooks.NativeHookCapabilities.from_names(()),
            statuses=(status, status),
            source_commit=FIXED_TAG_COMMIT,
            source_digests=(),
            plugin_evidence_sha256="",
            reason_code="verified",
        )
    with pytest.raises(ValueError, match="digest"):
        NativeHookSourceDigest(target="approval", sha256="not-a-digest")


@pytest.mark.parametrize(
    ("target", "before", "after", "capability"),
    [
        (
            "turn_finalizer",
            'if final_response and not interrupted:\n        invoke_hook(',
            'if final_response:\n        invoke_hook(',
            "turn_terminal_result",
        ),
        (
            "tool_hooks",
            "block_message = resolve_pre_tool_block(",
            "block_message = passthrough_pre_tool_block(",
            "stable_tool_lifecycle",
        ),
        (
            "approval",
            'kwargs.setdefault("tool_call_id", _approval_tool_call_id.get())',
            'kwargs.setdefault("tool_call_id", "")',
            "approval_observe",
        ),
        (
            "subagent",
            "child_subagent_id=subagent_id,",
            "child_subagent_id=None,",
            "subagent_lifecycle",
        ),
    ],
)
def test_callsite_kwargs_and_timing_drift_closes_only_dependent_capability(
    tmp_path, target, before, after, capability,
):
    mutated = dict(_SOURCE_BY_TARGET)
    mutated[target] = mutated[target].replace(before, after)
    assert mutated[target] != _SOURCE_BY_TARGET[target]
    original = _SOURCE_BY_TARGET[target]
    _SOURCE_BY_TARGET[target] = mutated[target]
    try:
        root, provenance = _commit_source_root(tmp_path)
        result = probe_native_hook_capabilities(
            root,
            expected_commit=provenance.commit,
            provenance=provenance,
            plugin_evidence=_plugin_evidence(provenance.commit),
        )
    finally:
        _SOURCE_BY_TARGET[target] = original

    status = next(item for item in result.statuses if item.name == capability)
    assert status.available is False
    assert status.reason_code == "callsite_contract_mismatch"


def test_source_drift_closes_only_capabilities_that_depend_on_that_target(tmp_path):
    root, provenance = _commit_source_root(tmp_path)
    (root / "tools" / "approval.py").write_text("def changed():\n    pass\n")

    result = probe_native_hook_capabilities(
        root,
        expected_commit=provenance.commit,
        provenance=provenance,
        plugin_evidence=_plugin_evidence(provenance.commit),
    )

    assert result.capabilities.available == (
        HYBRID_REQUIRED_NATIVE_CAPABILITIES - {"approval_observe"}
    )
    approval = next(status for status in result.statuses if status.name == "approval_observe")
    assert approval.reason_code == "source_digest_mismatch"


def test_anchor_line_drift_closes_capabilities_that_depend_on_target(tmp_path):
    root, provenance = _commit_source_root(tmp_path)
    turn_source = next(item for item in provenance.sources if item.target == "turn_context")
    anchor = NativeHookAnchorProvenance(
        name="pre_llm_call",
        line_start=1,
        line_end=1,
        slice_path="slices/pre_llm_call.py",
        slice_sha256="sha256:" + "0" * 64,
    )
    sources = tuple(
        NativeHookSourceProvenance(
            target=item.target,
            relative_path=item.relative_path,
            sha256=item.sha256,
            anchors=(anchor,) if item is turn_source else item.anchors,
        )
        for item in provenance.sources
    )

    result = probe_native_hook_capabilities(
        root,
        expected_commit=FIXED_TAG_COMMIT,
        provenance=FixedTagNativeHookProvenance(
            commit=FIXED_TAG_COMMIT,
            sources=sources,
        ),
        plugin_evidence=_plugin_evidence(FIXED_TAG_COMMIT),
    )

    status = next(item for item in result.statuses if item.name == "turn_start")
    assert status.available is False
    assert status.reason_code == "source_anchor_mismatch"


def test_no_follow_source_symlink_fails_closed_without_leaking_path(tmp_path):
    root, provenance = _commit_source_root(tmp_path)
    approval = root / "tools" / "approval.py"
    real = root / "tools" / "approval-real.py"
    approval.rename(real)
    approval.symlink_to(real.name)

    result = probe_native_hook_capabilities(
        root,
        expected_commit=provenance.commit,
        provenance=provenance,
        plugin_evidence=_plugin_evidence(provenance.commit),
    )

    status = next(status for status in result.statuses if status.name == "approval_observe")
    assert status.available is False
    assert status.reason_code == "source_not_regular"
    assert str(root) not in repr(result)


def test_expected_commit_mismatch_fails_closed_before_source_claims(tmp_path):
    root, provenance = _commit_source_root(tmp_path)

    result = probe_native_hook_capabilities(
        root,
        expected_commit="0" * 40,
        provenance=provenance,
        plugin_evidence=_plugin_evidence(provenance.commit),
    )

    assert result.capabilities.available == frozenset()
    assert result.reason_code == "expected_commit_unsupported"
    assert result.source_digests == ()


def test_detector_consumes_probe_without_version_or_valid_hooks_guessing(tmp_path):
    root, provenance = _commit_source_root(tmp_path)

    result = detect.detect_native_hook_capabilities(
        root,
        expected_commit=provenance.commit,
        provenance=provenance,
        plugin_evidence=_plugin_evidence(provenance.commit),
    )

    assert result.capabilities.available == HYBRID_REQUIRED_NATIVE_CAPABILITIES


def test_provenance_rejects_duplicate_or_unknown_targets():
    source = NativeHookSourceProvenance(
        target="plugin_manager",
        relative_path="hermes_cli/plugins.py",
        sha256="sha256:" + "0" * 64,
    )
    with pytest.raises(ValueError, match="targets"):
        FixedTagNativeHookProvenance(commit="0" * 40, sources=(source, source))


def test_provenance_rejects_target_mapped_to_wrong_relative_path():
    provenance = load_fixed_tag_native_hook_provenance(FIXED_TAG_PROVENANCE_PATH)
    source = next(item for item in provenance.sources if item.target == "approval")

    with pytest.raises(ValueError, match="path"):
        NativeHookSourceProvenance(
            target="approval",
            relative_path="tools/not-approval.py",
            sha256=source.sha256,
            anchors=source.anchors,
        )


def test_manifest_contains_no_absolute_paths_or_raw_source():
    payload = json.loads(FIXED_TAG_PROVENANCE_PATH.read_text())
    encoded = json.dumps(payload, sort_keys=True)

    assert "/private/tmp" not in encoded
    assert "source" not in payload
    assert all(not Path(item["relative_path"]).is_absolute() for item in payload["files"])
