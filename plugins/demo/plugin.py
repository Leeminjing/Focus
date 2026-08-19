from langchain_core.tools import tool

from focus.plugins.schemas import PluginDeclaration


@tool
def demo_echo(text: str) -> str:
    """Echo the given text back. Demo plugin tool."""
    return f"demo-echo: {text}"


async def before_model(state, runtime):
    return {"title": state.get("title")}


def after_model(state, runtime):
    return None


def build_plugin(context):
    return PluginDeclaration(
        tools=[demo_echo],
        hooks={"hook.before_model": [before_model], "hook.after_model": [after_model]},
    )
