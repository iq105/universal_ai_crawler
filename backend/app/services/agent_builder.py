"""agent_builder.py：项目唯一 Agent 构建入口（LangChain create_agent）

全项目只走 create_agent 一条路径，无手写 ReAct 循环、无额外 StateGraph 包装层。
create_agent 返回 CompiledStateGraph，直接支持 .astream() / .aget_state() / Command(resume)。
"""
import asyncio
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    ToolRetryMiddleware,
    ToolCallLimitMiddleware,
    ContextEditingMiddleware,
    ToolErrorMiddleware,
    ToolCallRequest,
)

from app.config import settings
from app.core.checkpointer import get_checkpointer
from app.core.context import CrawlerRunContext
from app.core.llm import get_chat_model
from app.prompts import AGENT_SYSTEM_PROMPT
from app.services.middleware import (
    AuditMiddleware,
    ChallengeInterceptor,
    DSMLFallback,
    FinalAnswerMiddleware,
    GuardMiddleware,
    SteeringMiddleware,
)
from app.tools import get_agent_tools


# --------------- 状态扩展 ---------------

class CrawlerAgentState(AgentState):
    """Agent 子图状态：在框架管理的 messages 之上，追加任务上下文字段。

    字段说明：
    - task_id / url / instruction：任务元信息，供工具（save_items 等）读取
    - done / final_answer / status：Agent 结束标记，供大图 route / run_manager 判断终态
    - page_state：当前页面状态快照（挑战检测等），供中间件/工具参考
    """
    task_id: str = ""
    url: str = ""
    instruction: str = ""
    page_state: dict = {}
    done: bool = False
    final_answer: str = ""
    status: str = ""


# --------------- 系统提示词（统一在 app/prompts 维护，AGENT.md #26）---------------

SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT


# --------------- 构建 Agent ---------------

_agent_instance = None


# ──────────────────────────────────────────────────────────
# 异常处理三层架构
#
# Layer 1: ToolRetryMiddleware（最内层）— 先执行
#   白名单 _RETRYABLE_EXCEPTIONS 里的异常 → 重试 3 次，指数退避
#   不在白名单里 → 不 retry，直接传播给 ToolError
#   retry 全耗尽 → on_failure="error" 抛给 ToolError
#
# Layer 2: ToolErrorMiddleware（ToolRetry 外层）— retry 耗尽后的兜底
#   任何能让模型理解的异常 → 转成 ToolMessage(status=error)
#   模型下一轮能看到错误消息 + 修复提示 → 自己换策略
#
# Layer 3: 异常传播终止（ToolError 外层）
#   编程 bug（AssertionError/AttributeError 等）→ ToolRetry 不 retry → ToolError 不处理 → 传播终止
#   你看到 ERROR 日志 → 修代码
#
# 关键：retry_on 必须是白名单（default_retry_on 对所有异常 retry，编程 bug 也 retry 3 次浪费时间）
# ──────────────────────────────────────────────────────────


def _build_retryable_exceptions() -> tuple:
    """构建 ToolRetryMiddleware 的 retry_on 白名单。

    只包含"重试有意义"的异常——网络瞬断、超时、页面加载慢，retry 一下可能就好了。
    编程 bug（AssertionError/AttributeError/NameError...）绝对不 retry。
    """
    exc_list: list[type] = [
        # Python 标准库
        ConnectionError, TimeoutError, OSError, asyncio.TimeoutError,
    ]
    # httpx 全家桶（browser_manager 用 httpx 转发到 Node.js browser-service）
    try:
        import httpx
        exc_list.append(httpx.HTTPError)  # 覆盖 ConnectError / TimeoutException / HTTPStatusError / ProxyError / DecodingError...
    except ImportError:
        pass
    # Playwright 异常（browser-service 底层用）
    try:
        from playwright._impl._errors import Error as PlaywrightError
        exc_list.append(PlaywrightError)
    except ImportError:
        pass
    return tuple(exc_list)


_RETRYABLE_EXCEPTIONS = _build_retryable_exceptions()


def _on_tool_error(exc: Exception, request: ToolCallRequest) -> str | None:
    """ToolErrorMiddleware 的 on_error 回调——retry 耗尽后的最后一道防线。

    此时 ToolRetryMiddleware 已经在更内层 retry 过 3 次了（如果异常在 _RETRYABLE_EXCEPTIONS 里）。
    能到达这里的异常分两种：
      A. 在白名单里但 retry 全失败 → 应该转成 ToolMessage 给模型看
      B. 不在白名单里（ValueError/RuntimeError 等工具级异常）→ 工具自己能处理，也转成 ToolMessage
      C. 编程 bug（AssertionError/AttributeError）→ 不处理，返回 None 让异常传播终止 Agent

    判断原则：异常是否携带"工具/网络/页面"语境——有就转 ToolMessage，没有就 terminate。
    """
    from app.core.logger import get_logger
    _log = get_logger("agent_builder")
    name = request.tool_call.get("name") or ""
    exc_type = type(exc).__name__
    _log.warning("tool error (after retry) %s: %s: %s", name, exc_type, exc)

    # Case A: 在 retry 白名单里（retry 过 3 次还是失败）→ 告诉模型换策略
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return _fmt_tool_error(name, exc)

    # Case B: 工具级可预期错误（参数错/结构错/页面元素找不到）→ 告诉模型改调用方式
    if isinstance(exc, (ValueError, KeyError, TypeError, IndexError)):
        return _fmt_tool_error(name, exc)

    # Case B 兜底：RuntimeError 带"工具/网络/页面"语境 → 也转 ToolMessage
    # 注意：RuntimeError 本身不在 _RETRYABLE_EXCEPTIONS 里（太泛），但 retry 层已经先检查过白名单
    # 能走到这里说明它没被 retry → 不浪费 retry 次数，直接转 ToolMessage 告诉模型
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        TOOL_CTX = (
            "timeout", "timed out", "connect", "connection", "network",
            "unreachable", "refused", "reset", "not found", "selector",
            "element", "click", "browser", "playwright", "page", "navigate",
            "status", "response", "proxy", "certificate", "ssl",
            "超时", "连接", "网络", "不可达", "找不到", "未找到", "元素", "无法", "失败",
        )
        if any(p in msg for p in TOOL_CTX):
            return _fmt_tool_error(name, exc)

    # Case C: 编程 bug / 未知异常 → 不拦截，传播终止让开发者看到
    _log.error("tool error 未匹配任何 recoverable 规则，终止 agent：%s: %s", exc_type, exc)
    return None


def _fmt_tool_error(tool_name: str, exc: Exception) -> str:
    """格式化错误为模型可读字符串（附带简短修复提示）"""
    hint_map = {
        "connect": "网络连接失败，请检查 browser-service 是否在运行，或换个策略重试",
        "timeout": "操作超时，可能是页面加载慢，请稍等后重试或换更轻量的操作",
        "timed out": "操作超时，请减少 timeout 参数或换更轻量的操作",
        "not found": "元素不存在，请先用 get_page_snapshot 查看页面结构再操作",
        "selector": "选择器无效，请先用 get_page_snapshot 确认页面元素",
        "element": "元素未找到或不可交互，请换一种操作方式",
        "playwright": "浏览器操作失败，请换一种操作方式或刷新页面",
        "network": "网络问题，请检查网络连接或稍后重试",
        "unreachable": "服务不可达，请确认 browser-service 是否在运行",
        "status": "HTTP 状态码异常，可能是页面不存在或需要特殊处理",
    }
    hint = ""
    msg_lower = str(exc).lower()
    for key, val in hint_map.items():
        if key in msg_lower:
            hint = f"（提示：{val}）"
            break
    return f"工具 {tool_name} 执行出错：{type(exc).__name__}: {exc}{hint}"


def build_agent(checkpointer: Any) -> Any:
    """构建并编译 Agent 子图（只构建一次，模块级缓存）。

    返回 CompiledStateGraph，可作为节点嵌入 crawler_graph.py 大图，
    也可直接 astream/invoke。
    """
    global _agent_instance
    if _agent_instance is not None:
        return _agent_instance

    model = get_chat_model()
    tools = get_agent_tools()

    _agent_instance = create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        state_schema=CrawlerAgentState,
        context_schema=CrawlerRunContext,
        checkpointer=checkpointer,
        middleware=[
            # ---- 自定义横切中间件（按 docs §5.1，外层→内层）----
            # 1. 审计：tool_start/tool_result 事件（替换 registry.execute 审计）
            AuditMiddleware(),
            # 2. 运行中指挥：drain steering 队列注入 user 消息（替换 agent_think:117-130）
            SteeringMiddleware(),
            # 3. 挑战拦截：检测到验证码/登录墙/5秒盾/反爬 → interrupt 交人工（替换 agent_act:366-373）
            ChallengeInterceptor(),
            # 4. 守卫：截图频率 / 导出无数据拦截（替换 agent_think:143-170,287-339）
            GuardMiddleware(),
            # 5. DSML 兜底 + nudge 重试（替换 agent_think:178-204,236-264）
            DSMLFallback(nudge=True),
            # 6. 完成判定：模型无 tool_calls 且回复符合「完成」语义 → done/final_answer（替换 agent_think:221-227）
            FinalAnswerMiddleware(),
            # 7. 步数上限
            ToolCallLimitMiddleware(run_limit=settings.max_steps),

            # ---- ⚠️ 关键顺序：ToolRetry 必须在 ToolError 内侧（更内层）----
            # 洋葱模型：工具抛错时，ToolRetry 先 retry，全失败才轮到 ToolError 转 ToolMessage
            # 如果 ToolError 在里面，它会把异常转成 ToolMessage，ToolRetry 根本看不到！
            #
            # 执行链（外层→内层）：
            #   Audit → Challenge → ToolError → ToolRetry → 真实工具
            # 异常传播（内层→外层）：
            #   真实工具抛 exc
            #     → ToolRetry：先 retry 3 次（on_failure="error" → 重试全挂则 raise）
            #     → ToolError：retry 耗尽后的兜底 → 转 ToolMessage(status=error) 给模型
            #     → 编程 bug（AssertionError 等）→ ToolRetry 不 retry → ToolError 也不处理 → 传播终止
            ToolErrorMiddleware(on_error=_on_tool_error),
            ToolRetryMiddleware(
                max_retries=3,
                backoff_factor=2.0,
                initial_delay=1.0,
                retry_on=_RETRYABLE_EXCEPTIONS,   # 只有这组异常才 retry，编程 bug 不 retry
                on_failure="error",               # retry 全耗尽后 raise exc → 交给外层 ToolError
            ),

            # 10. 上下文裁剪（防长任务上下文膨胀）
            ContextEditingMiddleware(),
        ],
        debug=True,
    )
    return _agent_instance


# --------------- 对外入口（替代 crawler_graph.py）---------------

_agent_singleton = None


async def get_agent() -> Any:
    """获取全局唯一 Agent 实例（create_agent 构建的 CompiledStateGraph）。

    替代旧 crawler_graph.get_graph()——现在 create_agent 返回的就是 graph，
    不用再包一层 StateGraph。
    """
    global _agent_singleton
    if _agent_singleton is None:
        cp = await get_checkpointer()
        _agent_singleton = build_agent(cp)
    return _agent_singleton


async def initial_state_for(url: str = "", user_message: str = "") -> dict:
    """构建 Agent 初始 state

    create_agent 模式下 url 可选——大模型会自己调 resolve_target 拿到目标。
    messages 初始塞一条用户原始消息，让 agent 直接开始工作。
    """
    return {
        "task_id": "",  # 由 config 里的 thread_id 自动填充
        "url": url,
        "instruction": user_message,
        "status": "running",
        "messages": [{"role": "user", "content": user_message}] if user_message else [],
        "done": False,
        "page_state": {},
    }
