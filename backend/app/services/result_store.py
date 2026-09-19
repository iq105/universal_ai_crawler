"""result_store.py：爬取数据的唯一 CRUD 入口

只管 crawl_results 一张表。对话历史全在 LangGraph checkpoint 里，
我们不存 messages，不存 tasks，不存 logs。
"""
import logging
from sqlalchemy import func, select
from app.core.db import get_session
from app.models import CrawlResult

logger = logging.getLogger(__name__)


async def save_results(task_id: str, items: list[dict], page_no: int = 1) -> int:
    """批量入库，返回实际插入条数（自动去重）"""
    if not items:
        return 0
    # === 去重：同 task_id 下，优先按 url 去重；没有 url 则按 title 去重 ===
    url_key = "url"
    title_key = "title"
    has_any_key = any(url_key in it or title_key in it for it in items)
    existing_keys: set[str] = set()
    if has_any_key:
        async with get_session() as sess:
            existing = await sess.scalars(
                select(CrawlResult.data).where(CrawlResult.task_id == task_id))
            for data in existing.all():
                if isinstance(data, dict):
                    if data.get(url_key):
                        existing_keys.add(str(data[url_key]).strip())
                    elif data.get(title_key):
                        existing_keys.add(str(data[title_key]).strip())

    clean = []
    skipped = 0
    for i, it in enumerate(items):
        data = {k: v for k, v in it.items() if not str(k).startswith("_")}
        data.pop("page_no", None)

        # 去重检查
        if has_any_key:
            key_val = None
            if url_key in data and data[url_key]:
                key_val = str(data[url_key]).strip()
            elif title_key in data and data[title_key]:
                key_val = str(data[title_key]).strip()
            if key_val and key_val in existing_keys:
                skipped += 1
                continue
            if key_val:
                existing_keys.add(key_val)

        clean.append(CrawlResult(task_id=task_id, item_index=i, page_no=page_no, data=data))

    if not clean:
        logger.info("save_results task_id=%s page=%d 全部跳过（重复）", task_id, page_no)
        return 0

    async with get_session() as session:
        session.add_all(clean)
        await session.commit()
    logger.info("save_results task_id=%s page=%d count=%d skipped_dup=%d", task_id, page_no, len(clean), skipped)
    return len(clean)


async def count_results(task_id: str) -> int:
    async with get_session() as session:
        return int(await session.scalar(
            select(func.count()).select_from(CrawlResult).where(CrawlResult.task_id == task_id)) or 0)


async def get_results(task_id: str, offset: int = 0, limit: int = 100) -> list[dict]:
    async with get_session() as session:
        stmt = (select(CrawlResult).where(CrawlResult.task_id == task_id).order_by(CrawlResult.id.asc()).offset(
            offset).limit(limit))
        rows = await session.execute(stmt)
        out = []
        for r in rows.scalars().all():
            out.append(
                {"id": r.id, "task_id": r.task_id, "item_index": r.item_index, "page_no": r.page_no, "data": r.data,
                    "created_at": r.created_at, })
        return out
