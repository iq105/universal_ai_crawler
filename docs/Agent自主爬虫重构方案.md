# 万能爬虫智能化重构：Agent 全程自主控制浏览器（Node 版 Playwright）

## Context（背景）

当前爬虫是"固定流水线"（意图→加载→分析→方案→提取→翻页→校验→入库），不够智能：
- 无自主探索能力，遇到异常只会按固定路径重试
- 无浏览器隐身与会话保持，易触发反爬
- 只能表单输入 URL+指令，不能对话指挥
- 5秒盾/滑块/验证码无对策，直接中断
- 只支持文本数据，不支持视频/音频

用户确认的方向：
1. **Agent 全程自主控制浏览器**（LangGraph ReAct 探索-决策循环），用户对话指挥
2. **有头浏览器**（HEADLESS 默认 false），人工介入可直接操作弹出窗口
3. **验证码/5秒盾/滑块：全部依赖人工**（检测到即中断，人工处理后"继续"）；做浏览器隐身减少触发
4. **Playwright 使用 JS 版**（不用 Python 版）——JS 生态反检测资源更成熟（playwright-extra、stealth 插件、指纹随机化），更利于模拟人工操作
5. **导出格式**：Word、Excel、PDF、JSON
6. **视频/音频爬取**（yt-dlp，独立阶段）

## 总体架构

```
┌────────────────────────────────────────────────────────────┐
│  Vue3 前端 (5173)  对话区 · 任务面板 · 数据预览 · 人工介入    │
└──────────────────────────┬─────────────────────────────────┘
                           │ REST + SSE
┌──────────────────────────▼─────────────────────────────────┐
│  FastAPI 后端 (8000)                                       │
│  LangGraph Agent 循环（agent_think ⇄ agent_act）            │
│  ToolRegistry（Python executor → HTTP 调用 browser-service）│
└──────────────────────────┬─────────────────────────────────┘
                           │ REST (httpx, 原子操作)
┌──────────────────────────▼─────────────────────────────────┐
│  browser-service（Node.js 独立进程，后端 lifespan 自动拉起） │
│  Express + playwright-js + stealth 指纹伪装                │
│  单 Chromium 有头实例 · storage_state 会话持久化            │
└────────────────────────────────────────────────────────────┘
```

**为什么 Node 服务独立进程**：浏览器操作与反检测集中在 JS 生态；Python 侧 LangGraph/工具/事件逻辑零改动（executor 内部由"直接调用 Playwright"改为"HTTP 调 Node 服务"）；人工介入时 Node 端有头窗口可直接操作。

## 核心架构：单图双入口

在 [crawler_graph.py](file:///g:/wokspace_python/universal_crawler/backend/app/services/crawler_graph.py) 中**保留旧流水线全部节点**，新增 Agent 循环节点。`graph_mode`（`"pipeline"|"agent"`）决定入口路由：

```
START → route_entry ──(pipeline)──→ intent_understand → …原流水线… → store
                  └──(agent)──→ agent_think ⇄ agent_act（ReAct 循环，max_steps）
                                     │ done / ask_human / 超步数
                                     ▼
                                human_intervene（共享节点，resume 后 agent 模式回 agent_think）
```

**为什么单图双入口**：checkpointer 零迁移，旧任务 checkpoint 引用的节点名全部保留，旧任务 resume 完全兼容；顶部表单入口与新对话入口并行。

**状态扩展**（CrawlerState 追加，全部可选键向后兼容）：
```python
graph_mode: str      # "pipeline" | "agent"
messages: list       # [{"role","content"}] 对话/工具观察历史
page_state: dict     # {url,title,text,detect:{login,captcha,5s_shield,anti_bot}}
next_action: dict    # {"name","args"}
step_count: int
max_steps: int
plan: str
done: bool
final_answer: str
pending_human_message: str
```

**运行中对话指挥**：不二次 interrupt，用进程内 per-task steering 队列（新文件 `core/steering.py`，`dict[str, asyncio.Queue]`）。`/api/chat` 对 running 任务只入队回 `chat_ack`；`agent_think` 每轮 drain 队列把新指令注入对话。

## Phase 1：配置与依赖

- `backend/app/config.py`：`headless=False`（有头默认）、`stealth_enabled=True`、`storage_state_path=""`、`max_steps=30`、`media_dir="artifacts/media"`、`browser_service_url="http://127.0.0.1:8765"`、`browser_service_auto_start=True`
- `.env.example`：同步新增，注释说明 HEADLESS 可覆盖
- **backend/requirements.txt**：移除 `playwright`、`playwright-stealth`；追加 `httpx>=0.27`、`python-docx>=1.1.0`、`reportlab>=4.0.0`、`yt-dlp>=2024.8.6`
- **新目录 `browser-service/`**（Node.js 微服务）：
  - `package.json`：`express`、`playwright`、`playwright-extra`、`puppeteer-extra-plugin-stealth`、`cors`
  - `server.js`：Express 入口，路由注册、健康检查、启动参数（port/headless/storage_state_path）
  - `browser.js`：浏览器单例（有头/无头由 env 控制、UA/时区/视口、storage_state 载入/落盘）
  - `stealth.js`：指纹伪装——优先 `puppeteer-extra-plugin-stealth`；不兼容时降级手动 `addInitScript`（覆盖 `navigator.webdriver`、plugins、languages、`window.chrome`、WebGL vendor/renderer、hardwareConcurrency、fonts），并随机化 UA 池
  - `routes.js`：原子操作路由（见 Phase 2）
- `backend/app/core/events.py` 新增：`EVENT_TASK_CREATED`、`EVENT_CHAT_ACK`、`EVENT_PLAN`

## Phase 2：Node 浏览器服务（重写浏览器层）

**Node 侧路由（REST，统一 JSON 请求/响应）**：

| 路由 | 功能 |
| --- | --- |
| `GET /health` | 服务健康 + 浏览器状态 |
| `POST /open_page` | `{url, wait_until, timeout}` 打开页面 + 挑战检测 |
| `POST /get_page_snapshot` | `{max_text_len}` 可见文本 |
| `POST /get_simplified_html` | `{max_len}` 精简 HTML |
| `POST /get_page_state` | `{max_text_len}` → `{url,title,text,detect}` |
| `POST /check_challenges` | 统一挑战检测：URL正则/状态码(401,403,429)/关键词（captcha、geetest、`Just a moment`、`Checking your browser`、cf-challenge、access denied、访问过于频繁、安全验证）+ iframe 检测 → `{detected, kind: login\|captcha\|5s_shield\|anti_bot\|none, confidence}` |
| `POST /scroll_page` | `{direction, amount, max_scrolls}` |
| `POST /scroll_to_element` | `{selector, text}` |
| `POST /click_element` | `{selector, text, timeout, force}` |
| `POST /fill_input` | `{selector, value}` |
| `POST /evaluate_js` | `{script, args}` |
| `POST /expand_all` | `{selector, max_clicks}` |
| `POST /list_interactive_elements` | `{max}` 收集 button/a/input/select/textarea |
| `POST /screenshot` | `{path}` |
| `POST /wait_network_idle` | `{timeout}` |
| `POST /save_storage_state` | `{path}` 会话落盘 |

**Python 侧（backend/app/browser/browser_manager.py 重写）**：
- 改为 HTTP 客户端：`httpx.AsyncClient` 封装，提供 `call(path, **payload) -> dict`；`lifespan` 自动拉起 Node 子进程（`node browser-service/server.js`，`browser_service_auto_start=True` 时），带健康检查轮询与失败重启；`close()` 时终止子进程
- 保留模块级单例 `browser_manager`；`get_page()` 移除（工具不再需要 page 对象）
- `browser_tools.py` 各 executor 内部改为 `await browser_manager.call("/open_page", ...)`，**工具注册表/事件审计/schema 全部不变**
- `browser_service_auto_start=False` 时视为外部服务（独立 `npm start` 启动），仅健康检查

## Phase 3：新工具集（tools/）

注册到 [tools/__init__.py](file:///g:/wokspace_python/universal_crawler/backend/app/tools/__init__.py)（沿用 TOOL_SCHEMAS/TOOL_EXECUTORS/_DESCRIPTIONS 模式，description 一句话说清用途）：

| 工具 | 功能 |
| --- | --- |
| `check_challenges` | 转调 Node `/check_challenges` |
| `get_page_state` | 转调 Node `/get_page_state`，供 agent 每轮读取 |
| `wait_network_idle` | 转调 Node `/wait_network_idle` |
| `scroll_to_element` | 转调 Node `/scroll_to_element` |
| `expand_all` | 转调 Node `/expand_all` |
| `list_interactive_elements` | 转调 Node `/list_interactive_elements` |
| `save_session_state` | 转调 Node `/save_storage_state` |
| `ask_human`（哨兵） | 求助人工 → 中断 |
| `finish_task`（哨兵） | 带摘要收尾 → store |
| `update_plan`（哨兵） | 更新计划并推送 EVENT_PLAN |

- `click_element` schema 加 `force`；`save_items` 的 `task_id` 为空时回退 `get_ctx().task_id`

## Phase 4：Agent 循环（核心）

**新文件** `backend/app/services/agent_nodes.py`：
- `agent_think`：drain steering → 组装消息（System + 裁剪后历史 + 最新 page_state）→ `get_chat_model().bind_tools(tool_registry.schemas())` 调用 → thought 推 `assistant_reasoning` → 有 tool_calls 则写 `next_action`，无则 `done=True + final_answer`；bind_tools 异常降级 `llm_json` 结构化（同路径）
- `agent_act`：`step_count+1`；特判 `ask_human`（→needs_intervene/human_ask）、`finish_task`（→done）、`update_plan`；其余 `tool_registry.execute`，成功/异常都写 observation 进 messages；页面变更类工具（open_page/click/scroll/fill/evaluate_js/wait_network_idle/scroll_to_element/expand_all）执行后刷新 page_state
- 路由：`done→store`；`needs_intervene→human_intervene`；`step_count>=max_steps`→有数据则 store（部分结果），无数据则中断求助；否则回 `agent_think`
- `_trim_messages(limit=20)`：只保留最新 page_state 快照（6000字符截断），防上下文膨胀

**新文件** `backend/app/core/steering.py`：`SteeringChannel.enqueue/drain/unregister` + 模块单例 `steering`

**crawler_graph.py**：新增 `route_entry`、`agent_think`/`agent_act` 节点与条件边；`human_intervene` payload 加 `human_ask`；resume 后 agent 模式一律回 `agent_think`；agent 模式 resume 时若有 storage_state 配置则触发 Node `/save_storage_state`；`store` 节点 agent 模式用 `final_answer` 覆盖完成提示；`initial_state_for(task, graph_mode="pipeline")` 注入 agent 初始状态

## Phase 5：对话 API（/api/chat，SSE）

**新文件** `backend/app/api/streaming.py`：从 runs.py 迁出 `CancellationManager`/`_consume`/`_sse_stream`（逻辑不变），增加 steering register/unregister、可选 `created_event`（stream 开头发 `task_created`）
**runs.py**：改为从 streaming.py import（端点不变）
**新文件** `backend/app/api/chat.py` + `schemas/chat.py`：
- `POST /api/chat` body `{message, task_id?}`：
  - 有 task_id 且 running → `steering.enqueue` → 回 `chat_ack` 短流
  - 有 task_id 且 waiting_interrupt → 复用 `_sse_stream(resume)`（聊天输入与 InterruptDialog 等价续跑）
  - 有 task_id 且已结束 → 提示开新任务
  - 无 task_id（新会话）→ 正则提取 URL 线索 → `create_task` → `initial_state_for(task, graph_mode="agent")` → `_sse_stream`（先发 `task_created`）
- **main.py**：`include_router(chat.router)`

**SSE 事件协议**：复用现有 events.py 模式；新增 `task_created {task_id,url}`、`chat_ack {message}`、`plan {text}`；其余沿用（task_status/assistant_reasoning/tool_start/tool_result/items_batch/interrupt/done/error）

## Phase 6：前端对话 + 导出

**ChatPanel.vue**：底部加输入区（textarea 回车发送 + 按钮，`sending` prop），`emit("send", text)`
**App.vue**：
- `handleChatSend(text)`：中断中→`resumeTask`；运行中→`steerChat`；否则→`newChat`
- `newChat`：发 `/chat` SSE 新任务流（复用 `ssePost`）；`dispatchEvent` 加 `task_created`（设 currentTaskId+refreshTasks）、`chat_ack`、`plan`
- `steerChat`：发 `/chat` 带 task_id
**store.js**：加 `plan` 字段
**TaskPanel.vue**：导出按钮区加 **DOCX / PDF**（`download('docx'/'pdf')`）
**ToolCard.vue**：新工具中文 label 映射 + 降噪（check_challenges/get_page_state 等收进 HIDDEN_NAMES）

## Phase 7：导出 Word/PDF（export_service.py）

- `_export_docx(data)`：python-docx 建表（`_union_keys` 表头+行），中文原生支持
- `_export_pdf(data)`：reportlab，注册 CID 字体 `UnicodeCIDFont("STSong-Light")`（中文无需字体文件），Table+Paragraph
- `export()` 加 `docx/pdf` 分支；`api/export.py` format 白名单扩为 `csv,json,xlsx,docx,pdf`

## Phase 8：视频/音频下载（独立阶段）

**新文件** `backend/app/tools/media_tools.py`：
- `download_media(url, format="best", extract_audio=False)`：`yt_dlp` 惰性 import（缺失返回友好错误），`asyncio.to_thread` 防阻塞，返回 `{ok, files[], title, duration}`；m3u8 原生支持；ffmpeg 缺失提示安装
- 注册 `download_media` 工具；下载结果走 `save_items` 落库
- 外部可选依赖：ffmpeg（`winget install Gyan.FFmpeg`），.env 配 `FFMPEG_PATH`

## 验证方案

准备两个本地 mock 页（临时，不入库）：`mock_shield.html`（"Just a moment…5秒后自动进入"）、`mock_login.html`（表单+cookie 门）

| 场景 | 预期 |
| --- | --- |
| 后端启动 | lifespan 自动拉起 browser-service，`/health` 通过，`navigator.webdriver=false` |
| 对话"帮我抓 quotes.toscrape.com 名言和作者，翻到第2页" | 弹出有头 Chromium，自动 open→检测→提取→翻页→保存，共20条 |
| 打开 mock_shield.html | 检出 5s_shield→中断弹窗→用户在真实窗口等5秒→"继续"→resume 重检通过 |
| 打开 mock_login.html | 检出 login→中断→用户窗口手动登录→"继续"→storage_state 落盘，第二任务免登录 |
| 抓取中发"够了，停" | `chat_ack` 确认，Agent 下一轮提前 finish |
| 发"你能做什么？" | Agent 直接回答（0条数据，提示为 final_answer 非"保存0条"） |
| 导出 | CSV/JSON/XLSX/DOCX/PDF 均 200，DOCX/PDF 中文无乱码 |
| 旧流水线回归 | 顶部表单入口正常（浏览器操作同样走 Node 服务），旧 waiting_interrupt 任务 resume 正常 |
| 冒烟 | 图编译通过无 KeyError；browser-service 路由全部响应 |

## 风险与注意

1. **stealth 兼容**：新版 Chromium 与 `puppeteer-extra-plugin-stealth` 有兼容问题——Node 侧 try/fallback 手动 addInitScript 双保险，验证时重点观察 `navigator.webdriver` 与指纹值
2. **Node 服务生命周期**：由后端 lifespan 自动拉起/终止；崩溃时健康检查失败自动重启；`browser_service_auto_start=False` 支持外部独立启动（调试用）
3. **通信开销**：每个原子操作一次 HTTP 调用，延迟毫秒级可接受；后续如需高频可换 WebSocket（本期不做）
4. **上下文膨胀**：`_trim_messages` 限 20 条 + 快照截断；如仍大后续可加摘要节点（本期不做）
5. **单页限制**：Node 服务单 context 单 page，Agent 靠工作纪律"先提取再导航"
6. **有头部署**：服务器需真实桌面/虚拟显示；HEADLESS 保持可配置
7. **工具数量膨胀**（约20个）：description 精炼，"何时用/给什么参数"
8. **yt-dlp 依赖较重**：惰性 import + try/except，不装也能跑其他功能
