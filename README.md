# Universal AI Crawler — LLM 驱动的万能浏览器爬虫 Agent

> 一款让大模型自主操控浏览器、像人一样抓取任意网站数据的智能爬虫框架。支持反爬对抗、代理轮换、断点续爬、多标签页、SPA 请求拦截等生产级能力。

Universal AI Crawler 是一个基于 **LangGraph + Playwright + FastAPI** 的 LLM Agent 爬虫系统。你只需要用自然语言告诉它"帮我抓京东搜索『机械键盘』前 100 条的标题/价格/店铺"，它会：

1. 🤖 自主打开浏览器、导航、点击、翻页
2. 🛡️ 自动检测并绕过反爬（5秒盾、滑块、验证码 OCR、登录墙）
3. 💾 每抓一批立即入库（支持断点续爬）
4. 📊 自动导出 CSV/Excel/JSON/Markdown
5. 🔄 同一域名自动复用登录态，跨域名代理轮换

---

## ✨ 核心特性

### 🤖 LLM Agent 自主驱动
- **LangGraph create_agent**：44 个原子工具，大模型自主决策用哪个
- **Context Editing Middleware**：自动裁剪历史消息，控制 token 预算
- **Steering 队列**：运行中可注入新指令（"顺便也抓评论"）
- **Human-in-the-loop**：需要登录/验证码时暂停，用户处理完自动继续

### 🛡️ 反爬对抗
- **手写 Stealth Init Script**：覆盖 webdriver / plugins / WebGL / window.chrome / permissions / toString
- **域名级 Context 隔离**：每个主域名一个 Playwright context，按 hostname 自动切换
- **Proxy 轮换池**：`PROXIES=socks5://ip1:port,socks5://ip2:port` 跨域名轮换代理
- **请求屏蔽**：默认屏蔽字体/图标/埋点/广告（加速 + 减少特征）
- **SPA API 拦截**：`mock_add(matcher=r"/api/xxx", body=...)` 拦截 XHR 返回假数据

### 🌐 浏览器控制（44 个工具）

| 类别 | 工具 |
|---|---|
| 导航 | `open_page` / `scroll_page` / `wait_for` |
| 交互 | `click_element` / `fill_input` / `select_option` / `press_key` / `hover_element` |
| 多标签 | `list_tabs` / `open_tab` / `switch_tab` / `close_tab` |
| iframe | `switch_iframe` |
| 文件 | `upload_file` / `download_file` |
| 页面信息 | `get_page_state` / `get_page_snapshot` / `get_simplified_html` / `evaluate_js` |
| 反爬 | `check_challenges` / `solve_captcha` / `screenshot` |
| 提取 | `extract_by_scheme` / `llm_extract` / `smart_extract` |
| 数据 | `save_items`（自动按 url/title 去重）/ `list_items` / `export_data`（csv/excel/json/md/pdf/docx） |
| 登录态 | `save_login_state` / `get_login_states`（按域名分文件持久化） |
| 代理/拦截 | `proxy_status` / `proxy_rotate` / `block_add` / `block_reset` / `mock_add` / `mock_clear` |
| 分页 | `smart_paginate`（next_button / infinite_scroll / url_param 自动探测） |
| 其他 | `probe_media` / `download_media` / `save_crawl_config` / `close_browser` |

### 💾 数据与恢复
- **LangGraph Checkpointer**：SQLite 或 PostgreSQL 持久化对话历史 + 中间状态
- **断点续爬**：崩了重开 → Agent 自动看到"已存 N 条"提示，从第 N+1 条继续
- **按域名登录态**：`artifacts/browser/storage/{domain}.json` 自动保存/加载
- **失败自动去重**：`save_items` 按 url/title 自动跳过重复条目

### 📡 实时 SSE 事件流
前端通过 SSE 订阅任务进度，实时看到：
- `assistant_delta`（大模型回复流式片段）
- `tool_start` / `tool_result`（工具调用过程）
- `items_saved`（数据入库批次）
- `challenge_detected`（反爬挑战需要处理）
- `agent_final`（任务完成总结）

---

## 🏗️ 架构

```
┌────────────────────────────────────────────────────────────┐
│  Python 后端 (FastAPI + LangGraph Agent)                    │
│                                                              │
│  chat endpoint → run_manager.start()                         │
│    ├─ graph.astream() ─┬─ messages mode → assistant_delta    │
│    │                   └─ updates mode → tool/model 事件     │
│    ├─ middleware 链：                                        │
│    │   DSMLFallback（模型超时重试）                           │
│    │   ContextEditing（历史裁剪）                             │
│    │   SteeringMiddleware（运行中注入）                        │
│    │   ChallengeInterceptor（反爬拦截）                       │
│    └─ agent_tools.py（@tool 函数 → 调 browser_manager）      │
│         ↓ HTTP/JSON                                          │
│  ───────────── 进程边界 ──────────────                       │
│                                                              │
│  Node.js browser-service (Express + Playwright)              │
│    ├─ browser.js（BrowserManager 单例）                       │
│    │   ├─ 域名级 context 管理 + storage_state                 │
│    │   ├─ proxy 轮换池 + 指纹固化                             │
│    │   ├─ page.route 请求拦截（block + mock）                 │
│    │   └─ stealth init script                                 │
│    └─ routes.js（27 个 REST 路由）                            │
│         ↓                                                    │
│  Playwright → Chromium                                       │
└────────────────────────────────────────────────────────────┘
```

---

## 🚀 快速开始

### 环境要求
- Python 3.11+
- Node.js 18+（browser-service 用）
- Chrome / Chromium（Playwright 自动下载）
- SQLite 或 PostgreSQL

### 安装

```bash
# 1. clone
git clone https://github.com/yourname/universal-ai-crawler.git
cd universal-ai-crawler

# 2. Python 后端
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 3. Node.js browser-service
cd ../browser-service
npm install

# 4. 配置
cp backend/.env.example backend/.env
# 编辑 .env，填入你的 LLM API Key（支持 DeepSeek / OpenAI / 兼容接口）
```

### 启动

```bash
# 先启动 Node.js browser-service（8765 端口）
cd browser-service
node server.js &

# 再启动 Python FastAPI（8899 端口）
cd ../backend
python run_server.py &
```

或用 docker-compose：

```bash
docker-compose up -d
```

### 使用

前端打开后，直接用自然语言告诉它：

```
帮我抓京东上搜索「机械键盘」的商品，要标题、价格、店铺名、评论数，按销量排序，抓前 100 条
```

Agent 会：
1. open_page("https://www.jd.com")
2. 检测到登录墙 → 暂停等你扫码
3. 你扫码完 → 自动保存登录态 → 继续
4. 搜索 "机械键盘" → 按销量排序
5. 翻页 → smart_extract 提取 → save_items 入库
6. 第 3 页 → smart_paginate 自动点下一页
7. 第 100 条 → 完成总结 → export_data 导出 CSV

### 进阶配置

```bash
# 代理轮换池（支持 socks5 / http / https，逗号分隔）
PROXIES="socks5://1.2.3.4:1080,socks5://5.6.7.8:1080"

# 无头模式（生产用）
HEADLESS=true

# 语言/地域
LOCALE=zh-CN
TIMEZONE=Asia/Shanghai

# 持久化 headless 指纹
# 进程内固化 + 按域名存 storage_state，真实用户指纹不会突变

# 数据库（默认 SQLite，可换 PostgreSQL）
DATABASE_URL=sqlite+aiosqlite:///./artifacts/agents.db
# 或
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/crawler_db
```

---

## 🧩 使用场景

| 场景 | 用户输入示例 |
|---|---|
| 电商竞品调研 | "帮我抓京东上所有 DDR5 内存条，对比价格和店铺" |
| 招聘信息聚合 | "抓 Boss 直聘上北京的 Python 后端职位，看哪些公司要 5 年经验" |
| 房产信息 | "贝壳找房上朝阳区的二手房，按总价排序，抓前 50 条的户型/价格/小区" |
| 舆情监测 | "小红书上关于某品牌的笔记，按点赞数排前 20" |
| 数据导出 | "看看之前抓了什么，把上次那个任务导出成 Excel" |
| 反爬对抗 | proxy_rotate() 换个 IP 继续；solve_captcha() 自动过验证码 |
| SPA 加速 | mock_add(matcher=r"/api/products", body='...') 拦截 API 返回假数据 |

---

## 🛠️ 技术栈

| 层 | 技术 |
|---|---|
| Agent 框架 | LangGraph 1.x（create_agent） |
| LLM 适配 | LangChain 1.x（LangChain OpenAI / DeepSeek / 兼容接口） |
| 浏览器引擎 | Playwright 1.x（Chromium） |
| 浏览器服务 | Node.js 18 + Express |
| 后端 | FastAPI + Uvicorn |
| 数据库 | SQLite（默认）/ PostgreSQL（生产可选） |
| ORM | SQLAlchemy 2.x async |
| 流式通信 | SSE（Server-Sent Events） |
| 进程通信 | HTTP + JSON（Python ↔ Node.js） |

---

## ❓ FAQ

**Q: 不装 Node.js 能用吗？**
A: 不能。浏览器自动化靠 Playwright（Node 原生更稳定），Python 端只做 HTTP 客户端。

**Q: 支持哪些 LLM？**
A: 所有 OpenAI 兼容接口（DeepSeek / OpenAI / 阿里云百炼 / 火山方舟 / 本地 Ollama 等）。配置 `LLM_BASE_URL` + `LLM_API_KEY` 即可。

**Q: 反爬能过吗？**
A: 有头模式 + 代理轮换 + 指纹伪装 + 请求拦截，大部分中小网站没问题。但高级风控（JS 挑战/行为 ML）无法 100% 自动过，需要人工介入。

**Q: 断点续爬是怎么实现的？**
A: LangGraph Checkpointer 持久化对话历史。任务崩了重开，`run_manager.input()` 会查询 checkpoint + 注入 `[断点续爬提示] 已存 N 条` SystemMessage，Agent 自动从下一条继续。

**Q: 数据存在哪？**
A: SQLite/PostgreSQL 的 `crawl_results` 表，按 `task_id` 分任务。对话历史在 LangGraph checkpoint 里。

**Q: 能同时跑多个任务吗？**
A: 可以。每个任务独立 thread_id，独立 context，互不干扰。但 Chromium 是单实例，所有任务共用。

**Q: 有免费试用额度吗？**
A: 完全开源免费，自行部署。LLM API 费用自理。

---

## 📄 License

MIT

## 📬 联系方式

微信：**Speaker-AI**

如有问题、建议、合作意向，随时联系。

---

**关键词**：AI crawler / LLM agent / web scraping / browser automation / LangGraph / Playwright / 万能爬虫 / 智能体 / 反爬 / 数据采集

**GitHub Topics**：`ai-crawler` `llm-agent` `web-scraping` `browser-automation` `langgraph` `playwright` `fastapi` `antibot-bypass` `multi-tool-agent`
