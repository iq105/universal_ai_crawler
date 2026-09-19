"""反封禁自愈守卫 + 代理池轮换

能力：
- 检测 429/403/登录墙/反爬 等信号，指数退避重试
- 配置了代理池时，连续被封自动轮换下一个代理（重启浏览器会话）
- 提供 IPC 限速（可选）
"""
import asyncio

from app.config import settings
from app.core.event_bus import RunContext, get_ctx
from app.core.events import EVENT_TASK_LOG

# 从 .env 读取代理池：逗号分隔，如
# PROXY_LIST=socks5://127.0.0.1:1080,http://user:pass@host:port
_PROXY_LIST = [p.strip() for p in (settings.proxy_list or "").split(",") if p.strip()]
_MAX_ROTATE = 8  # 单任务最多轮换次数
_MAX_RETRIES = max(1, settings.anti_ban_max_retries)

# 直连 + 代理池
class ProxyRotator:
    def __init__(self, proxies: list[str] | None = None) -> None:
        self.proxies = proxies or _PROXY_LIST
        self.index = -1  # -1 = 直连
        self.hits = 0
        self.rotations = 0

    @property
    def current(self) -> str | None:
        if self.index < 0:
            return None
        return self.proxies[self.index]

    def next(self) -> str | None:
        if not self.proxies:
            return None
        self.index = (self.index + 1) % len(self.proxies)
        self.rotations += 1
        return self.current


_rotator = ProxyRotator()


def is_banned_status(status: int | None, text: str = "") -> bool:
    """判断是否为封禁/限流信号"""
    if status in (429, 403, 503):
        return True
    low = (text or "").lower()
    return any(k in low for k in ("blocked", "access denied", "访问被拒绝", "访问过于频繁", "请求被拒绝"))


def _backoff_delay(attempt: int) -> float:
    """指数退避：1s, 2s, 4s ... 上限 10s"""
    return min(2 ** attempt, 10)


async def with_guard(fn, *args, **kwargs):
    """包一层重试/退避守卫。fn 返回 (result_dict)：
        - 返回含 status_code/detect 的 dict
        - 命中封禁信号 → 退避重试；仍失败且可轮换 → 轮换代理
    """
    from app.browser.browser_manager import browser_manager  # 局部导入避免循环

    ctx: RunContext | None = None
    try:
        ctx = get_ctx()
    except Exception:  # noqa: BLE001
        ctx = None

    last = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            last = await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = {"status_code": None}
            # 网络异常也退避重试一次
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_backoff_delay(attempt))
                continue
            raise

        body = last if isinstance(last, dict) else {}
        st = body.get("status_code")
        detect = body.get("detect") or {}

        # B02 修复：5 秒盾是 navigate 内置自动等待的正常过程（首次响应就是 503），
        # 不能当成封禁重试 —— 旧逻辑对每个 Cloudflare 站点都多余重试 3 次，
        # 反而在风控日志里留下密集访问记录。
        if detect.get("five_sec_shield") and not detect.get("anti_bot"):
            return last

        banned = is_banned_status(st) or detect.get("anti_bot")
        if not banned:
            return last
        # 命中封禁：传达到前端日志层（不中断，退避重试）
        if ctx is not None:
            await ctx.bus.emit(
                EVENT_TASK_LOG,
                level="warn",
                source="anti_ban",
                message=f"检测到限流/封禁信号(status={st})，退避 {_backoff_delay(attempt) * 1000:.0f}ms 后重试 {attempt}/{_MAX_RETRIES}",
            )
        await asyncio.sleep(_backoff_delay(attempt))

    # 多次仍被封，尝试轮换代理（最多 _MAX_ROTATE 次）
    if _rotator.proxies and _rotator.rotations < _MAX_ROTATE:
        proxy = _rotator.next()
        if ctx is not None:
            await ctx.bus.emit(
                EVENT_TASK_LOG,
                level="info",
                source="anti_ban",
                message=f"连续被封，切换到代理 {proxy or '直连'}",
            )
        await browser_manager.reconnect(proxy=proxy)
    return last


def reset_for_task() -> None:
    """任务开始时重置轮换计数"""
    _rotator.rotations = 0