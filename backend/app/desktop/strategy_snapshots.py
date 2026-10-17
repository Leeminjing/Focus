"""按内容 hash 保存不可变策略正文；读取只接受本轮冻结目录，不查询当前安装。

输入为完整冻结快照、目录与分页请求；输出为经 hash/目录校验的 UTF-8 正文页。
快照不依赖 Memory 外键，编辑和删除安装不会破坏历史读取。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, mapped_column

from focus.history import content_hash
from focus.persistence.base import Base
from backend.app.desktop.evolution_strategies import strategy_catalog


class FrozenStrategySnapshot(Base):
    __tablename__ = "frozen_strategy_snapshots"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PatrolStrategyReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["strategy"] = "strategy"
    memory_id: str = Field(min_length=1, max_length=120)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    section: Literal["evolution", "inheritance"] = "evolution"
    cursor: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=8192, ge=256, le=16384)


async def save_strategy_snapshots(session, snapshots):
    if snapshots:
        await session.execute(insert(FrozenStrategySnapshot).values([
            {"content_hash": item["content_hash"], "payload": item} for item in snapshots
        ]).on_conflict_do_nothing(index_elements=["content_hash"]))


async def read_strategy_page(session, strategies, request):
    frozen = next((item for item in strategies if item.get("memory_id") == request.memory_id
                   and item.get("content_hash") == request.content_hash), None)
    if frozen is None:
        raise ValueError("策略读取超出冻结目录 identity/hash")
    if "strategy" in frozen:  # Historical Observation already contains immutable text.
        snapshot = frozen
    else:
        row = await session.get(FrozenStrategySnapshot, request.content_hash)
        if row is None:
            raise ValueError("冻结策略正文缺失")
        snapshot = row.payload
    body = {key: value for key, value in snapshot.items() if key != "content_hash"}
    if snapshot.get("memory_id") != request.memory_id or content_hash(body) != request.content_hash:
        raise ValueError("冻结策略正文 hash 不匹配")
    if strategy_catalog((snapshot,))[0] != strategy_catalog((frozen,))[0]:
        raise ValueError("冻结策略目录与正文不匹配")
    encoded = snapshot["strategy"][request.section].encode("utf-8")
    if request.cursor > len(encoded):
        raise ValueError("策略游标超出冻结正文")
    try:
        encoded[:request.cursor].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("策略游标不在 UTF-8 边界") from exc
    content = encoded[request.cursor:request.cursor + request.max_bytes].decode("utf-8", errors="ignore")
    end = request.cursor + len(content.encode("utf-8"))
    return {"source": "strategy", "memory_id": request.memory_id, "content_hash": request.content_hash,
            "section": request.section, "cursor": request.cursor, "total_bytes": len(encoded),
            "next_cursor": end if end < len(encoded) else None, "content": content}
