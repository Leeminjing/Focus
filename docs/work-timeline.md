# Work Timeline

Work Timeline is the durable, cross-Round history of one Loop. It is a read
projection of committed canonical events, separate from current LoopFact state
and Task Progress. Context and Round are filters, not independent histories.

## Storage and ordering

`WorkTimelineProjector` consumes the Loop journal into `work_timeline_entries`.
One source event produces at most one entry. Both source identity and Loop
sequence have unique constraints; deterministic entry IDs make retries and
cursor recovery idempotent. Entry insertion and cursor advancement share a
transaction. A failed page rolls back and retries without skipping history.
The global supervised consumer covers stopped as well as running Loops, with
bounded visits and failure isolation between Loops.

Entries keep bounded presentation summaries and object references, including
`fact_id + fact_revision`, Contexts, Run, Round, decision, input and causation.
They have no foreign key to the journal. File contents and evidence bodies stay
in their existing versioned stores and are fetched only on inspection.

`sequence` is the only ordering authority. `recorded_at` preserves the journal
record timestamp; it is not an exact wall-clock commit timestamp. Optional
`occurred_at` is a known business timestamp. In particular, fact verification
records keep the original fact occurrence time and their own record time. They
are revisions of one fact, not new executions of its test or tool.

## Event sources and causality

The display allowlist includes submitted/observed/handled inputs, authorized
Patrol decisions, Context publication/derivation, Run settlement, Round state
and non-tool fact revisions. Tool activity stays in the existing activity and
conversation interfaces. Unknown event kinds advance the consumer without
creating display entries.

At authority commit, `record_work_decision` captures a public decision summary,
actual observed inputs and affected action Contexts in the same transaction.
Deferred execution does not turn this into a claim that its work is finished.
Context derivation events carry their publishing decision. Run/Fact references
follow the persisted Run and Directive back to their actual decision.

Submitted inputs have no Round until an observed/handled transition establishes
one. Never attach an input or a late result to the Loop's current Round merely
because it is currently active. A selected input query follows actual decision
references to related Context/Run/Fact entries.

For older journals, a Round envelope explicitly witnessing authority commit can
anchor a historical decision using its persisted identity. A current decision
pointer without that historical witness does not invent a past event.

## Retention and rollout

Migration `06a1b2c3d4e5` adds only the timeline table; existing fact and revision
schemas are unchanged. Normal application migration upgrades existing installs.

Journal pruning requires **both** `loop_facts` and `work_timeline` cursors, even
before their first initialization, and respects every other registered consumer
and existing evidence pins. A Timeline reference does not pin its source event
forever: the durable entry survives source cleanup.

If an installation already pruned an earlier prefix before Timeline existed,
projection writes an explicit history-gap entry and continues from the earliest
retained sequence. It does not reconstruct a supposedly complete chronology
from current entities. Canonical events disabled in the past cannot be recovered
as authentic history. Persisted timeline history lasts for the lifetime of its
Loop; explicit Loop deletion cascades to its entries.

## Query and UI

`GET /desktop/api/agent-loops/{loop_id}/timeline` supports:

- `before`: exclusive sequence cursor for earlier history.
- `after`: exclusive sequence cursor for incremental history; cannot combine
  with `before`.
- `category`: `fact` or `user_decision`; omitted means all display events.
- `context_id`, `round_id`, `intent_id`: exact scope/causality filters.
- `limit`: 1–200, default 80.

Pages return ascending entries, `next_before`, `next_after`, `has_more`,
`projected_through` and `source_sequence`. A read freezes the projector boundary
before reading rows, so a concurrently committed page is not skipped. The
incremental cursor advances over hidden/non-display events only after all
matching entries in its bounded page have been delivered. Scope and evidence
permissions reuse the existing Loop access rules and source visibility.

`work-timeline.js` owns UI pagination, filters and delayed-response isolation.
It follows the existing single Live connection and retries while the projector
is catching up. Idle history does not need another persistent event stream.
Unchanged articles and expanded evidence remain mounted; paging preserves the
reading position and new events expose a jump action when reading older work.
Evidence inspection requests the referenced revision and shows that revision's
presentation, state and evidence. It must never substitute the current fact for
an unavailable historical version. File links still open the existing pinned
version inspector and support ordinary input references.

## Validation

- `backend/tests/test_work_timeline.py`: PostgreSQL ordering, concurrent/repeated
  consumption, keyset paging, filters, API permission/parameter checks, atomic
  retry, real input/decision links, source deletion and migration round trip.
- `desktop/src/renderer/features/files/work-timeline.test.cjs`: stable entry identity, DOM/scroll retention,
  stale filter responses, escaping and retry behavior.
- `desktop/tests/e2e/work-timeline.e2e.py --output <directory>`: headless Playwright using
  production assets and fixture HTTP. Covers all three filters, earlier pages,
  exact historical evidence, input/Context navigation, draft/focus preservation,
  incremental insertion, read failure and desktop/mobile screenshots. This is a
  browser acceptance test; it does not claim an external provider/Agent run.
- Existing Patrol, file feedback, Live projection, kernel, lineage and storage
  maintenance tests cover integration and neighboring behavior.
