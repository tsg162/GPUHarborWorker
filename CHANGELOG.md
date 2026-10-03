# 0.2.0

- Exclusive persisted GPU leases, idempotent submission, and recovery of jobs that
  complete during worker downtime.
- Low-disk validation never deletes work. Cleanup previews by default; verified
  backups and pinning protect terminal workspaces.
- Complete checkpoint-directory manifests, whole-set retention, canonical artifact
  paths, deduplication and checksum reuse.
- Resumable 8 MiB uploads, ranged downloads, project archives, explicit training
  interpreters, hashed dependency environments, and shared caches.
- Verified mounted/rclone backups and CLI resume from a worker or offline snapshot.
- Independent SSE log cursors and off-thread file/GPU operations.
- Fixed tunnel origin ports, supervised worker/tunnel lifecycle, pinned installer
  dependencies, authenticated readiness, and reusable Vast image/template recipes.
- Dashboard authentication, loopback binding, credential redaction, and locked deps.

See `vast/README.md` for deployment and `examples/train.py` for checkpoint integration.
