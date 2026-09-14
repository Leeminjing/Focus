"""本文件对外提供桌面领域的无状态标识生成函数。

输入为空；输出为 32 位十六进制随机标识。具体工作流为调用 UUID4 并去除分隔符，供需要在
DesktopService 之外创建实体的单一职责服务复用，避免反向依赖聚合服务。

示例：material_id = new_desktop_id()。
"""

import uuid


def new_desktop_id() -> str:
    return uuid.uuid4().hex
