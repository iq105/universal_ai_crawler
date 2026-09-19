"""run_manager 事件流集成测试（AGENT.md #50：状态机/运行管理器核心路径）。

用假模型驱动 agent 模式全链路（不依赖浏览器/真实 LLM）：
- run_manager.start 后事件总线收到 _graph_end
- 任务终态写回数据库为 success
- 完成后 run_manager.is_finished 为真
"""
import asyncio
from unittest.mock import patch

from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from app.core.run_manager import run_manager
from app.services import task_service


class FakeLLM:
    """首轮直接给「完成」语义终态，不调用任何工具，图自然收敛。"""

    def bind_tools(self, tools, **kwargs):
        return self

    def with_config(self, *args, **kwargs):
        return self

    async def ainvoke(self, *args, **kwargs):
        return AIMessage(content="完成：抓取任务总结，共 0 条。")

    async def astream(self, *args, **kwargs):
        yield AIMessageChunk(content="完成：抓取任务总结，共 0 条。")


async def _drain(bus, task_id, timeout=30.0):
    """收集事件直到 _graph_end / _graph_error 或任务落地。"""
    events = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        ev = await bus.get(timeout=1.0)
        if ev is None:
            if await run_manager.is_finished(task_id):
                break
            continue
        events.append(ev)
        if ev.get("type") in ("_graph_end", "_graph_error"):
            break
    return events


async def test_run_manager_agent_flow_end_to_end():
    from app.services import agent_builder, crawler_graph

    task = await task_service.create_task("https://example.com", instruction="抓取标题")

    agent_builder._agent_instance = None
    with patch("app.services.agent_builder.get_chat_model", return_value=FakeLLM()):
        graph = crawler_graph.build_graph(InMemorySaver())

    config = {"configurable": {"thread_id": task.id}}
    state = await crawler_graph.initial_state_for(task, graph_mode="agent", user_message="抓取标题")
    bus = await run_manager.start(task.id, graph, config, initial_state=state)

    events = await _drain(bus, task.id)
    types = [e.get("type") for e in events]
    assert "_graph_end" in types, f"未收到 _graph_end：{types}"

    # _graph_end 先于 finally 中的落库，等 run 真正结束再读库
    deadline = asyncio.get_event_loop().time() + 10.0
    while not await run_manager.is_finished(task.id):
        assert asyncio.get_event_loop().time() < deadline, "run 未在超时内结束"
        await asyncio.sleep(0.05)

    saved = await task_service.get_task(task.id)
    assert saved is not None
    assert saved.status == "success", f"任务终态应为 success，实际 {saved.status}"
    assert await run_manager.is_finished(task.id)

    await run_manager.remove(task.id)
    agent_builder._agent_instance = None