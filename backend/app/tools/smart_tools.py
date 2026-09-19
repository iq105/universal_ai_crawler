"""智能工具集：媒体下载 / 验证码识别 / 智能提取
统一注册为 Agent 可用工具，执行时复用实现层。
"""
import logging

from app.browser.browser_manager import browser_manager
from app.core.event_bus import get_ctx
from app.core.llm import llm_json
from app.prompts import EXTRACT_ITEMS_SYSTEM, EXTRACT_ITEMS_USER_SMART
from app.services import captcha_solver, media_downloader
from app.services.extraction import clean_html, smart_extract_local

logger = logging.getLogger(__name__)


async def probe_media(url: str) -> dict:
    """探测 URL 是否可下载媒体并返回信息（标题/时长/格式）。"""
    result = await media_downloader.probe_media(url)
    return result


async def download_media(url: str) -> dict:
    """下载视频/音频到媒体目录，返回落地文件路径。"""
    return await media_downloader.download_media(url)


async def solve_captcha(selector: str = "", fields: str = "") -> dict:
    """自动识别当前页面验证码（可指定验证码元素选择器）。
    识别成功返回 text；识别失败返回 ok=false，交由人工介入。"""
    shot = await browser_manager.screenshot_element(selector=selector)
    base64_data = shot.get("base64") or ""
    if not base64_data:
        return {"ok": False, "reason": "无法获取验证码截图", "suggest": "window"}

    result = captcha_solver.ocr_text(base64_data)
    if result.get("ok"):
        return {"ok": True, "source": "local", "text": result["text"], "confirm": True}
    # 本地识别失败 → 提示交人工
    return {"ok": False, "reason": result.get("reason", "识别失败")}


async def smart_extract(item_selector: str = "", fields: list | None = None, item_hint: str = "") -> dict:
    """智能提取：结构识别 → 正则 → LLM 兜底。返回 {items, source}。"""
    ctx = get_ctx()
    try:
        html = await browser_manager.html(max_len=30000)
    except Exception as exc:  # noqa: BLE001
        logger.error("smart_extract html 获取失败: %s", exc)
        return {"items": [], "source": "error", "error": f"html 获取失败: {exc}"}

    try:
        local = smart_extract_local(html, item_selector=item_selector, fields=fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("smart_extract_local 抛异常，跳过: %s", exc)
        local = {"items": []}
    if local.get("items"):
        return local

    # LLM 兜底
    content = clean_html(html, 12000)
    fields_desc = ", ".join(f["name"] if isinstance(f, dict) else str(f) for f in (fields or []))
    hint = f"额外提示：{item_hint}\n" if item_hint else ""
    try:
        result = await llm_json([("user", EXTRACT_ITEMS_USER_SMART.format(limit=12000, content=content,
                                                                           fields=fields_desc or "all", hint=hint))], ctx,
            system_hint=EXTRACT_ITEMS_SYSTEM, )
    except Exception as exc:  # noqa: BLE001
        logger.error("smart_extract LLM 调用异常: %s", exc)
        return {"items": [], "source": "llm_error", "error": str(exc)}

    items = []
    if isinstance(result, dict):
        raw = result.get("items") or result.get("data") or result
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = [raw]
    elif isinstance(result, list):
        items = result
    return {"items": items, "source": "llm"}


async def resolve_target_tool(message: str) -> dict:
    ''':redoc: 解析用户自然语言 → 目标 URL + 指令
    把用户说的话（如'帮我爬淘宝上2024年新款手机'）解析成可访问的 URL 和爬取指令。
    '''
    from app.services.target_resolver import resolve_target
    url, instruction = await resolve_target(message)
    if not url:
        return {'url': None, 'instruction': None, 'error': '无法从你的话推断要访问哪个网站，请补充目标网址或明确站点名称。'}
    return {'url': url, 'instruction': instruction, 'original_message': message}

