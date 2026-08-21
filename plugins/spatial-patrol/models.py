"""spatial-patrol 插件的数据模型:空间锚点与空间小兵共用的单表 ORM。

对外提供:
    SpatialAnchor — spatial_anchors 表 ORM(锚点先行成立,投放后升格为小兵记录)

输入:
    spatial_id: str — 锚点/小兵唯一 id(32 位 hex)
    task_id: str — 所属桌面任务(desktop_threads.task_id)
    kind: str — "anchor"(纯锚点) / "patrol"(已投放小兵)
    content_ref: str — 载体引用(工作区内相对路径)
    page: int — 页号(单页载体为 1)
    x / y: float — 归一化坐标 0..1(内容原始尺寸)
    region: dict | None — 预留区域字段(第一版不生效)
    status: str — anchor: active/invalid;patrol: deployed/done/dismissed/invalid
    task_instruction: str — 小兵任务指令(投放时给出)
    run_id: str | None — 最近一次执行 run

工作流:
    点击 → kind=anchor,status=active 落库;投放 → kind=patrol,status=deployed,
    task_instruction/run_id 写入;完成 → done;回收 → dismissed;载体失效 → invalid。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class SpatialAnchor(Base):
    __tablename__ = "spatial_anchors"

    spatial_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="anchor")
    content_ref: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    region: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    task_instruction: Mapped[str] = mapped_column(Text, nullable=False, default="")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "spatial_id": self.spatial_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "content_ref": self.content_ref,
            "page": self.page,
            "x": self.x,
            "y": self.y,
            "region": self.region,
            "status": self.status,
            "task_instruction": self.task_instruction,
            "run_id": self.run_id,
        }
