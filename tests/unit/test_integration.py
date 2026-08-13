import pytest

from hermes_feishu_card.integration import (
    HYBRID_REQUIRED_NATIVE_CAPABILITIES,
    HYBRID_REQUIRED_PATCH_GROUPS,
    LEGACY_REQUIRED_PATCH_GROUPS,
    NATIVE_REQUIRED_CAPABILITIES,
    IntegrationMode,
    NativeHookCapabilities,
    PatchCapabilities,
    capability_fingerprint,
    select_integration_mode,
)


def native(*names: str) -> NativeHookCapabilities:
    return NativeHookCapabilities.from_names(names)


def patches(*names: str) -> PatchCapabilities:
    return PatchCapabilities.from_names(names)


def test_selector_prefers_fully_verified_native_hooks():
    decision = select_integration_mode(
        native(*NATIVE_REQUIRED_CAPABILITIES),
        patches(*LEGACY_REQUIRED_PATCH_GROUPS),
    )
    assert decision.supported is True
    assert decision.mode is IntegrationMode.NATIVE_HOOKS
    assert decision.required_patch_groups == frozenset()


def test_v020_shape_selects_hybrid_not_native():
    decision = select_integration_mode(
        native(*HYBRID_REQUIRED_NATIVE_CAPABILITIES),
        patches(*HYBRID_REQUIRED_PATCH_GROUPS),
    )
    assert decision.mode is IntegrationMode.HYBRID
    assert decision.required_patch_groups == HYBRID_REQUIRED_PATCH_GROUPS


def test_selector_falls_back_to_legacy_only_with_complete_legacy_groups():
    decision = select_integration_mode(native(), patches(*LEGACY_REQUIRED_PATCH_GROUPS))
    assert decision.mode is IntegrationMode.LEGACY_PATCH
    assert decision.required_patch_groups == LEGACY_REQUIRED_PATCH_GROUPS


def test_selector_rejects_incomplete_paths():
    decision = select_integration_mode(native(), patches("ingress_binding"))
    assert decision.supported is False
    assert decision.mode is None
    assert "missing" in decision.reason


def test_fingerprint_is_order_independent_and_domain_separated():
    left = capability_fingerprint(
        native("turn_start", "turn_terminal_result"),
        patches("answer_delta", "thinking_delta"),
    )
    right = capability_fingerprint(
        native("turn_terminal_result", "turn_start"),
        patches("thinking_delta", "answer_delta"),
    )
    changed = capability_fingerprint(
        native("turn_start"), patches("answer_delta", "thinking_delta")
    )
    assert left == right
    assert left.startswith("sha256:")
    assert left != changed


@pytest.mark.parametrize(
    "factory", [NativeHookCapabilities.from_names, PatchCapabilities.from_names]
)
def test_capability_models_reject_unknown_names(factory):
    with pytest.raises(ValueError, match="unknown"):
        factory(["invented_capability"])
