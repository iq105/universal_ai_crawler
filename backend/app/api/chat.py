"""对话式爬取 API：/api/chat

用户以聊天形式下达爬取任务：
- 有 task_id（续聊）→ 恢复/指挥已有 LangGraph checkpoint
- 无 task_id（新任务）→ 用 uuid4 当 thread_id，graph.astream 自动存 checkpoint
返回 task_id，前端再通过 GET /api/tasks/{id}/stream 订阅事件流。

不再走 task_service.create_task / task_service.get_task——
LangGraph checkpointer 管对话，result_store 管爬取数据，thread_store 管历史列表。
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.events import EVENT_CHAT_ACK
from app.core.run_manager import run_manager
from app.core.steering import steering
from app.services.agent_builder import get_agent, initial_state_for

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatIn(BaseModel):
    message: str = Field(..., description="用户消息/指令（可包含目标网站名称，如'京东'或完整URL）")
    task_id: str | None = Field(None, description="已有任务 ID（续聊/指挥）")


@router.post("")
async def chat(body: ChatIn):
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    agent = await get_agent()

    # ── 已有 thread_id：续聊 / 指挥 / 恢复 ──
    # 全部交给 run_manager.input（统一入口）处理，它内部已经覆盖：
    #   - 内存 run 正在跑且未暂停 → steering 注入
    #   - checkpoint 有 pending interrupt → resume Command
    #   - checkpoint 有历史已终态 → continue（加新 HumanMessage）
    #   - 全新 → start
    # 严禁 chat 层再做 is_running / steering 分支：
    #   is_running() 不检查 paused 状态，暂停中的 run 会被误判为"运行中"
    #   → steering.push 没人消费 → 消息石沉大海
    if body.task_id:
        tid = body.task_id
        config = {"configurable": {"thread_id": tid}}
        bus = await run_manager.input(tid, agent, config, message)
        logger.info("chat → run_manager.input task_id=%s", tid)
        return {"task_id": tid, "ack": "消息已发送，正在执行…", "created": False}

    # ── 新任务：生成 thread_id，让 Agent 自己从消息里提取目标网站 ──
    # 关键变化：不再由 chat 层预解析 URL（以前用 resolve_target），而是交给 Agent 自己：
    #   Agent system prompt 第一条就是「先 open_page 打开目标页面」
    #   Agent 从用户消息（"帮我抓京东机械键盘" / "抓取 https://item.jd.com/123.html"）
    #   自己推理出正确 URL → 调 open_page → 开始工作
    #   这样做的好处：减少一次前置 LLM 调用（resolve_target 也是 llm_json 推断），
    #   避免 chat 层推断出的 URL 和 Agent 自己推断的 URL 不一致
    tid = uuid.uuid4().hex.replace("-", "")[:32]
    config = {"configurable": {"thread_id": tid}}

    init_state = await initial_state_for(url="", user_message=message)
    bus = await run_manager.start(tid, agent, config, initial_state=init_state)
    await steering.push(tid, message)  # 首条指令让 agent 首轮消费
    await bus.emit(EVENT_CHAT_ACK, text="任务已创建并开始执行，请稍候…")
    logger.info("chat → 新任务 task_id=%s", tid)
    return {"task_id": tid, "ack": "任务已创建并开始执行", "created": True}
