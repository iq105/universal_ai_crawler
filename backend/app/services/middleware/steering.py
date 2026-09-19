"""SteeringMiddleware：运行中指挥消息注入（替换 agent_nodes.agent_think 的 drain）

原逻辑在 agent_nodes.py:117-130：
- 每轮把 steering 队列中的用户新指令作为 user 消息注入 Agent 对话
- 首轮（messages 为空）注入任务描述（url + instruction）

此处用 abefore_model 在每次模型调用前执行相同逻辑。
"""
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage

from app.core.events import EVENT_TASK_LOG
from app.core.logger import get_logger

_log = get_logger("middleware.steering")


class SteeringMiddleware(AgentMiddleware):
    async def abefore_agent(self, state: Any, runtime: Any) -> dict | None:
        """首轮注入任务描述（messages 为空时）"""
        messages = list(state.get("messages") or [])
        if messages:
            return None
        ctx = runtime.context
        url = state.get("url") or ""
        instruction = state.get("instruction") or ""
        if url:
            content = f"任务目标 URL：{url}\n用户指令：{instruction}"
        else:
            content = (f"用户想获取：{instruction or '（未给出具体网址）请自行决定要访问的站点/URL'}\n"
                "（若用户提到知名站点可直接用 open_page 打开其主页；否则可先用搜索引擎找到合适目标，再继续后续步骤。）")
        _log.info("[steering] 首轮注入任务描述 task_id=%s", ctx.task_id)
        return {"messages": [HumanMessage(content=content)]}

    async def abefore_model(self, state: Any, runtime: Any) -> dict | None:
        """每轮消费运行中指挥消息"""
        ctx = runtime.context
        if ctx.steering is None:
            return None
        msgs = await ctx.steering.drain(ctx.task_id)
        if not msgs:
            return None
        added = [HumanMessage(content=m) for m in msgs]
        if ctx.bus is not None:
            for m in msgs:
                await ctx.bus.emit(EVENT_TASK_LOG, level="info", source="chat", message=f"收到新指令：{m}")
        _log.info("[steering] 注入指令 %d 条 task_id=%s", len(msgs), ctx.task_id)
        # messages 用 add_messages reducer，返回新消息列表即可追加
        return {"messages": added}