"""存储工具：结果入库 / 数据导出 / 数据查询

全走 result_store 和 thread_store，不再有 task_service。
task_id 即 LangGraph thread_id。
"""
import logging
import os
import time

from app.core.event_bus import get_ctx
from app.core.events import EVENT_ITEMS_BATCH
from app.services import result_store, thread_store

logger = logging.getLogger(__name__)


async def save_items(items: list[dict], page_no: int = 1, task_id: str = "") -> dict:
    """保存条目；task_id 缺省时自动从运行上下文获取，Agent 无需感知任务 ID"""
    if not task_id:
        task_id = get_ctx().task_id
    inserted = await result_store.save_results(task_id, items, page_no)
    ctx = get_ctx()
    await ctx.bus.emit(EVENT_ITEMS_BATCH, items=items, count=len(items))
    logger.info("storage_tools.save_items task_id=%s page=%d count=%d", task_id, page_no, inserted)
    return {"saved": inserted}


async def get_task(task_id: str = "") -> dict:
    """读取任务信息（简化版：只返回 task_id + 数据条数）

    对话历史全在 LangGraph checkpoint 里，前端通过 GET /messages 读。
    """
    if not task_id:
        task_id = get_ctx().task_id
    count = await result_store.count_results(task_id)
    return {"task_id": task_id, "total_items": count}


async def list_items(task_id: str = "", limit: int = 200) -> dict:
    """读取已保存的数据条目（从数据库）。

    当用户说"数据呢？"、"看看爬了什么"、"有没有数据"时调用。
    """
    if not task_id:
        task_id = get_ctx().task_id
    items = await result_store.get_results(task_id, limit=limit)
    fields: list[str] = []
    seen: set[str] = set()
    for row in items:
        for k in row["data"].keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    total = await result_store.count_results(task_id)
    return {"task_id": task_id, "count": len(items), "total": total, "fields": fields,
            "items": items[:limit]}


async def export_data(format: str = "csv", task_id: str = "") -> dict:
    """把已保存的数据导出成文件（csv/excel/json/markdown），返回文件路径。

    参数:
        format: 导出格式，支持 csv / excel / json / markdown / pdf / docx
        task_id: 要导出哪个任务的数据；缺省时取当前任务
    """
    from app.services import export_service

    if not task_id:
        task_id = get_ctx().task_id

    fmt = (format or "csv").lower().replace("xlsx", "excel")
    fmt_map = {"excel": "excel", "csv": "csv", "json": "json", "markdown": "md", "md": "md",
               "pdf": "pdf", "docx": "docx", "doc": "docx"}
    fmt = fmt_map.get(fmt, fmt)

    items = await result_store.get_results(task_id)
    if not items:
        return {"error": f"任务 {task_id} 没有数据可以导出", "count": 0}

    try:
        data_bytes, filename, mime = await export_service.export(task_id, fmt)
    except Exception as e:  # noqa: BLE001
        logger.error("export_data 失败 task_id=%s fmt=%s: %s", task_id, fmt, e)
        return {"error": f"导出失败: {e!s}", "count": len(items)}

    out_dir = os.path.join("artifacts", "exports", task_id)
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = f"{ts}_{filename}"
    out_path = os.path.join(out_dir, safe_name)
    with open(out_path, "wb") as f:
        f.write(data_bytes)

    ctx = get_ctx()
    await ctx.bus.emit("export_done", task_id=task_id, filename=safe_name, path=out_path, count=len(items), fmt=fmt)
    logger.info("export_data task_id=%s fmt=%s path=%s count=%d", task_id, fmt, out_path, len(items))

    return {"task_id": task_id, "format": fmt, "filename": safe_name, "path": out_path, "count": len(items),
            "mime": mime, "message": f"✅ 已导出 {len(items)} 条数据为 {fmt} 格式：{safe_name}"}


async def list_tasks(limit: int = 10) -> dict:
    """列出最近的任务历史（从 LangGraph checkpoints 反向提取）

    当用户说"导出刚才的数据"、"历史记录"时调用。
    返回 task_id 列表——大模型可以告诉用户 "你可以说「导出 xxx」来导出刚才的任务"。
    """
    threads = await thread_store.list_threads(limit=limit)
    return {"tasks": [{"task_id": t["task_id"]} for t in threads], "count": len(threads)}
