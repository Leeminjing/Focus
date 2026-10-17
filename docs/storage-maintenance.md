# Storage maintenance

Open **Settings → Data → Storage maintenance** to see PostgreSQL database size,
Journal size/event count, backup disk usage, and backup inventory. Journal size
is part of the database total. Deleting rows makes space reusable; PostgreSQL
does not necessarily shrink its files. The maintenance path does not run
`VACUUM FULL` or delete user workspace files, conversations, Context revisions,
facts, checkpoints, or source evidence.

The policy lives at `~/.focus/storage-policy.json` (or `FOCUS_GLOBAL_HOME`). The
defaults are seven days for Journal events, thirty days for backups, and at least
three backups **of each type**. Automatic cleanup and periodic backup start
disabled. Enable them explicitly in the data settings. Backup interval `0`
disables periodic backup. The Gateway checks once at startup and hourly while
Focus is running; this is not an OS scheduler. Missed intervals result in one
backup on the next check. Upgrade acceptance (`FOCUS_UPDATE_CHECK=1`) starts no
maintenance worker. Failed checks are logged and shown in the panel, then retried
on the next hourly check.

Save the policy, then choose **Preview cleanup**. A preview expires after ten
minutes and can be executed once. Changing the policy invalidates it. Execution
only removes entries included in that preview, and checks current protections
again, so the actual count can be smaller. Each Loop transaction removes at most
5,000 events, advances its replay boundary monotonically, and saves a reducer
checkpoint first. Cleanup keeps a contiguous suffix; one early protected event
can retain later events too. Larger backlogs drain over subsequent checks.
Loops commit independently; failures roll back the affected Loop and the panel
reports completed counts alongside errors. Re-preview to retry remaining work.

Journal protections include:

- Explicit `retained_until` pins and events younger than the policy.
- Every registered projector's unread suffix; no fact cursor means no deletion.
- Unresolved retryable/quarantined event projections.
- Fact provenance, nested fact evidence, observation/input references, visible
  sequence boundaries, and journal causation references. Source records themselves
  are retained.

Backup protections include:

- A pending update/recovery journal blocks deletion of all backup points.
- `install.json`'s current accepted recovery point is always retained.
- The most recent configured number of each backup type, regardless of age.
- All backups inside the retention period, regardless of count.
- Unknown, incomplete, malformed, linked or unusual entries. These are displayed
  for inspection and never automatically removed.

Backup creation and removal share the updater's transaction lock and re-read
recovery metadata while holding it. Backup cleanup only touches recognised direct
children of the fixed backup directory and refuses linked roots or contents.

## Regular database backups

**Back up database now** creates a custom-format PostgreSQL archive under
`~/.focus/backups/regular-<timestamp>-<random>/database.dump`. The command uses the
same full `pg_dump`, archive listing verification and SHA-256 implementation as
the upgrade recovery path. `backup.json` is published only after those checks
succeed; interrupted attempts remain protected. Regular backups contain the
database, including stored checkpoints and history. They omit Python/npm
dependency snapshots and external workspace files. Upgrade recovery points keep
their existing complete database/dependency snapshots.

The built-in entry point supports only the bundled local PostgreSQL and requires
Node and Docker. Custom database configurations show an unavailable message and
must use their own database backup tooling. A scheduled backup uses the same
checks. Keep an external copy if protection from failure of the local disk is
required.

For a regular archive restore, stop Focus, verify the archive's SHA-256 against
`backup.json`, and use PostgreSQL `pg_restore` with the matching application/schema
version. A regular backup is a database archive, not an upgrade rollback pair;
use the updater's recovery journal for interrupted upgrades. See
[managed releases](managed-releases.md) for upgrade rollback.
