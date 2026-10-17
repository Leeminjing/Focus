# Unified diagnostics

Run `focus doctor` while Focus is running to inspect its Gateway. The command
discovers the desktop's dynamic port from `~/.focus/diagnostics-endpoint.json`.
The file contains a loopback URL and process ID, with no session secret; normal
shutdown removes it. A stale or invalid endpoint falls back to offline inspection.

```sh
focus doctor
focus doctor --json
focus doctor --output focus-diagnostics.json
focus doctor --url http://127.0.0.1:8765 --timeout 5
```

From a source checkout, use the Python environment installed for Focus, from the
repository root:

```sh
python -m backend.app.doctor --url http://127.0.0.1:8765 --json
```

Exit codes: **0** means the collected checks passed, **1** means degraded health
or an unavailable Gateway, **2** means invalid arguments or a failed report export.
`--output` saves the same complete JSON document as `--json`, including when
checks fail. Relative export paths use the caller's working directory; existing
files are overwritten. `--timeout` bounds each Gateway or
database probe; offline fallback can require a second probe. URLs must be HTTP
loopback addresses with an explicit port. HTTP redirects and environment proxies
are disabled.

`GET /health` returns the same versioned report (`report_version: 1`), with:

| Field | Meaning |
| --- | --- |
| `status`, `issues` | `ok` or `degraded`, with stable issue codes. |
| `version` | Application version, exact release tag when available, Git commit and Python version. |
| `startup` | Last recorded startup's stage statuses and durations; unavailable if no log exists. |
| `runtime` | Supervisor/component state, consecutive failures, error types and current in-memory Run status. |
| `database.schema` | Actual Alembic heads compared with this checkout's expected heads. |
| `database.loops`, `database.runs` | Durable status counts and bounded samples with IDs for correlation. Live RunManager state is under `runtime.runs`. |
| `database.queues` | Counts by state, outstanding item counts and oldest outstanding timestamps for dispatch, events, workers, publication, context runs and task progress. |
| `database.budgets`, `database.events` | Usage for the sampled Loops and recent journal event identities, linked by Loop/Run/entity IDs. |
| `database.errors` | Unresolved projection failures plus curator/task-progress error counts. |

Rows are sampled at 20 per section; components at 200, with total and unhealthy
counts computed before truncation. Queue counts are not truncated. Backlog alone
does not cause degraded health. Schema mismatch, database probe failure, unhealthy
supervised components and persisted component/delivery errors do. HTTP remains
**200** when the Gateway can answer, so the desktop startup liveness probe remains
compatible; diagnostic consumers must inspect the JSON status. Release acceptance
checks database compatibility independently of existing user-work errors.

When Focus is stopped, doctor loads the configured database and executes reads
without opening the application lifespan, launching services, running migrations,
claiming queue items or calling models. Runtime state is marked unavailable.
Schema mismatch skips all application table queries so an incomplete upgrade can
still be diagnosed. `FOCUS_GLOBAL_HOME` and `FOCUS_DATABASE_URL` retain their
normal meanings.

Exports omit credentials, configuration documents, workspace paths, conversation
content, tool/event payloads and arbitrary error messages. They contain operational
identifiers and counts. Error types and unresolved failure IDs support looking up
the detailed local logs without copying their contents into a support report.
