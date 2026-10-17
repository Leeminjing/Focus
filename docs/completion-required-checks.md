# Mission 完成验收范围

Verifier、CompletionGuard 和 CompletionEligibilityPolicy 使用当前 Mission revision 的 `completion_checks`。

- `required=true` 或省略 `required`：必须为 `satisfied`，具有通过来源校验的类型化证据，并且不在 `unresolved` 中。
- `required=false`：仍须逐项返回真实检查结果。未检查可返回 `unknown`，未满足可返回 `unsatisfied`；状态、`explanation` 和 `unresolved` 保留在最终结果中，不阻止完成。
- 任何项声明 `satisfied` 都必须提供合法证据，包括非必需项。
- 总体验证结论 `conclusion` 按必需项判定，完成时必须明确为 `satisfied`。
- 所有项均为非必需时，验收范围允许判为 `satisfied`，即使所有项都是 `unknown` 或 `unsatisfied`。仍须独立 Verifier，并通过新鲜度、活动 Run、人工 gate、Portfolio 发布、workspace adoption 和授权等完成门禁。
- 没有声明任何检查时仍不允许完成，与“已声明检查但全部非必需”区分。

Guard 直接接收当前 Mission 的检查定义并集中执行验收判定；Eligibility 复用 Guard 后继续核对真实证据来源。用户改变验收范围会激活新的 Mission revision，旧验证仍受 revision 新鲜度检查约束，需要针对新范围重新验证。
