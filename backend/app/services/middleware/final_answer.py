"""FinalAnswerMiddleware：任务完成判定（替换 agent_nodes.agent_think 的终止判断）

策略：每次模型输出（aafter_model）后检查——若无工具调用且回复符合「完成」语义，
立即把 done/final_answer/status 写入子图状态。create_agent 框架看到无 tool_calls 的
AIMessage 后子图自然结束（不进入 tools 节点），顶层图 / run_manager 依据这些字段判终态。
"""
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from app.core.events import EVENT_CHAT_ACK, EVENT_TASK_LOG
from app.core.logger import get_logger

_log = get_logger("middleware.final_answer")


def _is_completion(text: str) -> bool:
    """判定 LLM 是否已完成任务总结（与 agent_nodes._is_completion 保持一致）"""
    if not text:
        return False
    first = text.lstrip()[:20]
    if first.startswith("完成"):
        return True
    if "完成" in first or "已抓取" in first or "抓取完成" in first:
        return True
    markers = ["抓取任务总结", "任务总结", "抓取来源", "共 **"]
    return any(m in text for m in markers)


class FinalAnswerMiddleware(AgentMiddleware):
    async def aafter_model(self, state: Any, runtime: Any) -> dict | None:
        messages: list = list(state.get("messages") or [])
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage):
            return None
        # 有工具调用 → 继续循环，不判完成
        if getattr(last, "tool_calls", None):
            return None
        content = getattr(last, "content", "") or ""
        if not isinstance(content, str) or not content.strip():
            return None
        stripped = content.strip()
        if not _is_completion(stripped):
            return None
        ctx = runtime.context
        if ctx.bus is not None:
            await ctx.bus.emit(EVENT_CHAT_ACK, text=stripped)
        _log.info("[final_answer] 任务完成判定 task_id=%s text=%.100s", ctx.task_id, stripped)
        return {"done": True, "final_answer": stripped, "status": "success"}


__all__ = ["FinalAnswerMiddleware", "_is_completion"]