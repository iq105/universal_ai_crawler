"""Agent 模式工具集：用 @tool 包装底层实现。

每个工具内部调用对应的底层函数（browser_tools/parse_tools/storage_tools/smart_tools），
需要运行上下文（task_id/bus/steering）的工具通过 `runtime: ToolRuntime` 参数注入，
再经 ctx_from_context / restore_ctx 桥接 contextvar，使得 pipeline 遗留代码
（依赖 get_ctx() 的底层函数）在 agent 模式下也能正常工作。
"""
from typing import Any, Callable, Coroutine

from langgraph.prebuilt import ToolRuntime
from langchain.tools import tool

from app.core.context import ctx_from_context, restore_ctx

# ---- langgraph 1.2.11 的 ToolRuntime 缺 __get_pydantic_core_schema__ 补丁 ----
# （langchain-core issue #35931 / langgraph PR #7227 已修复但未发布到当前版本）
# 不补齐的话，@tool 推断 schema 时对 `runtime: ToolRuntime = None` 抛
# PydanticInvalidForJsonSchema(CallableSchema)，工具无法绑定 model。
# 这里补一个 classmethod，让 Pydantic 把 ToolRuntime 视为「可空任意类型」。
from pydantic_core import core_schema


def _toolruntime_pydantic_schema(cls, source, handler) -> core_schema.CoreSchema:
    return core_schema.nullable_schema(core_schema.any_schema())


ToolRuntime.__get_pydantic_core_schema__ = classmethod(_toolruntime_pydantic_schema)

# ---------- 底层实现延迟导入（避免循环 import） ----------

def _browser_tools():
    from app.tools import browser_tools
    return browser_tools

def _parse_tools():
    from app.tools import parse_tools
    return parse_tools

def _smart_tools():
    from app.tools import smart_tools
    return smart_tools

def _storage_tools():
    from app.tools import storage_tools
    return storage_tools


def _inject(fn: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
    """无需 context 的工具：直接调用底层，不做 contextvar 桥接。"""
    return fn


def _bridge(fn: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
    """需要注入 context 的工具：调用前注入 contextvar，结束后恢复。

    langgraph 的 ToolNode 识别 `ToolRuntime` 注解并注入运行上下文（runtime.context
    即 create_agent 的 context_schema 实例 CrawlerRunContext）。这里把 runtime.context
    桥接进 contextvar，使 pipeline 遗留代码（依赖 get_ctx()）在 agent 模式下也能工作。
    """
    async def wrapper(*args: Any, runtime: ToolRuntime, **kwargs: Any) -> Any:
        rc, token = ctx_from_context(runtime.context)
        try:
            return await fn(*args, **kwargs)
        finally:
            restore_ctx(token)
    # 保留原函数签名供 @tool 推断 schema（ToolRuntime 参数会被 ToolNode 过滤注入）
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__annotations__ = {**fn.__annotations__, "runtime": ToolRuntime}
    return wrapper


# ======================================================================
#  浏览器类工具（全部不需要 context）
# ======================================================================

@tool
async def open_page(url: str, wait_until: str = "domcontentloaded", timeout: int = 30000, wait_shield: bool = True) -> dict:
    """打开一个 URL，并检测登录墙、验证码、反爬拦截。返回 {title, url, status_code, login_wall, captcha, anti_bot, detect}。
    wait_shield=True 时自动等待 Cloudflare 5 秒盾通过（一般保持默认）。"""
    return await _browser_tools().open_page(url, wait_until=wait_until, timeout=timeout, wait_shield=wait_shield)

@tool
async def get_page_state() -> dict:
    """获取当前页面完整状态：URL、标题、登录墙/验证码/5秒盾/滑块/反爬检测。Agent 每次打开页面后必须调用此工具判断是否有挑战。"""
    return await _browser_tools().get_page_state()

@tool
async def get_page_snapshot(max_text_len: int = 6000) -> str:
    """获取当前页面可见文本快照，供 LLM 阅读页面内容，分析结构。比 get_simplified_html 更轻量，优先使用。"""
    return await _browser_tools().get_page_snapshot(max_text_len=max_text_len)

@tool
async def get_simplified_html(max_len: int = 20000) -> str:
    """获取精简后的页面 HTML（保留 class/id 结构），供生成 CSS 选择器使用。"""
    return await _browser_tools().get_simplified_html(max_len=max_len)

@tool
async def scroll_page(direction: str = "down", amount: int = 1200, max_scrolls: int = 10) -> dict:
    """向下或向上滚动页面。用于无限滚动翻页、加载更多内容。"""
    return await _browser_tools().scroll_page(direction=direction, amount=amount, max_scrolls=max_scrolls)

@tool
async def click_element(selector: str = "", text: str = "", timeout: int = 5000) -> dict:
    """按 CSS 选择器或页面文本点击元素。用于翻页按钮、加载更多等交互操作。selector 和 text 二选一，优先用 selector。"""
    return await _browser_tools().click_element(selector=selector, text=text, timeout=timeout)

@tool
async def fill_input(selector: str, value: str) -> dict:
    """填写文本输入框（selector 为 CSS 选择器，value 为填入值）。模拟人类打字节奏：鼠标弧线→聚焦→Control+A 清空→逐字打字带随机停顿。用于表单填写、搜索框输入等场景。"""
    return await _browser_tools().fill_input(selector, value)

@tool
async def select_option(selector: str, value: str = "", label: str = "", index: int = -1, timeout: int = 5000) -> dict:
    """下拉框选择，同时支持原生 <select> 和自定义下拉（antd Select / el-select 等）。
    原生 select 直接用 Playwright selectOption（自动触发 change 事件）。
    自定义下拉自动点开后按文本匹配点击。
    value/label/index 三选一即可。"""
    return await _browser_tools().select_option(selector, value=value, label=label, index=index, timeout=timeout)

@tool
async def upload_file(selector: str, file_path: str) -> dict:
    """文件上传：向页面上的 <input type="file"> 元素传入本地文件路径。
    Playwright 自动处理被 UI 框架隐藏（display:none）的 input 场景。"""
    return await _browser_tools().upload_file(selector, file_path)

@tool
async def press_key(key: str = "Enter") -> dict:
    """在页面按下键盘按键（Enter/Escape/Tab 等），用于触发搜索、关闭弹窗等操作。"""
    return await _browser_tools().press_key(key)

@tool
async def evaluate_js(script: str, js_args: dict | None = None) -> object:
    """在当前页面执行 JavaScript 代码并返回结果。支持提取自定义数据、滚动到底部、统计元素数量等操作。
    内置经验库会自动修复大模型常见的 JS 错误（如顶层 return）。
    返回值为 JS 执行结果，可能是字符串、数字、列表或字典。"""
    return await _browser_tools().evaluate_js(script, js_args)

@tool
async def screenshot(path: str = "") -> dict:
    """对当前页面截图（极度消耗资源，仅在需要查看视觉元素或反爬挑战时调用，否则用 get_page_snapshot 代替）。"""
    return await _browser_tools().screenshot(path)

@tool
async def wait_for(what: str = "load", target: str = "", timeout: int = 15000) -> dict:
    """等待页面加载稳定或等待指定内容出现：what='load' 等待网络稳定，what='selector' 等待选择器出现，what='text' 等待文本出现。"""
    return await _browser_tools().wait_for(what=what, target=target, timeout=timeout)

@tool
async def check_challenges() -> dict:
    """检测当前页面是否存在验证码、5秒盾、滑块、登录墙等反爬挑战。返回挑战类型和详情，供 Agent 决定是否交人工。"""
    return await _browser_tools().check_challenges()

@tool
async def save_login_state() -> dict:
    """手动保存当前域名的登录态（cookies + localStorage + sessionStorage）。
    系统在导航时和登录挑战恢复后已自动保存，一般不需要手动调。
    但如果你知道用户刚刚完成了一个不走导航的纯 JS 登录，或者想立即落盘，
    可以显式调用此工具。下次重新访问该域名会自动复用这份登录态。"""
    return await _browser_tools().save_login_state()

@tool
async def get_login_states() -> dict:
    """列出所有已保存的登录态（按域名分文件）。返回每个域名的文件大小、最后保存时间。"""
    from app.browser.browser_manager import browser_manager
    return {"saved_states": await browser_manager.state_list()}


# ======================================================================
#  多标签页管理
# ======================================================================

@tool
async def list_tabs() -> dict:
    """列出当前浏览器所有标签页。返回 [{index, url, title, is_active}] 和 count。
    爬详情页前可以先看看有哪些标签页已打开。"""
    return await _browser_tools().list_tabs()

@tool
async def open_tab(url: str = "") -> dict:
    """新开一个标签页，可选立即导航到 url。返回新标签页的 index、url、title。
    常见用法：先 open_tab 打开列表页，再在列表页里点链接进入详情——详情页会自动在新标签页打开，列表页状态不会丢。"""
    return await _browser_tools().open_tab(url=url)

@tool
async def switch_tab(target: str) -> dict:
    """切到指定标签页。target 可以是 index 的字符串形式（如 '0'、'1'），也可以是 url 或 title 的关键字模糊匹配。
    列表→详情结构的网站：先在新标签页爬详情，爬完切回列表页继续。"""
    return await _browser_tools().switch_tab(target=target)

@tool
async def close_tab(target: str = "current") -> dict:
    """关闭标签页。target='current' 关当前页，或数字字符串（如 '1'）关指定页。只剩 1 个时不可关。
    详情页爬完关掉，自动切回相邻标签页。"""
    return await _browser_tools().close_tab(target=target)


# ======================================================================
#  鼠标悬停
# ======================================================================

@tool
async def hover_element(selector: str = "", text: str = "") -> dict:
    """鼠标悬停触发下拉菜单、tooltip、二级分类菜单等。selector 和 text 二选一，优先用 selector。
    电商的二级分类菜单、后台管理的操作下拉（编辑/删除）通常需要 hover 展开后才能点击内部元素。"""
    return await _browser_tools().hover_element(selector=selector, text=text)


# ======================================================================
#  iframe 切换
# ======================================================================

@tool
async def switch_iframe(target: str | None = None) -> dict:
    """切进 iframe 操作内部元素。target=null 或空切回主 frame；
    传 CSS 选择器（如 '#payment-frame'）、iframe name、或 frame url 关键字都可以。
    银行支付页、第三方登录弹窗、旧后台管理系统大量用 iframe，找不到元素时先确认是不是在 iframe 里。"""
    return await _browser_tools().switch_iframe(target=target)


# ======================================================================
#  文件下载
# ======================================================================

@tool
async def download_file(selector: str = "", text: str = "", url: str = "",
                        filename: str = "", timeout: int = 60000) -> dict:
    """触发下载并保存到本地。三种触发方式三选一：
    1. selector：点击页面上的下载按钮（CSS 选择器）
    2. text：点击页面上的下载按钮（按文本匹配，如「导出 CSV」「下载」）
    3. url：直接下载一个文件 URL（不走页面，最快，比如已知文件直链时）
    filename 可选，指定保存文件名；不传则用浏览器建议名。
    返回 {path, filename, size} —— 文件绝对路径、文件名、字节大小。"""
    return await _browser_tools().download_file(
        selector=selector, text=text, url=url, filename=filename, timeout=timeout)


# ======================================================================
#  解析类工具
# ======================================================================

@tool
async def extract_by_scheme(scheme: dict) -> list[dict]:
    """按提取方案（选择器模板）批量提取页面条目。方案需包含 item_selector 和 fields 字段。
    返回条目列表，每条为一个字典。"""
    return await _parse_tools().extract_by_scheme(scheme)

@tool
async def llm_extract(content: str, fields: list, item_hint: str = "", runtime: ToolRuntime = None) -> list[dict]:
    """用 LLM 从页面内容中结构化提取列表条目（兜底方案）。content 为页面文本，fields 为目标字段列表，每项含 name 字段。"""
    _rc, token = ctx_from_context(runtime.context)
    try:
        return await _parse_tools().llm_extract(content, fields, item_hint)
    finally:
        restore_ctx(token)


# ToolRuntime 无默认值（ToolNode 总在运行时注入）


# ======================================================================
#  存储类工具（除 list_tasks 外全部需要 context）
# ======================================================================

@tool
async def save_items(items: list[dict], page_no: int = 1, task_id: str = "", runtime: ToolRuntime = None) -> dict:
    """把提取到的数据条目批量写入数据库并推送实时预览。提取到任何数据后必须立即调用此工具保存，不要等全部抓完。
    参数 task_id 可留空（框架自动填充）。"""
    _rc, token = ctx_from_context(runtime.context)
    try:
        return await _storage_tools().save_items(items, page_no=page_no, task_id=task_id)
    finally:
        restore_ctx(token)

@tool
async def get_task(task_id: str = "", runtime: ToolRuntime = None) -> dict:
    """读取当前任务的基本信息（URL、指令、状态、已提取条数）。task_id 可留空自动取当前任务。"""
    _rc, token = ctx_from_context(runtime.context)
    try:
        return await _storage_tools().get_task(task_id=task_id)
    finally:
        restore_ctx(token)

@tool
async def list_items(task_id: str = "", limit: int = 200, runtime: ToolRuntime = None) -> dict:
    """读取已保存的数据条目（从数据库）。当用户说「数据呢」、「看看爬了什么」时调用。返回条目列表和字段名。task_id 可留空。"""
    _rc, token = ctx_from_context(runtime.context)
    try:
        return await _storage_tools().list_items(task_id=task_id, limit=limit)
    finally:
        restore_ctx(token)

@tool
async def export_data(format: str = "csv", task_id: str = "", runtime: ToolRuntime = None) -> dict:
    """把已保存的数据导出成文件（csv/excel/json/markdown/pdf/docx）。当用户说「导出」、「下载数据」时调用，不需要重新爬取。
    返回文件路径和下载链接。task_id 可留空自动取当前任务。"""
    _rc, token = ctx_from_context(runtime.context)
    try:
        return await _storage_tools().export_data(format=format, task_id=task_id)
    finally:
        restore_ctx(token)

@tool
async def list_tasks(limit: int = 10) -> dict:
    """列出最近的任务历史。当用户说「导出刚才的」、「之前爬了什么」时调用，用于确定用户指的是哪个任务。
    返回任务列表，含 id、指令、URL、状态、条目数、创建时间。"""
    return await _storage_tools().list_tasks(limit=limit)


# ======================================================================
#  智能类工具
# ======================================================================

@tool
async def probe_media(url: str) -> dict:
    """探测 URL 是否为可下载的视频/音频媒体，返回媒体类型（direct/fetchable/page）和基本信息（标题、时长）。
    下载前先调用此工具判断目标是否支持。"""
    return await _smart_tools().probe_media(url)

@tool
async def download_media(url: str) -> dict:
    """下载视频/音频到本地媒体目录，支持国内外主流站点及 m3u8 直链。返回落地文件路径和文件大小。"""
    return await _smart_tools().download_media(url)

@tool
async def solve_captcha(selector: str = "", fields: str = "") -> dict:
    """自动识别当前页面的验证码（本地 OCR，不联网）。识别成功返回 text，失败返回 ok=false 建议交人工。
    selector 留空则整页识别，或指定验证码图片元素的 CSS 选择器。"""
    return await _smart_tools().solve_captcha(selector=selector, fields=fields)

@tool
async def smart_extract(item_selector: str = "", fields: list | None = None, item_hint: str = "", runtime: ToolRuntime = None) -> dict:
    """智能提取：优先结构识别→正则→LLM 兜底。无需提前分析页面结构，Agent 首次探索时可直接调用。
    返回 {items: [...], source: "local|llm"}。"""
    _rc, token = ctx_from_context(runtime.context)
    try:
        return await _smart_tools().smart_extract(item_selector=item_selector, fields=fields, item_hint=item_hint)
    finally:
        restore_ctx(token)

@tool
async def resolve_target(message: str) -> dict:
    """把用户自然语言（如「帮我爬淘宝上2024新款手机」）解析成目标 URL 和爬取指令。新任务对话开始时第一个调用。
    返回 {url, instruction, original_message}。"""
    return await _smart_tools().resolve_target_tool(message)


# ======================================================================
#  会话控制
# ======================================================================

@tool
async def close_browser() -> dict:
    """关闭浏览器会话（browser-service + 所有标签页 + Chromium 进程）。
    用于「关闭浏览器」「结束任务」「退出」等场景。关闭后如果再需要浏览，
    系统会自动重新拉起浏览器进程（但登录态需要重新加载/重新登录）。"""
    from app.browser.browser_manager import browser_manager
    await browser_manager.close()
    return {"ok": True, "msg": "浏览器会话已关闭"}


# ======================================================================
#  高级：Proxy / 请求拦截 / 分页 / 断点续爬 / crawl config
# ======================================================================

@tool
async def proxy_status() -> dict:
    """查看当前代理池状态：配置了哪些代理、各域名绑定了哪个代理。"""
    from app.browser.browser_manager import browser_manager
    return await browser_manager.proxy_status()


@tool
async def proxy_rotate() -> dict:
    """强制轮换到下一个代理（同一 hostname 下次创建 context 时生效）。
    场景：当前 IP 被封/变慢，换个代理继续爬。
    注意：同一域名绑定的代理会重置，下次 open_page 时重新分配。"""
    from app.browser.browser_manager import browser_manager
    return await browser_manager.proxy_rotate()


@tool
async def block_add(pattern: str) -> dict:
    """追加一条 URL 屏蔽规则（正则）。匹配的 URL 会被 204 拦截不加载，加速页面 + 减少被识别特征。
    默认已屏蔽：字体、图标、埋点/广告脚本（cdn/tracker/analytics/hotjar/doubleclick 等）。
    pattern: 字符串正则，如 r"banner|advert" 或 ".font-awesome"  """
    from app.browser.browser_manager import browser_manager
    return await browser_manager.block_add(pattern)


@tool
async def block_reset() -> dict:
    """清空所有自定义屏蔽规则，恢复默认（字体+图标+埋点+广告）。"""
    from app.browser.browser_manager import browser_manager
    return await browser_manager.block_reset()


@tool
async def mock_add(matcher: str, body: str = "", status: int = 200,
                   content_type: str = "") -> dict:
    """添加一个请求 mock：匹配 URL 返回自定义内容。
    场景：SPA 页面数据全靠 XHR/fetch，拦截 API 返回 JSON 比渲染页面快 10 倍且不会被前端反爬。
    matcher: 字符串正则（如 r"/api/v1/products"）
    body:  要返回的内容（JSON 字符串或普通文本）
    status: HTTP 状态码（默认 200）
    content_type: 如 "application/json" """
    from app.browser.browser_manager import browser_manager
    return await browser_manager.mock_add(
        matcher=matcher, status=status, body=body, content_type=content_type,
    )


@tool
async def mock_clear() -> dict:
    """清空所有自定义 mock。"""
    from app.browser.browser_manager import browser_manager
    return await browser_manager.mock_clear()


@tool
async def smart_paginate(
    items_count: int,
    target_count: int,
    pagination_type: str = "auto",
) -> dict:
    """自动执行分页抓取：根据已爬条数和目标条数，智能判断并翻页继续。
    内部策略（auto）：优先点「下一页」按钮 → 再试无限滚动 → 再试「加载更多」→ 再试 URL 参数（?page=N）。
    items_count:  已保存的条目数（从 save_items / list_items 的返回里取）
    target_count: 目标总条数（用户要多少）
    pagination_type: "auto"（默认）/ "next_button" / "infinite_scroll" / "load_more" / "url_param" / "none"
    返回 {ok, pages_crawled, items_per_page, recommended_next} 供你决定下一步。"""
    from app.browser.browser_manager import browser_manager
    from app.core.run_manager import get_ctx

    if items_count >= target_count:
        return {"ok": True, "done": True, "msg": f"已达目标 {target_count} 条"}

    def _js_infinite_scroll() -> str:
        # 滚动到页面底部，触发懒加载
        return """
        (async () => {
            const before = document.querySelectorAll(
                '[class*="item"],[class*="card"],[class*="product"],[class*="job"],[class*="list-item"]'
            ).length;
            window.scrollTo(0, document.body.scrollHeight);
            await new Promise(r => setTimeout(r, 2000));
            const after = document.querySelectorAll(
                '[class*="item"],[class*="card"],[class*="product"],[class*="job"],[class*="list-item"]'
            ).length;
            return {before, after, grew: after > before};
        })()
        """

    async def _try_next_button() -> bool:
        js = """
        (() => {
            const selectors = [
                'a.next-page', 'a.next', '.pager-next', '.pagination .next',
                'li.next > a', 'a[rel="next"]', 'button.next',
                '.page-next', '.page_next', '[class*="next-page"]', '[class*="pagination-next"]'
            ];
            for (const s of selectors) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) {
                    el.click();
                    return {clicked: true, selector: s};
                }
            }
            // 文本兜底
            const allLinks = document.querySelectorAll('a, button');
            for (const el of allLinks) {
                const txt = (el.textContent || '').trim();
                if ((txt === '下一页' || txt === 'Next' || txt === '>' || txt === '›')
                    && el.offsetParent !== null) {
                    el.click();
                    return {clicked: true, text: txt};
                }
            }
            return {clicked: false};
        })()
        """
        res = await browser_manager.evaluate(js)
        return res.get("clicked", False)

    async def _try_scroll() -> bool:
        res = await browser_manager.evaluate(_js_infinite_scroll())
        return res.get("grew", False)

    # 根据 pagination_type 或 auto 探测执行
    tried = []
    if pagination_type in ("auto", "next_button"):
        if await _try_next_button():
            await browser_manager.evaluate("await new Promise(r => setTimeout(r, 1500))")
            return {"ok": True, "method": "next_button", "msg": "点击了下一页"}
        tried.append("next_button")

    if pagination_type in ("auto", "infinite_scroll"):
        if await _try_scroll():
            return {"ok": True, "method": "infinite_scroll", "msg": "滚动加载更多"}
        tried.append("infinite_scroll")

    if pagination_type in ("auto", "url_param"):
        current_url = await browser_manager.evaluate("location.href")
        import re
        m = re.search(r'[?&]page=(\d+)', current_url)
        if m:
            next_page = int(m.group(1)) + 1
            new_url = re.sub(r'([?&])page=\d+', rf'\1page={next_page}', current_url)
            await browser_manager.open_page(new_url)
            return {"ok": True, "method": "url_param", "url": new_url}
        tried.append("url_param")

    return {
        "ok": False,
        "msg": f"未能自动翻页（试了 {', '.join(tried)}），需要你手动操作或换目标网站",
    }


@tool
async def save_crawl_config(
    site_name: str,
    url_pattern: str,
    extract_fields: list[str],
    pagination_type: str = "auto",
    notes: str = "",
) -> dict:
    """把爬取经验总结成可复用的 JSON 配置（爬虫 profile），下次新任务可以直接加载不用再让 Agent 摸。
    场景：你成功爬了「京东机械键盘」，想记住「京东用 .gl-item 卡片、下一页是 .pager-next、按销量排 URL 参数 sort_param=...」。
    site_name: 站点标识（如 "jd_mechanical_keyboard"）
    url_pattern: URL 模板（如 "https://search.jd.com/Search?keyword={keyword}&page={page}"）
    extract_fields: 要提取的字段列表（如 ["title", "price", "shop", "url"]）
    pagination_type: 分页策略（auto / next_button / infinite_scroll / url_param）
    notes: 备注信息（反爬注意、登录要求等）"""
    import json, os, time
    config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "..", "..", "artifacts", "crawl_configs")
    os.makedirs(config_dir, exist_ok=True)
    safe_name = "".join(c for c in site_name if c.isalnum() or c in "_-").strip("_")
    if not safe_name:
        safe_name = f"crawl_{int(time.time())}"
    config = {
        "site_name": site_name,
        "url_pattern": url_pattern,
        "extract_fields": extract_fields,
        "pagination_type": pagination_type,
        "notes": notes,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    file_path = os.path.join(config_dir, f"{safe_name}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return {"ok": True, "path": file_path, "config": config}


# ======================================================================
#  全局列表（供 get_agent_tools() 使用）
# ======================================================================

ALL_AGENT_TOOLS: list = [
    open_page,
    get_page_state,
    get_page_snapshot,
    get_simplified_html,
    scroll_page,
    click_element,
    fill_input,
    select_option,
    upload_file,
    press_key,
    evaluate_js,
    screenshot,
    wait_for,
    check_challenges,
    save_login_state,
    get_login_states,
    list_tabs,
    open_tab,
    switch_tab,
    close_tab,
    hover_element,
    switch_iframe,
    download_file,
    extract_by_scheme,
    llm_extract,
    save_items,
    get_task,
    list_items,
    export_data,
    list_tasks,
    probe_media,
    download_media,
    solve_captcha,
    smart_extract,
    resolve_target,
    close_browser,
    # === 新增：Proxy / 拦截 / 分页 / 配置 ===
    proxy_status,
    proxy_rotate,
    block_add,
    block_reset,
    mock_add,
    mock_clear,
    smart_paginate,
    save_crawl_config,
]
