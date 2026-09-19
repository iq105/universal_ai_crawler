"""运行 API：SSE 启动 / 恢复（人工介入续跑）/ 流订阅 / 取消

不再查 crawl_tasks 表——thread_id 是否存在由 LangGraph checkpointer 决定。
不再 update_status —— LangGraph checkpoint 自动存状态。
不再 add_message —— LangGraph checkpoint 自动存对话。
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.event_bus import RunEventBus
from app.core.events import sse_format
from app.core.run_manager import run_manager
from app.services.agent_builder import get_agent, initial_state_for

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["runs"])


class CancellationManager:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(self, task_id: str) -> None:
        async with self._lock:
            self._events[task_id] = asyncio.Event()

    async def cancel(self, task_id: str) -> None:
        async with self._lock:
            ev = self._events.get(task_id)
            if ev is not None:
                ev.set()

    async def is_cancelled(self, task_id: str) -> bool:
        async with self._lock:
            ev = self._events.get(task_id)
            return bool(ev is not None and ev.is_set())

    async def unregister(self, task_id: str) -> None:
        async with self._lock:
            self._events.pop(task_id, None)


cancellations = CancellationManager()


async def _stream_from_bus(task_id: str, bus: RunEventBus):
    """从运行事件总线消费并转发为 SSE

    所有对话/状态全由 LangGraph checkpoint 自动存，不再手动调 task_service。
    """
    await cancellations.register(task_id)
    interrupted = False
    errored = False
    final_status = "failed"
    logger.info("[SSE] 订阅启动 task_id=%s", task_id)

    try:
        # 重连/晚连接：先重放未消费的中断载荷
        pending_interrupt = await run_manager.get_interrupt(task_id)
        if pending_interrupt:
            logger.info("[SSE] 重连，重放 interrupt task_id=%s", task_id)
            interrupted = True
            yield sse_format({"type": "interrupt", "data": pending_interrupt})

        while True:
            item = await bus.get(timeout=30)
            if item is None:
                if await run_manager.is_finished(task_id):
                    break
                continue  # 保活空转
            typ = item["type"]
            data = item["data"]

            if typ == "_graph_chunk":
                chunk = data["chunk"]
                if "__interrupt__" in chunk:
                    interrupted = True
                    try:
                        payload = chunk["__interrupt__"][0].value
                    except (KeyError, IndexError, TypeError):
                        payload = None
                    yield sse_format({"type": "interrupt", "data": payload})
            elif typ == "_graph_error":
                errored = True
                err = data["error"]
                logger.error("[SSE] _graph_error task_id=%s err=%s", task_id, err)
                yield sse_format({"type": "error", "data": {"message": err}})
                break
            elif typ == "_graph_end":
                break
            else:
                if typ in ("agent_final", "assistant_delta", "interrupt"):
                    pass
                    #logger.info("[SSE] 转发事件 type=%s task_id=%s data=%s", typ, task_id,data)
                yield sse_format({"type": typ, "data": data})

        # 确定最终状态（不再手动 update_status）
        if not interrupted and not errored:
            if await cancellations.is_cancelled(task_id):
                final_status = "cancelled"
            else:
                final_status = "done"  # ✅ 不再判 success/failed——用户自己判断
                try:
                    graph = await get_agent()
                    config = {"configurable": {"thread_id": task_id}}
                    st = await graph.aget_state(config)
                    vals = st.values or {}
                    if vals.get("done"):
                        final_status = "done"
                except Exception as exc:
                    logger.warning("[SSE] 终态查询失败 task_id=%s: %s", task_id, exc)
            yield sse_format({"type": "task_status", "data": {"status": final_status}})
        elif interrupted:
            final_status = "waiting_interrupt"
        elif errored:
            final_status = "failed"

        logger.info("[SSE] 流结束 task_id=%s final=%s", task_id, final_status)
        yield sse_format({"type": "done", "data": {"status": final_status}})
    finally:
        await cancellations.unregister(task_id)


@router.post("/{task_id}/run")
async def run_task(task_id: str):
    graph = await get_agent()
    config = {"configurable": {"thread_id": task_id}}
    # 全新 run（续跑已有 checkpoint 用 resume）
    init_state = await initial_state_for()
    bus = await run_manager.start(task_id, graph, config, initial_state=init_state)
    logger.info("[run] task_id=%s", task_id)
    return StreamingResponse(_stream_from_bus(task_id, bus), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}, )


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, body: dict | None = None):
    graph = await get_agent()
    config = {"configurable": {"thread_id": task_id}}
    value = (body or {}).get("value") if isinstance(body, dict) else body
    # body 为空或没有 value → 默认给"继续"（interrupt 恢复）
    if not value:
        value = "继续"
    bus = await run_manager.start(task_id, graph, config, is_resume=True, resume_value=value)
    logger.info("[resume] task_id=%s value=%r", task_id, value)
    return StreamingResponse(_stream_from_bus(task_id, bus), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}, )


@router.get("/{task_id}/stream")
async def stream_task(task_id: str):
    """挂到已启动运行的 SSE 流（聊天式爬取的前端订阅入口）"""
    bus = await run_manager.get_bus(task_id)
    if bus is None:
        # 任务未运行：直接结束
        logger.info("[stream] 无活跃 run task_id=%s，立即 done", task_id)
        async def _idle():
            yield sse_format({"type": "done", "data": {"status": "idle"}})

        return StreamingResponse(_idle(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}, )
    return StreamingResponse(_stream_from_bus(task_id, bus), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}, )


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    await cancellations.cancel(task_id)
    logger.info("[cancel] task_id=%s", task_id)
    return {"ok": True}


@router.post("/{task_id}/pause")
async def pause_task(task_id: str):
    paused = await run_manager.pause(task_id)
    if paused:
        logger.info("[pause] 成功 task_id=%s", task_id)
        return {"ok": True, "message": "已暂停，处理完后可发送「继续」恢复"}
    return {"ok": False, "message": "任务未在运行中"}
