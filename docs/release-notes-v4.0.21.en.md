# V4.0.21 Release Notes

Release date: 2026-07-28

V4.0.21 is a content-integrity hotfix for Issue #155 and locks the combined image/`system.notice` regression for Issue #147. It does not change the card UI or configuration.

## Issue #155: explicit answer/tool archive boundary

- Only an explicit `answer -> tool` boundary may archive that answer segment into the reasoning-and-tools timeline.
- For `tool -> answer -> completed`, an earlier tool is not archive evidence: the completed card retains the whole user-visible answer instead of stripping its streamed prefix merely because a tool appeared earlier in the turn.
- Multiple `answer -> tool` segments still archive in their own explicit later-tool order, while the terminal answer remains visible.

## Issue #147: image and notice combined regression

- The automated regression covers an accepted completed event: the card carries visible text once, matching native media text is suppressed once, and the native image continues through Hermes' native media sender.
- In the same turn, an accepted queued `system.notice` produces no uncertain-delivery warning; unrelated later native text is not suppressed.
- Existing `MEDIA:` cleanup, attachment summaries, and `native_delivery=required` behavior remain unchanged.

## Verification boundary

- Task 1 focused answer-ordering coverage passed `74 passed`; Task 2 image/notice combination coverage passed `277 passed` and exposed no new root cause requiring a production runtime change.
- The real Feishu image smoke remains pending. Before release, verify one completed card plus one native image with no gray duplicate answer and no uncertain-delivery warning; automated regression is not client visual acceptance.
- These notes include no real chat/message/user identifiers, credentials, tokens, or local paths.

## Upgrade

```bash
export HFC_VERSION=v4.0.21
curl -fsSL https://raw.githubusercontent.com/baileyh8/hermes-feishu-streaming-card/main/install.sh | bash
```
