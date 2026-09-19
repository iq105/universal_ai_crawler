"""crawler_graph 状态机单元测试（AGENT.md #50：状态机必须有单测）。

覆盖：
- 图结构包含 pipeline 全部节点与 agent 子图节点
- initial_state_for 按模式正确构造初态（pipeline 走 instruction 字段，agent/chat 走 messages）
- 路由函数在关键状态下的走向
"""
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from app.services import crawler_graph


PIPELINE_NODES = {
    "intent_understand", "page_load", "page_analyze", "scheme_generate",
    "data_extract", "pagination", "data_validate", "human_intervene", "store",
}


class _FakeLLM:
    def bind_tools(self, tools):
        return self

    def with_config(self, *args, **kwargs):
        return self


def test_build_graph_contains_all_nodes():
    from app.services import agent_builder

    agent_builder._agent_instance = None
    with patch("app.services.agent_builder.get_chat_model", return_value=_FakeLLM()):
        graph = crawler_graph.build_graph(InMemorySaver())
    names = set(graph.get_graph().nodes.keys())
    assert PIPELINE_NODES.issubset(names), f"缺少节点：{PIPELINE_NODES - names}"
    assert "agent" in names, "缺少 agent 子图节点"
    agent_builder._agent_instance = None


class _Task:
    id = "t-1"
    url = "https://example.com"
    instruction = "抓取标题"
    max_pages = 3
    status = "pending"


async def test_initial_state_for_pipeline_puts_instruction_in_state():
    st = await crawler_graph.initial_state_for(_Task(), graph_mode="pipeline")
    assert st["graph_mode"] == "pipeline"
    assert st["instruction"] == "抓取标题"
    assert st["url"] == "https://example.com"
    assert not st.get("messages"), "pipeline 模式不应注入 messages"


async def test_initial_state_for_agent_uses_user_message():
    st = await crawler_graph.initial_state_for(_Task(), graph_mode="agent", user_message="你好")
    assert st["graph_mode"] == "agent"
    msgs = st.get("messages") or []
    assert msgs and msgs[-1]["content"] == "你好", f"agent 模式应把 user_message 放入 messages：{msgs}"


def test_route_after_intent():
    assert crawler_graph.route_after_intent({"needs_login": True}) == "human_intervene"
    assert crawler_graph.route_after_intent({"needs_login": False}) == "page_load"


def test_route_after_load():
    assert crawler_graph.route_after_load({"needs_intervene": True}) == "human_intervene"
    assert crawler_graph.route_after_load({"needs_intervene": False}) == "page_analyze"


def test_route_after_extract():
    assert crawler_graph.route_after_extract({"needs_intervene": True}) == "human_intervene"
    assert crawler_graph.route_after_extract({"needs_rescheme": True}) == "scheme_generate"
    assert crawler_graph.route_after_extract({}) == "pagination"


def test_route_after_pagination():
    assert crawler_graph.route_after_pagination({"pagination_done": True}) == "data_validate"
    assert crawler_graph.route_after_pagination({"pagination_done": False}) == "data_extract"