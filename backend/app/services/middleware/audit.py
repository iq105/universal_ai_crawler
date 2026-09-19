"""AuditMiddleware：审计日志（替换 registry.execute 里的 tool_start/tool_result 事件推送）

原逻辑位于 core/registry.py:42-56（pipeline 用），agent 模式改由本中间件钩子实现：
- awrap_tool_call：工具调用前后推送 tool_start / tool_result 事件（替换 agent_nodes.agent_act 审计）
- aafter_model：模型输出后记录最后一次回复（辅助）
- aafter_agent：Agent 完成后的汇总日志
"""
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest

from app.core.events import EVENT_TOOL_RESULT, EVENT_TOOL_START
from app.core.logger import get_logger

_log = get_logger("middleware.audit")


class AuditMiddleware(AgentMiddleware):
    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        ctx = request.runtime.context
        name = request.tool_call.get("name") or ""
        args = request.tool_call.get("args") or {}
        tool_call_id = request.tool_call.get("id") or ""
        if ctx.bus is not None:
            await ctx.bus.emit(EVENT_TOOL_START, id=tool_call_id, name=name, arguments=args)
        _log.info("[audit] tool_start %s args=%.300s", name, args)
        try:
            result = await handler(request)
        except Exception as exc:  # noqa: BLE001
            if ctx.bus is not None:
                await ctx.bus.emit(EVENT_TOOL_RESULT, id=tool_call_id, name=name, error=str(exc))
            _log.warning("[audit] tool_error %s: %s", name, exc)
            raise
        # result 通常是 ToolMessage/Command，提取可读内容
        content = _result_text(result)
        if ctx.bus is not None:
            await ctx.bus.emit(EVENT_TOOL_RESULT, id=tool_call_id, name=name, content=content)
        _log.info("[audit] tool_result %s -> %.200s", name, content)
        return result
    # 模型调用完成，agent结束完成之后才打印和显示
    async def aafter_agent(self, state: Any, runtime: Any) -> None:
        task_id = runtime.context.task_id
        last = (state.get("messages") or [])[-1:] or [None]
        text = getattr(last[0], "content", "") if last[0] else ""
        if isinstance(text, str):
            text = text[:200]
        _log.info("[audit] agent_done task_id=%s last=%.200s", task_id, text)
        return None


def _result_text(result: Any) -> str:
    """从 ToolNode 返回值提取可展示文本（ToolMessage 或 Command.update）"""
    try:
        from langchain_core.messages import ToolMessage
        if isinstance(result, ToolMessage):
            content = result.content
            return content if isinstance(content, str) else str(content)
    except Exception:  # noqa: BLE001
        pass
    try:
        from langgraph.types import Command
        if isinstance(result, Command):
            return str(result.update)
    except Exception:  # noqa: BLE001
        pass
    return str(result)