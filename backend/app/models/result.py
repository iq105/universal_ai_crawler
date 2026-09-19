"""爬取结果表（唯一需要我们自己维护的业务表）

LangGraph 的 checkpointer 表存对话，我们只存爬取的数据。
task_id 即 LangGraph thread_id，无外键约束（因为 crawl_tasks 表已删）。
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# SQLite 对 BIGINT 主键不自增，需退化为 INTEGER
AutoId = BigInteger().with_variant(Integer, "sqlite")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CrawlResult(Base):
    __tablename__ = "crawl_results"

    id: Mapped[int] = mapped_column(AutoId, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)  # LangGraph thread_id
    item_index: Mapped[int] = mapped_column(Integer, default=0)
    page_no: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
