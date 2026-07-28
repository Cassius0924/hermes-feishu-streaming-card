# Installer Safety

[中文](installer-safety.md) | [English](installer-safety.en.md)

The installer is designed to perform only minimal, verifiable, recoverable writes. Version text changes can fall back to `gateway/run.py` anchors, but uncertain code structure, backups, manifests, or file-safety checks still fail closed.

## Pre-install Checks

Before installation, the installer verifies:

- The Hermes directory exists and contains the expected `gateway/run.py`.
- Hermes version metadata is parseable, or `gateway/run.py` contains a structure recognized by the current hook. Supported inputs include `VERSION=v2026.4.23+`, Git tag `v2026.4.23+`, `0.18.x` semantic versions, descriptive version strings, and unparseable versions paired with verifiable anchors.
- `gateway/run.py` contains an insertion point recognized by the current hook.
- Existing install state, backup, and manifest are not contradictory.
- If the Hermes directory contains `venv/bin/python`, `.venv/bin/python`, or the Windows `Scripts/python.exe` equivalent, that runtime Python must be able to import `hermes_feishu_card.hook_runtime`; otherwise setup installs the current plugin release into that venv before patching Hermes.

If a check fails, Hermes files are not modified.

Run a read-only diagnostic first:

```bash
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --hermes-dir ~/.hermes/hermes-agent
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --hermes-dir ~/.hermes/hermes-agent --explain
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --hermes-dir ~/.hermes/hermes-agent --json
```

Diagnostic output includes Hermes support status, Hermes root, `gateway/run.py`, `run_py_exists`, `version_source`, `version`, `minimum_supported_version`, `hook_strategy`, `compatibility`, anchors, and `reason`. From V3.9.1, source-stripped Hermes roots that have valid anchors but no version metadata display `version: unknown (source-stripped metadata)` so an anchor strategy is not mistaken for an actual version. From V3.6.2, diagnostics also include `runtime_import`, which confirms whether the Python interpreter actually used by Hermes Gateway can import `hermes_feishu_card.hook_runtime`. `--explain` renders runtime import, streaming config, manifest/backup/run.py install state, and next-step recommendations as a human-readable summary. `--json` emits a machine-readable report with `schema_version`, top-level `status`, `runtime_import`, `install_state`, and `recommendations` for issue templates and automation. All `doctor` modes are read-only and do not write Hermes files.

## Repair

```bash
python3 -m hermes_feishu_card.cli repair --hermes-dir ~/.hermes/hermes-agent --yes
python3 -m hermes_feishu_card.cli setup --repair --hermes-dir ~/.hermes/hermes-agent --config ~/.hermes_feishu_card/config.yaml --yes
```

`repair` only fixes install-state this project can verify. If backup is missing but the current `run.py` can safely remove this project's owned patch, it recreates the backup. If manifest is missing, malformed, or stale after backup recreation, it rebuilds the manifest. If the current unpatched source is identical to the old backup, it automatically clears the stale backup/manifest. V3.9.1 also recovers a narrowly defined marker-only state: the manifest patched hash must equal the expected patch rebuilt from the verified backup, and the current file may differ from that expected patch only on this project's owned BEGIN/END marker lines.

If an intentional Hermes upgrade replaced the unpatched source so the current `run.py` (or cron source) differs from the verified old backup, recovery refuses to treat it as ordinary stale state by default. After confirming the difference came from an intentional Hermes upgrade, opt in explicitly:

```bash
# Recover old state and reinstall from the upgraded source in one command
python3 -m hermes_feishu_card.cli install --hermes-dir ~/.hermes/hermes-agent --accept-hermes-upgrade --yes

# Or run the two phases separately
python3 -m hermes_feishu_card.cli repair --hermes-dir ~/.hermes/hermes-agent --accept-hermes-upgrade --yes
python3 -m hermes_feishu_card.cli install --hermes-dir ~/.hermes/hermes-agent --yes
```

`setup` also accepts `--accept-hermes-upgrade`. The option never restores the old backup over upgraded Hermes source. It clears only verified stale HFC backup/manifest artifacts, after which installation backs up and patches the current upgraded source. The current source must parse and expose supported hook anchors, the manifest must be valid, and the old backup must be unchanged and match its manifest hash. Missing or corrupt backups, invalid manifests, symlinks, unreadable files, unknown markers, unsupported current source, or remaining owned patches still fail closed.

`status` and `start` resolve `HERMES_DIR` from an explicit `--hermes-dir`, the selected env file, the config-adjacent `.env`, or process environment, then check hook state read-only. When a Hermes upgrade replaced the source but the old backup/manifest still verify, they report `hook.status: upgrade_repair_required` and print the explicit recovery command plus `hermes gateway start`; `start` refuses before launching the sidecar, preventing a silent “healthy sidecar, missing Gateway hook” state. User edits, corruption, unsupported source, or incomplete evidence report `manual_review_required` without offering the `--accept-hermes-upgrade` shortcut.

## V4.1 Runtime Integrity

New installs write `integrity.mode: safe`; an old config without the section remains `notify`. An older installation may run `integrity migrate-safe --config CONFIG --hermes-dir HERMES_DIR --yes` only when provenance verifies. Success prints `sidecar.restart_required: true` and `gateway.restart_required: false`: restart the sidecar to load the mode, but the migration itself does not require a Gateway restart.

After restart, the Gateway runtime sends `runtime.hello` / `runtime.heartbeat` in a separate signing domain. These events prove only the current HFC runtime generation and liveness. They carry no paths, source hashes, chat ids, or secrets and cannot authorize a file write by themselves. `safe` still requires the current Git HEAD to descend from the recorded HEAD, target blobs to equal current HEAD, backup/manifest/anchors/reversible patch evidence to agree, and a fresh fingerprint check immediately before mutation.

When strict repair successfully reinstalls the hook, readiness reports `gateway.restart_required: true`. HFC never restarts or kills Gateway automatically. The operator selects a suitable window, and a later matching `runtime.hello` clears the state. A missing authenticated control secret, source-stripped root, symlink, dirty target, branch rewind, user edit, old manifest, or changing evidence refuses automatic repair.

## Backup And Manifest

Installation saves a backup of `gateway/run.py` before writing the patched file, then writes a manifest. The manifest records at least:

- Relative `run_py` path.
- Hash of the patched `run.py`.
- Relative backup path.
- Backup hash.

`restore` and `uninstall` use the manifest to verify the current `run.py` and backup are in a state owned by this installer. If user changes or unknown tool changes are detected, the command refuses to overwrite.

## Atomic Writes

The installer writes `run.py`, backup files, and manifest files via temporary file replacement to avoid truncated files. If any install step fails, already-written state should be rolled back or cleaned up.

## Restore And Uninstall

```bash
python3 -m hermes_feishu_card.cli restore --hermes-dir ~/.hermes/hermes-agent --yes
python3 -m hermes_feishu_card.cli uninstall --hermes-dir ~/.hermes/hermes-agent --yes
```

`restore` restores the Hermes file that existed before installation. `uninstall` removes the hook and install state owned by this plugin. Neither command should overwrite unverifiable user changes.

When migrating from legacy/dual historical installs, read [migration.en.md](migration.en.md). Historical `legacy/installer_v2.py`, `legacy/gateway_run_patch.py`, and `legacy/patch_feishu.py` wrote patches outside the current manifest model and must not be assumed recoverable by current `restore`.

## Degraded Behavior

If the sidecar is unavailable, times out, or returns an error, the Hermes hook lets Hermes continue with native text replies. Card failure is a plugin failure, not an Agent workflow failure.

Hook import or emit exceptions also remain fail-open, but should not be fully silent. From V3.6.2, injected hook blocks write `[hermes-feishu-card] hook failed: ...` to Hermes stderr so runtime venv, import, or sidecar emit problems are diagnosable from Gateway logs.
