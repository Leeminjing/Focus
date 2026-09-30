r"""本文件对外提供隔离演练子进程的索引构建入口。

输入为模式、冻结 Observation JSON 和输出目录；输出为真实提交边界信号、完整 index identity 与实际模型调用数。
工作流为从环境指定的测试数据库新建 sessionmaker，构建或在提交前／后停留，父进程可强杀以验证持久恢复。
示例：python -m backend.tests.incremental_index_process build input.json output-dir。
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.incremental_index_support import ModelProbe


async def main():
    mode, input_path, output_path = sys.argv[1:]
    target = Path(output_path)
    observation = SimpleNamespace(
        **json.loads(Path(input_path).read_text(encoding="utf-8"))
    )
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    probe = ModelProbe()
    service = probe.service(async_sessionmaker(engine, expire_on_commit=False))
    if mode == "before_commit":
        publish = service._publish_one

        async def hold(session, index):
            (target / "prepared.json").write_text(
                json.dumps({"calls": len(probe.calls), "pid": os.getpid()}),
                encoding="utf-8",
            )
            await asyncio.Event().wait()
            return await publish(session, index)

        service._publish_one = hold
    try:
        result = await service.build(observation)
        if result.blocker_code:
            raise RuntimeError(result.blocker_summary)
        index = result.indexes[0]
        (target / "done.json").write_text(
            json.dumps(
                {
                    "index_id": index.index_id,
                    "record_ids": index.inheritance.record_ids,
                    "mode": index.inheritance.mode,
                    "reused": len(index.inheritance.reused_segment_ids),
                    "calls": len(probe.calls),
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        if mode == "after_commit":
            await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
