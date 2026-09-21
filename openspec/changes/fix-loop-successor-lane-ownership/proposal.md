## Why

A stopped Loop can leave its primary `CurationLane` active, so authorizing a successor Loop for the same Context violates the single-publisher database constraint even though successor eligibility is valid. The backend then emits a plain-text 500 response which the renderer blindly parses as JSON, replacing the actionable database failure with `Unexpected token 'I'`.

## What Changes

- Define Context curation ownership as an explicit lifecycle tied to the owning Loop rather than an incidental consequence of creating a Lane.
- Release terminal Loop ownership by retiring all non-retired managed Lanes in the same authoritative transaction that converges the Loop to `completed`, `stopped`, or `failed`.
- Reconcile legacy terminal Loops whose Lanes still occupy publisher ownership, preserving the historical Program, Portfolio, Lane, Loop, and Run identities.
- Make successor authorization acquire a Context-scoped ownership lock, reconcile a terminal predecessor, and return a structured conflict when a genuinely nonterminal owner still holds the Context.
- Preserve the existing one-live-publisher invariant; the repair must not weaken or remove the partial unique constraint.
- Establish a shared desktop API response decoder that accepts successful JSON responses but safely represents non-JSON and malformed error responses with HTTP status, content type, bounded body text, and request context.
- Ensure Agent Loop authorization shows the original structured server failure when available and a stable transport/server message otherwise; JSON decoding errors must never replace the causal error.
- Require explicit user approval before implementation. After approval, the first implementation action must consult Context7 for authoritative guidance on transactional ownership handoff, PostgreSQL partial unique indexes and locking, idempotent repair migrations, and robust Fetch response decoding. If Context7 is unavailable, implementation stops and asks the user.
- Require focused modules and single-responsibility classes, protected helper methods where appropriate, bounded methods, and declarative file-header comments describing exports, inputs, outputs, workflow, and an example; nonessential inline comments remain prohibited.

## Capabilities

### New Capabilities

- `loop-curation-ownership`: Authoritative acquisition, release, reconciliation, and successor handoff of the single live Curation Lane publisher for a Context.
- `desktop-api-error-responses`: Stable decoding and presentation of JSON and non-JSON desktop API failures without masking the causal server error.

### Modified Capabilities

None. The repository's main specification set does not yet contain Loop curation ownership or desktop API error-response contracts.

## Impact

- Loop terminal transitions, runtime convergence, successor authorization, activation eligibility, Curation Program/Lane repositories, startup repair, and database migrations.
- Agent Loop HTTP APIs and the shared renderer API request layer used by Loop authorization and related surfaces.
- Existing terminal Loop rows with active or paused Curation Lanes require an idempotent repair path; historical records and identifiers remain intact.
- Regression coverage must include stopped/completed/failed predecessors, nonterminal ownership conflicts, concurrent successor authorization, legacy repair, transaction rollback, JSON errors, plain-text 500 responses, malformed JSON, and bounded error-body handling.
