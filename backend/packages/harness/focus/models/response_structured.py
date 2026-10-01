"""本文件对外提供 structured_runnable 的 Responses 输出 schema bridge。

输入为模型、Pydantic/JSON schema、输出方法与 include_raw；输出为兼容既有认知调用的 Runnable。
具体工作流为生成 text.format，completed 后解析 JSON 并校验 schema；解析错误按 include_raw 合同返回或抛出。
示例：await model.with_structured_output(Plan, include_raw=True).ainvoke(messages)。
"""

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_function
from pydantic import BaseModel

from focus.models.response_output import response_text


def structured_runnable(model, schema, method, include_raw, strict, kwargs):
    if method not in {"json_schema", "json_mode", "function_calling"} or schema is None:
        raise ValueError("Responses structured output 需要明确 schema 与受支持方法")
    options = dict(kwargs)
    tools = options.pop("tools", ())
    if options:
        raise ValueError("未支持的 structured output 参数: " + ", ".join(options))
    function = convert_to_openai_function(schema, strict=(strict if strict is not None else True) if method == "json_schema" else None)
    if method == "function_calling":
        runnable = model.bind_tools([schema, *tools], tool_choice=function["name"])
    else:
        format_ = {"type": "json_object"} if method == "json_mode" else {
            "type": "json_schema", "name": function["name"], "schema": function["parameters"], "strict": strict if strict is not None else True,
        }
        if model.provider_contract.provider == "deepseek":
            format_.pop("strict", None)
        runnable = model.bind(text={"format": format_}, tools=list(tools))

    def parse(message):
        try:
            if method == "function_calling":
                calls = [call for call in message.tool_calls if call["name"] == function["name"]]
                if len(calls) != 1:
                    raise ValueError("结构化输出必须返回唯一 schema function")
                value = calls[0]["args"]
            else:
                value = json.loads(response_text(message))
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                parsed = schema.model_validate(value)
            else:
                Draft202012Validator(function["parameters"]).validate(value)
                parsed = value
            return {"raw": message, "parsed": parsed, "parsing_error": None} if include_raw else parsed
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            if include_raw:
                return {"raw": message, "parsed": None, "parsing_error": exc}
            raise
    return runnable | RunnableLambda(parse)
