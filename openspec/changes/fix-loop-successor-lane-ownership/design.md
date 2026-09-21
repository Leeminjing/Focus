## Context

See `proposal.md` for motivation. The failure is reproducible against the current PostgreSQL state: the predecessor Loop is `stopped`, the newest direct-user Main Run is eligible and unbound, but the predecessor Program still owns an `active` `CurationLane` for the same Context. Successor creation inserts another active Lane and PostgreSQL rejects it through `uq_curation_lane_managed_publisher`. The failed transaction rolls back correctly, but the Loop-specific renderer client unconditionally calls `response.json()`, so the plain-text 500 body hides the causal error.

The database already expresses the correct global invariant: a Context may have only one managed Lane whose lifecycle is not `retired`. The repair must preserve that invariant and historical curation entities. Existing Loop terminal writes occur through more than one path, while `LoopRuntimeConvergence` already centralizes cancellation of unfinished runtime work for pause/stop flows. The renderer also has two divergent HTTP paths: the general client tolerates text errors, while the Loop client assumes JSON.

Implementation remains blocked until the user approves these artifacts. After approval, Context7 consultation is the first application step and must cover transactional ownership handoff, PostgreSQL partial unique indexes/locking, idempotent data repair, and Fetch body decoding.

## Goals / Non-Goals

**Goals:**

- Make Context publishing ownership an explicit domain lifecycle with one authoritative acquisition/release boundary.
- Ensure every terminal Loop path releases its managed Lanes atomically and produces causal audit information.
- Repair historical terminal owners safely and allow the reported successor authorization to succeed without manual database edits.
- Serialize successor ownership by Context and turn legitimate conflicts into structured 409 responses.
- Consolidate renderer HTTP response decoding so causal server failures survive JSON, text, HTML, empty, and malformed bodies.
- Retain focused modules, single-responsibility classes/functions, bounded methods, protected helpers, and required declarative file headers.

**Non-Goals:**

- Allowing more than one live publisher for a Context.
- Deleting predecessor Programs, Lanes, Portfolios, Loops, Runs, or audit history.
- Reusing a predecessor Lane as the successor's Lane; each Loop retains its own historical curation graph.
- Automatically stopping or stealing ownership from a nonterminal predecessor.
- Exposing raw SQL exception details or desktop session credentials in the UI.
- Redesigning the Loop creation screen beyond accurate adjacent error presentation.

## Decisions

### 1. Keep the partial unique index as the final invariant

`uq_curation_lane_managed_publisher` remains in place. The bug is a missing ownership release, not an overly strict constraint. Application checks provide domain errors, while the database constraint remains the last race-safety boundary.

Removing or weakening the index was rejected because concurrent processes could then create two authoritative publishers. Catching the integrity exception alone was rejected because it would keep the stale ownership and merely improve the message.

### 2. Introduce a focused curation ownership boundary

A dedicated ownership module will expose core operations for:

- reading and locking the live owner for a Context;
- retiring all non-retired managed Lanes for a proven terminal owning Loop;
- preparing successor acquisition by reconciling a terminal predecessor or returning a structured conflict;
- repairing legacy terminal ownership idempotently.

The module will operate on persisted Loop-to-Program and Program-to-Lane relationships. It will not create Loops, decide eligibility, converge runtime work, or format HTTP responses. Public methods represent domain operations; query/build helpers remain protected or module-private.

Embedding the logic directly in `AgentLoopService.start` was rejected because release, migration repair, terminal convergence, and successor acquisition all need the same rules.

### 3. Centralize terminal Loop finalization

A terminal-lifecycle coordinator will own the transaction-level sequence for `completed`, `stopped`, and `failed`:

1. validate and lock the Loop transition;
2. apply the terminal status, health, completion timestamp, grant revocation, and final result where applicable;
3. converge unfinished runtime work;
4. release curation ownership through the ownership module;
5. append causal terminal and ownership events;
6. flush as one transaction.

Existing terminal paths in the service, Kernel, budget/failure handling, waiting-user actions, and restart recovery must route through this boundary. `LoopRuntimeConvergence` remains responsible only for unfinished runtime work; it does not absorb ownership policy.

Adding retirement calls independently at each current assignment site was rejected because future terminal paths could repeat the defect and partial failures could commit mismatched Loop/Lane state.

### 4. Serialize successor acquisition with the Context row

Successor creation will lock the selected `DesktopThread` row before the final eligibility and ownership checks. Context identity, not Run identity, is the ownership key. Within the transaction it will:

1. lock the Context;
2. re-evaluate newest direct-user Run eligibility;
3. resolve the current non-retired managed Lane and owning Loop;
4. reconcile it only when the persisted owner is terminal;
5. reject nonterminal or inconsistent ownership as a structured 409;
6. create the successor Program/Lane and activation lineage.

The database unique index still arbitrates unexpected races. If it fires despite the domain lock, the transaction maps it to an ownership-integrity error rather than leaking SQL details.

Run-only advisory locking was rejected because two successor requests can select different Runs while targeting the same Context publisher.

### 5. Retire rather than transfer or delete predecessor Lanes

A successor receives a new Program and Lane. The predecessor Lane transitions to `retired`, retaining its stable identity and historical Portfolio relationships. Ownership release emits an idempotent canonical event keyed by Loop, Context, Lane, and terminal revision.

Transferring the same Lane was rejected because it would rewrite historical ownership and blur which Loop produced prior Portfolio revisions. Deletion was rejected because it would destroy auditability.

### 6. Repair legacy ownership both at upgrade and at acquisition

An additive data migration retires non-retired managed Lanes whose Program is owned by a terminal Loop. It does not revive Lanes on downgrade. Startup reconciliation records or completes any missing audit projection without blocking unrelated API readiness. Successor acquisition repeats the proof-and-retire operation idempotently so installations that missed or interrupted repair remain usable.

Relying only on a one-time migration was rejected because restored backups and interrupted upgrades can reintroduce stale rows. Relying only on lazy acquisition was rejected because degraded ownership would remain invisible until the next authorization attempt.

### 7. Use one pure renderer response decoder

A DOM-independent module will consume a Fetch `Response` exactly once and return decoded success data or throw a typed desktop API error containing:

- HTTP status and status text;
- content type;
- operation/request label, never credentials;
- structured payload and `detail` when valid;
- a bounded plain-text body excerpt when JSON is absent or malformed.

The general renderer API helper and Loop API client will use this module. Loop-specific formatting may add Context, Run, Loop, or Lane labels from structured detail, but it cannot replace the original payload. Error text is rendered through existing escaping/text-content paths.

Adding a local `try/catch` around `response.json()` only in Loop start was rejected because the same assumption exists at multiple Loop endpoints and would continue diverging from the general client.

### 8. Preserve the repository's source structure and comment policy

Ownership persistence, terminal orchestration, and response decoding remain separate modules. New or modified files must keep their declarative file-header contract current: exported surface, input meanings, output meanings, workflow, and example. No explanatory implementation comments are added inside the files unless an invariant cannot be expressed through names, types, or structure.

Architecture tests will enforce dependency direction: the pure response decoder has no DOM dependency; curation ownership does not depend on HTTP schemas; terminal lifecycle may depend on convergence and ownership, but neither depends back on the coordinator.

## Risks / Trade-offs

- **[Risk] Lock-order changes introduce deadlocks** → Lock the Context before final successor eligibility and Lane ownership reads; keep terminal release limited to Loop and Lane rows; add concurrent PostgreSQL tests with bounded timeouts.
- **[Risk] A malformed historical Program-to-Loop relationship is retired incorrectly** → Reconcile only relationships proven by persisted `AgentLoop.program_id`; fail closed and surface an ownership-integrity conflict otherwise.
- **[Risk] Terminal status commits while Lane release fails** → Execute terminal lifecycle and ownership release in one transaction and test forced-failure rollback.
- **[Risk] Data migration retires a Lane that operators expected to resume** → Restrict migration to terminal Loop states only; paused, pausing, waiting, and stopping owners remain live.
- **[Risk] Renderer retains sensitive or oversized server bodies** → Cap excerpts, store no request headers/session secret, and render as escaped plain text.
- **[Trade-off] Downgrade does not reactivate repaired Lanes** → Reactivation could recreate conflicting publishers; historical retirement remains the safer state and old code can still read retired Lanes.

## Migration Plan

1. After explicit approval, run the required Context7 preflight and record the cited guidance before editing application source.
2. Add red regression fixtures reproducing the stopped-Loop/active-Lane database state and the plain-text 500 JSON-decoding symptom.
3. Add the ownership and terminal-lifecycle boundaries, route every terminal transition through them, and protect successor acquisition with the Context lock.
4. Add an additive data migration for provable terminal ownership and an idempotent runtime reconciliation/audit pass.
5. Add the shared response decoder and migrate both the general renderer request helper and Loop client.
6. Run migration upgrade/downgrade/upgrade checks, PostgreSQL concurrency and rollback tests, renderer unit/DOM tests, Loop end-to-end authorization, and full relevant regressions.
7. Re-run the original rollback-based reproduction against the repaired database state and verify authorization succeeds without manual row edits.

Rollback removes new code and any schema additions, if ultimately required, but intentionally leaves safely retired historical Lanes retired. No rollback path deletes audit or curation history.
