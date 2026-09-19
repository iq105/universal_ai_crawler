"""SQLAlchemy 异步引擎与会话"""
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker, create_async_engine, )
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """建引擎 + 建表（SQLite/PostgreSQL 通用）"""
    global _engine, _session_factory
    _engine = create_async_engine(settings.database_url, echo=False,connect_args={ "check_same_thread" : False })
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncSession:
    if _session_factory is None:
        await init_db()
    async with _session_factory() as session:
        yield session


async def close_db() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
