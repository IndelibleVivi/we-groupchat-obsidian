# Historical exact relation Markdown repair

This is a retained operator procedure for one historical relation-rendering
incident, not an installation step or a general Obsidian cleanup command.
[Ordinary knowledge audit and output maintenance](../README.md#monitor-digest-and-obsidian-workflow)
remain in the README.

## Applicability

The CLI [scripts/repair_relation_markdown.py](../scripts/repair_relation_markdown.py)
uses the fixed `LIVE_EXPECTATIONS` profile in
[core/relation_markdown_cleanup.py](../core/relation_markdown_cleanup.py).
That profile binds a verified pre-repair backup digest and exact historical
edge counts. An arbitrary backup with the same filename is not admissible;
a mismatch fails with `backup_checksum` or the corresponding evidence error.
The command has no profile-override option. Do not change the constants to make
a different incident pass.

The original private backup is not distributed. Its old machine-specific path
is deliberately absent here. Continue only when the operator already holds the
matching provenance backup, has reviewed the exact current database and vault,
and has authorized that repair. Otherwise this procedure does not apply.

## Preview, inspect, apply or roll back

All paths are explicit; there is no implicit config lookup. Stop the monitor
and keep every external vault writer idle during the live repair window.
The backup must be a regular private file with mode `0600` and match the
canonical profile. The run directory must be new under an existing private
parent. Replace the placeholders with reviewed local inputs:

```bash
.venv/bin/python scripts/repair_relation_markdown.py preview \
  --backup "<matching-private-provenance-backup.db>" \
  --db "<stopped-current-monitor_knowledge.db>" \
  --vault-root "<monitored-vault-root>" \
  --obsidian-subdir "<monitored-subdirectory>" \
  --run-dir "<new-private-run-directory>" \
  --generator-commit "<reviewed-generator-commit>" \
  --json

.venv/bin/python scripts/repair_relation_markdown.py status \
  --run-dir "<private-run-directory>" \
  --json

.venv/bin/python scripts/repair_relation_markdown.py apply \
  --run-dir "<private-run-directory>" \
  --manifest-sha256 "<full-manifest-sha256>" \
  --confirm "APPLY_EXACT_RELATION_MARKDOWN:<full-manifest-sha256>" \
  --json

.venv/bin/python scripts/repair_relation_markdown.py rollback \
  --run-dir "<private-run-directory>" \
  --manifest-sha256 "<full-manifest-sha256>" \
  --confirm "ROLLBACK_EXACT_RELATION_MARKDOWN:<full-manifest-sha256>" \
  --json
```

`preview` writes a private sealed run artifact; its manifest contains local
paths and titles. Default CLI output is redacted. `--sensitive` exposes only
bounded path/title examples (five by default, maximum 20), never note bodies,
reasons or rendered relation lines.

`apply` changes only proven exact Markdown lines. It does not repair SQLite,
re-export notes or invoke an external vault writer. Do not broaden it into a
general `updates::` search-and-delete. Keep the sealed run artifact for status
inspection and the manifest-bound rollback path; a failed or interrupted run
is not permission to create a new plan over unknown state.

The separate `scripts/repair_relations.py` command operates on canonical
relation rows and has its own audit/count/backup/confirmation contract in the
README. It is not interchangeable with this Markdown-only procedure.
