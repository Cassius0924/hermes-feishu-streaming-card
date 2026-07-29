# V4.1.2 Release Notes

[English](release-notes-v4.1.2.en.md) | [中文](release-notes-v4.1.2.md)

V4.1.2 is a narrow upgrade-recovery hotfix for V4.1.1. It fixes a race where the brief stale-heartbeat window during a normal Hermes Gateway restart could be persisted as a second restart fence and require another restart.

## Fix

- When the verified Hermes on-disk integrity plan is already `installed`, `runtime_heartbeat_waiting`, `runtime_heartbeat_missing`, and `runtime_heartbeat_stale` are liveness/readiness states only. They do not create a persistent restart/manual-review fence by themselves.
- Readiness remains degraded while the Gateway is restarting. A new `runtime.hello` with a different runtime id and matching generation/package restores `runtime_ready` directly, without a second restart.
- Generation/package mismatches, unavailable control authentication, existing manual-review/restart fences, and a real strict repair remain fail-closed. HFC still never restarts Hermes Gateway automatically.

## Upgrade

Keep using the official installation path and do not edit Hermes source by hand:

```bash
export HFC_VERSION=v4.1.2
hermes-feishu-card doctor --config CONFIG --hermes-dir HERMES_DIR --explain
hermes-feishu-card setup --config CONFIG --hermes-dir HERMES_DIR --yes
```

If `doctor` explicitly requires a Gateway restart, first confirm that there are no active sessions, then restart once through the normal Hermes service command. Recheck `status` / `doctor`; readiness should report `ready` and `runtime_ready`.

## Acceptance Scope

- Automated coverage reproduces the full race: installed plan, old runtime hello, stale heartbeat, coordinator check, new runtime hello, and no persisted fence.
- Release gates also include the full pytest suite, build and isolated `site-packages` provenance, exact merge SHA, public tag/install, Release assets, local and remote macOS upgrades, and a real Hermes-configured-model Feishu streaming-card run.
- This release does not test or change automatic compression behavior.
