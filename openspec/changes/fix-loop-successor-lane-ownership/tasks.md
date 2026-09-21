## 1. Approval, Research, and Red Reproduction

- [x] 1.1 Confirm the user has explicitly approved applying `fix-loop-successor-lane-ownership`, then invoke the available Context7 skill/MCP/tool before any application source edit; record cited guidance on transactional ownership handoff, PostgreSQL partial unique indexes and row/advisory locking, idempotent data repair, and Fetch response decoding in `context7-preflight.md`. If Context7 is unavailable, stop and ask the user.
- [x] 1.2 Convert the rollback-based production reproduction into a focused PostgreSQL regression fixture containing a stopped predecessor, active managed Lane, and newer unbound direct-user Run; verify successor authorization fails specifically on `uq_curation_lane_managed_publisher` before the fix.
- [x] 1.3 Add a renderer regression test whose Loop start response is plain-text `500 Internal Server Error`; verify the current client fails with the reported `Unexpected token 'I'` symptom before the fix.
- [x] 1.4 Document and enforce module dependency boundaries for curation ownership, terminal lifecycle, activation/service orchestration, migration repair, and the pure renderer response decoder; verify architecture tests fail on reverse dependencies or DOM coupling.

## 2. Curation Ownership Domain

- [x] 2.1 Implement a focused ownership result/conflict contract carrying Context, Lane, Program, owner Loop, lifecycle, and release reason; verify unit tests distinguish free, terminal-stale, nonterminal-conflict, and inconsistent states.
- [x] 2.2 Implement the ownership repository's Context/live-owner lock and lookup without creating or terminating Loops; verify PostgreSQL tests return the single non-retired owner and retain the partial unique index.
- [x] 2.3 Implement idempotent terminal release that retires every non-retired managed Lane proven to belong to the terminal Loop's Program; verify repeated release changes each Lane once and preserves all stable identifiers and Portfolio relationships.
- [x] 2.4 Implement successor ownership preparation that reconciles only proven terminal owners and rejects nonterminal or inconsistent owners with a domain conflict; verify stopped/completed/failed and running/paused/waiting/stopping matrices.
- [x] 2.5 Emit stable acquisition, terminal-release, repair, and rejected-conflict audit data through existing canonical Loop events; verify replay remains idempotent and contains Loop, Context, Program, Lane, reason, and time identities.

## 3. Atomic Terminal Lifecycle

- [x] 3.1 Introduce a single-purpose terminal lifecycle coordinator for `completed`, `stopped`, and `failed` transitions, delegating runtime convergence and curation ownership release to their existing focused modules; verify its public methods and protected helpers remain bounded and acyclic.
- [x] 3.2 Route user stop and typed wait-request stop through the terminal lifecycle boundary; verify the Loop status, grant, runtime work, and Lane retirement commit together.
- [x] 3.3 Route Patrol stop, successful completion, hard-budget termination, unrecoverable failure, and restart terminalization through the same boundary; verify a static/behavioral test enumerates all production terminal status writes and rejects bypasses.
- [x] 3.4 Inject failures at runtime convergence and ownership release; verify each failed finalization rolls back Loop status, grant changes, audit events, and Lane lifecycle as one transaction.
- [x] 3.5 Update terminal lifecycle, runtime convergence, and ownership module file headers to describe exports, input/output meanings, workflow, and examples; verify the repository header-policy test passes and no nonessential inline comments were added.

## 4. Successor Ownership Acquisition

- [x] 4.1 Lock the selected Context row before final activation eligibility and ownership checks, then acquire the successor Program/Lane in the same transaction; verify two requests selecting different Runs cannot create two live publishers.
- [x] 4.2 Integrate terminal-owner reconciliation into successor creation before inserting the new Lane; verify the captured stopped-Loop/active-Lane regression now creates exactly one successor and retires the predecessor Lane.
- [x] 4.3 Map nonterminal and inconsistent ownership conflicts to structured HTTP 409 responses containing stable owner Loop, Lane, Context, and recovery reason fields; verify no raw integrity exception or SQL text reaches the response.
- [x] 4.4 Preserve activation-key and selected-Run idempotency under Context locking; verify concurrent equivalent authorization returns one Loop while competing non-equivalent authorization returns the committed winner as a conflict.
- [x] 4.5 Preserve readiness-token behavior when a newer direct-user Run arrives during authorization; verify Context locking does not reintroduce stale Run fallback or mutate predecessor history.

## 5. Legacy Ownership Repair

- [x] 5.1 Add an additive data migration that retires only active or paused managed Lanes whose persisted Program owner Loop is `completed`, `stopped`, or `failed`; verify the reported `368921fd…` data shape repairs without deleting or changing Loop, Run, Program, Lane, or Portfolio identifiers.
- [x] 5.2 Make migration downgrade intentionally leave safely retired Lanes retired while rolling back any schema additions; verify downgrade/upgrade does not reactivate conflicting publishers.
- [x] 5.3 Add idempotent startup ownership reconciliation and audit completion for restored or partially upgraded data without blocking unrelated API readiness; verify two recovery cycles converge to identical ownership and event state.
- [x] 5.4 Add fail-closed handling for orphaned or ambiguous non-retired Lanes whose ownership cannot be proven; verify they remain unchanged and appear as a scoped ownership-integrity diagnostic.

## 6. Shared Desktop API Error Contract

- [x] 6.1 Implement a DOM-independent response decoder that reads a Fetch response once and returns successful JSON/empty results or throws a typed error with status, content type, operation, structured payload, detail, and bounded raw excerpt; verify unit tests cover JSON, text, HTML, empty, and malformed bodies.
- [x] 6.2 Enforce error-body size limits, plain-text rendering, and credential exclusion; verify oversized bodies are truncated and desktop session headers never appear in error snapshots or logs.
- [x] 6.3 Refactor the general renderer API helper to use the shared decoder without changing successful endpoint behavior; verify existing desktop request tests remain green.
- [x] 6.4 Refactor the Loop API client to use the same decoder and retain its domain formatting only as a post-decode layer; verify every direct `response.json()` assumption in Loop request paths is removed.
- [x] 6.5 Present structured ownership conflicts and stable non-JSON server errors beside the authorization button while preserving the user's Mission form and retry identity; verify DOM tests never display `Unexpected token` for HTTP failures.
- [x] 6.6 Update every modified renderer source header to declare exports, input/output meanings, workflow, and an example; verify the header-policy and no-DOM decoder boundary tests pass.

## 7. End-to-End and Regression Verification

- [x] 7.1 Run focused backend tests for ownership lookup/release, every terminal path, transaction rollback, successor reconciliation, structured conflicts, concurrency, migration, startup repair, and audit replay; verify all pass against PostgreSQL.
- [x] 7.2 Run focused frontend unit and DOM tests for the shared decoder, Loop client formatting, form preservation, plain-text 500, malformed JSON, structured 409, and oversized error bodies; verify all pass.
- [x] 7.3 Run the Electron end-to-end flow: stop a Loop, send a new direct-user message in the same Context, authorize a successor, and observe the new Loop; verify no manual database edits or restart is required.
- [x] 7.4 Re-run the original rollback-based diagnostic probe against a copy of the repaired production-shaped data; verify it reports a successful successor and one retired predecessor Lane while leaving the source database unchanged.
- [x] 7.5 Run the full relevant backend, desktop Node/DOM, Electron, migration, architecture, and Loop regression suites; inspect failures without weakening assertions and verify no existing Loop, curation, Run, or API contract regresses.
- [x] 7.6 Run `openspec validate fix-loop-successor-lane-ownership --strict` and the OpenSpec verification workflow; verify all requirements, scenarios, implementation tasks, file-header rules, and module boundaries are satisfied before marking the change complete.
