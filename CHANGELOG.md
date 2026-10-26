# 2.0.2 — 2026-10-26

- Recognize active/upcoming pre-recorded Premieres from processed upload status and fixed non-zero duration; never reinterpret ended archives from those fields alone.
- Track recognized Premieres durably across restarts. Suppress scheduled/live/ended stream messages for those videos and enqueue the existing "Новое видео" event after an actual transition out of upcoming.
- Refresh known, unpublished Premieres every 30 seconds near their scheduled start, using video IDs only. Normal discovery, stream notifications, and ordinary upload checks keep their existing 30-minute interval.
- Preserve confirmed history, baseline and delivery ambiguity protections. No messages are replayed from historical Premieres.

Timing depends on YouTube API status updates, the polling interval, and Telegram availability. New Premieres must first be discovered by the normal channel check. YouTube Data API has no explicit Premiere field; unprocessed/missing-duration metadata cannot be positively classified until available.

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
