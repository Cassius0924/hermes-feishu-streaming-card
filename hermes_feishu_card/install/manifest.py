from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping


CURRENT_INSTALL_MANIFEST_VERSION = 2
LEGACY_INSTALL_MANIFEST_VERSIONS = frozenset({1})
CRON_INSTALL_MANIFEST_FIELDS = frozenset(
    {
        "cron_py",
        "cron_patched_sha256",
        "cron_backup",
        "cron_backup_sha256",
    }
)
BASE_INSTALL_MANIFEST_FIELDS = frozenset(
    {
        "base_py",
        "base_patched_sha256",
        "base_backup",
        "base_backup_sha256",
    }
)
UNSUPPORTED_INSTALL_MANIFEST_VERSION_MESSAGE = (
    "install manifest version is unsupported; newer installer required; "
    "refusing to mutate"
)


class ManifestVersionError(ValueError):
    pass


class ManifestStructureError(ValueError):
    pass


def validate_install_manifest_version(manifest: Mapping[str, object]) -> int:
    """Return the understood version or reject unsafe mutation semantics.

    Manifests without an explicit version predate versioning and are treated as
    legacy v1. Writers always emit the current version. Unknown versions are
    never guessed because they may own targets this installer does not know.
    """

    if "manifest_version" not in manifest:
        return 1
    version = manifest.get("manifest_version")
    if type(version) is int and version in {
        *LEGACY_INSTALL_MANIFEST_VERSIONS,
        CURRENT_INSTALL_MANIFEST_VERSION,
    }:
        return version
    raise ManifestVersionError(UNSUPPORTED_INSTALL_MANIFEST_VERSION_MESSAGE)


def validate_install_manifest(manifest: Mapping[str, object]) -> int:
    version = validate_install_manifest_version(manifest)
    _validate_managed_field_group(
        manifest,
        CRON_INSTALL_MANIFEST_FIELDS,
        "cron",
    )
    _validate_managed_field_group(
        manifest,
        BASE_INSTALL_MANIFEST_FIELDS,
        "exact Base",
    )
    return version


def _validate_managed_field_group(
    manifest: Mapping[str, object],
    fields: frozenset[str],
    label: str,
) -> None:
    present = fields.intersection(manifest)
    if present and present != fields:
        raise ManifestStructureError(
            f"manifest {label} ownership fields are incomplete; refusing to mutate"
        )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
