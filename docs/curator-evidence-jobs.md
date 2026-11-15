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
Exact-bound Curators created before this schema are adopted in a short recovery
transaction. Frozen Worker facts are retained when new evidence is composed.
Worker recovery/control transitions synchronize unfinished job states in the same
PostgreSQL transaction; ready results remain immutable. Legacy assessment
supplements are accepted only when no matching modern task exists and the frozen
Observation generation and sources still match.

Round execution only queues the requested evidence and releases its coordinator
lease. Index/model waits belong to Workers and use Worker budget ownership.
`ContextEvidenceIndexPort` separates evidence scheduling from index execution.
The default `focus-context-index` adapter and the local reference implementation
share scoped builds, Worker budget ownership and independent Context publication.

## Model round trips

Patrol's initial request retains the complete frozen task delta. Within
`patrol.initial_evidence_max_bytes` (16384 by default), it also includes complete,
successful Worker results from the current Round and message evidence from the
frozen Context revisions related to task deltas or user inputs. Context messages
already present in frontier previews are omitted from this supplement. A bounded
selection declares incomplete history and omitted message counts; it never
replaces the frozen Observation or represents a whole Context. Large Worker
results and historical sources retain their identity/hash paging contract.

For a fresh v2 Curator session, a complete authorized corpus of at most 64 exact
reads that fits `curation_small_evidence_max_bytes` (16384 by default) and one
request page is read deterministically. Query admission, candidate provenance,
exact-read accounting and checkpoints use the existing session controller. The
model generates work advice directly from these exact reads: one model request
replaces query planning, read selection and work advice. Oversized corpora,
constrained retrieval budgets and tasks with selectable evolution strategies
retain the selective planning flow. Set the byte threshold to 0 to disable the
optimization. Resume reuses the prepared evidence and validated model results.

The top-level `patrol` configuration selects `model_name`, `output_method`,
`max_output_tokens` and `streaming` independently of deep indexing. A Loop's
explicit `patrol_model_name` overrides that model selection; otherwise an unset
`patrol.model_name` uses the normal default for compatibility. Patrol defaults to
2048 output tokens. Curator/indexing still uses `curation_default`, its 8192-token
default and any existing role-specific overrides. Changing the ordinary Run
model in Loop equipment does not override an independently selected Patrol model.

## Structured request measurements

Patrol and structured Workers emit content-free `structured_request` metrics for
successful and failed requests. `estimated_input_tokens` covers the serialized
request, including instructions, frozen input and bound schema/tools. Native
structured-output modes no longer duplicate the JSON schema in message text;
`prompt_json` still includes it. Estimates remain estimates; `input_tokens` and
`output_tokens` come from Provider usage, and stay null when unreported.

`ttft_ms` measures the first nonempty text, public reasoning or tool output chunk;
`output_duration_ms` measures from that chunk until model completion/error.
Empty opening chunks do not count. Nonstreaming requests have unknown TTFT and
output duration. `curation_streaming` and `patrol.streaming` enable streaming by
default; parsing still waits for the complete response. The stream transport
does not add another model request. Existing Patrol retry admission and receipts
also cover wrapped transient Responses transport errors.

The performance report groups these metrics by category, model and schema and
reports input/output token and latency distributions, usage-report coverage and
schema success rates. This success rate measures schema validation, not semantic
decision approval or Kernel acceptance. Compare these measurements on the same
task workload before selecting another model; no latency or monetary saving is
inferred from the offline call-count tests.
