"""工具注册入口：定义 TOOL_SCHEMAS / TOOL_EXECUTORS 并注册到 ToolRegistry"""
from app.core.registry import tool_registry
from app.tools import browser_tools, parse_tools, smart_tools, storage_tools

TOOL_SCHEMAS = {"open_page": {"type": "object",
    "properties": {"url": {"type": "string", "description": "要打开的完整 URL"},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"]},
        "timeout": {"type": "integer"}, }, "required": ["url"], },
    "get_page_snapshot": {"type": "object", "properties": {"max_text_len": {"type": "integer"}}, },
    "get_simplified_html": {"type": "object", "properties": {"max_len": {"type": "integer"}}, },
    "scroll_page": {"type": "object",
        "properties": {"direction": {"type": "string", "enum": ["down", "up"]}, "amount": {"type": "integer"},
            "max_scrolls": {"type": "integer"}, }, }, "click_element": {"type": "object",
        "properties": {"selector": {"type": "string"}, "text": {"type": "string"}, "timeout": {"type": "integer"}, }, },
    "evaluate_js": {"type": "object", "properties": {"script": {"type": "string"}, "args": {"type": "object"}, },
        "required": ["script"], },
    "fill_input": {"type": "object", "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
        "required": ["selector", "value"], },
    "select_option": {"type": "object",
        "properties": {"selector": {"type": "string", "description": "下拉框元素的 CSS 选择器"},
            "value": {"type": "string", "description": "要选中的 option value（原生 select 用）"},
            "label": {"type": "string", "description": "要选中的 option 文本（原生 select 用，或自定义下拉按文本匹配）"},
            "index": {"type": "integer", "description": "要选中的选项索引（原生 select 或自定义下拉都支持，-1 表示不使用）"},
            "timeout": {"type": "integer"}}, "required": ["selector"], },
    "upload_file": {"type": "object",
        "properties": {"selector": {"type": "string", "description": "文件上传 input 的 CSS 选择器"},
            "file_path": {"type": "string", "description": "本地文件的绝对路径"}}, "required": ["selector", "file_path"], },
    "screenshot": {"type": "object", "properties": {"path": {"type": "string"}}, },
    "get_page_state": {"type": "object", "properties": {}, }, "wait_for": {"type": "object",
        "properties": {"what": {"type": "string", "enum": ["load", "selector", "text"], "description": "等待对象类型"},
            "target": {"type": "string", "description": "选择器或文本（what=load 时忽略）"},
            "timeout": {"type": "integer"}, }, "required": ["what"], },
    "check_challenges": {"type": "object", "properties": {}, }, "press_key": {"type": "object",
        "properties": {"key": {"type": "string", "description": "按键名，如 Enter/Escape/Tab"}}, },
    "extract_by_scheme": {"type": "object", "properties": {"scheme": {"type": "object"}}, "required": ["scheme"], },
    "llm_extract": {"type": "object",
        "properties": {"content": {"type": "string"}, "fields": {"type": "array", "items": {"type": "object"}},
            "item_hint": {"type": "string"}, }, "required": ["content", "fields"], }, "save_items": {"type": "object",
        "properties": {"task_id": {"type": "string"}, "items": {"type": "array", "items": {"type": "object"}},
            "page_no": {"type": "integer"}, }, "required": ["task_id", "items"], },
    "get_task": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"], },
    "probe_media": {"type": "object",
        "properties": {"url": {"type": "string", "description": "要探测的媒体地址(视频/音频 URL 或直链)"}},
        "required": ["url"], }, "download_media": {"type": "object",
        "properties": {"url": {"type": "string", "description": "要下载的媒体地址(视频/音频/m3u8 直链)"}},
        "required": ["url"], }, "solve_captcha": {"type": "object",
        "properties": {"selector": {"type": "string", "description": "验证码图片元素的选择器，留空则整页 OCR"},
            "fields": {"type": "string", "description": "可选的验证码类型说明"}, }, },
    "smart_extract": {"type": "object",
        "properties": {"item_selector": {"type": "string", "description": "列表条目选择器(可选)，提供则优先"},
            "fields": {"type": "array", "items": {"type": "object"},
                "description": "目标字段定义(可选): [{name, selector}]", },
            "item_hint": {"type": "string", "description": "条目结构提示(可选)"}, }, },
    "resolve_target": {"type": "object",
        "properties": {"message": {"type": "string", "description": "用户的自然语言请求，如'爬淘宝2024新款手机'"}},
        "required": ["message"], },
    "list_items": {"type": "object",
        "properties": {"task_id": {"type": "string", "description": "任务ID，留空自动用当前任务"},
            "limit": {"type": "integer", "description": "最多返回多少条，默认200"}}, },
    "export_data": {"type": "object",
        "properties": {"format": {"type": "string", "enum": ["csv", "excel", "json", "markdown", "pdf", "docx"],
            "description": "导出格式"}, "task_id": {"type": "string", "description": "任务ID，留空自动用当前任务"}},
        "required": ["format"], },
    "list_tasks": {"type": "object",
        "properties": {"limit": {"type": "integer", "description": "最多列出多少条最近任务，默认10"}}, },
    "list_tabs": {"type": "object", "properties": {}, },
    "open_tab": {"type": "object", "properties": {"url": {"type": "string", "description": "可选，立即导航到的 URL"}}, },
    "switch_tab": {"type": "object", "properties": {"target": {"description": "index 数字或 url/title 关键字"}}, "required": ["target"], },
    "close_tab": {"type": "object", "properties": {"target": {"description": "'current' 或 index 数字"}}, },
    "hover_element": {"type": "object",
        "properties": {"selector": {"type": "string"}, "text": {"type": "string"}}, },
    "switch_iframe": {"type": "object", "properties": {"target": {"description": "CSS 选择器 / name / url 关键字；不传切回主 frame"}}, },
    "download_file": {"type": "object",
        "properties": {"selector": {"type": "string", "description": "触发下载的按钮 CSS 选择器"},
            "text": {"type": "string", "description": "触发下载的按钮文本"},
            "url": {"type": "string", "description": "直接下载的文件 URL"},
            "filename": {"type": "string"}, "timeout": {"type": "integer"}}, },
}

TOOL_EXECUTORS = {"open_page": browser_tools.open_page,
                  "get_page_snapshot": browser_tools.get_page_snapshot,
    "get_simplified_html": browser_tools.get_simplified_html,
                  "scroll_page": browser_tools.scroll_page,
    "click_element": browser_tools.click_element, "evaluate_js": browser_tools.evaluate_js,
    "fill_input": browser_tools.fill_input, "select_option": browser_tools.select_option,
    "upload_file": browser_tools.upload_file, "screenshot": browser_tools.screenshot,
    "get_page_state": browser_tools.get_page_state, "wait_for": browser_tools.wait_for,
    "check_challenges": browser_tools.check_challenges, "press_key": browser_tools.press_key,
    "extract_by_scheme": parse_tools.extract_by_scheme, "llm_extract": parse_tools.llm_extract,
    "save_items": storage_tools.save_items, "get_task": storage_tools.get_task, "probe_media": smart_tools.probe_media, "download_media": smart_tools.download_media,
    "solve_captcha": smart_tools.solve_captcha, "smart_extract": smart_tools.smart_extract,
    "resolve_target": smart_tools.resolve_target_tool,
    "list_items": storage_tools.list_items, "export_data": storage_tools.export_data,
    "list_tasks": storage_tools.list_tasks,
    "list_tabs": browser_tools.list_tabs, "open_tab": browser_tools.open_tab,
    "switch_tab": browser_tools.switch_tab, "close_tab": browser_tools.close_tab,
    "hover_element": browser_tools.hover_element,
    "switch_iframe": browser_tools.switch_iframe,
    "download_file": browser_tools.download_file, }

_DESCRIPTIONS = {"open_page": "打开一个 URL，并检测登录墙、验证码、反爬拦截",
    "get_page_snapshot": "获取当前页面可见文本快照（供 LLM 阅读页面内容）",
    "get_simplified_html": "获取精简后的页面 HTML（保留 class/id 结构，供生成选择器）",
    "scroll_page": "向下/向上滚动页面（无限滚动翻页）", "click_element": "按 CSS 选择器或文本点击元素（翻页按钮/加载更多）",
    "evaluate_js": "在页面执行 JS，返回结果（数据提取核心）", "fill_input": "填写文本输入框，模拟人类打字节奏（鼠标弧线→聚焦→Control+A 清空→逐字打字带随机停顿）。用于表单填写、搜索框输入等场景。",
    "select_option": "下拉框选择，同时支持原生 <select> 和自定义下拉（antd Select / el-select 等）。value/label/index 三选一即可，自定义下拉会自动点开后按文本匹配点击。",
    "upload_file": "文件上传：向页面上的 <input type=\"file\"> 传入本地文件。Playwright 自动处理被 UI 框架隐藏（display:none）的 input。",
    "screenshot": "对当前页面截图", "get_page_state": "获取页面完整状态：URL、标题、登录墙/验证码/5秒盾/滑块/反爬检测",
    "wait_for": "等待页面加载稳定，或等待指定选择器/文本出现",
    "check_challenges": "检测当前页面是否存在验证码、5秒盾、滑块、登录墙等反爬挑战",
    "press_key": "在页面按下键盘按键（Enter/Escape/Tab 等）", "extract_by_scheme": "按提取方案（选择器模板）批量提取条目",
    "llm_extract": "用 LLM 从页面内容中结构化提取条目（兜底方案）", "save_items": "把提取的条目批量写入数据库并推送预览",
    "get_task": "读取任务信息", "probe_media": "探测 URL 是否为可下载媒体(视频/音频)，返回标题时长等；可判断目标类型",
    "download_media": "下载视频/音频到媒体目录，返回落地文件路径(支持国内外站点及m3u8直链)",
    "solve_captcha": "自动识别当前页面验证码(本地OCR)，识别不了会提示交人工",
    "smart_extract": "智能提取：优先结构识别/选择器，再正则，最后LLM兜底；返回结构化条目",
    "resolve_target": "把用户自然语言（如'爬淘宝2024新款手机'）解析成目标URL和爬取指令。开新任务时第一个调用。",
    "list_items": "读取已保存的数据条目（从数据库）。用户说'数据呢'、'看看爬了什么'时调用。返回完整条目+字段名。",
    "export_data": "把已保存的数据导出成文件（csv/excel/json/markdown/pdf/docx），返回文件路径。用户说'导出'、'下载数据'时调用。不用重新爬取，数据已经在DB里。",
    "list_tasks": "列出最近的任务历史。用户说'导出刚才的'、'之前爬了什么'时调用，帮你找到用户指的是哪个任务。",
    "list_tabs": "列出当前浏览器所有标签页。返回每个标签页的 index、url、title、是否活跃。爬详情页前先看看有哪些标签页已打开。",
    "open_tab": "新开一个标签页，可选立即导航到 url。列表→详情结构的网站推荐这样用——详情页在新标签页打开，列表页状态不会丢。",
    "switch_tab": "切到指定标签页。target 可以是 index 数字（如 1）或 url/title 关键字（如 'item.jd'）。",
    "close_tab": "关闭标签页。target='current' 关当前页，或 index 数字。只剩 1 个时不可关。",
    "hover_element": "鼠标悬停触发下拉菜单、tooltip、二级分类菜单等。电商的二级分类菜单、后台管理的操作下拉通常需要 hover 展开后才能点击。",
    "switch_iframe": "切进 iframe 操作内部元素；不传 target 切回主 frame。银行支付页、第三方登录弹窗、旧后台管理系统大量用 iframe。",
    "download_file": "触发下载并保存到本地。三种方式三选一：selector（点按钮）/ text（按文本点）/ url（直接下载文件直链）。返回 {path, filename, size}。", }


def register_all() -> None:
    for name, schema in TOOL_SCHEMAS.items():
        tool_registry.register(name, schema, TOOL_EXECUTORS[name], description=_DESCRIPTIONS.get(name, name), )


def get_agent_tools() -> list:
    """返回 @tool 装饰的 BaseTool 数组，直接丢给 create_agent(tools=[...])。

    agent 模式与 pipeline 模式共享同一套底层实现：
    - agent 用 @tool 版本（框架的 ToolNode 自动做消息往返/审计）
    - pipeline 用 TOOL_EXECUTORS 字典直接调底层函数（不经 create_agent）
    两条通道独立，互不影响。
    """
    from app.tools.agent_tools import ALL_AGENT_TOOLS
    return list(ALL_AGENT_TOOLS)


register_all()
