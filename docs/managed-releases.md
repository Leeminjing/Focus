# Stable releases and recovery

Public installation and `focus update` use GitHub's latest **published, non-prerelease** release with a `vX.Y.Z` tag. They fetch that exact tag, resolve it to a commit, and record the commit only after acceptance. An unavailable release API, a missing tag, or no eligible release is an error; there is no fallback to `main`. Keep release tags immutable and protect them in the repository settings. Publishing a stable Release is the maintainer's acceptance decision.

The README bootstrap URLs download the `install.ps1` / `install.sh` assets of that stable Release. Upload those files from the accepted commit before publishing. A tag alone does not make a release available to installers.

## Release procedure

1. Update `backend/packages/harness/uv.lock` when Python dependencies change. Export the desktop requirements from that lock and commit the result:

   ```sh
   uv export --frozen --project backend/packages/harness --extra desktop --no-emit-project --no-dev --no-hashes --no-annotate --output-file scripts/desktop-requirements.txt
   ```

2. Run the release transaction tests and the disposable PostgreSQL drill. On Windows:

   ```powershell
   node --test desktop/managed-update.test.cjs desktop/managed-distribution.test.cjs desktop/update-lock.test.cjs desktop/startup-ui.test.cjs
   $env:FOCUS_UPDATE_POSTGRES_TEST = '1'
   node --test desktop/managed-update-postgres.test.cjs
   ```

   On macOS use `FOCUS_UPDATE_POSTGRES_TEST=1 node --test desktop/managed-update-postgres.test.cjs`. The drill requires Docker and an installed desktop Python environment (`FOCUS_TEST_PYTHON` can select it). It creates a uniquely named disposable PostgreSQL container, applies real migrations, checks the real Gateway, and restores Context, Revision and run rows after destructive schema changes. It never uses the desktop database.

3. On both Windows and macOS, accept a clean install and an upgrade from the previous stable release in a disposable OS account. Keep a Context, an immutable Revision, and run history in the old installation and verify them after success and injected migration/health failures. Include a forced termination after migration, followed by `focus update` to recover. Confirm that a direct Electron launch is refused while updating.
4. Create the `vX.Y.Z` tag on the accepted commit and a draft GitHub Release. Attach that commit's `install.ps1` and `install.sh`. Publish as the latest stable release only after the OS acceptance checks pass. Do not move an existing tag or publish a prerelease as stable.

## Update transaction

Close Focus first and keep Docker Desktop running. The Windows CLI holds exclusive update and running locks; macOS start and update share one atomic lock. Direct Electron processes are also checked. The command entry point lives in `~/.focus/runtime/cli`, outside the checkout, so it survives a code rollback. Existing `main` installations must rerun the new installer once to install that protected entry point; this bootstrap clones the stable release separately and does not reset existing application code before backup.

The first public workflow manages only the default local PostgreSQL in `desktop/compose.yaml`. Alternate `FOCUS_DATABASE_URL` / `FOCUS_DB_*` settings are refused before any database or application mutation. Custom database deployments need their own backup and restore procedure.

Each update (including repairing the same release) performs these steps:

1. Resolve the stable release and fetch its exact commit.
2. Start the existing local database. Copy the current Python environment and `node_modules` to a private recovery directory, and create a full custom-format `pg_dump`. Verify that PostgreSQL can read the archive and record its SHA-256.
3. Write a recovery journal at `~/.focus/update-state.json`, containing old/new commits and the previous install metadata. Save a standalone `recover.cjs` and `recovery.json` with the recovery point.
4. Replace code with the resolved release commit, use `npm ci`, install the explicit Electron binary, and install the exported, pinned Python dependencies.
5. Apply Alembic migrations immediately. Compare actual migration heads and read every mapped column, then open the real Gateway lifespan and check `/health`, the authenticated task list and the desktop page. During this check user work, recovery writers and background dispatchers stay stopped; no model requests are sent.
6. Record the accepted release, exact commit and recovery path in `install.json`; remove the pending journal and report success. Managed startup checks acceptance and schema compatibility without applying migrations. Developer clones retain their existing migration-on-start behavior.

Recovery points include full database content, including checkpoints/store, Contexts, immutable Revisions and run history. They also contain complete dependency snapshots, so rollback does not need npm/PyPI downloads. They consume disk space proportional to the database plus installed dependencies. Backups are retained by default; **Settings → Data → Storage maintenance** provides retention policy, cleanup preview, and optional periodic cleanup and regular database backup. The active recovery journal and current accepted recovery point are protected. See [storage maintenance](storage-maintenance.md).

## Failure and recovery

Dependency, migration, schema, Gateway or acceptance-record failures all restore the previous commit and **recreate the entire previous database** from the checked archive. Recreating the database also removes tables added by the failed migration. The old dependency directories are restored byte for byte to their original locations, and a previously accepted version must pass its health check before the journal is removed. A restored legacy `main` installation is recorded as a recovered baseline, not a stable release.

If recovery fails or the update is terminated, the journal remains and the protected launcher blocks startup. Close any remaining Focus processes, resolve the reported cause (for example, start Docker or free disk space), and run:

```sh
focus update
```

While a journal exists, this command first retries restoring the saved old pair without resolving or fetching a release, downloading packages, or pulling an image. A successful recovery ends that command; run `focus update` again to attempt the stable release. Never fix a failed migration by resetting Git alone or deleting the journal.

If the command files themselves are damaged, the recovery point contains a standalone entry point. With all Focus processes stopped and no concurrent updater, run `node <recovery-point>/recover.cjs update <focus-home> <focus-home>/app <managed-python>`; use `runtime/venv/Scripts/python.exe` on Windows and `runtime/venv/bin/python` on macOS. It reads the existing pending journal and retries recovery. Retain the recovery point and its journal if this also fails; it is the complete record required for repair.

This workflow protects the stopped application's PostgreSQL state. It does not take OS snapshots of external workspaces, and public release acceptance must not include migrations that write to user workspace files.
