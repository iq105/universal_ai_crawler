"""任务管理 API

不再查 crawl_tasks 表——历史列表从 LangGraph checkpoints 反向提取，
对话历史从 graph.aget_state() 读取，爬取数据从 result_store 读。
"""
import logging

from fastapi import APIRouter, HTTPException

from app.services import result_store, thread_store
from app.services.agent_builder import get_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(limit: int = 50):
    """侧边栏历史列表：从 LangGraph checkpoints 反向提取 thread_id 列表

    每个 thread_id 对应一次对话。标题 = 首条 user 消息。
    """
    threads = await thread_store.list_threads(limit=limit)
    graph = await get_agent()
    out = []
    for t in threads:
        title = await thread_store.get_first_user_message(graph, t["task_id"])
        result_count = await result_store.count_results(t["task_id"])
        out.append({
            "task_id": t["task_id"],
            "title": title,
            "result_count": result_count,
        })
    logger.info("list_tasks 返回 %d 条", len(out))
    return out


@router.get("/{task_id}/messages")
async def get_messages(task_id: str, limit: int = 500):
    """加载任务的完整对话历史（页面刷新/点击历史任务时使用）

    直接从 LangGraph checkpoint 里读——前端每次刷新都是完整恢复。
    """
    graph = await get_agent()
    msgs = await thread_store.get_thread_messages(graph, task_id)
    if not msgs:
        raise HTTPException(status_code=404, detail="对话不存在或已清空")
    logger.info("get_messages task_id=%s count=%d", task_id, len(msgs))
    return {"task_id": task_id, "messages": msgs, "count": len(msgs)}


@router.get("/{task_id}/results")
async def get_results(task_id: str, offset: int = 0, limit: int = 100):
    """查爬取的数据"""
    limit = min(limit, 500)
    total = await result_store.count_results(task_id)
    items = await result_store.get_results(task_id, offset, limit)
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """彻底删除会话：LangGraph checkpoint + 爬取数据"""
    from app.services.agent_builder import get_agent
    graph = await get_agent()
    result = await thread_store.delete_thread(task_id, graph)
    if result["checkpoint_deleted"]:
        logger.info("delete_task 成功 task_id=%s results_deleted=%d", task_id, result["results_deleted"])
        return {"ok": True, "task_id": task_id, **result}
    logger.error("delete_task checkpoint 删除失败 task_id=%s", task_id)
    raise HTTPException(status_code=500, detail="checkpoint 删除失败，请查看后端日志")
