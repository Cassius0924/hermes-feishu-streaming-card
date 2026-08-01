# Release Readiness

[中文](release-readiness.md) | [English](release-readiness.en.md)

Current release candidate: `4.2.3`. A real Feishu click proved the native WebSocket action reached the Gateway, but the V4.2.2 hook dropped `update_evidence_fingerprint` while forwarding the payload, so the sidecar failed closed before the evidence-bound transition. V4.2.3 restores only that field while preserving fast acknowledgement and the identity, chat, expiry, preflight, drain, and evidence-validation boundaries. Full automation, build, CI, real private-chat acceptance, exact merge SHA, public tag/install, and Release assets are marked passed only after completion.

V3.9.0 was released on 2026-07-11, and V3.9.1 was released on 2026-07-11. The V4.0.13 all-command lifecycle remains intact; V4.2.0 narrows only a private-chat bare `/update` into the stricter dedicated maintenance card.

## Ready

- Hermes `v2026.4.23+` detection and fail-closed installation.
- Minimal Hermes hook, backup, manifest, restore, and uninstall.
- Sidecar `/events`, `/health`, and process `start/status/stop`.
- Feishu CardKit HTTP client, covered by mock Feishu server and real Feishu test app for tenant token, send, and update flows.
- Manual `smoke-feishu-card` command.
- E2E preview artifacts and generator.
- Real long-card stress test: one Feishu card updated to 16k Chinese characters.
- Real Hermes `v2026.4.23` `restore -> install` loop verification.
- Hermes `0.13.0+` / `0.14.0` / `0.15.x` / `0.17.x` / `0.18.x` / `v2026.5.16+` / `v2026.6.19+` / `v2026.7.1+` / `v2026.7.7.2` use the `gateway_run_013_plus` hook strategy, while older `v2026.4.x` keeps `legacy_gateway_run`.
- Feishu card button interactions are covered through local mock acceptance for `interaction.requested`, `/card/actions`, and `/interactions/{interaction_id}`; localhost/private sidecars use WebSocket-native callbacks in default `auto` mode, while explicit `card.interaction_mode: text` retains numbered-text fallback.
- Feishu thread messages can carry optional `thread_id`; with a reply anchor, the sidecar uses the Feishu reply API to create the initial card in the original thread, and later updates keep PATCHing the same card.
- Cron delivery can extract chat ids from `deliver: "feishu:oc_xxx"` and can resolve `deliver: origin`, `deliver: all`, and `origin,all` through Feishu origins or scheduler targets, avoiding plain-text fallback for scheduled Feishu deliveries; `deliver: local` remains no delivery.
- Long Markdown tables and fenced code blocks over `MAIN_CONTENT_CHUNK_CHARS` are split as complete repeated structures to avoid raw Markdown rendering.
- Thinking/interim assistant messages use complete `append_block` chunks to avoid delta accumulation truncation or missing text.
- Runtime event sends, sidecar updates, and terminal PATCH calls are ordered/coalesced for the same message id.
- Newer Hermes streams that begin with `answer.delta`, `thinking.delta`, `tool.updated`, or `message.completed` without `message.started` still create the initial Feishu/Lark card.
- Native Hermes `Working` heartbeats, context-window/compression notices, automatic session resets, skill loading, and self-improvement reviews are normalized as `system.notice`; session notices prefer the active card timeline, while task-external notices use compact standalone cards.
- In Feishu/Lark topic replies, later `answer.delta`, `thinking.delta`, `tool.updated`, and `system.notice` events resolve through `reply_to_message_id` back to the same card even when Hermes uses a different internal streaming `message_id`, preventing frozen topic timelines and duplicate gray native notices.
- In Feishu/Lark topic groups that reuse the same `message_id` across consecutive turns, completed or failed old sessions are cleared and a fresh card is created; duplicate `message.started` events during an active turn still stay ignored to avoid accidental second cards.
- Gateway runtime coalesces high-frequency `thinking.delta` / `answer.delta` events inside the Hermes process, covering V3.8.1 issue #74 and reducing stream-reader thread pressure.
- Terminal events flush pending deltas for the same message before final card rendering.
- Feishu-side `/hfc help/status/doctor/monitor` commands return read-only diagnostic cards with hashed context ids.
- Accepted `/hfc` diagnostic commands ACK Hermes Gateway quickly and send the real Feishu/Lark card in the background, preventing `/hfc status` from double-sending a card plus the gray native `Unknown command /hfc` reply.
- Generic attachment summaries in completed cards no longer trigger native final-reply fallback; real `MEDIA:`, local file paths, and Hermes media/file locals still keep the native file/media delivery path available.
- Group `/hfc status` reports chat binding state, fallback/default routing, the suggested `bots bind-chat` command, and group slash-command behavior boundaries while real @robot and allowlist admission remains owned by Hermes Gateway.
- Pre-tool answers stay in the primary body first, then archive into the auxiliary timeline when the next answer or terminal event arrives; terminal cards strip already archived intermediate prefaces.
- Auxiliary timeline reasoning and tool details use separate text sizes and visual weight, while raw `thinking.delta` stays out of the user-visible timeline.
- Tool details can show argument summaries, duration, and failure reasons while keeping timeline rendering compact.
- Independent slash-command confirmations support Feishu command cards: `/new`, `/reset`, `/undo`, and high-cost `/model <model>` prompts render as standalone command cards when available.
- Feishu/Lark WebSocket long-connection deployments dynamically gain native `send_slash_confirm(...)` and `send_model_picker(...)` card support; button clicks route through `_on_card_action_trigger` back into Hermes' original handlers.
- When WebSocket-native cards are available, the sidecar `interaction.requested` pre-card is skipped so the same slash command does not show both a sidecar choice card and a native button card.
- No-argument `/model` selection can use a Feishu-only `send_model_picker(...)` card, call Hermes's callback, and update the same command card with the result.
- V4.2.0 intercepts only a bare `/update` in a Feishu private chat: after read-only inspection it shows a 120-second maintenance confirmation, then an independent runtime runs `hermes update --yes`, reinstalls the same HFC version, and restores the hook, sidecar, and Gateway. Group, non-Feishu, alias, and parameterized commands retain Hermes' original path. Run `maintenance status` first.
- Terminal events ACK Hermes quickly while slow Feishu PATCH calls complete in the background, preventing duplicate native replies after interrupts or update backlogs.
- `load_config()` reads a `.env` file next to the selected config file while preserving real process environment variables as the highest-precedence source.
- `install.sh` imports only Feishu/sidecar variables from `.env`, avoiding execution of unrelated values such as paths with spaces.
- `install.sh` retries pip with `--break-system-packages` when uv/PEP 668 reports an externally managed Python environment.
- Windows sidecar process `stop/status` avoids POSIX process-group signals and uses Windows-specific PID/`taskkill` handling.
- `doctor --json` / `doctor --explain` report config, sidecar, Hermes, streaming, install_state, and recommendations.
- `doctor --explain` / `install` suggest the Hermes CLI `Project:` directory as the correct `--hermes-dir` when `gateway/run.py` is missing and `hermes -V` is available.
- `setup` / `install` detect the Hermes runtime venv Python and install the same plugin release there; `doctor` reports `runtime_import`.
- `install-docker.sh` supports installer/update workflows inside existing Hermes Docker containers with defaults `HERMES_DIR=/opt/hermes`, `HFC_CONFIG=/opt/data/config.yaml`, and `HFC_ENV_FILE=/opt/data/.env`.
- `docker-compose.example.yml` documents bind mounts and one-shot `bash install-docker.sh` execution for container topologies.
- Docker/source-stripped Hermes roots without `VERSION` and `.git` metadata can fall back to `gateway/run.py` anchors in `doctor`, `install`, and `setup`; diagnostics report `version_source: gateway anchors`. If version metadata exists but is unparseable, verifiable anchors allow diagnostics to report `VERSION + gateway anchors` or `git tag + gateway anchors` and continue.
- Hook import/emit failures remain fail-open but write `[hermes-feishu-card] hook failed: ...` diagnostic warnings to Hermes stderr.
- `repair --hermes-dir ... --yes` and `setup --repair` repair verifiable manifest/backup state and refuse unverifiable user edits.
- Structured attachment, media, and file objects keep card summaries while preserving Hermes native media/file delivery paths.
- `smoke-feishu-card --profile-id`, `bots test --profile-id`, CLI `status`, and `/health.routing.profiles` support profile-scoped troubleshooting.
- Hermes key release matrix covers `v2026.4.23`, `v2026.5.7`, `v2026.5.16+`, `v2026.5.29`, `v2026.6.19+`, `v2026.7.1+`, `v2026.7.7.2`, `0.13.x`, `0.14.x`, `0.15.x`, `0.17.x`, `0.18.x`, semantic versions with or without a `v` prefix, and descriptive version metadata.
- GitHub Actions Python 3.9 / 3.12 test matrix for PRs and pushes, plus Windows parser validation for `install.ps1`.
- Release assets workflow packages macOS/Linux/Windows installers and checksums for tags.
- V3.9.0 operations cards support diagnosis, recheck, two-step safe repair, and restart confirmation; private chats do not compare operators, while group repair/restart confirmation stays with the initiator. Use CLI fallback when the card is unavailable.
- The state-dir transport root automatically creates a private-permission transport secret. No secret configuration is required, and diagnostics/cards never output it.
- Setup resolves profile/event URL by explicit argument, process environment, selected env file, then default; only `doctor` shows the complete redacted identity/profile/event-endpoint route chain, `status` summarizes runtime routing/profile events, and `/health` reports actual routing-health fields.
- Install/setup can automatically repair known-safe state; `--no-repair` opts out, and unverifiable user edits remain refused. Cleanup history and metrics are bounded and hashed.
- Operations-card WebSocket callbacks ACK immediately, authenticated actions enter a bounded background queue with finite retry, and every authenticated state PATCHes the original card without making recheck/repair/restart wait for Feishu PATCH completion.
- Automated release gate: `1172 passed, 3 skipped` on Python 3.9 and Python 3.12. Operations semaphore/publish-lock state is initialized only inside the active event loop, preserving the declared Python 3.9 support.
- Real Feishu private-chat acceptance passed on 2026-07-11: `/hfc doctor` produced no gray native unknown-command reply; localized details and two consecutive rechecks (including the background successor) ACKed in 156–201 ms without a target-callback timeout toast and updated the same card; sandboxed two-step safe repair, card-triggered Gateway restart, and the normal completed-card footer passed with zero sidecar send/update failures.
- V3.9.1 regression coverage includes completed-answer boundaries, interrupted terminal ordering, asynchronous model-picker callbacks, loopback no-proxy behavior, marker-only recovery, and refusal of unknown edits.
- V3.9.1 automated release gate: `1198 passed, 3 skipped` on both Python 3.9 and Python 3.12, followed by `git diff --check`.
- V3.10.0 bare `/resume` picker tests cover the original Hermes handler, group initiator, topic metadata, expired/invalid state, fail-open behavior, and immediate ACK.
- V3.10.0 footer tests prove only the escaped model label changes color; element ids, field order, separators, text size, and non-completed states remain unchanged.
- V4.0.0 combines Hermes tool names and `tool.updated.detail` into deterministic non-completed Header action summaries and streams public `thinking.delta` independently in the body; final `answer.delta` remains the primary body content.

## Required Pre-release Checks

```bash
python3 -m pytest -q
python3 -m hermes_feishu_card.cli doctor --config config.yaml.example --hermes-dir ~/.hermes/hermes-agent --explain
python3 -m hermes_feishu_card.cli install --hermes-dir ~/.hermes/hermes-agent --yes
python3 -m hermes_feishu_card.cli restore --hermes-dir ~/.hermes/hermes-agent --yes
```

Real Feishu integration must use local config or environment variables for `FEISHU_APP_ID` and `FEISHU_APP_SECRET`. Do not commit App Secret, tenant token, real chat_id, or sensitive screenshots. Public screenshots must be checked for secrets and private conversation content before being added to the repository.

## V3.9.0 Manual Acceptance Progress

- Existing-container Docker: fresh install, pinned upgrade, known-safe corrupt-marker auto-repair, user-edit refusal, main/child profile endpoint mapping, and final `doctor`. **Pending acceptance**.
- Real Feishu private chat: `/hfc doctor`, localized details, recheck, a second click from the background successor, same-card PATCH, sandboxed two-step safe repair, card-triggered Gateway restart, and the normal footer snapshot. **Passed on 2026-07-11**.
- Real Feishu cron: a no-agent one-shot result reached a normal completed card; sidecar event receive/apply/card-send metrics succeeded with no fallback. **Passed on 2026-07-11**.
- Profile route mismatch: a temporary invalid `HERMES_FEISHU_CARD_PROFILE_ID` produced a redacted `profile_unknown` route chain, and removing the temporary environment restored the default profile without changing persistent config. **Passed on 2026-07-11**.
- V3.10.0 real Feishu `/resume`: private chat, group initiator, topic placement, and same-card PATCH passed; changed-operator rejection remains automation-backed because the test group had one human participant.

Acceptance also exposed an upstream Hermes `cron run` status-reporting bug: a successful finite one-shot can print `Ran now: failed` because Hermes re-reads `last_status` after the completed job record has already been deleted. This does not indicate a card-delivery failure; the acceptance decision uses the matching Feishu card, sidecar metrics, and saved cron output. The plugin deliberately does not add another source patch for Hermes `tools/cronjob_tools.py` just to mask this upstream CLI issue.

## V3.9.1 Release Gates

- Python 3.9 / 3.12 full automation: **passed (`1198 passed, 3 skipped`)**.
- `git diff --check`: **passed**.
- Real Feishu focus: model-picker callbacks, interrupted terminal cards, and completed-answer preservation follow the [Feishu acceptance checklist](wiki/feishu-acceptance.md); public evidence remains redacted.
- Release assets: verify macOS, Linux, Windows, and checksums after tagging.

## V3.10.0 Release Gates

- Focused interaction/installer/render matrix: **passed (`416 passed`)**.
- Python 3.9 / 3.12 full automation: **passed (`1216 passed, 3 skipped`)**.
- Real Feishu: private chat, group initiator, topic same-thread update, and footer passed; changed-operator rejection is covered by automation.
- `v4.0.0`: **released on 2026-07-12**. The release-assets workflow succeeded; the macOS, Linux, Windows, and checksums assets were complete and checksum-verified; installation from the public tag reported version `4.0.0` and the CLI started successfully.
- `v3.10.0`: **released on 2026-07-11** with all four assets verified.

## V4.0.1 Release Gates

- Issue #106 data-flow regression, normal/queued completion, and V4.0.0 hook-upgrade tests: **passed**.
- Hook/patcher/install/server hot-path matrix: **passed (`509 passed`)**.
- Hermes `extract_media()` verification: **passed**, preserving the media path with an empty native-visible text body.
- Full automation: **passed (`1257 passed, 3 skipped`)**; `git diff --check` passed.
- Local package smoke: **passed**. The sdist and wheel built successfully, and a clean venv imported version `4.0.1`.
- V4.0.1 public installation and Release assets: **passed**; all four assets were present and checksum-verified.

## V4.0.3 Release Gates

- Stale-hook exact media-text deduplication, one-shot consumption, media preservation, and sidecar fail-open regressions: **passed**.
- Hook/patcher/install/server hot-path matrix: **passed (`513 passed`)**.
- Full automation: **passed (`1269 passed, 3 skipped`)**; `git diff --check` passed.
- Local package: **passed**. The sdist and wheel built successfully, and a clean venv imported version `4.0.3` from `site-packages`.
- Public installation and Release assets: **pending post-tag verification**.

## V4.0.2 Release Gates

- Recovery/install regression matrix: **passed (`121 passed`)**.
- Real local upgrade from an older owned hook: **passed**. Recovery emitted `run.py: reapplied current hook`; doctor reported a complete, consistent install state; Gateway and sidecar resumed.
- Issue #107 opt-in quota footer: **passed**. The server/render/subscription-usage focused matrix reported `237 passed`; a read-only call through the local Hermes native API returned and formatted both Session and Weekly windows.
- Full automation: **passed (`1266 passed, 3 skipped`)**; `git diff --check` passed.
- Local package: **passed**. The sdist and wheel built successfully, and a clean venv imported version `4.0.2` from `site-packages`.
- Public installation and Release assets: **pending post-tag verification**.

## V4.0.0 Release Gates

- Session/render/status focused tests: **passed (`139 passed`)**.
- Server/hook/model-picker hot-path matrix: **passed (`341 passed`)**.
- Private/group real-Feishu four-state acceptance: **passed on 2026-07-12**. Running, waiting, failed, and completed states updated one card in place; runtime action summaries remained independent from public interim output; non-completed footers contained status only; completed cards kept the native reply quote without a duplicate Card JSON Header; no gray native duplicate or callback timeout appeared.
- Real Feishu `/model`: **passed on 2026-07-12**. Provider and model data came directly from the same upstream Hermes CLI picker list; provider navigation, Back, model switching, and same-card result updates all succeeded.
- All four public screenshots: **passed privacy and visual review**, retaining only redacted real-Feishu card regions.
- Full automation: **passed (`1252 passed, 3 skipped`)**; `git diff --check` passed.
- Local release-package smoke: **passed**. The sdist and wheel built successfully, a clean Python 3.12 venv imported version `4.0.0`, and the Hermes `v2026.7.7.2` doctor confirmed runtime import, streaming, and install state.
- Verify macOS, Linux, Windows, and checksums assets after tagging.

The `v3.9.0` release-assets workflow publishes four assets: the macOS tarball, Linux tarball, Windows zip, and checksums file: `hermes-feishu-card-v3.9.0-macos.tar.gz`, `hermes-feishu-card-v3.9.0-linux.tar.gz`, `hermes-feishu-card-v3.9.0-windows.zip`, and `hermes-feishu-card-v3.9.0-checksums.txt`.

## V4.2.3 Release Gates

- The WebSocket hook must forward `update_evidence_fingerprint` unchanged from the card value to the sidecar; the missing-field regression was observed red before the fix and green afterward: **passed**.
- The related hook/runtime/server/Feishu SDK matrix reports **`670 passed, 1 skipped`**. Full pytest reports **`2309 passed, 5 skipped`**; `git diff --check`, sdist/wheel, and clean Python 3.12 `site-packages` package/distribution/CLI provenance: **local candidate gate passed**. PR CI, exact merge SHA, public tag/install, Release assets, and real Feishu confirm/cancel: **pending release-flow verification**.
- Real acceptance must observe a sidecar update attempt, the original-card transition, and proof that cancel did not start the updater; a click or Gateway action log alone is insufficient.

## V4.2.2 Release Gates

- The native card action must return its empty acknowledgement first, then let the sidecar asynchronously PATCH the original confirmation card; Feishu API latency must stay outside the callback deadline: **focused regression passed**.
- Cancel must persist `cancelled`, render the terminal cancellation card, and never schedule the updater; confirm must attempt the locking/preparing card transition before independent maintenance is scheduled: **related operations/server/hook-runtime matrix passed (`378 passed`)**.
- Full pytest reports **`2307 passed, 5 skipped`** on both Python 3.9 and 3.12; `git diff --check`, wheel/sdist, clean Python 3.12 `site-packages` package/distribution/CLI provenance, PR CI, exact merge SHA, public tag/install, and Release assets: **release flow passed**. The subsequent real Feishu click exposed the dropped WebSocket evidence fingerprint, so terminal cancellation did not complete and is superseded by the V4.2.3 candidate.

## V4.2.1 Release Gates

- Startup adapter installation must register the live Gateway runner before runtime control starts, and the first heartbeat must carry complete `_active_work_count()` aggregate evidence: **focused regression passed**.
- Missing, failing, negative, or non-integer aggregates remain refused and are never downgraded to zero work: **safety boundary retained**.
- Full pytest reported **`2306 passed, 4 skipped`** on both Python 3.9 and 3.12; `git diff --check`, wheel/sdist, clean `site-packages`, and the maintenance runtime: **local candidate gate passed**. PR CI, exact merge SHA, public tag/install, and Release assets: **verified during release**.

## V4.2.0 Release Gates

- Read-only inspection, 120-second confirmation binding, cancel/expiry/replay/cross-operator rejection, and the dedicated maintenance card for a bare private-chat `/update`: **automated coverage passed**.
- Exact-wheel provisioning, durable job/journal/lock, official `hermes update --yes`, same-version HFC reinstall, hook/service restoration, and `maintenance status/resume`: **automated coverage passed**.
- Unrelated tracked changes, incomplete Git state, artifact/version drift, and failed final verification stop; untracked files remain and no custom Git rollback runs: **automated coverage passed**.
- Full pytest reported **`2304 passed, 4 skipped`** on Python 3.9 and **`2303 passed, 5 skipped`** on Python 3.12; `git diff --check`, wheel/sdist, clean Python 3.12 `site-packages` package/distribution/CLI provenance, and real `maintenance provision/status` independent-runtime and runner-import checks: **local candidate gate passed**.
- PR CI, real Feishu private-chat card acceptance, exact merge SHA, public tag/install, and Release assets: **verified during release**.

## V4.1.4 Release Gates

- Remove the manifest from regular Gateway, Hermes v0.19.0 required exact Base, and optional Cron states produced by the public v4.0.14 package imported from `site-packages`; V4.1.4 official install must print `manifest: rebuilt` / `install ok` and doctor must return to `installed`: **passed isolated local reproduction**.
- Unicode comments plus all-CRLF source, native Windows relative paths, and the no-directory-fd portable install path must pass without being mislabeled as the root cause: **isolated old-package fixtures and the equivalent branch passed; reporter's real Windows confirmation pending**.
- Migration is allowed only when lenient legacy Gateway block removal exactly matches the clean backup and strict Cron/Base removal independently matches each backup; `--no-repair`, missing targets, outside-block edits, and concurrent edits before write/rollback must preserve evidence and refuse: **safety-boundary regression passed**.
- Full pytest **`2221 passed, 5 skipped`**, `git diff --check`, wheel/sdist, and isolated Python 3.12 `site-packages` package/distribution/CLI provenance: **passed locally**; PR CI, Issue #171 official Windows-flow retest, exact merge SHA, public tag/install, and Release assets: **pending**.

## V4.1.3 Release Gates

- An old plan binding for the same target can transition only after two current recovery/integrity-plan checks report installed, two checks confirm the sidecar stopped, and the fence CAS snapshot remains unchanged: **focused regression passed**.
- A different target, state drift, remaining pidfile/health, unknown legacy fence, or unverifiable plan remains fail-closed; a non-empty restart/hash fence is preserved: **safety-boundary regression passed**.
- `doctor --explain` must print complete `integrity migrate-safe` and `integrity acknowledge-review` commands without exposing paths, fingerprints, or private state evidence: **diagnostic regression passed**.
- PR #168 must select only the native text callback that calls `_stream_consumer.on_delta` and relocate an older hook: **independent review, full automation, and real Hermes source injection passed; merged with contributor authorship preserved**.
- Hermes `1a3a9de` TurnRunner source must restore 14 managed hook blocks, one of each of the six moved hooks, place status after ctx binding, remain idempotent and byte-for-byte removable, and report `supported/full`; unknown shapes must be `not safely patchable`: **regression, real-source verification, and PR #170 CI passed**.
- Combined-candidate full pytest **`2207 passed, 4 skipped`**, `git diff --check`, wheel/sdist, and isolated Python 3.12 `site-packages` package/distribution/CLI entry-point provenance: **passed locally**; Issue #158 official Ubuntu retest, Issue #169 latest-Hermes real Feishu retest, candidate CI, exact merge SHA, public tagged install, and Release assets: **pending**.

## V4.1.2 Release Gates

- The installed-plan, old-runtime hello, stale heartbeat, coordinator check, and new matching hello race must create no fence and restore ready in one cycle: **focused regression passed**.
- Generation/package mismatch, unavailable control authentication, manual-review/restart fences, and real strict repair remain fail-closed with no automatic Gateway restart: **safety-boundary regression passed**.
- Stable-wrapper detection plus the explicit fail-open fallback must prevent one call from traversing both the stable and legacy progress paths; real Hermes-configured-model runs on the local and remote MacBook Pro each rendered one timed terminal entry: **automation and dual-machine real Feishu passed**.
- Candidate full pytest **`2197 passed, 4 skipped`**, `git diff --check`, wheel/sdist, and isolated `site-packages`/CLI provenance: **passed**; exact merge SHA, public tagged install, and Release assets: **pending after merge**.

## V4.1.1 Release Gates

- A verified `installed` plan neither repairs nor writes a restart/manual-review fence while the first heartbeat is waiting/missing, and resumes normal evaluation after a matching `runtime.hello`: **candidate focused and full regressions passed**.
- `integrity acknowledge-review` requires installed + unreachable sidecar health + no pidfile; empty hash can clear an unresolvable fence while a non-empty hash keeps the restart fence until a different-runtime-id matching hello: **CLI, persistence, and restart simulation passed**.
- A legacy `0644` pidfile is tightened only inside a private owned `0700` state directory through fd identity binding; a pidfile-less process is never silently adopted/killed and requires the operator to stop the old service before rerunning: **real macOS process tests passed; Linux CI remains pending**.
- Setup installs/rechecks through the Hermes runtime venv and uses `/health` package version plus Python identity to decide whether to restart sidecar; sidecar and Gateway are then restarted manually: **local and remote upgrade acceptance pending**.
- Candidate `20b7b06`: full pytest **`2194 passed, 4 skipped`**, `git diff --check`, wheel/sdist build, isolated `site-packages` provenance, and wheel real-process tests **`8 passed`**; **CI, exact merge SHA, public tagged install, Release assets, Linux/Docker, and real Feishu remain pending release gates**.

## V4.1.0 Release Gates

- Exact/profile-scoped `bindings.native_chats`, two-stage hook/sidecar enforcement, fail-open direct-card paths, and card-based `/hfc`: **focused matrix and real card → native → card acceptance pending**.
- Default `table_overflow_mode=compact`, fenced fake-table exclusion, and a text-only terminal native handoff above 28,000 bytes using a V2 descriptor, stable UUIDs, the Hermes ledger, and delivered-then-ACK order; outside the window the exact descriptor expires while visible-marker bounded upstream recovery remains ordinary fail-open: **real seven-table and oversized-handoff acceptance pending**.
- `integrity.mode` safe/notify/off, signed `runtime.hello` / `runtime.heartbeat`, strict repair, `sidecar.restart_required`, and no automatic Gateway restart: **upgrade simulation pending**.
- All four `service.manager` modes, non-escalating `auto`, and ordinary Docker containers: **Linux manager and Docker Compose smoke pending**.
- Full pytest, `git diff --check`, build/isolated `site-packages`, exact merge SHA, public tagged install, and Release assets: **release workflow pending**.

## V4.0.21 Release Gates

- Issue #155: only an explicit `answer -> tool` boundary can archive an answer; `tool -> answer -> completed` must retain the full user-visible terminal answer: **passed focused ordering coverage (`74 passed`)**.
- Issue #147: after the completed card accepts the event, matching native media text is suppressed once, the native image still delivers, and an accepted queued notice emits no uncertain-delivery warning: **passed hook-runtime combination coverage (`277 passed`)**.
- Current README, install guidance, Docker Compose, and bilingual user guides pin `v4.0.21`; UI and configuration remain unchanged: **passed documentation gate**.
- Real Feishu image acceptance: **passed (2026-07-28)**. Observed one marker-bearing, non-running completion card plus one native image with no uncertain-delivery warning; a normal tool turn retained two answer segments in one card, with zero bot native marker duplicates.
- Final sidecar metrics were `events_received/events_applied=23/23`, 1 send success and 16 update successes; event/auth rejection, send/update failures, notice uncertain warnings, and notice update failures were all zero. Gateway Feishu WebSocket was connected, and Hermes venv site-packages was 4.0.21.
- Final local release gate: full pytest reported `1526 passed, 4 skipped in 53.56s`; `uv build` produced `hermes_feishu_streaming_card-4.0.21.tar.gz` and `hermes_feishu_streaming_card-4.0.21-py3-none-any.whl`. A clean Python 3.12 venv installed from the wheel with imports in `site-packages`, package/distribution versions both `4.0.21`, `hermes-feishu-card = hermes_feishu_card.cli:main` present, and CLI --help exit 0.
- This acceptance does not claim screenshot or desktop/mobile visual QA and does not replace real fault injection; public tagged installer and Release-asset post-tag verification remain pending.

## V4.0.20 Release Gates

- Existing-card notices return `accepted` only when `applied=true` and the asynchronous PATCH is queued; the hook treats that explicit acknowledgement as handled: **passed hook/server regressions**.
- Initial independent notice create/reply keeps the three delivery outcomes; queued work is not represented as delivered, and the request does not wait for every PATCH: **passed existing delivery regressions**.
- After PATCH retry exhaustion, `notice_update_failures` increments once and `last_update_error` retains only the exception type plus validated `status_code` / `api_code`: **passed fault-injection and redaction assertions**.
- Final full automation: **passed (`1517 passed, 4 skipped`)**; sdist/wheel, isolated `site-packages` import of `4.0.20`, the public tagged installer, and Release assets are rechecked during release.

## V4.0.19 Release Gates

- Hermes venv Python omits `--user` by default while the system-Python fallback keeps user installs: **passed installer regression**.
- Pip failures preserve their real exit status and prevent setup from running: **passed red/green regression**.
- A fresh Hermes venv without `HFC_PIP_USER` completed installation and imported the target version from venv `site-packages`: **passed real install smoke**.
- Final full automation, sdist/wheel, public tagged installer, and Release assets are rechecked during release.

## V4.0.18 Release Gates

- When the Hermes adapter uses `extra_ua_tags`, the installer checks the real SDK constructor; older adapters do not trigger installation and compatible newer SDKs are not forced backward: **passed CLI/diagnostics regressions**.
- `doctor` reports `feishu_sdk_incompatible` read-only; `setup/install` must install `lark-oapi==1.6.8` and pass the follow-up capability check: **passed red/green integration coverage**.
- A real Hermes v0.19.0 Gateway recovered `✓ feishu connected` after moving from `lark-oapi 1.5.3` to `1.6.8`; all 214 runtime packages are dependency-compatible: **passed**.
- Final full automation: **passed (`1511 passed, 4 skipped`)**; sdist/wheel, isolated `site-packages` import of `4.0.18`, the public tagged installer, and Release assets are rechecked during release.

## V4.0.17 Release Gates

- Two parallel same-name tools use distinct `call_id` values and preserve independent previews and completion events: **passed session/patcher regressions**.
- Started/completed counts once per invocation, all duration metadata is removed from details, and one duration remains on each headline: **passed session/renderer regressions**.
- Patch compilation, idempotency, and exact restore against the current local Hermes original Gateway source: **passed**; the compatibility fallback without stable callback anchors is unchanged.
- Final full automation: **passed (`1508 passed, 4 skipped`)**; sdist/wheel, isolated `site-packages` import of `4.0.17`, public tagged installer, and local runtime provenance are rechecked during release.

## V4.0.16 Release Gates

- Initial Header/body responsibilities, removal of the empty body placeholder after tool start, and unchanged final-answer/footer behavior: **passed renderer/session/server regression coverage**.
- Hermes `kwargs.duration` extraction, `duration_ms` propagation, started/completed fallback, terminal-only non-fabrication, and query/argument preservation: **passed real callback-shape smoke plus automation**.
- Final full automation: **passed (`1504 passed, 4 skipped`)**; sdist/wheel, isolated `site-packages` import of `4.0.16`, the public tagged installer, and local runtime provenance are rechecked during release.
- This patch does not claim a new Feishu client visual retest. V4.0.15 covered the real Hermes/Feishu loading and tool-state path; this delta is verified through the real callback shape and card JSON smoke.

## V4.0.15 Release Gates

- Issue #141's compact tool timeline, loading/running spinner, same-card PATCH path, stop conditions, terminal drain, and topic/reply anchors: **passed focused automation and real Hermes/Feishu model validation**.
- Read-only detection after a Hermes upgrade, `start` refusal, explicit recovery, installed state after recovery, and fail-closed user edits: **passed a temporary-fixture upgrade loop plus the local real-upgrade diagnosis**.
- Final full automation: **passed (`1498 passed, 4 skipped`)**; sdist/wheel, isolated `site-packages` import of `4.0.15`, and CLI smoke: **passed**; `git diff --check` is rerun before tagging.

## V4.0.14 Release Gates

- Non-terminal heartbeat state, same-anchor reuse, different-anchor isolation, orphaned six/nine-minute updates, and final completion: **passed focused automation**.
- Stable independent-card recovery after an unknown delivery outcome and the existing fail-open branches: **passed regression coverage**.
- The real-Feishu `v4.0.13` reproduction for Issue #142 is recorded. This candidate does not wait another real six/nine minutes and does not describe the equivalent automated replay as a client visual retest.
- Final full automation: **passed (`1488 passed, 3 skipped`)**; sdist/wheel, isolated Python 3.12 `site-packages` import of `4.0.14`, and CLI smoke: **passed**; `git diff --check` is rerun before tagging.

## V4.0.13 Release Gates

- Generic command contexts, same-card multi-feedback, concurrent single-create behavior, long Markdown, exact create/PATCH fallback, and all `/compress` branches: **passed**.
- Dedicated `/model`, bare `/resume`, confirmation, `/hfc`, Agent-turn, media, and `/update` restart-boundary regressions: **passed**.
- Real Feishu client command matrix and final desktop/mobile visual acceptance: **not run and not claimed as passed**.
- Final full automation: **passed (`1482 passed, 4 skipped`)**; `git diff --check`, sdist/wheel, and isolated Python 3.12 import/CLI smoke are verified before tagging.

## V4.0.12 Release Gates

- Focused compaction hook/session/render/server and text-size schema/merge/render/device matrices: **passed**.
- A real selected-env subprocess starts as `healthy/live`; a credential-free subprocess starts as `degraded/noop`, returns `not_sent`, and does not increase successes: **passed**.
- Automatic long-session compaction smoke and final desktop/mobile visual confirmation: **not run by release decision and not claimed as passed**.
- Final full automation: **passed (`1460 passed, 4 skipped`)**; `git diff --check`, sdist/wheel, and clean Python 3.12 import `4.0.12` also passed.
- Annotated tag `v4.0.12` points to merge commit `00a48a7`; release-assets workflow `29632908140` succeeded, and all four assets/checksums plus the public tagged installer: **passed**.

## Current Boundaries

Automated tests do not access real Feishu and do not start a real Hermes Gateway. Real integration remains a local/manual acceptance flow. After successful testing, record only redacted results; never commit credentials, real chat_id, or sensitive screenshots.
