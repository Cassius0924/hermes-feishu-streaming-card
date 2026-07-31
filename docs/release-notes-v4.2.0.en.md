# Hermes Feishu Streaming Card v4.2.0

> Candidate release notes. This implementation branch does not publish, tag, or merge itself.

## Added

- A bare `/update` in a Feishu private chat inspects Hermes, the Git worktree, HFC hooks, the update target, active work, and the pinned maintenance artifact before showing a 120-second confirmation card.
- After confirmation, an independent process outside the Hermes checkout runs the official `hermes update --yes`, reinstalls the exact current HFC version from a private cache, restores hooks, and restarts and verifies the sidecar and Gateway.
- The original card reports draining, hook restoration, Hermes update, HFC reinstall, service startup, verification, and the final result.
- Local recovery commands are available under `hermes-feishu-card maintenance provision|status|run|resume`.

## Safety boundary

- Only an exact bare `/update` in a Feishu private chat is intercepted. Group, non-Feishu, alias, and parameterized commands keep the original Hermes behavior.
- Confirmation is bound to the initiator, chat, profile, update target, local evidence, and a 120-second expiry.
- The workflow never adds `--force`, `--force-venv`, or `--no-backup`, and never performs a custom Git reset, checkout, stash, or rollback.
- Untracked files are preserved. Unrelated tracked changes, incomplete Git operations, artifact drift, or final verification failures stop the workflow.
