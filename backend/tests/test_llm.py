"""LLM 封装单元测试（AGENT.md #50：LLM 封装必须有单测，Mock 掉外部依赖）。

覆盖 core/llm.py：
- get_chat_model 从 settings 构建（Mock 掉 ChatOpenAI）
- 空 system_hint 回退到 prompts 中的默认系统提示（AGENT.md #26）
- llm_json 解析 JSON（含 markdown 代码围栏）
- llm_chat 流式拼接并注入默认 system 提示
"""
from unittest.mock import patch

from langchain_core.messages import AIMessage, AIMessageChunk

from app.core import llm
from app.prompts import LLM_CHAT_DEFAULT_SYSTEM, LLM_JSON_DEFAULT_SYSTEM


def test_get_chat_model_uses_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    with patch("app.core.llm.ChatOpenAI") as mock_cls:
        llm.get_chat_model()
    kwargs = mock_cls.call_args.kwargs
    assert kwargs["model"] == settings.llm_model
    assert kwargs["temperature"] == settings.llm_temperature
    assert kwargs["api_key"] == "test-key"


def test_get_chat_model_requires_api_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    try:
        llm.get_chat_model()
    except RuntimeError as exc:
        assert "LLM_API_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺少 API Key 时应抛 RuntimeError")


async def test_llm_json_default_system_hint():
    captured = {}

    class _Model:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return AIMessage(content='{"ok": true}')

    with patch("app.core.llm.get_chat_model", return_value=_Model()):
        result = await llm.llm_json([("user", "hi")])

    assert result == {"ok": True}
    assert captured["messages"][0] == ("system", LLM_JSON_DEFAULT_SYSTEM)


async def test_llm_json_explicit_system_hint_and_code_fence():
    captured = {}

    class _Model:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return AIMessage(content='```json\n{"items": [1, 2]}\n```')

    with patch("app.core.llm.get_chat_model", return_value=_Model()):
        result = await llm.llm_json([("user", "x")], system_hint="S")

    assert result == {"items": [1, 2]}
    assert captured["messages"][0] == ("system", "S")


async def test_llm_chat_streams_and_uses_default_system():
    captured = {}

    class _Model:
        async def astream(self, messages):
            captured["messages"] = messages
            yield AIMessageChunk(content="he")
            yield AIMessageChunk(content="llo")

    with patch("app.core.llm.get_chat_model", return_value=_Model()):
        out = await llm.llm_chat([("user", "hi")])

    assert out == "hello"
    assert captured["messages"][0].content == LLM_CHAT_DEFAULT_SYSTEM


def test_parse_json_tolerates_noise():
    assert llm.parse_json('前言 {"a": 1} 后语') == {"a": 1}
    assert llm.parse_json('{"a": 1}') == {"a": 1}