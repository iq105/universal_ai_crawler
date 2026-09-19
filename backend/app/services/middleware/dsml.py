"""DSMLFallback 中间件：模型未返回 tool_calls 时的兜底 + 模型级超时 retry

原逻辑 agent_nodes.py:178-204（_extract_calls + nudge）+ 236-264（_parse_dsml_tool_calls）：
1. 若模型返回了 tool_calls → 直接用
2. 否则解析文本中的 DSML 标记（DeepSeek 兜底）
3. 若既无 tool_calls 又不像是完成/提问（只说了计划）→ 注入 nudge 提示再调一次模型
4. 仍无 tool_calls 但有 DSML → 解析

⚠️ 额外职责：模型调用异常 retry（Agent 模式关键防线）
LangChain 1.6 的 stream_chunk_timeout / httpx timeout / SSL 瞬断 → 都在 model.ainvoke 这一层抛
ToolRetryMiddleware 只管 tool 节点，模型节点的异常不经过它，这里兜住。
"""
import asyncio
import json
import re
import time
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage

from app.core.logger import get_logger

_log = get_logger("middleware.dsml")

_DSML_INVOKE_RE = re.compile(r'<｜｜DSML｜｜\s*invoke\s+name="([^"]+)"\s*>(.*?)</｜｜DSML｜｜\s*invoke\s*>', re.DOTALL)

# 哪些异常值得 retry（模型调用瞬断/超时，retry 一次大概率恢复）
# 注意：StreamChunkTimeoutError 继承 TimeoutError（Python 3.11+ TimeoutError ≡ asyncio.TimeoutError）
_MODEL_RETRY_EXC: tuple[type, ...] = (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)


class DSMLFallback(AgentMiddleware):
    def __init__(self, nudge: bool = True, model_retry: int = 1):
        super().__init__()
        self.nudge = nudge
        self.model_retry = model_retry  # 模型调用超时/瞬断 retry 次数

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> Any:
        # === 模型调用级超时/瞬断 retry ===
        last_exc: Exception | None = None
        for attempt in range(1 + self.model_retry):
            try:
                response = await handler(request)
                break  # 成功
            except _MODEL_RETRY_EXC as exc:
                last_exc = exc
                _log.warning("[dsml] 模型调用异常 attempt=%d/%d %s: %s",
                             attempt + 1, 1 + self.model_retry, type(exc).__name__, exc)
                if attempt < self.model_retry:
                    await asyncio.sleep(2 ** attempt)  # 指数退避 1s → 2s
                    continue
                # retry 耗尽 → 让异常冒泡到 run_manager（它会 emit _graph_error）
                raise
            except Exception as exc:  # noqa: BLE001
                # 编程 bug / 未知异常 → 不 retry，直接冒泡
                _log.error("[dsml] 模型调用未 retryable 异常 %s: %s", type(exc).__name__, exc)
                raise
        else:
            # 理论上到不了（上面 raise 或 break），纯防御
            assert last_exc is not None
            raise last_exc  # type: ignore[misc]

        # === DSML / nudge 兜底 ===
        result = await self._ensure_tool_calls(request, response, handler)
        return result

    async def _ensure_tool_calls(self, request: ModelRequest, response: ModelResponse,
            handler: Callable) -> ModelResponse:
        ai_msgs = [m for m in response.result if isinstance(m, AIMessage)]
        if not ai_msgs:
            return response
        last = ai_msgs[-1]
        content = last.content if isinstance(last.content, str) else str(last.content or "")

        # 已有结构化 tool_calls → 无需兜底
        if getattr(last, "tool_calls", None):
            return response

        # 1. 解析 DSML 标记
        calls = _parse_dsml_tool_calls(content)
        if calls:
            new_last = _with_tool_calls(last, calls)
            new_result = [new_last if m is last else m for m in response.result]
            _log.info("[dsml] 从 DSML 标记解析工具调用：%s", [t.get("name") for t in calls])
            return ModelResponse(result=new_result, structured_response=response.structured_response)

        # 2. nudge：模型只说了计划，没有调用工具，又不是完成/提问 → 提示后重试一次
        if self.nudge and not _is_completion(content.strip()) and not content.lstrip().startswith("问"):
            nudge = ("你上一条回复只描述了下一步计划，但没有调用任何工具。"
                     "请立即调用一个具体工具（如 open_page / get_page_state / get_page_snapshot）来真正执行下一步，"
                     "不要仅用文字描述计划，也不要输出任何 DSML 标记文本。若确实遇到必须向用户确认的问题，请以「问」字开头再说明。")
            retry_request = request.override(messages=list(request.messages) + [last, SystemMessage(content=nudge)])
            response2 = await handler(retry_request)
            ai2 = [m for m in response2.result if isinstance(m, AIMessage)]
            if ai2:
                last2 = ai2[-1]
                content2 = last2.content if isinstance(last2.content, str) else str(last2.content or "")
                if not getattr(last2, "tool_calls", None):
                    calls2 = _parse_dsml_tool_calls(content2)
                    if calls2:
                        new_last2 = _with_tool_calls(last2, calls2)
                        new_result2 = [new_last2 if m is last2 else m for m in response2.result]
                        _log.info("[dsml] nudge 后从 DSML 解析工具调用：%s", [t.get("name") for t in calls2])
                        return ModelResponse(result=new_result2, structured_response=response2.structured_response)
            _log.info("[dsml] nudge 后仍无工具调用，返回原响应")
            return response2

        return response


def _with_tool_calls(msg: AIMessage, calls: list[dict]) -> AIMessage:
    new = msg.model_copy(deep=True)
    new.tool_calls = calls
    return new


def _parse_dsml_tool_calls(content: str) -> list[dict]:
    """兜底解析 DeepSeek 偶发泄漏的 DSML 工具调用标记（原 agent_nodes._parse_dsml_tool_calls）"""
    if not content or "DSML" not in content:
        return []
    out: list[dict] = []
    for i, m in enumerate(_DSML_INVOKE_RE.finditer(content)):
        name = (m.group(1) or "").strip()
        if not name:
            continue
        raw = (m.group(2) or "").strip()
        args: dict = {}
        if raw:
            cleaned = raw.replace("\u200b", "").replace("\ufeff", "")
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, ValueError):
                args = {}
        out.append({"name": name, "args": args, "id": f"dsml_{int(time.time() * 1000)}_{i}", "type": "tool_call"})
    return out


def _is_completion(text: str) -> bool:
    """判定 LLM 是否已完成任务总结（原 agent_nodes._is_completion）"""
    if not text:
        return False
    first = text.lstrip()[:20]
    if first.startswith("完成"):
        return True
    if "完成" in first or "已抓取" in first or "抓取完成" in first:
        return True
    markers = ["抓取任务总结", "任务总结", "抓取来源", "共 **"]
    return any(m in text for m in markers)