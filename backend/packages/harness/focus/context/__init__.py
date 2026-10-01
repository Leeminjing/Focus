"""本文件对外提供 Focus execution context 的纯 WorldState 合同。

输入为宿主 sections 与精确 checkpoint 基线；输出为 append-only 更新及可恢复 snapshot。
具体工作流为分别由 world_state 编译、sections 投影与 middleware 持久提交承担，不依赖 Desktop ORM。
示例：from focus.context import WorldSection, compile_world_state。
"""

from focus.context.world_state import RENDERER_VERSION, WorldSection, compile_world_state

__all__ = ["RENDERER_VERSION", "WorldSection", "compile_world_state"]
