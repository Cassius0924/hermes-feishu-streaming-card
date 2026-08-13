from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Iterable

from ..integration import KNOWN_NATIVE_CAPABILITIES, NativeHookCapabilities


FIXED_TAG_COMMIT = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
FIXED_TAG_PROVENANCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "hermes_v2026_8_3_native_capabilities"
    / "provenance.json"
)

HFC_REGISTERED_HOOKS = frozenset({
    "pre_llm_call",
    "post_llm_call",
    "on_session_end",
    "on_session_reset",
    "on_session_finalize",
    "pre_tool_call",
    "post_tool_call",
    "pre_approval_request",
    "post_approval_response",
    "subagent_start",
    "subagent_stop",
})

_EXPECTED_TARGETS = frozenset({
    "plugin_manager",
    "turn_context",
    "turn_finalizer",
    "tool_hooks",
    "approval",
    "subagent",
    "gateway",
    "cron",
    "base",
})
_RELATIVE_PATHS = {
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
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_SLICE_BYTES = 128 * 1024
_REASON_CODES = frozenset({
    "verified",
    "expected_commit_invalid",
    "expected_commit_unsupported",
    "provenance_commit_mismatch",
    "hermes_root_invalid",
    "source_commit_mismatch",
    "plugin_evidence_missing",
    "plugin_evidence_invalid",
    "plugin_source_commit_mismatch",
    "plugin_attestation_unverified",
    "plugin_runtime_unverified",
    "entrypoint_ambiguous",
    "entrypoint_identity_mismatch",
    "plugin_not_enabled",
    "registration_incomplete",
    "source_missing",
    "source_not_regular",
    "source_digest_mismatch",
    "source_anchor_mismatch",
    "source_ast_invalid",
    "callsite_contract_mismatch",
    "authenticated_ingress_missing",
    "answer_delta_missing",
    "thinking_delta_missing",
    "interaction_resolver_missing",
    "terminal_consumer_missing",
    "command_platform_notice_missing",
    "cron_hook_missing",
    "exact_native_delivery_missing",
})


@dataclass(frozen=True)
class NativeHookAnchorProvenance:
    name: str
    line_start: int
    line_end: int
    slice_path: str
    slice_sha256: str

    def __post_init__(self) -> None:
        if not self.name or not re.fullmatch(r"[a-z0-9_]+", self.name):
            raise ValueError("invalid anchor name")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise ValueError("invalid anchor lines")
        _validate_relative_path(self.slice_path)
        _validate_digest(self.slice_sha256)


@dataclass(frozen=True)
class NativeHookSourceProvenance:
    target: str
    relative_path: str
    sha256: str
    anchors: tuple[NativeHookAnchorProvenance, ...] = ()

    def __post_init__(self) -> None:
        if self.target not in _EXPECTED_TARGETS:
            raise ValueError("unknown provenance target")
        _validate_relative_path(self.relative_path)
        if self.relative_path != _RELATIVE_PATHS[self.target]:
            raise ValueError("provenance target path mismatch")
        _validate_digest(self.sha256)
        names = [anchor.name for anchor in self.anchors]
        if len(names) != len(set(names)):
            raise ValueError("duplicate anchor names")


@dataclass(frozen=True)
class FixedTagNativeHookProvenance:
    commit: str
    sources: tuple[NativeHookSourceProvenance, ...]

    def __post_init__(self) -> None:
        if _COMMIT_RE.fullmatch(self.commit) is None:
            raise ValueError("invalid provenance commit")
        targets = [source.target for source in self.sources]
        if len(targets) != len(set(targets)) or set(targets) != _EXPECTED_TARGETS:
            raise ValueError("provenance targets must be exact and unique")


@dataclass(frozen=True)
class PluginManagerSubprocessEvidence:
    source_commit: str
    attestation_verified: bool
    subprocess_completed: bool
    runtime_binding_verified: bool
    entrypoint_identity_verified: bool
    plugins_enabled_exact: bool
    registration_verified: bool
    entrypoint_group: str
    entrypoint_key: str
    entrypoint_value: str
    distribution_name: str
    matching_entrypoint_count: int
    matching_enabled_count: int
    registered_hooks: frozenset[str]
    runtime_executable_sha256: str
    runtime_purelib_sha256: str
    entrypoint_origin_sha256: str
    attestation_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "attestation_verified",
            "subprocess_completed",
            "runtime_binding_verified",
            "entrypoint_identity_verified",
            "plugins_enabled_exact",
            "registration_verified",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError("plugin evidence booleans must be exact")
        if type(self.matching_entrypoint_count) is not int:
            raise ValueError("invalid entrypoint count")
        if type(self.matching_enabled_count) is not int:
            raise ValueError("invalid enabled count")
        hooks = frozenset(self.registered_hooks)
        if not all(type(name) is str for name in hooks):
            raise ValueError("invalid registered hooks")
        object.__setattr__(self, "registered_hooks", hooks)
        if _COMMIT_RE.fullmatch(self.source_commit) is None:
            raise ValueError("invalid evidence commit")
        for digest in (
            self.runtime_executable_sha256,
            self.runtime_purelib_sha256,
            self.entrypoint_origin_sha256,
            self.attestation_sha256,
        ):
            _validate_digest(digest)


@dataclass(frozen=True)
class NativeCapabilityStatus:
    name: str
    available: bool
    reason_code: str
    callsite_signature: str

    def __post_init__(self) -> None:
        if self.name not in KNOWN_NATIVE_CAPABILITIES:
            raise ValueError("unknown capability status")
        if type(self.available) is not bool:
            raise ValueError("capability availability must be boolean")
        if self.reason_code not in _REASON_CODES:
            raise ValueError("invalid capability reason")
        _validate_digest(self.callsite_signature)


@dataclass(frozen=True)
class NativeHookSourceDigest:
    target: str
    sha256: str

    def __post_init__(self) -> None:
        if self.target not in _EXPECTED_TARGETS:
            raise ValueError("unknown source digest target")
        _validate_digest(self.sha256)


@dataclass(frozen=True)
class NativeHookCapabilityProbe:
    capabilities: NativeHookCapabilities
    statuses: tuple[NativeCapabilityStatus, ...]
    source_commit: str
    source_digests: tuple[NativeHookSourceDigest, ...]
    plugin_evidence_sha256: str
    reason_code: str

    def __post_init__(self) -> None:
        names = [status.name for status in self.statuses]
        if len(names) != len(set(names)) or set(names) != KNOWN_NATIVE_CAPABILITIES:
            raise ValueError("capability statuses must be exact and unique")
        available = frozenset(
            status.name for status in self.statuses if status.available
        )
        if available != self.capabilities.available:
            raise ValueError("capability statuses do not match available set")
        targets = [item.target for item in self.source_digests]
        if len(targets) != len(set(targets)):
            raise ValueError("source digests must be unique")
        if self.source_commit and _COMMIT_RE.fullmatch(self.source_commit) is None:
            raise ValueError("invalid source commit")
        if self.plugin_evidence_sha256:
            _validate_digest(self.plugin_evidence_sha256)
        if self.reason_code not in _REASON_CODES:
            raise ValueError("invalid probe reason")


@dataclass(frozen=True)
class _ContractCheck:
    available: bool
    reason_code: str
    signature: str


def load_fixed_tag_native_hook_provenance(
    path: str | Path,
) -> FixedTagNativeHookProvenance:
    raw = _read_regular_file(Path(path), _MAX_MANIFEST_BYTES)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid provenance manifest") from exc
    if type(payload) is not dict or set(payload) != {"commit", "files"}:
        raise ValueError("invalid provenance manifest shape")
    files = payload.get("files")
    if type(files) is not list:
        raise ValueError("invalid provenance file list")
    sources: list[NativeHookSourceProvenance] = []
    for item in files:
        if type(item) is not dict or set(item) != {
            "target", "relative_path", "sha256", "anchors"
        }:
            raise ValueError("invalid provenance file shape")
        anchor_items = item["anchors"]
        if type(anchor_items) is not list:
            raise ValueError("invalid provenance anchors")
        anchors = []
        for anchor in anchor_items:
            if type(anchor) is not dict or set(anchor) != {
                "name", "line_start", "line_end", "slice_path", "slice_sha256"
            }:
                raise ValueError("invalid provenance anchor shape")
            anchors.append(NativeHookAnchorProvenance(**anchor))
        sources.append(
            NativeHookSourceProvenance(
                target=item["target"],
                relative_path=item["relative_path"],
                sha256=item["sha256"],
                anchors=tuple(anchors),
            )
        )
    return FixedTagNativeHookProvenance(
        commit=payload["commit"],
        sources=tuple(sources),
    )


def verify_provenance_slices(
    provenance: FixedTagNativeHookProvenance,
    *,
    fixture_root: str | Path,
) -> bool:
    root = Path(fixture_root)
    seen_paths: set[str] = set()
    if not all(source.anchors for source in provenance.sources):
        return False
    for source in provenance.sources:
        for anchor in source.anchors:
            if anchor.slice_path in seen_paths:
                return False
            seen_paths.add(anchor.slice_path)
            try:
                data = _read_bound_relative_file(
                    root, anchor.slice_path, _MAX_SLICE_BYTES
                )
            except (OSError, ValueError):
                return False
            if _sha256(data) != anchor.slice_sha256:
                return False
    return True


def probe_native_hook_capabilities(
    hermes_root: str | Path,
    *,
    expected_commit: str,
    provenance: FixedTagNativeHookProvenance,
    plugin_evidence: PluginManagerSubprocessEvidence | None,
) -> NativeHookCapabilityProbe:
    if _COMMIT_RE.fullmatch(expected_commit) is None:
        return _closed_probe(expected_commit, "expected_commit_invalid")
    if expected_commit != FIXED_TAG_COMMIT:
        return _closed_probe(expected_commit, "expected_commit_unsupported")
    if expected_commit != provenance.commit:
        return _closed_probe(expected_commit, "provenance_commit_mismatch")
    root = Path(hermes_root)
    if not _is_bound_root(root):
        return _closed_probe(expected_commit, "hermes_root_invalid")
    actual_commit = _git_head(root)
    if actual_commit != expected_commit:
        return _closed_probe(expected_commit, "source_commit_mismatch")
    if plugin_evidence is None:
        return _closed_probe(expected_commit, "plugin_evidence_missing")
    plugin_reason = _plugin_evidence_reason(plugin_evidence, expected_commit)
    if plugin_reason != "verified":
        return _closed_probe(
            expected_commit,
            plugin_reason,
            plugin_evidence_sha256=(
                plugin_evidence.attestation_sha256
                if _DIGEST_RE.fullmatch(plugin_evidence.attestation_sha256)
                else ""
            ),
        )

    source_text: dict[str, str] = {}
    source_reason: dict[str, str] = {}
    source_digests: list[NativeHookSourceDigest] = []
    for source in provenance.sources:
        try:
            data = _read_bound_relative_file(
                root, source.relative_path, _MAX_SOURCE_BYTES
            )
        except FileNotFoundError:
            source_reason[source.target] = "source_missing"
            continue
        except (OSError, ValueError):
            source_reason[source.target] = "source_not_regular"
            continue
        digest = _sha256(data)
        source_digests.append(
            NativeHookSourceDigest(target=source.target, sha256=digest)
        )
        if digest != source.sha256:
            source_reason[source.target] = "source_digest_mismatch"
            continue
        if source.anchors and not _anchors_match_source(data, source.anchors):
            source_reason[source.target] = "source_anchor_mismatch"
            continue
        try:
            text = data.decode("utf-8")
            ast.parse(text)
        except (UnicodeDecodeError, SyntaxError):
            source_reason[source.target] = "source_ast_invalid"
            continue
        source_text[source.target] = text

    contracts = {
        "authenticated_ingress": _probe_authenticated_ingress,
        "turn_start": _probe_turn_start,
        "turn_terminal_result": _probe_turn_terminal,
        "stable_tool_lifecycle": _probe_tool_lifecycle,
        "approval_observe": _probe_approval_observe,
        "subagent_lifecycle": _probe_subagent_lifecycle,
        "answer_delta": _probe_answer_delta,
        "thinking_delta": _probe_thinking_delta,
        "interaction_round_trip": _probe_interaction_round_trip,
        "final_delivery_disposition": _probe_terminal_disposition,
        "command_platform_notice": _probe_command_platform_notice,
        "cron_delivery": _probe_cron_delivery,
        "exact_native_delivery": _probe_exact_native_delivery,
    }
    dependencies = {
        "authenticated_ingress": ("plugin_manager", "gateway"),
        "turn_start": ("plugin_manager", "turn_context"),
        "turn_terminal_result": ("plugin_manager", "turn_finalizer"),
        "stable_tool_lifecycle": ("plugin_manager", "tool_hooks"),
        "approval_observe": ("plugin_manager", "tool_hooks", "approval"),
        "subagent_lifecycle": ("plugin_manager", "subagent"),
        "answer_delta": ("plugin_manager", "gateway"),
        "thinking_delta": ("plugin_manager", "gateway"),
        "interaction_round_trip": ("plugin_manager", "approval", "gateway"),
        "final_delivery_disposition": (
            "plugin_manager", "turn_finalizer", "gateway"
        ),
        "command_platform_notice": ("plugin_manager", "gateway"),
        "cron_delivery": ("plugin_manager", "cron"),
        "exact_native_delivery": ("plugin_manager", "base"),
    }
    statuses: list[NativeCapabilityStatus] = []
    for name in sorted(KNOWN_NATIVE_CAPABILITIES):
        targets = dependencies[name]
        drift = next(
            (source_reason[target] for target in targets if target in source_reason),
            None,
        )
        if drift is not None:
            statuses.append(_status(name, False, drift, targets))
            continue
        check = contracts[name](source_text)
        statuses.append(
            _status(
                name,
                check.available,
                check.reason_code,
                targets,
                signature=check.signature,
            )
        )
    available_names = frozenset(
        status.name for status in statuses if status.available
    )
    return NativeHookCapabilityProbe(
        capabilities=NativeHookCapabilities.from_names(available_names),
        statuses=tuple(statuses),
        source_commit=actual_commit,
        source_digests=tuple(sorted(source_digests, key=lambda item: item.target)),
        plugin_evidence_sha256=plugin_evidence.attestation_sha256,
        reason_code="verified",
    )


def _probe_authenticated_ingress(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    gateway = ast.parse(sources["gateway"])
    call = _single_hook_call(gateway, "pre_gateway_dispatch")
    auth_calls = _named_calls(gateway, "_is_user_authorized") + _named_calls(
        gateway, "authenticated"
    )
    nodes: list[ast.AST] = [manager]
    if call is not None:
        nodes.append(call)
    nodes.extend(auth_calls)
    available = (
        _plugin_manager_contract(manager)
        and call is not None
        and {"event", "gateway", "session_store"} <= _keyword_names(call)
        and bool(auth_calls)
        and call.lineno > max(item.lineno for item in auth_calls)
    )
    return _check(
        "authenticated_ingress",
        available,
        "verified" if available else "authenticated_ingress_missing",
        nodes,
    )


def _probe_turn_start(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    turn = ast.parse(sources["turn_context"])
    call = _single_hook_call(turn, "pre_llm_call")
    required = {
        "session_id", "task_id", "turn_id", "user_message",
        "conversation_history", "is_first_turn", "model", "platform",
    }
    assignment = _turn_id_assignment_before(turn, call)
    available = (
        _plugin_manager_contract(manager)
        and call is not None
        and required <= _keyword_names(call)
        and _call_enclosed_by_function(
            turn, call, "build_turn_context", "prepare_turn"
        )
        and assignment is not None
        and _call_result_is_consumed(turn, call)
    )
    return _check(
        "turn_start",
        available,
        "verified" if available else "callsite_contract_mismatch",
        [manager, call, assignment],
    )


def _probe_turn_terminal(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    tree = ast.parse(sources["turn_finalizer"])
    post = _single_hook_call(tree, "post_llm_call")
    end = _single_hook_call(tree, "on_session_end")
    post_required = {
        "session_id", "task_id", "turn_id", "assistant_response", "platform"
    }
    end_required = {
        "session_id", "task_id", "turn_id", "completed", "failed",
        "interrupted", "turn_exit_reason", "platform",
    }
    result_assignment = _named_assignment_between(tree, "result", post, end)
    available = (
        _plugin_manager_contract(manager)
        and post is not None
        and end is not None
        and post_required <= _keyword_names(post)
        and end_required <= _keyword_names(end)
        and _call_is_guarded_by(
            tree, post, required_names={"final_response", "interrupted"}
        )
        and post.lineno < end.lineno
        and result_assignment is not None
        and _call_precedes_return_in_same_function(tree, end, "result")
    )
    return _check(
        "turn_terminal_result",
        available,
        "verified" if available else "callsite_contract_mismatch",
        [manager, post, result_assignment, end],
    )


def _probe_tool_lifecycle(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    tools = ast.parse(sources["tool_hooks"])
    pre = _single_named_call(tools, "resolve_pre_tool_block")
    post = _single_hook_call(tools, "post_tool_call")
    required = {
        "task_id", "session_id", "tool_call_id", "turn_id", "api_request_id"
    }
    normal_post = _normal_post_tool_call(tools)
    available = (
        _plugin_manager_contract(manager)
        and _manager_pre_tool_contract(manager)
        and pre is not None
        and required <= _keyword_names(pre)
        and post is not None
        and required | {"duration_ms", "status"} <= _keyword_names(post)
        and _call_precedes_dispatch(tools, pre)
        and normal_post is not None
    )
    return _check(
        "stable_tool_lifecycle",
        available,
        "verified" if available else "callsite_contract_mismatch",
        [manager, pre, post, normal_post],
    )


def _probe_approval_observe(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    tools = ast.parse(sources["tool_hooks"])
    approval = ast.parse(sources["approval"])
    set_context = _single_named_call(tools, "set_current_observability_context")
    reset_context = _single_named_call(tools, "reset_current_observability_context")
    await_fn = _function(approval, "_await_gateway_decision")
    pre = _single_hook_call(await_fn, "pre_approval_request") if await_fn else None
    post = _single_hook_call(await_fn, "post_approval_response") if await_fn else None
    fire_fn = _function(approval, "_fire_approval_hook")
    invoke = _single_named_call(fire_fn, "invoke_hook") if fire_fn else None
    available = (
        _plugin_manager_contract(manager)
        and set_context is not None
        and {"turn_id", "tool_call_id"} <= _keyword_names(set_context)
        and reset_context is not None
        and _approval_contextvars_present(approval)
        and invoke is not None
        and any(keyword.arg is None for keyword in invoke.keywords)
        and pre is not None
        and post is not None
        and _approval_enqueue_pre_notify_wait_order(await_fn, pre, post)
    )
    return _check(
        "approval_observe",
        available,
        "verified" if available else "callsite_contract_mismatch",
        [manager, set_context, reset_context, fire_fn, pre, post],
    )


def _probe_subagent_lifecycle(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    tree = ast.parse(sources["subagent"])
    start = _single_hook_call(tree, "subagent_start")
    stop = _single_hook_call(tree, "subagent_stop")
    start_required = {
        "parent_session_id", "parent_turn_id", "child_session_id",
        "child_subagent_id", "child_role", "child_goal",
    }
    stop_required = {
        "parent_session_id", "parent_turn_id", "child_session_id",
        "child_role", "child_summary", "child_status", "duration_ms",
    }
    start_child = _keyword_value(start, "child_subagent_id")
    start_parent = _keyword_value(start, "parent_turn_id")
    stop_parent = _keyword_value(stop, "parent_turn_id")
    available = (
        _plugin_manager_contract(manager)
        and start is not None
        and stop is not None
        and start_required <= _keyword_names(start)
        and stop_required <= _keyword_names(stop)
        and _nonempty_expression(start_child)
        and _nonempty_expression(start_parent)
        and _nonempty_expression(stop_parent)
        and _parent_turn_assignment_precedes(tree, start)
        and _call_precedes_return_in_same_function(tree, start, "child")
    )
    return _check(
        "subagent_lifecycle",
        available,
        "verified" if available else "callsite_contract_mismatch",
        [manager, start, stop],
    )


def _probe_answer_delta(sources: dict[str, str]) -> _ContractCheck:
    return _missing_hook_contract(
        "answer_delta", "answer_delta_missing", sources, ("plugin_manager", "gateway")
    )


def _probe_thinking_delta(sources: dict[str, str]) -> _ContractCheck:
    return _missing_hook_contract(
        "thinking_delta", "thinking_delta_missing", sources,
        ("plugin_manager", "gateway"),
    )


def _probe_interaction_round_trip(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    approval = ast.parse(sources["approval"])
    gateway = ast.parse(sources["gateway"])
    resolver_calls = [
        node for tree in (approval, gateway) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_name(node) in {
            "resolve_approval_choice", "resolve_clarify_choice",
            "resolve_slash_confirmation",
        }
    ]
    available = _plugin_manager_contract(manager) and len(resolver_calls) >= 3
    return _check(
        "interaction_round_trip", available,
        "verified" if available else "interaction_resolver_missing",
        [manager, approval, gateway, *resolver_calls],
    )


def _probe_terminal_disposition(sources: dict[str, str]) -> _ContractCheck:
    manager = ast.parse(sources["plugin_manager"])
    finalizer = ast.parse(sources["turn_finalizer"])
    gateway = ast.parse(sources["gateway"])
    consumers = [
        node for node in ast.walk(gateway)
        if isinstance(node, ast.Call)
        and _call_name(node) in {
            "take_terminal_disposition", "consume_terminal_disposition"
        }
    ]
    end = _single_hook_call(finalizer, "on_session_end")
    available = (
        _plugin_manager_contract(manager)
        and end is not None
        and bool(consumers)
        and any(_call_result_is_consumed(gateway, node) for node in consumers)
    )
    return _check(
        "final_delivery_disposition", available,
        "verified" if available else "terminal_consumer_missing",
        [manager, end, gateway, *consumers],
    )


def _probe_command_platform_notice(sources: dict[str, str]) -> _ContractCheck:
    return _missing_hook_contract(
        "command_platform_notice", "command_platform_notice_missing", sources,
        ("plugin_manager", "gateway"),
    )


def _probe_cron_delivery(sources: dict[str, str]) -> _ContractCheck:
    return _missing_hook_contract(
        "cron_delivery", "cron_hook_missing", sources, ("plugin_manager", "cron")
    )


def _probe_exact_native_delivery(sources: dict[str, str]) -> _ContractCheck:
    return _missing_hook_contract(
        "exact_native_delivery", "exact_native_delivery_missing", sources,
        ("plugin_manager", "base"),
    )


def _plugin_manager_contract(tree: ast.AST) -> bool:
    entrypoint_calls = _named_calls(tree, "entry_points")
    selectors = [
        call for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "select"
        and any(
            keyword.arg == "group"
            and (
                isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "hermes_agent.plugins"
                or isinstance(keyword.value, ast.Name)
                and keyword.value.id == "ENTRY_POINTS_GROUP"
            )
            for keyword in call.keywords
        )
    ]
    enabled_fn = _function(tree, "_get_enabled_plugins")
    discovery = _function(tree, "_discover_and_load_inner")
    loader = _function(tree, "_load_plugin")
    load_ep = _function(tree, "_load_entrypoint_module")
    register_hook = _function(tree, "register_hook")
    invokes = _functions(tree, "invoke_hook")
    invoke = next(
        (
            function for function in invokes
            if "cb(**kwargs)" in ast.unparse(function)
            and "results.append(ret)" in ast.unparse(function)
        ),
        None,
    )
    if not all((entrypoint_calls, selectors, enabled_fn, discovery, loader, load_ep,
                register_hook, invoke)):
        return False
    discovery_text = ast.unparse(discovery)
    loader_text = ast.unparse(loader)
    register_text = ast.unparse(register_hook)
    invoke_text = ast.unparse(invoke)
    return (
        "enabled" in discovery_text
        and "_load_plugin" in discovery_text
        and "register" in loader_text
        and "PluginContext" in loader_text
        and "_hooks.setdefault" in register_text
        and "cb(**kwargs)" in invoke_text
        and "results.append(ret)" in invoke_text
    )


def _missing_hook_contract(
    name: str,
    reason_code: str,
    sources: dict[str, str],
    targets: tuple[str, ...],
) -> _ContractCheck:
    trees = [ast.parse(sources[target]) for target in targets]
    manager = trees[0]
    hook_calls = [
        node for tree in trees[1:] for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _hook_name(node) == name
    ]
    available = _plugin_manager_contract(manager) and bool(hook_calls)
    return _check(
        name,
        available,
        "verified" if available else reason_code,
        [*trees, *hook_calls],
    )


def _check(
    name: str,
    available: bool,
    reason_code: str,
    nodes: Iterable[ast.AST | None],
) -> _ContractCheck:
    material = []
    for node in nodes:
        if node is None:
            continue
        try:
            material.append(ast.dump(node, annotate_fields=True, include_attributes=False))
        except TypeError:  # Python 3.8 compatibility for include_attributes
            material.append(ast.dump(node, annotate_fields=True))
    payload = json.dumps(
        {"capability": name, "contracts": material},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _ContractCheck(available, reason_code, _sha256(payload))


def _manager_pre_tool_contract(tree: ast.AST) -> bool:
    function = _function(tree, "_get_pre_tool_call_directive_details")
    if function is None:
        # Real fixed tag names this helper; a minimal fixture may keep the same.
        return False
    calls = [call for call in ast.walk(function) if _hook_name(call) == "pre_tool_call"]
    if len(calls) != 1:
        return False
    required = {"session_id", "tool_call_id", "turn_id"}
    return required <= _keyword_names(calls[0])


def _single_hook_call(tree: ast.AST | None, hook_name: str) -> ast.Call | None:
    if tree is None:
        return None
    calls = [call for call in ast.walk(tree) if _hook_name(call) == hook_name]
    return calls[0] if len(calls) == 1 else None


def _hook_name(call: ast.AST) -> str | None:
    if not isinstance(call, ast.Call) or not call.args:
        return None
    function_name = _call_name(call)
    if function_name not in {"invoke_hook", "_invoke_hook", "_fire_approval_hook"}:
        return None
    first = call.args[0]
    return first.value if isinstance(first, ast.Constant) and type(first.value) is str else None


def _single_named_call(tree: ast.AST | None, name: str) -> ast.Call | None:
    if tree is None:
        return None
    calls = _named_calls(tree, name)
    return calls[0] if len(calls) == 1 else None


def _named_calls(tree: ast.AST | None, name: str) -> list[ast.Call]:
    if tree is None:
        return []
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == name
    ]


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _keyword_names(call: ast.Call) -> set[str]:
    return {keyword.arg for keyword in call.keywords if keyword.arg is not None}


def _function(tree: ast.AST, *names: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            return node
    return None


def _functions(
    tree: ast.AST,
    *names: str,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]


def _call_enclosed_by_function(
    tree: ast.AST,
    call: ast.Call,
    *names: str,
) -> bool:
    return any(
        function.lineno <= call.lineno <= (function.end_lineno or function.lineno)
        for name in names
        if (function := _function(tree, name)) is not None
    )


def _call_result_is_consumed(tree: ast.AST, call: ast.Call) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is call:
            return True
        if isinstance(node, ast.Return) and node.value is call:
            return True
    return False


def _call_is_guarded_by(
    tree: ast.AST,
    call: ast.Call,
    *,
    required_names: set[str],
) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not (node.lineno <= call.lineno <= (node.end_lineno or node.lineno)):
            continue
        names = {item.id for item in ast.walk(node.test) if isinstance(item, ast.Name)}
        if required_names <= names and any(
            isinstance(item, ast.UnaryOp)
            and isinstance(item.op, ast.Not)
            and isinstance(item.operand, ast.Name)
            and item.operand.id == "interrupted"
            for item in ast.walk(node.test)
        ):
            return True
    return False


def _call_precedes_return_in_same_function(
    tree: ast.AST,
    call: ast.Call,
    return_name: str,
) -> bool:
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (function.lineno <= call.lineno <= (function.end_lineno or function.lineno)):
            continue
        return any(
            isinstance(node, ast.Return)
            and node.lineno > call.lineno
            and isinstance(node.value, ast.Name)
            and node.value.id == return_name
            for node in ast.walk(function)
        )
    return False


def _call_precedes_dispatch(tree: ast.AST, call: ast.Call) -> bool:
    dispatches = _named_calls(tree, "dispatch") + _named_calls(
        tree, "run_tool_execution_middleware"
    )
    return bool(dispatches) and call.lineno < min(item.lineno for item in dispatches)


def _normal_post_tool_call(tree: ast.AST) -> ast.Call | None:
    calls = [
        call for call in _named_calls(tree, "_emit_post_tool_call_hook")
        if _keyword_value(call, "result") is not None
        and _keyword_value(call, "duration_ms") is not None
    ]
    return max(calls, key=lambda item: item.lineno) if calls else None


def _turn_id_assignment_before(
    tree: ast.AST,
    call: ast.Call | None,
) -> ast.Assign | ast.AnnAssign | None:
    if call is None:
        return None
    candidates: list[ast.Assign | ast.AnnAssign] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.lineno >= call.lineno:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Name) and target.id == "turn_id"
                or isinstance(target, ast.Attribute) and target.attr == "_current_turn_id"
            ):
                candidates.append(node)
                break
    return max(candidates, key=lambda item: item.lineno) if candidates else None


def _named_assignment_between(
    tree: ast.AST,
    name: str,
    first: ast.Call | None,
    second: ast.Call | None,
) -> ast.Assign | ast.AnnAssign | None:
    if first is None or second is None:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if first.lineno < node.lineno < second.lineno and any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            return node
    return None


def _keyword_value(call: ast.Call | None, name: str) -> ast.AST | None:
    if call is None:
        return None
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == name),
        None,
    )


def _nonempty_expression(node: ast.AST | None) -> bool:
    return not (
        node is None
        or isinstance(node, ast.Constant) and node.value in {None, ""}
    )


def _parent_turn_assignment_precedes(tree: ast.AST, call: ast.Call) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.lineno >= call.lineno:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(target, ast.Attribute) and target.attr == "_parent_turn_id"
            for target in targets
        ):
            return True
    return False


def _approval_contextvars_present(tree: ast.AST) -> bool:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if (
            isinstance(value, ast.Call)
            and _call_name(value) == "ContextVar"
            and len(targets) == 1
            and isinstance(targets[0], ast.Name)
        ):
            names.add(targets[0].id)
    source = ast.unparse(tree)
    return (
        {"_approval_turn_id", "_approval_tool_call_id"} <= names
        and "_approval_turn_id.get()" in source
        and "_approval_tool_call_id.get()" in source
    )


def _approval_enqueue_pre_notify_wait_order(
    tree: ast.AST | None,
    pre: ast.Call,
    post: ast.Call,
) -> bool:
    if tree is None:
        return False
    entry_calls = _named_calls(tree, "_ApprovalEntry")
    notify_calls = _named_calls(tree, "notify_cb")
    wait_calls = _named_calls(tree, "wait")
    return (
        bool(entry_calls and notify_calls and wait_calls)
        and entry_calls[0].lineno < pre.lineno < notify_calls[0].lineno
        and notify_calls[0].lineno < wait_calls[0].lineno < post.lineno
    )


def _status(
    name: str,
    available: bool,
    reason_code: str,
    targets: Iterable[str],
    *,
    signature: str | None = None,
) -> NativeCapabilityStatus:
    signature_payload = json.dumps(
        {"capability": name, "targets": sorted(targets)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return NativeCapabilityStatus(
        name=name,
        available=available,
        reason_code=reason_code,
        callsite_signature=signature or _sha256(signature_payload),
    )


def _closed_probe(
    expected_commit: str,
    reason_code: str,
    *,
    plugin_evidence_sha256: str = "",
) -> NativeHookCapabilityProbe:
    statuses = tuple(
        _status(name, False, reason_code, ())
        for name in sorted(KNOWN_NATIVE_CAPABILITIES)
    )
    return NativeHookCapabilityProbe(
        capabilities=NativeHookCapabilities.from_names(()),
        statuses=statuses,
        source_commit=expected_commit if _COMMIT_RE.fullmatch(expected_commit) else "",
        source_digests=(),
        plugin_evidence_sha256=plugin_evidence_sha256,
        reason_code=reason_code,
    )


def _plugin_evidence_reason(
    evidence: PluginManagerSubprocessEvidence,
    expected_commit: str,
) -> str:
    digests = (
        evidence.runtime_executable_sha256,
        evidence.runtime_purelib_sha256,
        evidence.entrypoint_origin_sha256,
        evidence.attestation_sha256,
    )
    if not all(_DIGEST_RE.fullmatch(value) for value in digests):
        return "plugin_evidence_invalid"
    if evidence.source_commit != expected_commit:
        return "plugin_source_commit_mismatch"
    if not evidence.attestation_verified:
        return "plugin_attestation_unverified"
    if not evidence.subprocess_completed or not evidence.runtime_binding_verified:
        return "plugin_runtime_unverified"
    if evidence.matching_entrypoint_count != 1:
        return "entrypoint_ambiguous"
    if (
        not evidence.entrypoint_identity_verified
        or evidence.entrypoint_group != "hermes_agent.plugins"
        or evidence.entrypoint_key != "hermes-feishu-card"
        or evidence.entrypoint_value != "hermes_feishu_card.hermes_plugin"
        or evidence.distribution_name != "hermes-feishu-streaming-card"
    ):
        return "entrypoint_identity_mismatch"
    if not evidence.plugins_enabled_exact or evidence.matching_enabled_count != 1:
        return "plugin_not_enabled"
    if (
        not evidence.registration_verified
        or evidence.registered_hooks != HFC_REGISTERED_HOOKS
    ):
        return "registration_incomplete"
    return "verified"


def _is_bound_root(root: Path) -> bool:
    try:
        if not root.is_absolute():
            return False
        info = root.lstat()
        return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    except OSError:
        return False


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    value = result.stdout.strip()
    return value if _COMMIT_RE.fullmatch(value) else ""


def _read_bound_relative_file(root: Path, relative_path: str, limit: int) -> bytes:
    _validate_relative_path(relative_path)
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ValueError("invalid bound root")
    current = root
    parts = PurePosixPath(relative_path).parts
    for part in parts[:-1]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("invalid source parent")
    return _read_regular_file(current / parts[-1], limit)


def _anchors_match_source(
    data: bytes,
    anchors: tuple[NativeHookAnchorProvenance, ...],
) -> bool:
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return False
    for anchor in anchors:
        if anchor.line_end > len(lines):
            return False
        extracted = "".join(
            lines[anchor.line_start - 1:anchor.line_end]
        ).encode("utf-8")
        if _sha256(extracted) != anchor.slice_sha256:
            return False
    return True


def _read_regular_file(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("source must be a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("source exceeds bound")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_relative_path(value: str) -> None:
    if type(value) is not str or not value:
        raise ValueError("invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid relative path")
    if str(path) != value or "\\" in value:
        raise ValueError("invalid relative path")


def _validate_digest(value: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("invalid sha256 digest")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
