"""GuardMiddleware：守卫逻辑（替换 agent_nodes 的截图频率守卫 + 导出无数据守卫）

1. 截图频率守卫（abefore_model）：最近 5 轮工具中出现 screenshot >= 2 次 → 注入 SystemMessage 强制禁止
2. 导出无数据守卫（abefore_model）：用户要导出/查询但最近 list_tasks/list_items 空 → 注入 SystemMessage 强制 done，不让 LLM 去爬
"""
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.core.events import EVENT_CHAT_ACK, EVENT_TASK_LOG
from app.core.logger import get_logger

_log = get_logger("middleware.guard")

_EXPORT_KEYWORDS = ("导出", "下载", "export", "下载数据", "看看数据", "数据呢", "查看数据", "查询数据")


class GuardMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: Any, runtime: Any) -> dict | None:
        messages: list = list(state.get("messages") or [])
        if not messages:
            return None
        ctx = runtime.context

        updates: dict = {}

        # ---- 截图频率守卫 ----
        recent_tool_names = [m.name for m in reversed(messages[-10:]) if
            isinstance(m, ToolMessage) and getattr(m, "name", None)][:5]
        screenshot_count = sum(1 for n in recent_tool_names if n == "screenshot")
        if screenshot_count >= 2:
            updates.setdefault("messages", []).append(
                SystemMessage(content=f"你刚才已经连续调了 {screenshot_count} 次 screenshot，这太多了！"
                                      "screenshot 非常消耗资源，除非遇到反爬挑战（5秒盾/滑块/验证码），否则绝对不要调。"
                                      "用 get_page_snapshot 查看页面文字内容，它更快更轻。现在请选择其他工具。"))
            _log.info("[guard] 截图频率守卫，screenshot_count=%d", screenshot_count)

        # ---- 导出无数据守卫（根据最近消息推断，强制 done）----
        if self._should_block_export(messages):
            reason = ("抱歉，没有找到相关的历史数据，无法导出/查询。"
                      "请先爬取数据后再导出。如果你想开始新的爬取任务，请直接说「帮我爬取 XX」。")
            if ctx.bus is not None:
                await ctx.bus.emit(EVENT_CHAT_ACK, text=reason)
            _log.info("[guard] 导出守卫拦截，强制完成")
            updates.setdefault("messages", []).append(
                SystemMessage(content="没有找到相关历史数据。请直接以「完成」结尾总结这段对话，告知用户没有数据可导出。"))
            # 追加一条 HumanMessage 只能干预模型，仍可能让模型去爬；
            # 更稳做法：把最终答案写入 state 并请求跳转到结束。
            updates["jump_to"] = "end"
            updates["final_answer"] = reason
            updates["done"] = True

        return updates if updates else None

    def _should_block_export(self, messages: list) -> bool:
        """最近用户消息含导出关键词 + 最近工具是 list_tasks/list_items 且空 → 阻断"""
        last_tool_name = None
        last_tool_result = None
        last_user_msg = None
        for m in reversed(messages):
            if isinstance(m, ToolMessage) and getattr(m, "name", None) and last_tool_result is None:
                last_tool_name = m.name
                last_tool_result = m.content
            elif isinstance(m, HumanMessage) and m.content and last_user_msg is None:
                last_user_msg = str(m.content)
            if last_tool_result is not None and last_user_msg is not None:
                break

        if last_tool_result is None or not last_user_msg:
            return False
        if last_tool_name not in ("list_tasks", "list_items"):
            return False
        if not any(kw in last_user_msg.lower() for kw in _EXPORT_KEYWORDS):
            return False

        # 解析工具结果文本（YAML/JSON）
        import json as _json
        import yaml as _yaml
        text = str(last_tool_result).strip()
        data = None
        for loader in (_json.loads, _yaml.safe_load):
            try:
                parsed = loader(text)
                if isinstance(parsed, dict):
                    data = parsed
                    break
            except Exception:  # noqa: BLE001
                continue

        if data is None:
            return True  # 解析失败当没数据
        if last_tool_name == "list_tasks":
            tasks = data.get("tasks") or []
            if len(tasks) <= 1:
                return True
            if all(t.get("count", 0) == 0 and t.get("item_count", 0) == 0 for t in tasks):
                return True
            return False
        items = data.get("items") or data.get("data") or []
        return len(items) == 0
