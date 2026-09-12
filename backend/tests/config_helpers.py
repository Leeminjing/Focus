"""本文件为 Python 测试提供跨文件复用的构造辅助，避免同一份配置模型与假模型在多处重复拼装。

对外提供:
    app_config_for — 构造只含一个模型条目的 AppConfig
    ToolCapableFakeChatModel — 支持 bind_tools 的脚本化假模型

输入:
    app_config_for: model_name — 模型条目名；supports_image_input — 图像输入能力声明，None 表示不写该字段
    ToolCapableFakeChatModel: scripted — 依次产出的消息；structured_args — 结构化输出工具的调用参数

输出:
    app_config_for → AppConfig
    ToolCapableFakeChatModel → BaseChatModel 实例，记录已绑定工具名与每次收到的消息

具体工作流:
    (1) app_config_for 拼最小可用模型条目（声明为 default），仅在显式传入时写入能力声明
    (2) ToolCapableFakeChatModel.bind_tools 记录工具名并返回自身——GenericFakeChatModel 未实现该方法，
        导致 response_format（ToolStrategy）在假模型下无法测试，故此处自行实现
    (3) 若给出了 structured_args 且存在已绑定工具，则产出一次指向该工具的工具调用；
        否则按 scripted 依次产出普通消息，用尽后重复最后一条

示例:
    config = app_config_for("deepseek-v4-flash", None)
    model = ToolCapableFakeChatModel(scripted=[AIMessage(content="看过了")], structured_args={"images": []})
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel, Field

from focus.config.app_config import AppConfig


def app_config_for(model_name: str, supports_image_input: bool | None) -> AppConfig:
    entry: dict = {
        "name": model_name,
        "display_name": model_name,
        "use": "focus.models.deepseek:DeepSeekChatOpenAI",
        "model": model_name,
        "api_key": "sk-test",
        "base_url": "https://api.deepseek.com",
        "default": True,
    }
    if supports_image_input is not None:
        entry["supports_image_input"] = supports_image_input
    return AppConfig(models=[entry])


class ToolCapableFakeChatModel(BaseChatModel):
    scripted: list[AIMessage] = Field(default_factory=list)
    structured_args: dict[str, Any] | None = None
    received: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tool_names: list[str] = Field(default_factory=list)
    produced: int = 0

    @property
    def _llm_type(self) -> str:
        return "tool-capable-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ToolCapableFakeChatModel":
        names = [
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
            for tool in (tools or [])
            if isinstance(tool, (dict, BaseModel)) or hasattr(tool, "name")
        ]
        self.bound_tool_names = [name for name in names if name]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        message = self._next_message()
        self.produced += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _next_message(self) -> AIMessage:
        if self.structured_args is not None and self.bound_tool_names:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.bound_tool_names[0],
                        "args": dict(self.structured_args),
                        "id": f"call-{self.produced}",
                        "type": "tool_call",
                    }
                ],
            )
        if not self.scripted:
            return AIMessage(content="")
        index = min(self.produced, len(self.scripted) - 1)
        return self.scripted[index]
