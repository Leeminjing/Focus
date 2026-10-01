## Purpose

保证 Focus 在普通任务、委派角色和默认执行入口中可靠装配工具能力，使模型请求、上下文说明、压缩预算与实际工具执行指向同一份已验证目录，并在任何模型调用前明确拒绝含糊的名称冲突。

## ADDED Requirements

### Requirement: Plugin tools have a single execution injection owner

The system SHALL inject each active plugin tool through one system-owned execution path. Tool discovery for equipment display MUST remain complete, while execution pool selection MUST exclude tools already owned by the plugin injection path. Selection MUST use declared source information rather than guessing from names.

#### Scenario: Main starts with an active demo plugin and compression enabled
- **WHEN** a Main task sends an ordinary message with the active demo plugin and compression enabled
- **THEN** graph assembly succeeds with exactly one demo_echo binding and reaches the model invocation normally

#### Scenario: Default execution discovers plugin and non-plugin tools
- **WHEN** an execution graph is created without an explicit tool list and discovery returns builtin, custom, MCP and plugin tools
- **THEN** every selected non-plugin tool is retained and each active plugin tool enters the graph exactly once

#### Scenario: Equipment displays plugin capabilities
- **WHEN** equipment is queried while an active plugin provides tools
- **THEN** the discovered plugin tools remain visible even though execution does not take them from the general pool

### Requirement: Tool name conflicts fail before graph execution

The system MUST validate the union of all ordinary and middleware-provided tools before compiling an executable graph. It MUST reject repeated names even when they refer to the same object or equivalent schemas, and SHALL identify the conflicting name and both registration owners. This behavior MUST be independent of compression settings and Provider protocol, and MUST NOT silently overwrite, deduplicate or rename tools.

#### Scenario: Different sources expose the same name
- **WHEN** two tools with the same name but different callables or execution effects are supplied by different injection owners
- **THEN** assembly fails with the name and both owners before any model HTTP request or tool side effect

#### Scenario: The same object is injected twice
- **WHEN** the same tool object is explicitly provided twice or appears in both ordinary and middleware declarations
- **THEN** assembly reports a duplicate injection rather than treating the second occurrence as harmless

#### Scenario: A middleware declaration contains an internal conflict
- **WHEN** one tool-providing middleware declares two tools with the same name
- **THEN** assembly rejects the conflict and identifies both declaration positions

#### Scenario: Compression or model protocol changes
- **WHEN** an invalid duplicate directory is assembled with compression disabled or with an explicit legacy Chat model
- **THEN** the same conflict is rejected before execution rather than relying on a later Provider or framework check

### Requirement: All capability consumers agree on the effective directory

The system SHALL use one validated ordered set of tool bindings for execution routing, model-visible capability context, compression request estimation and Provider request projection. Wire representations MAY differ by protocol, but their normalized names, descriptions and argument schemas MUST agree with the executed tools. Advertised tools MUST be executable bindings rather than discoveries absent from the graph.

#### Scenario: Responses request matches runtime and world state
- **WHEN** an OpenAI or DeepSeek Responses graph with plugin and ordinary tools prepares a request
- **THEN** the request tool directory, runtime execution bindings and current capability context have the same effective tool names and normalized schemas

#### Scenario: Compression estimates a tool-bearing request
- **WHEN** compression evaluates a request containing the effective directory and prepared world state
- **THEN** its request estimate equals the estimate of the final Provider payload using the same tools and request options

#### Scenario: A middleware contributes a distinct tool
- **WHEN** a supported middleware supplies a uniquely named tool in addition to ordinary tools
- **THEN** that tool is included once in routing, capability context, budgeting and the Provider directory in consistent effective order

### Requirement: Assembly snapshots preserve tool identity and reload isolation

The system SHALL retain the original callable and trusted tool metadata in an assembly snapshot. Changes to a returned directory container MUST NOT mutate that snapshot. Plugin reload SHALL affect subsequently assembled graphs without replacing tools in an already assembled graph.

#### Scenario: Plugins are reloaded between graph constructions
- **WHEN** a plugin's configured tool set changes and plugins are reloaded after a graph has been assembled
- **THEN** the old graph retains its original bindings and a new graph uses the newly loaded unique directory

#### Scenario: A directory consumer changes its returned list
- **WHEN** a consumer modifies a returned tool directory container
- **THEN** another consumer and the graph retain their validated bindings and source metadata

#### Scenario: A plugin is disabled for a new graph
- **WHEN** plugin discovery no longer includes a previously enabled tool after reload
- **THEN** a newly assembled graph neither advertises nor routes that tool, while an already assembled graph retains its prior snapshot

### Requirement: Role composition preserves valid tool behavior

The system SHALL preserve existing role-specific ordinary tools and plugin hooks while correcting injection ownership. Main, Teammate, Worker and Patrol graphs MUST receive each allowed active plugin binding once. Default and explicit tool entry points MUST enforce the same uniqueness contract.

#### Scenario: A unique plugin tool executes during a normal run
- **WHEN** a model requests an active uniquely named plugin tool in an otherwise valid task run
- **THEN** the tool invokes its original callable exactly once and its result participates in the normal tool exchange

#### Scenario: Different roles are assembled
- **WHEN** Main, Teammate, Worker or Patrol graphs are assembled with role-specific ordinary tools and active plugins
- **THEN** role-specific capability choices are preserved, plugin tools appear once, and plugin hooks retain their existing dispatch behavior

### Requirement: Tool visibility does not expand execution authority

The system MUST preserve existing tool effect contracts, runtime arguments and authorization enforcement when assembling the effective directory. Catalog inclusion MUST NOT authorize a previously prohibited tool operation.

#### Scenario: An advertised tool requires write access
- **WHEN** a tool requiring write access is present in a read-only run and the model requests it
- **THEN** existing authorization enforcement still blocks or interrupts the operation before its side effect, regardless of its presence in the directory
