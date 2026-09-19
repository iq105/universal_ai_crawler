"""统一日志配置

后端此前完全依赖事件总线推给前端，进程标准输出没有任何日志，导致异常难排查。
本模块提供：
- setup_logging()：在 run_gradio.py / main.py 启动时调用一次，把根日志输出到 stdout（带时间戳），
  并压制第三方库(httpx/gradio/uvicorn.access)噪音。
- get_logger(name)：模块内取 logger。所有 except 分支都应该记日志，禁止静默 pass。

日志格式（AGENT.md #5）：时间戳 | 级别 | 模块 | trace_id | 消息
trace_id 由 app/core/trace.py 的 contextvar 提供，过滤器自动注入每条日志。
"""
import logging
import sys

from app.core.trace import get_trace_id

_DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(trace_id)s | %(message)s"
_NOISY = ("httpx", "httpcore", "uvicorn.access", "uvicorn.error", "gradio")


class TraceIdFilter(logging.Filter):
    """给每条日志记录注入当前 trace_id（无上下文时为 '-'）"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, "%H:%M:%S"))
    handler.addFilter(TraceIdFilter())
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)