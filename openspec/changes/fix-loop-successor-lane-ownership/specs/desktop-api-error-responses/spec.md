## Purpose

Defines a stable desktop HTTP response contract so renderer surfaces preserve the causal backend failure across JSON, plain-text, empty, and malformed responses.

## ADDED Requirements

### Requirement: Desktop responses are decoded according to status and content
The renderer SHALL decode a successful response according to its declared content type and status, including empty responses. An unsuccessful response SHALL be converted into a typed application error even when its body is not valid JSON.

#### Scenario: Successful JSON response
- **WHEN** a desktop endpoint returns a successful status and a JSON body
- **THEN** the caller receives the decoded JSON payload

#### Scenario: Successful empty response
- **WHEN** a desktop endpoint returns a successful no-content response
- **THEN** the caller receives an empty result without a decoding failure

#### Scenario: Server returns plain text
- **WHEN** a desktop endpoint returns status 500 with `Internal Server Error` or other plain text
- **THEN** the caller receives a typed HTTP error containing the status and bounded server text rather than a JSON syntax error

### Requirement: Structured server errors retain their semantics
When an unsuccessful response contains a valid structured payload, the application error SHALL preserve the HTTP status, structured `detail`, machine-readable code, relevant entity identities, and user-facing message supplied by the server.

#### Scenario: Successor ownership conflict is structured
- **WHEN** authorization returns a conflict naming an owning Loop and Lane
- **THEN** the UI presents that conflict and retains the structured fields for recovery actions

#### Scenario: Validation error contains a detail collection
- **WHEN** an endpoint returns structured validation details
- **THEN** the decoder preserves those details instead of flattening them into an unrelated parsing message

### Requirement: Malformed response bodies do not mask HTTP failures
If an error response claims to be JSON but is malformed, the application SHALL report the HTTP failure with content type and bounded raw-body context. The JSON decoder's exception text MUST NOT replace the causal HTTP error.

#### Scenario: Malformed JSON error
- **WHEN** a status 500 response declares JSON but contains truncated or invalid JSON
- **THEN** the UI reports an HTTP 500 server-response error and retains a bounded body excerpt for diagnosis

#### Scenario: Empty error body
- **WHEN** an unsuccessful response has no body
- **THEN** the UI reports a stable message derived from the HTTP status and request operation

### Requirement: Error-body handling is bounded and safe
The decoder SHALL cap retained raw error text, avoid rendering it as trusted markup, and exclude request credentials or session headers from user-visible and diagnostic error objects.

#### Scenario: Oversized proxy error page
- **WHEN** a proxy or server returns a very large HTML error body
- **THEN** only a bounded plain-text excerpt is retained and displayed safely

#### Scenario: Error is recorded for diagnostics
- **WHEN** an application error is logged or attached to UI state
- **THEN** it contains request operation and response metadata but no desktop session secret

### Requirement: Desktop API clients share one error contract
Agent Loop authorization and other renderer HTTP clients SHALL use the same response-decoding contract. A feature-specific client MAY add domain formatting after decoding but MUST NOT directly assume every response is JSON.

#### Scenario: Agent Loop authorization receives a 500
- **WHEN** the Loop start endpoint returns a non-JSON error response
- **THEN** the authorization surface shows the stable decoded HTTP error and does not display `Unexpected token` from JSON parsing

#### Scenario: Domain conflict receives additional formatting
- **WHEN** the shared decoder returns a structured Loop conflict
- **THEN** the Loop client may format Run, Loop, Context, or Lane identities without discarding the original error payload
