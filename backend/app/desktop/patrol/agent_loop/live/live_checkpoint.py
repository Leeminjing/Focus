"""本文件对外提供 LoopLiveCheckpointStore。

输入为只读会话、Loop identity、sequence 边界与未脱敏的 reducer 投影；输出为有效缓存或单调检查点写入。
具体工作流为校验格式、identity 与边界，独立事务通过条件 upsert 保存有界投影；相同边界允许修复损坏缓存。
示例：`await LoopLiveCheckpointStore().load(session, loop_id, boundary)`。"""

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from backend.app.desktop.patrol.agent_loop.journal.journal_models import LoopLiveCheckpoint
from backend.app.desktop.patrol.agent_loop.projections.live_projection_contract import LoopLiveProjection


class LoopLiveCheckpointStore:
    VERSION = 1

    async def load(self, session, loop_id: str, boundary: int) -> LoopLiveProjection | None:
        row = await session.get(LoopLiveCheckpoint, loop_id)
        if row is None or row.schema_version != self.VERSION or row.last_sequence > boundary:
            return None
        try:
            projection = LoopLiveProjection.model_validate(row.projection)
        except ValidationError:
            return None
        if projection.loop_id != loop_id or projection.last_sequence != row.last_sequence:
            return None
        return projection

    async def save(self, session, projection: LoopLiveProjection) -> None:
        statement = insert(LoopLiveCheckpoint).values(
            loop_id=projection.loop_id, schema_version=self.VERSION,
            last_sequence=projection.last_sequence, projection=projection.model_dump(mode="json"),
        )
        await session.execute(statement.on_conflict_do_update(
            index_elements=["loop_id"],
            set_={"schema_version": self.VERSION, "last_sequence": statement.excluded.last_sequence,
                  "projection": statement.excluded.projection, "updated_at": func.now()},
            where=LoopLiveCheckpoint.last_sequence <= statement.excluded.last_sequence,
        ))
