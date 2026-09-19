"""从用户聊天消息中解析爬取目标：起始 URL + 明确指令

- 消息中直接含 http(s) 链接 → 直接用该地址，指令保留原消息
- 消息中以站点/域名描述目标（如"打开淘宝…"）→ 用 LLM 推断一个可起始访问的 URL 与清晰指令
- 推断不出可访问地址 → 返回空 URL，由上层在对话框追问用户

被 Gradio 界面与 /api/chat 共用。
"""
import re

from app.core.llm import llm_json
from app.prompts import RESOLVE_TARGET_SYSTEM, RESOLVE_TARGET_USER

_URL_RE = re.compile(r"https?://[^\s'\"<>，。；]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"^[\w-]+(\.[\w-]+)+")


def _extract_url(message: str) -> str:
    m = _URL_RE.search(message)
    if m:
        return m.group(0).rstrip(".,;)）")
    return ""


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("://"):
        url = "https" + url
    elif _DOMAIN_RE.match(url) and not url.startswith("http"):
        url = "https://" + url
    return url


async def resolve_target(message: str) -> tuple[str, str]:
    """解析用户消息，返回 (url, instruction)。两者都可能为空字符串。"""
    message = (message or "").strip()
    if not message:
        return "", ""

    direct = _extract_url(message)
    if direct:
        return normalize_url(direct), message

    result = await llm_json([("user", RESOLVE_TARGET_USER.format(message=message))],
        ctx=None, system_hint=RESOLVE_TARGET_SYSTEM, )

    url = normalize_url(str(result.get("url") or ""))
    instruction = str(result.get("instruction") or message).strip()
    return url, instruction
