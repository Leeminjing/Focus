"""Agent tools target only the server-bound browser of the executing Context."""
import json

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool

from focus.security.context import security_context_of
from focus.security.effects import NO_LOCAL_EFFECT, OPAQUE_LOCAL_EFFECT, declare_effect
from .transport import BrowserError


def _run(runtime):
    return security_context_of(runtime.context).extras["browser_run"]


def _error(error):
    # Keep result_unknown and native error codes available to the model.
    return json.dumps({"ok": False, "error": error.payload(), "retry_automatically": False}, ensure_ascii=False)


def build_browser_tools():
    @tool
    async def browser_observe(runtime: ToolRuntime[dict]) -> list:
        """取得当前 Context 浏览器租约并观察真实页面，返回网页截图、CSS 视口、URL 与 observation_id。

        同一租约覆盖观察及后续一段动作；完成后 browser_release 允许共享者接替。
        接管、交还或页面变化后必须重新调用本工具；busy 表示未接受，不能当作排队。
        """
        try:
            from focus.agents.image_inputs import MODEL_IMAGE_INPUT_KEY
            if runtime.context.get(MODEL_IMAGE_INPUT_KEY) is False:
                raise BrowserError("image_model_required", "浏览器观察需要 supports_image_input 的模型；请在模型设置中选择支持图片的模型")
            observation = await _run(runtime).observe()
            metadata = {key: value for key, value in observation.items() if key != "image"}
            metadata["image"] = {key: value for key, value in observation["image"].items() if key != "base64"}
            return [{"type": "text", "text": json.dumps(metadata, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + observation["image"]["base64"]}}]
        except BrowserError as error:
            raise ToolException(_error(error)) from error

    async def act(runtime, observation_id, action, coordinate_space="image"):
        try:
            result = await _run(runtime).act(observation_id, action, coordinate_space)
            return json.dumps(result, ensure_ascii=False)
        except BrowserError as error:
            raise ToolException(_error(error)) from error

    @tool
    async def browser_navigate(url: str, observation_id: str, runtime: ToolRuntime[dict]) -> str:
        """导航当前真实浏览器。须提供最近观察编号，完成或失败后重新观察；禁止自动重放结果未知的动作。"""
        return await act(runtime, observation_id, {"type": "navigate", "url": url})

    @tool
    async def browser_click(x: float, y: float, observation_id: str, runtime: ToolRuntime[dict], coordinate_space: str = "image") -> str:
        """点击最近截图中的位置。默认 image 像素自动换算 CSS 视口；也可明确指定 css_viewport。命令处理不代表业务成功，观察核实。"""
        return await act(runtime, observation_id, {"type": "click", "x": x, "y": y}, coordinate_space)

    @tool
    async def browser_type(text: str, observation_id: str, runtime: ToolRuntime[dict]) -> str:
        """向已点击聚焦的位置输入文字，操作当前 Context 的真实页面；使用最近 observation_id。"""
        return await act(runtime, observation_id, {"type": "text", "text": text})

    @tool
    async def browser_key(key: str, observation_id: str, runtime: ToolRuntime[dict]) -> str:
        """向当前页面发送 Enter、Tab、Escape、Backspace、Delete、方向键、Home、End、PageUp、PageDown 或 Space。"""
        return await act(runtime, observation_id, {"type": "key", "key": key})

    @tool
    async def browser_scroll(x: float, y: float, delta_y: float, observation_id: str, runtime: ToolRuntime[dict], delta_x: float = 0, coordinate_space: str = "image") -> str:
        """在最近截图位置滚动；位置按 image 或 css_viewport 换算，delta 为 CSS 像素。滚动后重新观察再选择目标。"""
        return await act(runtime, observation_id, {"type": "scroll", "x": x, "y": y, "delta_x": delta_x, "delta_y": delta_y}, coordinate_space)

    @tool
    async def browser_release(runtime: ToolRuntime[dict]) -> str:
        """结束一段观察/动作后释放租约，保留原页面并允许其他共享 Context 操作。再次操作必须重新观察。"""
        try:
            return json.dumps(await _run(runtime).release())
        except BrowserError as error:
            raise ToolException(_error(error)) from error

    # A user page can navigate local files and cause external effects. Reuse the
    # existing opaque-tool policy rather than pretending these are file reads.
    tools = [*(declare_effect(item, OPAQUE_LOCAL_EFFECT) for item in (
        browser_observe, browser_navigate, browser_click, browser_type, browser_key, browser_scroll)),
        declare_effect(browser_release, NO_LOCAL_EFFECT)]
    for item in tools:
        item.handle_tool_error = True
    return tools
