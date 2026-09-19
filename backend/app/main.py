"""FastAPI 入口"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import chat, export, runs, tasks
from app.browser.browser_manager import browser_manager
from app.config import settings
from app.core.db import close_db, init_db
from app.core.logger import setup_logging
from app.core.trace import new_trace_id, reset_trace_id, set_trace_id

setup_logging()
import app.tools  # noqa: F401  确保启动时完成工具注册


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()
    await browser_manager.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["*"],
    allow_headers=["*"], )


@app.middleware("http")
async def trace_id_middleware(request, call_next):
    """为每个请求分配 trace_id 并贯穿整个调用链（AGENT.md #5）。"""
    trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
    token = set_trace_id(trace_id)
    try:
        response = await call_next(request)
    finally:
        reset_trace_id(token)
    response.headers["X-Trace-Id"] = trace_id
    return response


# --- API 路由 ---
app.include_router(tasks.router)
app.include_router(runs.router)
app.include_router(export.router)
app.include_router(chat.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- 前端静态文件 ---
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
