r"""本文件对外提供 IndexInheritanceReceipt 与 IndexBuildPlan。

输入为本次冻结 segment inventories；输出为不可变构建计划和局部 proof inventory。
新构建使用 full 合同，局部缓存复用不依赖历史基线；旧 incremental 字段仅用于读取已保存 artifact 和独立库的兼容协议。
具体工作流为检查库存不交叠，再由完整 index 绑定本次实际记录 identities。
示例：plan = IndexBuildPlan(recomputed_segment_ids=ids, full_build_reason="no_baseline")。
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IndexInheritanceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["full", "incremental"] = "full"
    baseline_index_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prefix_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reused_segment_ids: tuple[str, ...] = ()
    recomputed_segment_ids: tuple[str, ...] = ()
    full_build_reason: str | None = "no_baseline"
    record_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_valid_plan(self) -> Self:
        reused, recomputed = self.reused_segment_ids, self.recomputed_segment_ids
        if len(set(reused + recomputed)) != len(reused) + len(recomputed):
            raise ValueError("index build segment inventory 重复或交叠")
        if self.mode == "incremental":
            if (
                not self.baseline_index_id
                or not self.prefix_hash
                or self.full_build_reason
            ):
                raise ValueError("incremental index 缺少基线／前缀证明")
        elif (
            self.baseline_index_id
            or self.prefix_hash
            or reused
            or not self.full_build_reason
        ):
            raise ValueError("full index 不得声称继承")
        return self


class IndexBuildPlan(IndexInheritanceReceipt):
    pass
