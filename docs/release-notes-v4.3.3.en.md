# Hermes Feishu Streaming Card V4.3.3

[中文](release-notes-v4.3.3.md) | [English](release-notes-v4.3.3.en.md)

V4.3.3 fixes delivery continuity when Hermes explicitly asks to create a thread from the current Feishu message before a concrete `thread_id` is available. That placement is now bound to the active `CardSession` instead of being inferred again from later event identities.

## Fixed

- When Hermes sends explicit `reply_in_thread=true` with a verified `reply_to_message_id`, the first schema 2.0 streaming card creates the thread through the Feishu reply API. Ordinary, repeated, and runtime-admission clarify/approval cards retain the same anchor and placement.
- The opt-in completion notification reuses the session's `reply_in_thread` intent. Without a concrete `thread_id`, it still sends as a thread reply to that anchor instead of falling back to the top-level chat.
- `FeishuClient.send_text_message()` now matches the card-send boundary: an explicit `reply_in_thread=true` without `reply_to_message_id` fails closed and rejects the send rather than posting top-level text. The default `reply_in_thread=false` behavior remains compatible.

## Safety boundaries

- This path requires both explicit thread intent and a verified `om_` reply anchor; a missing anchor is a rejection, not a best-effort top-level fallback.
- The original schema 2.0 streaming message remains the sole PATCH owner. Legacy interaction cards, callback tokens, chat/operator/profile binding, expiry, idempotency, Hermes patch ownership, and the archived `legacy/` runtime are unchanged.
- This release does not include PR #229's daemon-listener change: the `pytest-macos` required check timed out on the same subprocess test on two consecutive heads, and an author fix is still pending.

## Verification status

- Local regressions cover card/interaction/completion placement when the first reply has no concrete `thread_id`, plus the missing-anchor text-send path with no token lookup or Feishu API request.
- Full pytest, remote CI, exact merge SHA, package builds, public tag/install, Release assets/checksums, and real Feishu/Lark client acceptance are not yet complete for this release candidate. Automation is not represented as real-client evidence.

## Real Feishu acceptance still required

- From a top-level test-group message, trigger first-reply thread creation and confirm that the initial card, ordinary and runtime-admission interactions, and completion notification remain in one thread with no top-level fallback.
- Trigger explicit thread intent without an anchor and confirm that the completion notification posts no top-level text while only a sanitized rejection classification is recorded.
