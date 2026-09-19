"""LangGraph checkpointer：PostgreSQL 优先，SQLite 自动降级

- DATABASE_URL 以 postgres 开头 → AsyncPostgresSaver（断点续跑/人工介入恢复）
- 否则 → AsyncSqliteSaver（开箱即用）
"""
from typing import Any

from app.config import settings


async def get_checkpointer() -> Any:
    url = settings.database_url
    if url.startswith(("postgres", "postgresql")):
        return await _postgres_checkpointer(url)
    return await _sqlite_checkpointer(url)


async def _postgres_checkpointer(url: str) -> Any:
    import psycopg
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    # 把 SQLAlchemy 的 dialect 前缀换成 psycopg 可用的连接串
    conninfo = url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")
    conn = await psycopg.AsyncConnection.connect(conninfo)
    cp = AsyncPostgresSaver(conn)
    await cp.setup()
    return cp


async def _sqlite_checkpointer(url: str) -> Any:
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = url[len("sqlite+aiosqlite:///"):]
    conn = await aiosqlite.connect(path)
    cp = AsyncSqliteSaver(conn)
    await cp.setup()
    return cp
