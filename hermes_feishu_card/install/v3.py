from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping

from ..integration import (
    HYBRID_REQUIRED_NATIVE_CAPABILITIES,
    HYBRID_REQUIRED_PATCH_GROUPS,
    IntegrationDecision,
    IntegrationMode,
)
from .native_hooks import (
    FIXED_TAG_COMMIT,
    FIXED_TAG_PROVENANCE_PATH,
    load_fixed_tag_native_hook_provenance,
)
from .patcher import (
    HYBRID_PATCH_REGISTRY,
    HYBRID_PATCH_TARGET_ORDER,
    detect_patch_groups_by_target,
    remove_patch_snapshots,
    render_patch_snapshots_from_verified_originals,
)


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_SOURCE_BYTES = 2 * 1024 * 1024


class FixedTagInstallRefused(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class FixedTagHybridPlan:
    originals: Mapping[str, bytes]
    rendered: Mapping[str, bytes]
    verified_original_sha256: Mapping[str, str]
    patch_groups: frozenset[str]
    patch_targets: Mapping[str, frozenset[str]]
    expected_fragment_matrix: Mapping[str, tuple[tuple[str, str], ...]]
    capability_fingerprint: str

    def restore(self, snapshots: Mapping[str, bytes]) -> dict[str, bytes]:
        try:
            restored = remove_patch_snapshots(
                snapshots,
                expected_groups=self.patch_groups,
                expected_fragment_matrix=self.expected_fragment_matrix,
            )
        except Exception as exc:
            raise FixedTagInstallRefused("installed Hybrid patch is not exact") from exc
        if restored != dict(self.originals):
            raise FixedTagInstallRefused("Hybrid restore did not recover verified originals")
        return restored


def build_fixed_tag_hybrid_plan(
    checkout_root: str | Path,
    *,
    decision: IntegrationDecision,
    source_commit: str,
    plugin_evidence_sha256: str,
) -> FixedTagHybridPlan:
    if (
        type(decision) is not IntegrationDecision
        or decision.supported is not True
        or decision.mode is not IntegrationMode.HYBRID
        or decision.required_native_capabilities
        != HYBRID_REQUIRED_NATIVE_CAPABILITIES
        or decision.required_patch_groups != HYBRID_REQUIRED_PATCH_GROUPS
        or type(decision.fingerprint) is not str
        or _DIGEST_RE.fullmatch(decision.fingerprint) is None
        or source_commit != FIXED_TAG_COMMIT
        or type(plugin_evidence_sha256) is not str
        or _DIGEST_RE.fullmatch(plugin_evidence_sha256) is None
    ):
        raise FixedTagInstallRefused("fixed-tag probe evidence is incomplete")
    root = Path(checkout_root).expanduser().absolute()
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise FixedTagInstallRefused("fixed-tag checkout is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FixedTagInstallRefused("fixed-tag checkout must be a regular directory")
    try:
        provenance = load_fixed_tag_native_hook_provenance(
            FIXED_TAG_PROVENANCE_PATH
        )
    except (OSError, TypeError, ValueError) as exc:
        raise FixedTagInstallRefused("fixed-tag provenance is unavailable") from exc
    canonical_sha256 = {
        source.relative_path: source.sha256.removeprefix("sha256:")
        for source in provenance.sources
        if source.relative_path in HYBRID_PATCH_TARGET_ORDER
    }
    if set(canonical_sha256) != set(HYBRID_PATCH_TARGET_ORDER):
        raise FixedTagInstallRefused("fixed-tag provenance target set is incomplete")

    originals: dict[str, bytes] = {}
    for target in HYBRID_PATCH_TARGET_ORDER:
        path = root / target
        try:
            target_metadata = path.lstat()
            if (
                stat.S_ISLNK(target_metadata.st_mode)
                or not stat.S_ISREG(target_metadata.st_mode)
                or target_metadata.st_size > _MAX_SOURCE_BYTES
            ):
                raise FixedTagInstallRefused("fixed-tag source is not regular")
            content = path.read_bytes()
        except OSError as exc:
            raise FixedTagInstallRefused("fixed-tag source is unavailable") from exc
        digest = hashlib.sha256(content).hexdigest()
        if digest != canonical_sha256[target]:
            raise FixedTagInstallRefused(
                f"fixed-tag source digest mismatch for {target}"
            )
        originals[target] = content

    expected_matrix = HYBRID_PATCH_REGISTRY.target_fragments(
        decision.required_patch_groups
    )
    try:
        rendered = render_patch_snapshots_from_verified_originals(
            originals,
            verified_original_sha256=canonical_sha256,
            integration_mode="hybrid",
            required_patch_groups=decision.required_patch_groups,
            expected_fragment_matrix=expected_matrix,
        )
        detected = detect_patch_groups_by_target(
            rendered,
            expected_groups=decision.required_patch_groups,
            expected_fragment_matrix=expected_matrix,
        )
        for target, content in rendered.items():
            compile(content, target, "exec")
        restored = remove_patch_snapshots(
            rendered,
            expected_groups=decision.required_patch_groups,
            expected_fragment_matrix=expected_matrix,
        )
    except Exception as exc:
        raise FixedTagInstallRefused("fixed-tag aggregate render verification failed") from exc
    expected_targets = HYBRID_PATCH_REGISTRY.target_groups(
        decision.required_patch_groups
    )
    if detected != expected_targets or restored != originals:
        raise FixedTagInstallRefused("fixed-tag aggregate patch did not converge")
    return FixedTagHybridPlan(
        originals=MappingProxyType(dict(originals)),
        rendered=MappingProxyType(dict(rendered)),
        verified_original_sha256=MappingProxyType(dict(canonical_sha256)),
        patch_groups=decision.required_patch_groups,
        patch_targets=MappingProxyType(dict(expected_targets)),
        expected_fragment_matrix=MappingProxyType(dict(expected_matrix)),
        capability_fingerprint=decision.fingerprint,
    )
