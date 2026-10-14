# VPS deployment — 2026-10-14

Version 2.0.1 deployed to the existing Contabo youtubenotify service with owner approval. Other containers were not changed.

- 13 local automated tests passed, including 10 delivery/state tests.
- Docker image youtubenotify:2.0.1 built on VPS.
- Existing Telegram token, YouTube API key and channel mappings retained.
- Old instance stopped before starting the new one.
- Original compose and state backup retained under /opt/novaryn/apps/youtubenotify.
- State migrated from schema 2 to 3; 38 video records and historical event flags retained.
- First live monitoring pass succeeded; loopback health reported version 2.0.1, status ok and zero queued/uncertain/failed jobs.
- No test messages or historical notifications were sent during deployment.

A rollback must consider schema 3 outbox/history: stop the new bot and preserve current data before reverting. Restoring an old snapshot after live deliveries can repeat events. Do not restore blindly.
