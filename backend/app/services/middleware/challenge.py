"""ChallengeInterceptor：挑战检测 → 中断交人工（替换 agent_nodes.agent_act:366-373）

工具返回内容（ToolMessage.content，YAML/JSON 文本）中若含挑战信号
（登录墙/验证码/滑块/5秒盾/反爬），则：
1. 推送 warn 日志
2. 在 before_model 中调用 interrupt() 暂停图，等待人工处理
3. resume 后把人工回复作为 ToolMessage 返回给模型，让 Agent 决定下一步
"""
import json
from typing import Any, Callable

import yaml
from langchain.agents.middleware import AgentMiddleware, ToolCallRequest, ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command, interrupt

from app.core.events import EVENT_TASK_LOG
from app.core.logger import get_logger

_log = get_logger("middleware.challenge")

CHALLENGE_MESSAGES = {
    # —— 硬挑战：必须 interrupt 等人工处理 ——
    # login 也归为硬挑战：等用户手动登录 → storage 自动保存 → 继续
    "login": "检测到登录墙：该页面需要登录。请在浏览器窗口中手动完成登录（扫码/账号密码都行），登录成功后页面会自动跳转，登录态会自动保存。完成后告诉我「继续」。",
    "captcha": "检测到验证码/人机验证：请在弹出窗口中手动完成验证，然后告诉我「继续」。",
    "slider": "检测到滑块验证：请在弹出窗口中手动完成滑块验证，然后告诉我「继续」。",
    "anti_bot": "检测到反爬拦截：请在弹出窗口中手动处理（如完成安全验证），然后告诉我「继续」。",
    # —— 自动可解：5 秒盾由 navigate 自动等待，不 interrupt ——
    "five_sec_shield": "",  # 空字符串 = 跳过
}

# 哪些挑战是硬的（必须 interrupt 等人工），哪些是软的（给大模型提示即可）
# 注：SOFT_CHALLENGES 目前为空集 —— login 也按硬挑战处理（interrupt 等用户手动登录，
# 完成后自动保存登录态并继续），上方 SOFT_CHALLENGES 分支仅保留作扩展点（B27.4 注释修正）。
HARD_CHALLENGES = {"login", "captcha", "slider", "anti_bot"}
SOFT_CHALLENGES = set()


class ChallengeInterceptor(AgentMiddleware):
    """在 tools 执行后检测挑战信号，在 before_model 中 interrupt() 暂停图。"""

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        result = await handler(request)
        reason = _detect_from_message(result)
        if not reason:
            return result

        ctx = request.runtime.context
        msg = CHALLENGE_MESSAGES.get(reason, "检测到反爬挑战，需要人工处理")

        # five_sec_shield：navigate 里已经有自动等待逻辑，直接忽略
        if reason == "five_sec_shield":
            return result

        if reason in SOFT_CHALLENGES:
            # 软挑战（登录墙）：不 interrupt，只 emit 日志 + 给大模型 SystemMessage 提示
            _log.info("[challenge] 软挑战 task_id=%s reason=%s（提示模型自行判断）", ctx.task_id, reason)
            if ctx.bus is not None:
                await ctx.bus.emit(EVENT_TASK_LOG, level="warn", source="agent", message=f"⚠️ {msg}")
            # 往 state 里塞一条 SystemMessage，让大模型自己决定要不要让用户登录
            ctx._pending_soft_challenge = {"reason": reason, "message": msg}
            return result

        # 硬挑战（captcha / slider / anti_bot）：必须 interrupt 等人工
        if reason not in HARD_CHALLENGES:
            return result  # 未知类型，不处理

        if ctx.bus is not None:
            await ctx.bus.emit(EVENT_TASK_LOG, level="error", source="agent", message=f"🚨 {msg}")
        _log.warning("[challenge] 硬挑战 task_id=%s reason=%s（interrupt 等人工）", ctx.task_id, reason)

        # 标记 pending，由 before_model 检查并 interrupt()
        ctx._pending_challenge = {"reason": reason, "message": msg}
        return result

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        ctx = runtime.context

        # —— 先处理软挑战（login）：往 messages 里插一条 SystemMessage 提示大模型自己判断 ——
        soft = getattr(ctx, "_pending_soft_challenge", None)
        if soft:
            ctx._pending_soft_challenge = None
            hint = SystemMessage(content=f"[软挑战提示] {soft['message']}")
            messages = list(state.get("messages", []))
            messages.append(hint)
            # 注意：不 interrupt，直接让大模型带着这条提示继续思考
            # 如果同时还有硬挑战，硬挑战优先 interrupt
            if not getattr(ctx, "_pending_challenge", None):
                return {"messages": messages}
            # 有硬挑战 → 先插软提示，再继续往下走处理硬挑战
            state = {"messages": messages}

        # —— 硬挑战：必须 interrupt 等人工 ——
        challenge = getattr(ctx, "_pending_challenge", None)
        if not challenge:
            return None

        # 清除标记
        ctx._pending_challenge = None

        reason = challenge["reason"]
        msg = challenge["message"]

        # interrupt() 会暂停图，resume 值 = 用户最新指令
        guidance = interrupt({"type": "challenge", "reason": reason, "message": msg})

        text = _resume_text(guidance)

        # 🟢 登录挑战：用户处理完后自动保存登录态（storageState）
        # Node 侧在 framenavigated 时已自动保存，这里再兜底一次
        if reason == "login":
            try:
                from app.browser.browser_manager import browser_manager
                save_result = await browser_manager.save_state()
                _log.info("[challenge] 登录态已自动保存 host=%s", save_result.get("host"))
                if ctx.bus is not None:
                    await ctx.bus.emit(EVENT_TASK_LOG, level="info", source="agent",
                        message=f"✅ 登录态已自动保存（{save_result.get('host')}），下次访问该域名将自动复用")
            except Exception as exc:  # noqa: BLE001
                _log.warning("[challenge] 自动保存登录态失败：%s", exc)

        # 将人工回复作为 SystemMessage 注入，让 Agent 知道如何继续
        extra = ""
        if reason == "login":
            extra = "\n💡 登录态已自动保存到本地，下次访问该域名无需重新登录（除非 cookie 过期）。"
        system_msg = SystemMessage(
            content=f"挑战已由人工处理。用户回复：{text}{extra}\n请根据用户回复继续后续步骤。"
        )
        messages = list(state.get("messages", []))
        messages.append(system_msg)
        return {"messages": messages}


def _resume_text(guidance: Any) -> str:
    if isinstance(guidance, dict):
        return str(guidance.get("text") or guidance.get("message") or "继续")
    return str(guidance or "继续")


def _detect_from_message(result: Any) -> str | None:
    """从工具返回值提取挑战信号，返回 reason 或 None"""
    content = None
    if isinstance(result, ToolMessage):
        content = result.content
    elif isinstance(result, dict):
        return _detect_from_result(result)

    data = None
    if content is not None:
        if isinstance(content, str):
            data = _parse_text(content)
        elif isinstance(content, (list, tuple)) and content and isinstance(content[0], dict):
            data = content[0]

    if not isinstance(data, dict):
        return None
    return _detect_from_result(data)


def _parse_text(text: str) -> dict | None:
    """把 ToolMessage 的 YAML/JSON 文本解析成 dict"""
    text = text.strip()
    if not text:
        return None
    for loader in (json.loads, yaml.safe_load):
        try:
            parsed = loader(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            continue
    return None


def _detect_from_result(result: dict) -> str | None:
    """与 agent_nodes._detect_from_result 语义一致"""
    detect = result.get("detect")
    if isinstance(detect, dict):
        for key, reason in (("login", "login"), ("captcha", "captcha"), ("slider", "slider"),
                ("five_sec_shield", "five_sec_shield"), ("anti_bot", "anti_bot")):
            if detect.get(key):
                return reason
    for key, reason in (("login_wall", "login"), ("captcha", "captcha"), ("anti_bot", "anti_bot")):
        if result.get(key):
            return reason
    return None
