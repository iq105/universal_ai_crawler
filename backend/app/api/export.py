"""结果导出 API"""
import logging

from fastapi import APIRouter, HTTPException, Response

from app.services import export_service, result_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["export"])


@router.get("/{task_id}/export")
async def export_results(task_id: str, format: str = "csv"):
    if format not in ("csv", "json", "xlsx", "docx", "pdf"):
        raise HTTPException(status_code=400, detail="format 仅支持 csv/json/xlsx/docx/pdf")
    count = await result_store.count_results(task_id)
    if count == 0:
        raise HTTPException(status_code=404, detail="该任务没有爬取数据")
    content, filename, media_type = await export_service.export(task_id, format)
    logger.info("export_results task_id=%s format=%s count=%d", task_id, format, count)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
