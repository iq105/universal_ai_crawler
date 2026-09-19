"""LLM 封装：OpenAI 兼容 ChatOpenAI + JSON 结构化输出 + 推理/流式事件"""
import asyncio
import json
import logging
import re
from typing import Any, Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import settings
from app.core.event_bus import RunContext
from app.core.events import EVENT_ASSISTANT_DELTA, EVENT_ASSISTANT_REASONING
from app.prompts import LLM_CHAT_DEFAULT_SYSTEM, LLM_JSON_DEFAULT_SYSTEM

logger = logging.getLogger(__name__)

# 总请求超时（秒）——比 stream_chunk_timeout 更长，作为最终兜底
_LLM_TIMEOUT_S = 300

# 统一的超时异常集合：StreamChunkTimeoutError 是 TimeoutError 子类（Python 3.11+ TimeoutError ≡ asyncio.TimeoutError）
# 但这里显式用 tuple 包一层，兼容未来 Python 版本可能的变化
_LLM_TIMEOUT_EXC = (TimeoutError, asyncio.TimeoutError)


def _dump_messages(messages: Sequence[Any], max_each: int = 1200, max_total: int = 3000) -> str:
    """把发送给大模型的消息序列化为单行可读文本，用于控制台日志（避免刷屏）"""
    parts = []
    for m in messages:
        if isinstance(m, tuple):
            role, content = m[0], str(m[1])
        elif isinstance(m, BaseMessage):
            role, content = m.type, str(m.content)
        else:
            role, content = "unknown", str(m)
        content = " ".join(content.split())
        if len(content) > max_each:
            content = content[:max_each] + f"...[截断，原{len(content)}字]"
        parts.append(f"{role}: {content}")
    s = " | ".join(parts)
    return s[:max_total]


def get_chat_model() -> ChatOpenAI:
    if not settings.llm_api_key:
        raise RuntimeError("未配置 LLM_API_KEY，请在 backend/.env 中填写 OpenAI 兼容的 API Key")
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
        # 总请求超时（整个 HTTP 请求时长，含 connect + read）
        timeout=300,
        max_retries=2,
        # ⚠️ 关掉 LangChain 1.6 新加的 stream_chunk_timeout（默认 120s）
        # 这个是"流式 chunk 间隔超时"——已连上且已收到 chunk 后，中间超过 120s 没新 chunk 就抛
        # deepseek-chat 带 reasoning 阶段，模型"思考"时可能长时间不输出，极易误杀
        # 我们已有 timeout=300（总请求超时）+ httpx timeout=60（browser 侧）+ ToolRetryMiddleware
        # 三层保护，不需要再叠这层中间截断
        stream_chunk_timeout=None,
    )


def _to_messages(items: Sequence[Any], system_hint: str) -> list[BaseMessage]:
    """把 (role, content) 元组序列转换为消息对象"""
    out: list[BaseMessage] = [SystemMessage(content=system_hint)]
    for item in items:
        if isinstance(item, BaseMessage):
            out.append(item)
        else:
            role, content = item
            if role == "system":
                out.append(SystemMessage(content=content))
            else:
                out.append(HumanMessage(content=content))
    return out


def parse_json(text: str) -> dict:
    """稳健解析 LLM 输出的 JSON（容忍 ```json 代码块与前后杂质）"""
    if not text:
        return {}
    text = text.strip()
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                return {}
        else:
            return {}
    # LLM 可能返回纯字符串/数组/null → 兜底转成 dict
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {"items": parsed}
    return {"raw": parsed}


async def llm_json(messages: Sequence[tuple[str, str] | BaseMessage], ctx: RunContext | None = None,
    system_hint: str = "", ) -> dict:
    """调用 LLM 并解析 JSON；若有运行上下文则推送推理事件

    system_hint 为空时用 prompts/LLM_JSON_DEFAULT_SYSTEM（AGENT.md #26）。
    """
    if not system_hint:
        system_hint = LLM_JSON_DEFAULT_SYSTEM
    model = get_chat_model()
    full = [("system", system_hint)] + list(messages) if isinstance(messages[0], tuple) else list(messages)
    logger.info("LLM 发送[json] %s", _dump_messages(full))
    try:
        resp = await model.ainvoke(full)
    except _LLM_TIMEOUT_EXC as exc:
        logger.error("LLM 超时[json] %.1fs → 返回空 dict: %s", _LLM_TIMEOUT_S, exc)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM 调用异常[json] %s: %s", type(exc).__name__, exc)
        return {}
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    parsed = parse_json(content)
    logger.info("LLM 返回[json] %s", _summarize(parsed, max_len=1000))
    if ctx is not None:
        # 推理事件：展示结构化结果摘要，前端以可折叠块展示
        await ctx.bus.emit(EVENT_ASSISTANT_REASONING, text=_summarize(parsed, max_len=600))
    return parsed


async def llm_chat(messages: Sequence[tuple[str, str] | BaseMessage], ctx: RunContext | None = None,
    system_hint: str = "", ) -> str:
    """流式对话：逐字推送 assistant_delta / assistant_reasoning

    system_hint 为空时用 prompts/LLM_CHAT_DEFAULT_SYSTEM（AGENT.md #26）。
    """
    if not system_hint:
        system_hint = LLM_CHAT_DEFAULT_SYSTEM
    model = get_chat_model()
    full = _to_messages(messages, system_hint)
    logger.info("LLM 发送[chat] %s", _dump_messages(full))
    buffer: list[str] = []
    try:
        async for chunk in model.astream(full):
            if not isinstance(chunk, BaseMessage):
                continue
            delta = chunk.content or ""
            if isinstance(delta, str) and delta:
                buffer.append(delta)
                if ctx is not None:
                    await ctx.bus.emit(EVENT_ASSISTANT_DELTA, text=delta)
            # 兼容 DeepSeek 等带 reasoning_content 的模型
            reasoning = (chunk.additional_kwargs or {}).get("reasoning_content")
            if reasoning:
                if ctx is not None:
                    await ctx.bus.emit(EVENT_ASSISTANT_REASONING, text=reasoning)
    except _LLM_TIMEOUT_EXC as exc:
        logger.error("LLM 超时[chat] %.1fs: %s", _LLM_TIMEOUT_S, exc)
        if not buffer:
            return f"⚠️ LLM 响应超时（超过 {_LLM_TIMEOUT_S} 秒），请重试或换个简单点的问题。原始错误: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM 调用异常[chat] %s: %s", type(exc).__name__, exc)
        if not buffer:
            return f"⚠️ LLM 调用失败: {type(exc).__name__}: {exc}"
    result = "".join(buffer)
    logger.info("LLM 返回[chat] %s", result[:1000])
    return result


def _summarize(data: Any, max_len: int = 600) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(data)
    return text[:max_len]
