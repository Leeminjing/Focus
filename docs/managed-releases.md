# Update channels and recovery

Focus supports two managed channels. `dev` fetches `refs/heads/main` without querying Releases. `stable` uses GitHub's latest **published, non-prerelease** Release with a `vX.Y.Z` tag. Both pin the fetched commit and accept it only after the same protected transaction. An unavailable Release API, a missing tag, or no eligible Release is an error for stable; it never falls back to main. Keep release tags immutable and protected.

| Command | Behavior |
| --- | --- |
| `focus update` | Follow the installed channel |
| `focus update --dev` | Switch to latest main after acceptance |
| `focus update --stable` | Switch to latest formal Release after acceptance |

Legacy `branch: main` metadata means dev. Unknown channels and conflicting flags are rejected. A failed switch retains the original channel. Fresh installer default is stable; existing installs retain their channel unless explicitly overridden. PowerShell accepts `-Dev` / `-Stable`, macOS accepts `--dev` / `--stable`, and both accept `FOCUS_CHANNEL=dev|stable`. Command-line flags override the environment.

The README provides a raw-main bootstrap for dev, which works before any Release exists, and Release asset URLs for stable. Upload both installers from the accepted commit before publishing. A tag alone is insufficient. When a selected old Release lacks the dual-channel CLI protocol, the current installer stages a compatible controller from main separately; application code still targets the selected Release and passes the complete transaction.

## Release procedure

1. Update `backend/packages/harness/uv.lock` when Python dependencies change. Export the desktop requirements from that lock and commit the result:

   ```sh
   uv export --frozen --project backend/packages/harness --extra desktop --no-emit-project --no-dev --no-hashes --no-annotate --output-file scripts/desktop-requirements.txt
   ```

2. Run the release transaction tests and the disposable PostgreSQL drill. On Windows:

   ```powershell
   node --test desktop/tests/integration/managed-update.test.cjs desktop/tests/integration/managed-distribution.test.cjs desktop/tests/integration/update-lock.test.cjs desktop/tests/integration/startup-ui.test.cjs
   $env:FOCUS_UPDATE_POSTGRES_TEST = '1'
   node --test desktop/tests/integration/managed-update-postgres.test.cjs
   ```

   On macOS use `FOCUS_UPDATE_POSTGRES_TEST=1 node --test desktop/tests/integration/managed-update-postgres.test.cjs`. The drill requires Docker and an installed desktop Python environment (`FOCUS_TEST_PYTHON` can select it). It creates a uniquely named disposable PostgreSQL container, applies real migrations, checks the real Gateway, and restores Context, Revision and run rows after destructive schema changes. It never uses the desktop database.

3. On both Windows and macOS, accept a clean install and an upgrade from the previous stable release in a disposable OS account. Keep a Context, an immutable Revision, and run history in the old installation and verify them after success and injected migration/health failures. Include a forced termination after migration, followed by `focus update` to recover. Confirm that a direct Electron launch is refused while updating.
4. Create the `vX.Y.Z` tag on the accepted commit and a draft GitHub Release. Attach that commit's `install.ps1` and `install.sh`. Publish as the latest stable release only after the OS acceptance checks pass. Do not move an existing tag or publish a prerelease as stable.

## Update transaction

Close Focus first and keep Docker Desktop running. The Windows CLI holds exclusive update and running locks; macOS start and update share one atomic lock. Direct Electron processes are also checked. The command entry point lives in `~/.focus/runtime/cli`, outside the checkout. All older installations, including those with a stable-only external CLI, must rerun the current development installer from the README once. It runs a staged controller without replacing the old CLI or application code before backup. On success it replaces the bin launcher with a dispatcher selecting complete immutable CLI generations through `current.json`. Merely pulling Git cannot refresh the old external CLI. If an interrupted transaction is recovered during bootstrap, rerun the installer to finish acceptance.

The first public workflow manages only the default local PostgreSQL in `desktop/compose.yaml`. Alternate `FOCUS_DATABASE_URL` / `FOCUS_DB_*` settings are refused before any database or application mutation. Custom database deployments need their own backup and restore procedure.

Updates refuse tracked local edits before replacing code, so repository-local configuration changes cannot be silently discarded. Preserve those edits first. Existing global config, `.env`, extensions and user directories are retained.

Each update (including repairing the same release) performs these steps:

1. Resolve the installed or explicitly selected channel and fetch its exact main/tag commit.
2. Start the existing local database. Copy the current Python environment and `node_modules` to a private recovery directory, and create a full custom-format `pg_dump`. Verify that PostgreSQL can read the archive and record its SHA-256.
3. Write a recovery journal at `~/.focus/update-state.json`, containing old/new commits, the previous install metadata and CLI generation pointer. Save a standalone `recover.cjs`, compatible CLI bundle and `recovery.json` with the recovery point.
4. Replace code with the resolved commit, use `npm ci`, install the explicit Electron binary, and install the exported, pinned Python dependencies.
5. Apply Alembic migrations immediately. Compare actual migration heads and read every mapped column, then open the real Gateway lifespan and check `/health`, the authenticated task list and the desktop page. During this check user work, recovery writers and background dispatchers stay stopped; no model requests are sent.
6. Copy a complete compatible CLI into a new immutable generation, then atomically publish its pointer. Record the accepted channel, Release when stable, exact commit and recovery path in `install.json`; remove the pending journal and report success. Pre-protocol target versions retain the modern transaction controller, so switching to an old Release cannot downgrade the CLI to stable-only behavior. Managed startup checks acceptance and schema compatibility without applying migrations. Developer clones retain their existing migration-on-start behavior.

Recovery points include full database content, including checkpoints/store, Contexts, immutable Revisions and run history. They also contain complete dependency snapshots, so rollback does not need npm/PyPI downloads. They consume disk space proportional to the database plus installed dependencies. Backups are retained by default; **Settings → Data → Storage maintenance** provides retention policy, cleanup preview, and optional periodic cleanup and regular database backup. The active recovery journal and current accepted recovery point are protected. See [storage maintenance](storage-maintenance.md).

## Failure and recovery

Dependency, migration, schema, Gateway, CLI publication or acceptance-record failures restore the previous commit and **recreate the entire previous database** from the checked archive. Recreating the database also removes tables added by the failed migration. The old dependency directories and CLI pointer are restored; a previously accepted version must pass its health check before the journal is removed. A restored legacy main installation is recorded as a recovered baseline. Generation directories remain available for recovery; pruning them is deferred.

If recovery fails or the update is terminated, the journal remains and the protected launcher blocks startup. Close any remaining Focus processes, resolve the reported cause (for example, start Docker or free disk space), and run:

```sh
focus update
```

While a journal exists, this command first restores the old installation without resolving or fetching a target, downloading packages, or pulling an image. Recovery takes priority even with a channel switch flag. A successful recovery ends that command; run the desired update command again to retry. Old journals without CLI snapshots remain recoverable. Never repair a failed migration by resetting Git alone or deleting the journal.

If the command files themselves are damaged, the recovery point contains a standalone entry point. With all Focus processes stopped and no concurrent updater, run `node <recovery-point>/recover.cjs update <focus-home> <focus-home>/app <managed-python>`; use `runtime/venv/Scripts/python.exe` on Windows and `runtime/venv/bin/python` on macOS. It reads the existing pending journal and retries recovery. Retain the recovery point and its journal if this also fails; it is the complete record required for repair.

This workflow protects the stopped application's PostgreSQL state. It does not take OS snapshots of external workspaces, and public release acceptance must not include migrations that write to user workspace files.
