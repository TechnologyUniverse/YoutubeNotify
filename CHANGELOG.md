# 2.0.1 — 2026-10-14

- Persist an outbox before delivery and commit processed flags only after Telegram confirms a message.
- Preserve queued events across restart, API errors, rate limits and per-run delivery limits.
- Quarantine ambiguous network deliveries and interrupted sends without automatic duplicates.
- Keep rejected jobs visible for operator review; pause delivery without discarding events in SILENT_MODE.
- Migrate schema 2 to 3 while preserving historical flags and baseline. Legacy flags cannot prove historical delivery; old events are not replayed automatically.
- Stop processing when state is corrupt or cannot be saved.
- Report monitoring freshness, queue counts and last errors in status/health.
- Register a Telegram error handler without exposing tokens.

Exactly-once delivery cannot be guaranteed by Telegram send APIs. Review uncertain jobs against Telegram history before retrying. This release deliberately has no automatic retry for uncertain or rejected jobs.
