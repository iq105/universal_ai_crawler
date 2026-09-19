"""运行上下文 + 事件总线

RunContext 通过 contextvar 注入当前运行环境（事件总线、任务 ID），
LangGraph 节点与工具函数无需层层传参即可推送 SSE 事件。
"""
import asyncio
from contextvars import ContextVar

from app.core.events import make_event


"""
`set_run_context` 和`reset_run_context` 必须 成对出现 。如果`set` 了但忘了`reset` ，
ContextVar 里的旧 RunContext（旧 task_id + 旧 bus）会泄漏到下一个任务——新任务的工具函数会把事件发到旧 bus 上，前端永远看不到新任务的进度。
"""
class RunEventBus:
    """单任务运行期间的事件队列，API 层从中读取并转发为 SSE"""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()

    async def emit(self, event_type: str, **data) -> None:
        await self._queue.put(make_event(event_type, **data))

    def emit_nowait(self, event_type: str, **data) -> None:
        self._queue.put_nowait(make_event(event_type, **data))

    async def get(self, timeout: float = 30.0) -> dict | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None


class RunContext:
    """当前运行上下文"""

    def __init__(self, bus: RunEventBus, task_id: str, loop: asyncio.AbstractEventLoop | None = None):
        self.bus = bus
        self.task_id = task_id
        self.loop = loop or asyncio.get_running_loop()

    def emit_threadsafe(self, event_type: str, **data) -> None:
        """从非 asyncio 线程安全推送事件（如 yt-dlp progress_hooks 在 to_thread 里被调用时）"""
        try:
            fut = asyncio.run_coroutine_threadsafe(self.bus.emit(event_type, **data), self.loop)
            fut.result(timeout=2.0)  # 等一下确保事件入队，防止进程退出丢事件
        except Exception:
            # 线程里没 context、loop 已关、超时等——都忽略
            pass


_run_context: ContextVar[RunContext | None] = ContextVar("run_context", default=None)


def set_run_context(ctx: RunContext) -> object:
    return _run_context.set(ctx)


def reset_run_context(token: object) -> None:
    _run_context.reset(token)


def get_ctx() -> RunContext:
    ctx = _run_context.get()
    if ctx is None:
        raise RuntimeError("RunContext 未初始化：节点/工具只能在任务运行期间调用")
    return ctx
