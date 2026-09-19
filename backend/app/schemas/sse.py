"""SSE 事件模型"""
from typing import Any

from pydantic import BaseModel


class SSEEvent(BaseModel):
    type: str
    data: dict[str, Any]
