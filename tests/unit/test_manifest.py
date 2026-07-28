import hashlib

import pytest

from hermes_feishu_card.install.manifest import (
    CRON_INSTALL_MANIFEST_FIELDS,
    ManifestStructureError,
    ManifestVersionError,
    file_sha256,
    validate_install_manifest,
    validate_install_manifest_version,
)


def test_file_sha256_returns_stable_digest(tmp_path):
    path = tmp_path / "run.py"
    path.write_text("hello hermes\n", encoding="utf-8")

    assert file_sha256(path) == hashlib.sha256(b"hello hermes\n").hexdigest()


@pytest.mark.parametrize("manifest", [{}, {"manifest_version": 1}])
def test_validate_install_manifest_version_accepts_legacy_read_compatibility(manifest):
    assert validate_install_manifest_version(manifest) == 1


def test_validate_install_manifest_version_accepts_current_version():
    assert validate_install_manifest_version({"manifest_version": 2}) == 2


@pytest.mark.parametrize("version", [0, 3, 999, "2", True, None])
def test_validate_install_manifest_version_refuses_unknown_semantics(version):
    with pytest.raises(ManifestVersionError, match="newer installer required"):
        validate_install_manifest_version({"manifest_version": version})


@pytest.mark.parametrize("field", sorted(CRON_INSTALL_MANIFEST_FIELDS))
def test_validate_install_manifest_refuses_every_partial_cron_field(field):
    with pytest.raises(ManifestStructureError, match="cron ownership fields"):
        validate_install_manifest({"manifest_version": 2, field: "owned"})


def test_validate_install_manifest_accepts_complete_cron_field_group():
    manifest = {"manifest_version": 2}
    manifest.update({field: "owned" for field in CRON_INSTALL_MANIFEST_FIELDS})

    assert validate_install_manifest(manifest) == 2
