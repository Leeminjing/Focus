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

## Fact projection repair

In the workspace Patrol workbench, open **查看工作详情 → 工作控制 → 事实投影诊断**.
Expand a persisted failure to inspect its projector, source identity, error class,
failure count, event boundary and timestamps. After correcting its source or
implementation, choose **修复后重新生成**. Only quarantined `loop_facts`
`journal_event` and `run_backfill` units offer this action. Retryable units remain
under normal recovery; other projectors have no repair action here. The panel
supports pagination, refresh and including resolved records.

This operation regenerates materialized facts from existing sources. It does not
launch or retry a business Run, call a model, or grant permissions. Journal repair
holds the fact cursor lock and restores subsequent consumed events in order,
without moving the cursor backward or retrying other unresolved failures. This
keeps newer fact presentations current. Successful materialization and resolution
commit together. Another failure rolls back materialization and records a new
quarantined attempt, including for backfill or a missing source. A repeated request
for an already resolved failure returns its state without replaying again.
Source ordering uses canonical journal sequence, including for a backfilled Run
with a settlement event; legacy sources without journal entries use the Run's
settlement/creation time and stable ID. Transaction timestamps alone do not decide
which canonical observation is current. A savepoint rolls back failed materialization
while retaining the outer source/failure locks through attempt accounting.

Session-protected local API:

- `GET /desktop/api/agent-loops/{loop_id}/projection-failures`: unresolved failure
  metadata by default; `include_resolved=true`, `limit=1..100` and `before` support
  bounded inspection. Responses contain `items`, `has_more` and `next_before`.
- `POST /desktop/api/agent-loops/{loop_id}/projection-failures/{failure_id}/repair`:
  returns `processed` and the persisted `failure`. Check `failure.status`:
  HTTP 200 confirms an attempted operation, and only `resolved` confirms repair.
  Unknown or cross-Loop IDs return 404, unsupported states return 409, and an
  unavailable projector returns 503.

The workbench reloads its Live snapshot after an attempted repair so facts and
recovery diagnostics update even without a new business event. Diagnostic responses
and doctor exports omit arbitrary exception messages; detailed error text remains
in the local failure record.
