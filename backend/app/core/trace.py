"""trace_id 上下文（AGENT.md #5）

每个请求/运行分配唯一 trace_id，通过 contextvar 贯穿
API → Agent → LLM → 工具 → 数据库，日志格式自动携带。

- HTTP 层：中间件从 `X-Trace-Id` 读取或新建，并写回响应头
- 非 HTTP 入口（Gradio/后台运行）：run_manager.start 兜底 ensure
- contextvar 在 asyncio.create_task 时被复制，子任务自动继承
"""
import uuid
from contextvars import ContextVar

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")

DEFAULT_TRACE_ID = "-"


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: str) -> object:
    """设置当前上下文 trace_id，空值则新建。返回 token 供 reset。"""
    return _trace_id.set(trace_id or new_trace_id())


def reset_trace_id(token: object) -> None:
    _trace_id.reset(token)


def get_trace_id() -> str:
    return _trace_id.get() or DEFAULT_TRACE_ID


def ensure_trace_id() -> str:
    """确保当前上下文有 trace_id，返回之（无则新建并设置）。"""
    tid = _trace_id.get()
    if not tid:
        tid = new_trace_id()
        _trace_id.set(tid)
    return tid