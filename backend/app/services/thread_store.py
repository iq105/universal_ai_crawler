"""thread_store.py：从 LangGraph checkpoints 表反向提取历史对话

LangGraph 的 AsyncSqliteSaver 会自动建 checkpoints / writes 表，
对话历史全在里面。我们的任务列表（侧边栏）、对话恢复（刷新页面）
都从这里读，不再单独存 task / messages 表。

只读，不写——写全靠 LangGraph checkpointer。
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.db import get_session

logger = logging.getLogger(__name__)


async def list_threads(limit: int = 50) -> list[dict]:
    """列出所有有 checkpoint 的 thread_id（即"历史任务"）

    按最新 checkpoint 的 rowid 降序——rowid 是 SQLite 的写入顺序，
    越新的 checkpoint rowid 越大，所以最新的对话排最前。
    """
    sql = text("""
        SELECT thread_id, MAX(rowid) as last_row
        FROM checkpoints
        GROUP BY thread_id
        ORDER BY last_row DESC
        LIMIT :limit
    """)
    async with get_session() as session:
        rows = (await session.execute(sql, {"limit": limit})).fetchall()
    threads = []
    for r in rows:
        threads.append({
            "task_id": r[0],     # thread_id
            "last_row": r[1],    # 最新 checkpoint 的 rowid（排序列）
        })
    logger.info("list_threads 返回 %d 条", len(threads))
    return threads


async def get_thread_messages(graph, thread_id: str) -> list[dict]:
    """用 LangGraph 自己的 API 读出完整对话历史

    graph.aget_state 会从 checkpointer 里找 thread_id 对应的最新 checkpoint，
    返回 state.values["messages"]，其中每个 message 是 LangChain 的 BaseMessage。
    """
    import time
    t0 = time.time()
    config = {"configurable": {"thread_id": thread_id}}
    try:
        st = await graph.aget_state(config)
    except Exception as exc:
        logger.warning("get_thread_messages thread_id=%s graph.aget_state 失败: %s", thread_id, exc)
        return []

    vals = (st.values or {})
    raw_msgs = vals.get("messages", []) or []
    out: list[dict] = []
    for m in raw_msgs:
        role = getattr(m, "type", "") or getattr(m, "role", "")
        content = getattr(m, "content", "")
        if role in ("human", "HumanMessage"):
            out.append({"role": "user", "content": str(content)})
        elif role in ("ai", "assistant", "AIMessage", "AIMessageChunk"):
            # 过滤掉 tool_calls-only（无文本）的 AIMessage
            text = str(content or "").strip()
            if text:
                out.append({"role": "assistant", "content": text})
        # tool / system 消息不回传前端
    dt = time.time() - t0
    logger.info("get_thread_messages thread_id=%s count=%d %.2fs", thread_id, len(out), dt)
    return out


async def get_first_user_message(graph, thread_id: str) -> str:
    """从 checkpoint 中提取首条 user 消息当侧边栏标题"""
    msgs = await get_thread_messages(graph, thread_id)
    for m in msgs:
        if m["role"] == "user":
            text = m["content"].strip().replace("\n", " ")
            return text[:60] + ("…" if len(text) > 60 else "")
    return "(空对话)"


async def delete_thread(thread_id: str, graph=None) -> dict:
    """彻底删除一个 thread：LangGraph checkpoint + writes + crawl_results

    先手动 SQL 删（最可靠，SQLAlchemy 连接直接操作 SQLite 文件），
    再用 LangGraph 自己的 checkpointer.adelete_thread 再删一遍（双保险）。
    """
    import logging as _logging
    _log = _logging.getLogger("delete_thread")

    # 1. 手动 SQL 删 checkpoints + writes（最可靠，直接操作文件）
    checkpoint_ok = False
    try:
        async with get_session() as session:
            await session.execute(text("DELETE FROM writes WHERE thread_id = :tid"), {"tid": thread_id})
            await session.execute(text("DELETE FROM checkpoints WHERE thread_id = :tid"), {"tid": thread_id})
            await session.commit()
        checkpoint_ok = True
        _log.info("delete_thread checkpoint 手动 SQL 已删 thread_id=%s", thread_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("delete_thread checkpoint 手动 SQL 失败: %s", exc)

    # 2. LangGraph checkpointer 双保险（正确签名：直接传字符串，不是 dict）
    if graph is not None and hasattr(graph, "checkpointer") and graph.checkpointer is not None:
        try:
            await graph.checkpointer.adelete_thread(thread_id)
            _log.info("delete_thread checkpointer.adelete_thread 也跑了 thread_id=%s", thread_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("delete_thread checkpointer.adelete_thread 失败（没关系，手动 SQL 已经删了）: %s", exc)

    # 3. 删爬取数据
    try:
        from app.models import CrawlResult
        from sqlalchemy import delete as _del
        async with get_session() as session:
            result = await session.execute(_del(CrawlResult).where(CrawlResult.task_id == thread_id))
            await session.commit()
            deleted = result.rowcount or 0
        _log.info("delete_thread crawl_results 已删 thread_id=%s count=%d", thread_id, deleted)
    except Exception as exc:  # noqa: BLE001
        _log.error("delete_thread crawl_results 删除失败: %s", exc)
        deleted = 0

    return {"ok": True, "checkpoint_deleted": checkpoint_ok, "results_deleted": deleted}
