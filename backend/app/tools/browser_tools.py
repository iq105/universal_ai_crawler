"""浏览器原子工具（执行器）——全部转发到 Node.js browser-service

保持旧流水线（crawler_graph.py）的函数签名兼容；新增 Agent 模式需要的检测/等待类工具。
"""
from app.browser.browser_manager import browser_manager
from app.core.logger import get_logger

_log = get_logger("browser_tools")


async def open_page(url: str, wait_until: str = "domcontentloaded", timeout: int = 30000, wait_shield: bool = True) -> dict:
    res = await browser_manager.open_page(url, wait_until=wait_until, timeout=timeout, wait_shield=wait_shield)
    detect = res.get("detect") or {}
    # 兼容旧流水线字段
    out = {"title": res.get("title") or "", "url": res.get("url") or url, "status_code": res.get("status_code"),
        "login_wall": bool(detect.get("login")), "captcha": bool(detect.get("captcha")),
        "anti_bot": bool(detect.get("anti_bot") or detect.get("five_sec_shield") or detect.get("slider")),
        "detect": detect, "body_preview": res.get("body_preview") or "", }
    # B27.3：goto 超时/网络错误时带回错误信息（status_code 可能为 null），供上层判断
    if res.get("navigation_error"):
        out["navigation_error"] = res["navigation_error"]
    return out


async def get_page_state() -> dict:
    """获取页面完整状态：URL、标题、挑战检测（登录/验证码/5秒盾/滑块/反爬）"""
    return await browser_manager.page_state()


async def get_page_snapshot(max_text_len: int = 6000) -> str:
    return await browser_manager.snapshot(max_text_len=max_text_len)


async def get_simplified_html(max_len: int = 20000) -> str:
    return await browser_manager.html(max_len=max_len)


async def scroll_page(direction: str = "down", amount: int = 1200, max_scrolls: int = 10) -> dict:
    return await browser_manager.scroll(direction=direction, amount=amount, max_scrolls=max_scrolls)


async def click_element(selector: str = "", text: str = "", timeout: int = 5000) -> dict:
    return await browser_manager.click(selector=selector, text=text, timeout=timeout)


async def fill_input(selector: str, value: str) -> dict:
    return await browser_manager.fill(selector, value)


async def select_option(selector: str, value: str = "", label: str = "",
                        index: int = -1, timeout: int = 5000) -> dict:
    """下拉框选择：原生 <select> 用 Playwright 原生 selectOption；自定义下拉（antd/el-select）自动点开后按文本匹配。
    value / label / index 三选一即可。"""
    return await browser_manager.select_option(selector, value=value, label=label,
                                               index=index, timeout=timeout)


async def upload_file(selector: str, file_path: str) -> dict:
    """文件上传：向 <input type="file"> 传入本地文件路径。Playwright 自动处理隐藏 input（display:none）场景。"""
    return await browser_manager.upload_file(selector, file_path)


async def press_key(key: str = "Enter") -> dict:
    return await browser_manager.press(key)


async def evaluate_js(script: str, js_args: dict | None = None) -> object:
    """执行 JS 代码。内置经验库自动修复大模型常见错误：

    - 顶层 return → 自动包装成 async function（解决 Illegal return statement）
    - 顶层 await → 同上
    - 大模型经常生成的 document.xxx 小错误 → 自动修正
    Node.js 端也有一层 _wrapScript 兜底，两层保险。
    """
    fixed = _fix_llm_js(script or "")
    if fixed != script:
        _log.info("[evaluate_js] 经验库修复了 JS: %s → %s", script[:80], fixed[:80])
    return await browser_manager.evaluate(fixed, js_args)


# ---------- JS 经验库：大模型常犯的错误 ----------

_JS_FIX_RULES = [
    # 1. document.querySelector / querySelectorAll 可能被拼成 selectSingleNode / findElement 等
    ("document.querySelectorAll", "document.querySelectorAll"),
    ("document.selectSingleNode", "document.querySelector"),
    ("document.findElement", "document.querySelector"),
    ("document.getElementsByClass", "document.getElementsByClassName"),
    ("document.getElementByClass", "document.getElementsByClassName"),
    ("document.getElementsById", "document.getElementById"),
    # 2. textContent / innerText 经常搞混（没问题，但可提示）
    # 3. 常见拼写错误
    ("document.body.innertext", "document.body.innerText"),
    ("document.title.tolowercase", "document.title.toLowerCase"),
]


def _fix_llm_js(script: str) -> str:
    """Python 层经验库：在传给 Node.js 之前做正则修复。"""
    if not script:
        return script
    fixed = script
    for wrong, right in _JS_FIX_RULES:
        fixed = fixed.replace(wrong, right)
    return fixed


async def screenshot(path: str = "") -> dict:
    # path 为空时由 Node 端写入 artifacts/screenshots
    if not path:
        return await browser_manager.screenshot(dir="artifacts/screenshots")
    # 兼容旧签名：传入完整路径时取目录交给 Node 端，返回实际保存路径
    import os

    out_dir = os.path.dirname(path) or "artifacts/screenshots"
    res = await browser_manager.screenshot(dir=out_dir)
    return {"saved_path": res.get("saved_path") or path}


async def wait_for(what: str = "load", target: str = "", timeout: int = 15000) -> dict:
    """等待页面状态：load（网络稳定）/ selector / text"""
    if what == "load":
        await browser_manager.evaluate("() => new Promise(r => setTimeout(r, 1500))")
        return {"ok": True, "msg": "已等待网络稳定"}
    if what == "selector" and target:
        # 轮询目标出现
        for _ in range(int(timeout / 800)):
            exists = await browser_manager.evaluate(f"() => !!document.querySelector({target!r})")
            if exists:
                return {"ok": True, "msg": f"元素已出现: {target}"}
            await browser_manager.evaluate("() => new Promise(r => setTimeout(r, 800))")
        return {"ok": False, "msg": f"等待超时，元素未出现: {target}"}
    if what == "text" and target:
        for _ in range(int(timeout / 800)):
            text = await browser_manager.evaluate("() => document.body ? document.body.innerText : ''")
            if target in text:
                return {"ok": True, "msg": f"文本已出现: {target}"}
            await browser_manager.evaluate("() => new Promise(r => setTimeout(r, 800))")
        return {"ok": False, "msg": f"等待超时，文本未出现: {target}"}
    return {"ok": False, "msg": "wait_for 参数错误: what ∈ load|selector|text"}


async def check_challenges() -> dict:
    """检测当前页面挑战：登录墙/验证码/5秒盾/滑块/反爬（供 Agent 决策）"""
    return await browser_manager.page_state()


async def save_login_state() -> dict:
    """手动保存当前域名的登录态（cookies + localStorage + sessionStorage）。
    系统在导航时和登录挑战恢复后已自动保存，一般不需要手动调。
    但如果你知道用户刚刚完成了一个不走导航的纯 JS 登录，或者想立即落盘，
    可以显式调用此工具。下次重新访问该域名会自动复用这份登录态。"""
    return await browser_manager.save_state()


# ============ 多标签页管理 ============

async def list_tabs() -> dict:
    """列出当前浏览器 context 里所有标签页。返回每个标签页的 index、url、title、是否活跃。"""
    tabs = await browser_manager.tabs_list()
    return {"tabs": tabs, "count": len(tabs)}


async def open_tab(url: str = "") -> dict:
    """新开一个标签页，可选立即导航到 url。返回新标签页的 index、url、title。"""
    return await browser_manager.tabs_open(url=url)


async def switch_tab(target) -> dict:
    """切到指定标签页。target 可以是 int(index，0 开始)，也可以是 str（url 或 title 的关键字，模糊匹配）。"""
    return await browser_manager.tabs_switch(target=target)


async def close_tab(target="current") -> dict:
    """关闭标签页。target='current' 关当前页，或 int(index) 关指定页。只剩 1 个时不可关。"""
    return await browser_manager.tabs_close(target=target)


# ============ 鼠标悬停 ============

async def hover_element(selector: str = "", text: str = "") -> dict:
    """鼠标悬停触发下拉菜单、tooltip 等。selector 和 text 二选一，优先用 selector。
    电商的二级分类菜单通常需要 hover 展开后才能点击。"""
    return await browser_manager.hover(selector=selector, text=text)


# ============ iframe 切换 ============

async def switch_iframe(target: str | None = None) -> dict:
    """切进 iframe 操作内部元素。target=null 或空切回主 frame；
    传 CSS 选择器（如 '#payment-frame'）、iframe name、或 frame url 关键字都可以。"""
    return await browser_manager.switch_frame(target=target)


# ============ 文件下载 ============

async def download_file(selector: str = "", text: str = "", url: str = "",
                        filename: str = "", timeout: int = 60000) -> dict:
    """触发下载并保存到本地。三种触发方式三选一：
    1. selector：点击页面上的下载按钮（CSS 选择器）
    2. text：点击页面上的下载按钮（按文本匹配，如"导出 CSV"）
    3. url：直接下载一个文件 URL（不走页面，最快）

    filename 可选，指定保存文件名；不传则用浏览器建议名。
    返回 {path, filename, size} —— 文件绝对路径、文件名、字节大小。"""
    return await browser_manager.download_file(
        trigger_selector=selector,
        trigger_text=text,
        url=url,
        filename=filename,
        timeout=timeout,
    )
