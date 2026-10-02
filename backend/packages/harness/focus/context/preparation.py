"""本文件对外提供 AgentRequestPreparation 的只读实际请求预览端口。

输入为统一 Agent 工厂生成的模型、真实工具 catalog、WorldState middleware 和基础行为；输出为准备后的 Responses 请求。
工作流为复用执行时同一 middleware 和 projector，调用不采样、不执行工具、不保存 checkpoint。
示例：preparation.preview(messages, context) 可供 Desktop 显示和完整窗口计量。
fingerprint 绑定目标合同、模型选项、基础行为及真实工具 schema，不含凭据；启动 observer 可核对预览后配置变化。
"""
from dataclasses import dataclass
from dataclasses import asdict
from langchain_core.messages import SystemMessage
from focus.context.scoped import scoped_messages
from focus.models.response_projection import function_specs
from focus.security.context import security_context_of
from focus.history import content_hash


@dataclass(frozen=True)
class AgentRequestPreparation:
    model: object
    tools: tuple
    world_state: object
    instructions: str

    @property
    def fingerprint(self):
        options = {key: getattr(self.model, key, None) for key in
                   ("model_name", "base_url", "temperature", "top_p", "max_tokens", "reasoning_effort", "reasoning", "text")}
        return content_hash([asdict(self.model.provider_contract), options,
                             self.instructions, function_specs(self.tools), self.world_state.preparation_fingerprint])

    def preview(self, messages, context, *, state=None):
        prepared = self.world_state.preview_messages(state or {"messages": messages}, context)
        run_id = security_context_of(context).routing.run_id
        return self.model.request_payload(
            [SystemMessage(self.instructions), *scoped_messages(prepared, run_id)],
            tools=function_specs(self.tools), validate_window=False,
        )
