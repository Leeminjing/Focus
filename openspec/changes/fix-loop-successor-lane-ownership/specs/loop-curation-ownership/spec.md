## Purpose

Defines the authoritative lifecycle for a Context's single live curation publisher so terminal Loops release ownership and successor authorization remains safe, deterministic, and auditable.

## ADDED Requirements

### Requirement: A Context has at most one live curation publisher
For a given Context, the system SHALL permit at most one non-retired Curation Lane to hold managed publishing ownership at a time. Terminal historical owners MUST NOT retain live publishing ownership.

#### Scenario: One Loop actively manages a Context
- **WHEN** a Loop owns a non-retired managed Lane for a Context
- **THEN** another nonterminal Loop cannot acquire managed publishing ownership for that Context

#### Scenario: Historical Lanes remain queryable
- **WHEN** a managed Lane is retired after its Loop terminates
- **THEN** its stable Lane, Program, Portfolio, Loop, Context, and Run identities remain queryable as historical records

### Requirement: Terminal Loop transition releases curation ownership atomically
Every transition of a Loop to `completed`, `stopped`, or `failed` SHALL retire all non-retired managed Lanes owned through that Loop's Curation Program in the same authoritative transaction as the terminal transition. A rolled-back terminal transition MUST leave both the Loop and its ownership unchanged.

#### Scenario: User stops a Loop
- **WHEN** a running or waiting Loop commits a transition to `stopped`
- **THEN** all managed Lanes owned by that Loop are retired before the transaction commits

#### Scenario: Patrol completes a Loop
- **WHEN** Patrol commits an authorized transition to `completed`
- **THEN** the Loop's managed Lanes are retired in the same transaction

#### Scenario: Loop termination rolls back
- **WHEN** any required terminal-finalization operation fails
- **THEN** neither the terminal Loop status nor a partial Lane retirement is committed

### Requirement: Successor authorization reconciles terminal ownership
Successor authorization SHALL inspect the current publishing owner for the selected Context under an ownership-scoped lock. If the owner belongs to a terminal predecessor, the system SHALL idempotently retire the stale Lane before acquiring ownership for the successor.

#### Scenario: Stopped predecessor left an active Lane
- **WHEN** an eligible successor is authorized for a Context whose stopped predecessor still has an active or paused managed Lane
- **THEN** the stale Lane is retired and the successor Loop acquires ownership without violating the one-publisher invariant

#### Scenario: Terminal ownership was already released
- **WHEN** successor authorization encounters only retired historical Lanes
- **THEN** authorization proceeds without changing those historical records again

### Requirement: Nonterminal ownership conflict is explicit
If an active, pausing, paused, waiting, completing, or stopping Loop still owns the Context, successor authorization SHALL reject the request with a structured conflict identifying the owning Loop and Lane. The conflict MUST NOT surface as a database integrity error or generic 500 response.

#### Scenario: Existing Loop still waits for the user
- **WHEN** a successor authorization targets a Context owned by a `waiting_user` Loop
- **THEN** the response identifies the nonterminal owner and leaves both Loops and the Lane unchanged

#### Scenario: Ownership metadata is inconsistent
- **WHEN** a non-retired Lane cannot be associated with a valid terminal predecessor eligible for reconciliation
- **THEN** authorization fails closed with a structured ownership-integrity conflict and does not retire or replace the Lane

### Requirement: Concurrent successor authorization is serialized by Context
Ownership acquisition SHALL be serialized using the Context identity rather than only the selected Run identity. Equivalent retries SHALL converge on one successor Loop, and competing non-equivalent requests SHALL observe the committed winner as a structured conflict.

#### Scenario: User activates the same successor twice
- **WHEN** two equivalent authorization requests race for the same Context and selected Run
- **THEN** exactly one successor ownership is created and both responses resolve to that same Loop

#### Scenario: Different successor requests race
- **WHEN** two non-equivalent authorization requests concurrently target the same Context
- **THEN** one request commits and the other reports the committed ownership without producing a unique-constraint 500

### Requirement: Legacy ownership repair is idempotent
Upgrade and restart reconciliation SHALL detect terminal Loops with non-retired managed Lanes and retire only those Lanes whose ownership can be proven from persisted Loop and Program relationships. Repeating repair MUST converge to the same state without deleting or rewriting historical identities.

#### Scenario: Existing stopped Loop owns an active Lane
- **WHEN** an upgraded installation contains a stopped Loop whose Program still owns an active Lane
- **THEN** repair retires that Lane and a successor can subsequently be authorized

#### Scenario: Repair runs more than once
- **WHEN** migration or startup reconciliation is repeated after a successful repair
- **THEN** no additional ownership transitions or duplicate audit events are produced

### Requirement: Ownership transitions are observable
Acquisition, terminal release, legacy reconciliation, and rejected ownership conflicts SHALL expose stable causal information through existing Loop audit or live-event surfaces without leaking raw database exception text to the user.

#### Scenario: Terminal release is inspected
- **WHEN** a client or operator examines the terminated Loop's history
- **THEN** the release reason, affected Context and Lane identities, terminal Loop status, and transition time are available

#### Scenario: Successor acquisition follows repaired ownership
- **WHEN** a successor acquires a Context after stale ownership reconciliation
- **THEN** the successor history identifies the predecessor Loop and retired Lane that made the handoff possible
