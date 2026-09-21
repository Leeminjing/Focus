# Context7 implementation preflight

Consulted on 2026-09-20 after explicit apply approval and before any application source edit.

## PostgreSQL ownership and locking

Source: PostgreSQL current documentation, [Partial Indexes](https://www.postgresql.org/docs/current/indexes-partial.html) and row-locking examples returned by Context7 from the current PostgreSQL documentation set (`/websites/postgresql_current`).

- A partial unique index enforces uniqueness only for rows satisfying its predicate. The existing `managed_context_id IS NOT NULL AND lifecycle <> 'retired'` index is therefore the correct final one-live-publisher invariant and must remain.
- Domain reconciliation must change a terminal predecessor Lane to `retired` before inserting the successor Lane in the same transaction; weakening the index would remove the concurrency guarantee.
- `SELECT ... FOR UPDATE` protects the selected ownership row from concurrent modification. The successor transaction should first lock the stable Context row, then read/lock current Lane ownership using one documented lock order.
- Legacy repair should be a predicate-constrained `UPDATE` whose target set is derived from persisted terminal Loop ownership, making repeated runs idempotent.

## SQLAlchemy transaction behavior

Source: SQLAlchemy 2.0 documentation, [Transactions and Connection Management](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html), AsyncIO extension, and the session rollback FAQ returned by Context7 (`/websites/sqlalchemy_en_20`).

- ORM work must be framed by an explicit top-level transaction. A flush failure marks the transaction failed; the session cannot continue safely until its transaction is rolled back or the context exits.
- Terminal Loop status, grant revocation, runtime convergence, Lane retirement, and audit append belong to one `AsyncSession.begin()` boundary so any flush/error rolls all of them back.
- `select(...).with_for_update()` is the ORM expression for ownership row locking.
- Nested savepoints are suitable for isolated diagnostic/repair units, but `Session.commit()` targets the outer transaction; production ownership handoff should not depend on partial commits.
- Expected ownership conflicts should be detected before flush and mapped to domain errors. A database `IntegrityError` remains a last-line race signal and must be handled only after transaction rollback, never by continuing to use the failed session.

## Fetch response decoding

Source: MDN Web Docs, [Using the Fetch API](https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch) and [Response](https://developer.mozilla.org/en-US/docs/Web/API/Response), returned by Context7 (`/mdn/content`).

- Fetch resolves normally for HTTP error statuses, so callers must inspect `response.ok`/status before treating the response as success.
- Response bodies are streams consumed asynchronously. The decoder should consume the body once, preferably as bounded text, and then parse the captured text conditionally rather than calling both `json()` and `text()`.
- `Content-Type` should guide JSON expectations, but malformed or incorrectly labelled bodies still need a stable HTTP error fallback.
- A JSON parser exception is transport-decoding context, not the causal server failure; the typed error must retain HTTP status, operation, content type, structured detail when valid, and a bounded plain-text excerpt otherwise.

## Implementation constraints derived from the sources

1. Retain the PostgreSQL partial unique index.
2. Lock by Context identity and use a consistent row-lock order.
3. Reconcile terminal ownership and successor acquisition in one transaction.
4. Treat any flush/constraint failure as transaction-fatal and map it only after rollback.
5. Make legacy repair predicate-bounded and repeatable.
6. Read each Fetch response body exactly once and never assume an error body is JSON.
7. Keep raw error excerpts bounded and exclude request headers/session credentials.
