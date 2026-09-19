"""SSE 事件类型定义"""
import json

# 推送到前端的标准事件类型
EVENT_ASSISTANT_DELTA = "assistant_delta"
EVENT_ASSISTANT_REASONING = "assistant_reasoning"
EVENT_TOOL_START = "tool_start"
EVENT_TOOL_RESULT = "tool_result"
EVENT_TASK_STATUS = "task_status"
EVENT_TASK_PROGRESS = "task_progress"
EVENT_TASK_LOG = "task_log"
EVENT_ITEMS_BATCH = "items_batch"
EVENT_INTERRUPT = "interrupt"
EVENT_DONE = "done"
EVENT_ERROR = "error"
EVENT_TASK_CREATED = "task_created"
EVENT_CHAT_ACK = "chat_ack"
EVENT_PLAN = "plan"


def make_event(event_type: str, **data) -> dict:
    return {"type": event_type, "data": data}


def sse_format(event: dict) -> str:
    """把事件序列化为 SSE 文本行（data: {...}）"""
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
