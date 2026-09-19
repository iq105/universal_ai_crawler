"""工具层单元测试（AGENT.md #50：工具必须有单测）。

覆盖：
- 全部 @tool 工具可生成 pydantic schema（回归 langchain-core #35931：runtime 参数导致的 schema 崩溃）
- runtime 注入型工具被正确识别为 _injected_args_keys
- 面向模型的 schema（convert_to_openai_tool）不得暴露 runtime 字段
"""
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.tools import get_agent_tools

# 使用 ToolRuntime 注入运行上下文的工具（不应出现在模型可见参数里）
RUNTIME_INJECTED = {"llm_extract", "save_items", "get_task", "list_items", "export_data", "smart_extract"}


def _tools_by_name():
    return {t.name: t for t in get_agent_tools()}


def test_tool_count_and_unique_names():
    tools = get_agent_tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"工具名重复：{names}"
    assert len(tools) >= 24, f"工具数量异常：{len(tools)}"


def test_all_tool_schemas_generate():
    failures = []
    for tool in get_agent_tools():
        try:
            tool.args_schema.model_json_schema()
        except Exception as exc:  # noqa: BLE001
            failures.append((tool.name, repr(exc)))
    assert not failures, f"以下工具 schema 生成失败：{failures}"


def test_runtime_injected_tools_detected():
    tools = _tools_by_name()
    for name in RUNTIME_INJECTED:
        assert name in tools, f"缺少 runtime 注入工具：{name}"
        keys = tools[name]._injected_args_keys
        assert "runtime" in keys, f"{name} 未识别 runtime 注入：{keys}"


def test_model_facing_schema_excludes_runtime():
    tools = _tools_by_name()
    for name in RUNTIME_INJECTED:
        schema = convert_to_openai_tool(tools[name])
        props = schema["function"]["parameters"].get("properties", {})
        assert "runtime" not in props, f"{name} 的模型可见 schema 泄漏了 runtime 字段：{props}"


def test_plain_tools_schema_ok():
    tools = _tools_by_name()
    schema = convert_to_openai_tool(tools["open_page"])
    params = schema["function"]["parameters"]
    assert "url" in params["properties"]
    assert "url" in params.get("required", [])