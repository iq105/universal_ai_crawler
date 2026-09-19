"""服务端中间件包：create_agent 的子图横切关注点

按 docs/create_agent重构实施记录.md §5.1 注册顺序（外层→内层）导出：
Audit → Steering → Challenge → Guard → DSMLFallback  → 预置（Retry/Limit/Error/HITL…）

每个中间件实现 async hook（awrap_tool_call / abefore_model 等），
与 create_agent（state_schema=CrawlerAgentState, context_schema=CrawlerRunContext）配套。
"""
from app.services.middleware.audit import AuditMiddleware
from app.services.middleware.challenge import ChallengeInterceptor
from app.services.middleware.dsml import DSMLFallback
from app.services.middleware.final_answer import FinalAnswerMiddleware
from app.services.middleware.guard import GuardMiddleware
from app.services.middleware.steering import SteeringMiddleware

__all__ = [
    "AuditMiddleware",
    "SteeringMiddleware",
    "ChallengeInterceptor",
    "GuardMiddleware",
    "DSMLFallback",
    "FinalAnswerMiddleware",
]