"""agent_builder 单元测试（AGENT.md #1：LLM 统一封装 / #50：状态机核心可测）。

覆盖：
- agent_builder 复用 core.llm.get_chat_model（禁止业务代码直连 ChatOpenAI）
- 测试挂点 app.services.agent_builder.get_chat_model 仍可被 patch
- build_agent 在注入假模型后可编译出模型/工具节点
"""
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from app.core import llm
from app.services import agent_builder


class FakeLLM:
    """最小可控模型：不发起任何网络调用，直接给终态文本。"""

    def bind_tools(self, tools, **kwargs):
        return self

    def with_config(self, *args, **kwargs):
        return self


def test_get_chat_model_is_core_function():
    assert agent_builder.get_chat_model is llm.get_chat_model


def test_system_prompt_comes_from_prompts_module():
    from app.prompts import AGENT_SYSTEM_PROMPT

    assert agent_builder.SYSTEM_PROMPT == AGENT_SYSTEM_PROMPT
    assert "万能爬虫智能体" in agent_builder.SYSTEM_PROMPT


async def test_build_agent_compiles_with_patched_model():
    agent_builder._agent_instance = None
    with patch("app.services.agent_builder.get_chat_model", return_value=FakeLLM()):
        agent = agent_builder.build_agent(InMemorySaver())
    assert "model" in agent.nodes
    assert "tools" in agent.nodes
    agent_builder._agent_instance = None