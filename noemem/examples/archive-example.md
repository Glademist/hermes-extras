---
project: backups
category: ops
importance: 5
retrieval_tier: live
source_session_id: 20260101_101500_ab12cd34
---

# Nightly data backup to the mini PC

**Topic:** Backups
**Why:** User asked for an automated nightly backup of the data directory to the
mini PC, with a log.

**Outcome:** Nightly backup is live. `~/scripts/backup-nightly.sh` runs at 03:00,
uses `restic` over SSH to `donkey:/backups`, and appends each run to
`~/backups/backup.log`. Verify a run with `tail -n 1 ~/backups/backup.log`.

**Lesson:** none — the approach is standard and nothing new was learned.
