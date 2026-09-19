"""create_agent context_schema 定义与 contextvar 桥接

CrawlerRunContext 是 create_agent(context_schema=...) 注入的运行上下文类型。
agent 模式下，@tool 工具通过 `runtime: ToolRuntime` 声明注入参数，
ToolNode 从 runtime.context 传入 CrawlerRunContext（不是裸 InjectedToolArg——
langgraph 1.x 对 InjectedToolArg 只剥离用户参数，不注入值）。

桥接函数 ctx_from_context() 在每次工具调用前恢复 contextvar（set_run_context），
使得 pipeline 遗留代码（依赖 get_ctx() 的底层函数）在 agent 模式下也能正常工作。
"""
from typing import Any

from app.core.event_bus import RunContext, RunEventBus, set_run_context, reset_run_context


class CrawlerRunContext:
    """create_agent 的 context_schema：每次运行注入的只读上下文。"""

    def __init__(self, *, task_id: str, bus: RunEventBus, steering: Any = None, settings: Any = None):
        self.task_id = task_id
        self.bus = bus
        self.steering = steering
        self.settings = settings


def ctx_from_context(context: CrawlerRunContext) -> RunContext:
    """从 CrawlerRunContext 构造 contextvar RunContext 并注入，返回 token 供后续恢复。"""
    rc = RunContext(bus=context.bus, task_id=context.task_id)
    token = set_run_context(rc)
    return rc, token


def restore_ctx(token) -> None:
    """恢复 contextvar 到上一个值（每次工具调用结束后必须调用）。"""
    reset_run_context(token)
