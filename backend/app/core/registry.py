"""ToolRegistry：统一注册工具，执行时自动审计（事件推送 + 日志）"""
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.event_bus import RunContext, get_ctx
from app.core.events import EVENT_TOOL_RESULT, EVENT_TOOL_START


@dataclass
class ToolDef:
    name: str
    schema: dict
    executor: Callable[..., Awaitable[Any]]
    description: str = ""
    permissions: set = field(default_factory=set)
    audit: bool = True


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, name: str, schema: dict, executor: Callable[..., Awaitable[Any]], description: str = "",
            permissions: set | None = None, audit: bool = True, ) -> None:
        self._tools[name] = ToolDef(name=name, schema=schema, executor=executor, description=description,
            permissions=permissions or set(), audit=audit, )

    def get(self, name: str) -> ToolDef:
        if name not in self._tools:
            raise KeyError(f"工具未注册: {name}")
        return self._tools[name]

    def all(self) -> list[ToolDef]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        """供 LangChain 绑定使用（OpenAI 工具格式）"""
        return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
            for t in self._tools.values()]

    async def execute(self, name: str, ctx: RunContext | None = None, **kwargs) -> Any:
        tool = self.get(name)
        ctx = ctx or get_ctx()
        tool_call_id = f"{name}-{uuid.uuid4().hex[:8]}"
        if tool.audit:
            await ctx.bus.emit(EVENT_TOOL_START, id=tool_call_id, name=name, arguments=kwargs)
        try:
            result = await tool.executor(**kwargs)
            if tool.audit:
                await ctx.bus.emit(EVENT_TOOL_RESULT, id=tool_call_id, name=name, content=result)
            return result
        except Exception as exc:  # noqa: BLE001
            if tool.audit:
                await ctx.bus.emit(EVENT_TOOL_RESULT, id=tool_call_id, name=name, error=str(exc))
            raise


tool_registry = ToolRegistry()
