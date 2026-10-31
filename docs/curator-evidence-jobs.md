# Curator evidence tasks

Patrol may request `{"needs_evidence":["curator"],"context_ids":["context-id"]}`.
The IDs must exist in its frozen Observation. Omitting `context_ids` retains the
whole-frontier request contract, without silently truncating dependencies.
The shared Worker pool controls execution concurrency.

`loop_evidence_jobs` records each Context's index preparation and Curator result
separately, and records assessments for their exact source set. Job identity
includes Observation ID, generation, evidence kind and complete source Revision
references. `budget_reservation` points to the existing Worker budget owner and
its unique retry identity; actual admission and settlement use the existing
reservation/usage ledger. Jobs do not introduce another spending allowance.

A Worker claim increments the task fence. Completion validates the current
Worker attempt, Observation, generation and source references. Ready results are
immutable and hashed. Decision composition and retrieval checkpoint reuse verify
source identities again. Observation preemption invalidates unfinished jobs;
completed old artifacts remain available for audit and exact cache reuse.

Each complete Context index commits immediately, independently of sibling
indexing and Portfolio catalog assembly. Each completed Curator commits its own
result, an `EvidenceJobReady` Outbox event, and the Round's resumable status in one
transaction. PostgreSQL's existing `focus_loop_work` channel wakes consumers only
after commit. Unrelated pending Workers do not block a ready evidence result.
Recovery reads durable tasks and reuses the existing bounded Worker retries.

Round execution only queues the requested evidence and releases its coordinator
lease. Index/model waits belong to Workers and use Worker budget ownership.
`ContextEvidenceIndexPort` is the replacement seam for a future
`focus-context-index` adapter; the local implementation remains the default.
