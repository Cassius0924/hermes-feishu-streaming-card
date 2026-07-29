# V4.1.3 Release Notes

[English](release-notes-v4.1.3.en.md) | [中文](release-notes-v4.1.3.md)

V4.1.3 is a narrow upgrade-recovery hotfix for Issue #158. A real Hermes upstream update first removes the managed hook. After official `install` reinjects it, the current integrity-plan fingerprint changes while the manual-review fence remains bound to the pre-upgrade plan. V4.1.2 correctly rejected that binding mismatch but provided no supported transition from the old plan to the current plan, forcing the reporter to edit the fence manually.

## Fix

- `integrity acknowledge-review` can atomically migrate a plan binding for the same Hermes target only after two current recovery/integrity-plan checks report installed, two checks confirm no sidecar health and no pidfile, explicit `--yes`, an exact old/current `target_identity` match, and an unchanged fence snapshot CAS.
- A different target identity, state drift, a running sidecar, an unknown legacy fence, or a dirty/unverifiable plan still fails closed. This is not a force-clear mechanism.
- When an independent restart fence has a non-empty `pre_repair_runtime_hash`, acknowledgement clears manual review and updates the plan binding while preserving restart/hash until a different runtime id sends a generation/package-matching `runtime.hello`.
- `doctor --explain` prints the complete `integrity migrate-safe` command for `integrity_migration_required`. Other manual-review cases first require installed-evidence review and then print a complete `integrity acknowledge-review` command with explicit config, Hermes, and state paths.

## Upgrade and Recovery

Keep using the official installer and diagnostics. Do not edit `runtime-integrity-fence.json` or call internal Python functions:

```bash
export HFC_VERSION=v4.1.3
hermes-feishu-card doctor --config CONFIG --hermes-dir HERMES_DIR --explain
hermes-feishu-card setup --config CONFIG --hermes-dir HERMES_DIR --yes
```

If a real Hermes update removes the hook, rerun official `install` as directed by doctor. When install state is verified as installed but readiness remains `manual_review_required`, stop the sidecar, run the displayed `integrity acknowledge-review --config CONFIG --hermes-dir HERMES_DIR --state-dir STATE_DIR --yes`, start the sidecar, and manually restart Hermes Gateway once. Finally verify `readiness: ready`, `readiness.reason: runtime_ready`, and `hook.status: installed`.

## Candidate Acceptance Scope

- Automated coverage includes successful same-target plan transition, default refusal, different-target refusal, double current-plan/state/process checks, CAS protection, and restart/hash preservation.
- Diagnostics cover complete migration and manual-review commands without exposing real paths, fingerprints, or private state evidence.
- Before release, the Issue #158 reporter still needs to retest the candidate commit on Ubuntu 24.04 after a real Hermes upstream update using only the official flow; manual fence edits do not count as a pass.
- Exact merge SHA, public tag/install, and Release assets remain release-stage gates.
