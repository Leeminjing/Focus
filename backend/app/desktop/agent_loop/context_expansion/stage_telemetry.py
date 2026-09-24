r"""本文件对外提供 DerivationStageTimer。

输入为派生阶段、输入 identities 与算法版本；输出为包含输出 identities、耗时和安全摘要的 DerivationStageRecord。
具体工作流为在阶段执行前捕获单调时钟，在阶段结束或失败时冻结不可变记录，且耗时不参与业务 identity。
示例：`record = DerivationStageTimer("admission", ids, version).finish(outputs, summary)`。
"""

from __future__ import annotations

from time import perf_counter_ns

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    DerivationStage,
    DerivationStageRecord,
)


class DerivationStageTimer:
    def __init__(
        self,
        stage: DerivationStage,
        input_identities: tuple[str, ...],
        version: str,
    ) -> None:
        self._stage = stage
        self._input_identities = input_identities
        self._version = version
        self._started_ns = perf_counter_ns()

    def finish(
        self,
        output_identities: tuple[str, ...],
        safe_summary: str,
        *,
        failure_code: str | None = None,
    ) -> DerivationStageRecord:
        duration_ms = max(0.0, (perf_counter_ns() - self._started_ns) / 1_000_000)
        return DerivationStageRecord(
            stage=self._stage,
            input_identities=self._input_identities,
            output_identities=output_identities,
            version=self._version,
            duration_ms=duration_ms,
            safe_summary=safe_summary,
            failure_code=failure_code,
        )
