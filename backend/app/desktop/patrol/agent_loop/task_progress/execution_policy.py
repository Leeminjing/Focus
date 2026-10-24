"""本文件对外提供 ProgressExecutionPolicy，声明任务记忆的有限执行期限。

输入为请求期限、租约时长、续租间隔和尝试上限；输出为校验后的不可变运行政策。
具体工作流为验证有限正值及续租早于租约过期，分别约束模型请求和耐久领取；不改变 Token 预算或模型窗口。
示例：ProgressExecutionPolicy(request_seconds=900, lease_seconds=90, renew_seconds=30)。
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class ProgressExecutionPolicy:
    request_seconds: float = 900
    lease_seconds: float = 90
    renew_seconds: float = 30
    max_attempts: int = 3

    def __post_init__(self):
        if any(not math.isfinite(value) or value <= 0 for value in
               (self.request_seconds, self.lease_seconds, self.renew_seconds)):
            raise ValueError("任务记忆期限必须为有限正值")
        if self.renew_seconds >= self.lease_seconds or type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("续租间隔必须短于租约，尝试上限必须为正整数")
