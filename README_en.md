# Universal AI Crawler — LLM-Driven Universal Browser Crawler Agent

> An intelligent crawler framework that lets LLMs autonomously control browsers to scrape data from any website. Production-grade features: anti-bot evasion, proxy rotation, resume-from-breakpoint, multi-tab, SPA request interception, and more.

Universal AI Crawler is a **LangGraph + Playwright + FastAPI** based LLM Agent crawler system. You just describe in natural language what you want — _"Grab the top 100 mechanical keyboards from JD, extract title/price/shop/reviews, sort by sales"_ — and it will:

1. 🤖 Autonomously open browser, navigate, click, paginate
2. 🛡️ Auto-detect and evade anti-bot measures (5-second shield, slider, captcha OCR, login wall)
3. 💾 Save data immediately per batch (supports resume from breakpoint)
4. 📊 Auto-export CSV/Excel/JSON/Markdown
5. 🔄 Reuse login per domain, rotate proxy across domains

---

## ✨ Features

### 🤖 LLM Agent Autonomy
- **LangGraph create_agent**: 44 atomic tools, LLM decides which to use
- **Context Editing Middleware**: Auto-trim history to control token budget
- **Steering queue**: Inject new instructions while running
- **Human-in-the-loop**: Pause on login/captcha, auto-resume after user handles

### 🛡️ Anti-Bot Evasion
- **Custom Stealth Init Script**: Overwrites webdriver / plugins / WebGL / window.chrome / permissions / toString
- **Domain-Level Context Isolation**: One Playwright context per main domain, auto-switch by hostname
- **Proxy Rotation Pool**: `PROXIES=socks5://ip1:port,socks5://ip2:port` rotates across domains
- **Request Blocking**: Default-block fonts/icons/trackers/ads (faster + fewer fingerprints)
- **SPA API Interception**: `mock_add(matcher=r"/api/xxx", body=...)` intercepts XHR and returns fake data

### 🌐 Browser Control (44 Tools)

| Category | Tools |
|---|---|
| Navigation | `open_page` / `scroll_page` / `wait_for` |
| Interaction | `click_element` / `fill_input` / `select_option` / `press_key` / `hover_element` |
| Multi-tab | `list_tabs` / `open_tab` / `switch_tab` / `close_tab` |
| iframe | `switch_iframe` |
| Files | `upload_file` / `download_file` |
| Page Info | `get_page_state` / `get_page_snapshot` / `get_simplified_html` / `evaluate_js` |
| Anti-bot | `check_challenges` / `solve_captcha` / `screenshot` |
| Extraction | `extract_by_scheme` / `llm_extract` / `smart_extract` |
| Data | `save_items` (auto dedupe by url/title) / `list_items` / `export_data` (csv/excel/json/md/pdf/docx) |
| Login State | `save_login_state` / `get_login_states` (per-domain storage_state) |
| Proxy/Intercept | `proxy_status` / `proxy_rotate` / `block_add` / `block_reset` / `mock_add` / `mock_clear` |
| Pagination | `smart_paginate` (auto-detect: next_button / infinite_scroll / url_param) |
| Other | `probe_media` / `download_media` / `save_crawl_config` / `close_browser` |

### 💾 Persistence & Recovery
- **LangGraph Checkpointer**: SQLite or PostgreSQL persists conversation + intermediate state
- **Resume from Breakpoint**: Crash → restart → Agent auto sees "N items saved, continue from N+1"
- **Per-Domain Login State**: `artifacts/browser/storage/{domain}.json` auto-save/load
- **Auto Dedup**: `save_items` skips duplicates by url/title

### 📡 Real-time SSE Events
Subscribe via SSE and see live progress:
- `assistant_delta` (streaming LLM response)
- `tool_start` / `tool_result` (tool call lifecycle)
- `items_saved` (data入库 batch)
- `challenge_detected` (needs human)
- `agent_final` (task summary)

---

## 🏗️ Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Python Backend (FastAPI + LangGraph Agent)                 │
│                                                              │
│  chat endpoint → run_manager.start()                         │
│    ├─ graph.astream() ─┬─ messages mode → assistant_delta    │
│    │                   └─ updates mode → tool/model events   │
│    ├─ middleware chain:                                      │
│    │   DSMLFallback (model timeout retry)                    │
│    │   ContextEditing (history trimming)                     │
│    │   SteeringMiddleware (runtime injection)                 │
│    │   ChallengeInterceptor (anti-bot interception)          │
│    └─ agent_tools.py (@tool functions → browser_manager)    │
│         ↓ HTTP/JSON                                          │
│  ───────────── Process Boundary ──────────────               │
│                                                              │
│  Node.js browser-service (Express + Playwright)              │
│    ├─ browser.js (BrowserManager singleton)                   │
│    │   ├─ Domain-level context management + storage_state     │
│    │   ├─ Proxy rotation pool + fingerprint                  │
│    │   ├─ page.route request interception (block + mock)     │
│    │   └─ stealth init script                                 │
│    └─ routes.js (27 REST endpoints)                           │
│         ↓                                                    │
│  Playwright → Chromium                                       │
└────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- Node.js 18+
- Chrome / Chromium (Playwright downloads automatically)
- SQLite or PostgreSQL

### Install

```bash
# 1. clone
# 1. clone
github https://github.com/iq105/universal_ai_crawler.git
gitee https://gitee.com/iq105/universal_ai_crawler.git
git clone [https://github.com/yourname/universal-ai-crawler.git](https://github.com/iq105/universal_ai_crawler)
cd universal-ai-crawler

# 2. Python backend
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 3. Node.js browser-service
cd ../browser-service
npm install

# 4. Configure
cp backend/.env.example backend/.env
# Edit .env, fill in your LLM API Key (DeepSeek / OpenAI / any OpenAI-compatible API)
```

### Run

```bash
# Terminal 1: Node.js browser-service (port 8765)
cd browser-service
node server.js

# Terminal 2: Python FastAPI (port 8899)
cd ../backend
python run_server.py
```

Or with docker-compose:

```bash
docker-compose up -d
```

### Use

Open the frontend and just talk to it in natural language:

```
Grab the top 100 mechanical keyboards from JD, extract title/price/shop/reviews,
sort by sales, export to Excel.
```

### Advanced Configuration

```bash
# Proxy rotation pool (socks5 / http / https, comma-separated)
PROXIES="socks5://1.2.3.4:1080,socks5://5.6.7.8:1080"

# Headless mode (production)
HEADLESS=true

# Locale & timezone
LOCALE=en-US
TIMEZONE=America/New_York

# Database (SQLite default, PostgreSQL optional)
DATABASE_URL=sqlite+aiosqlite:///./artifacts/agents.db
# or
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/crawler_db
```

---

## 🧩 Use Cases

| Scenario | Example Input |
|---|---|
| E-commerce Research | "Grab all DDR5 RAM modules from JD, compare prices and shops" |
| Job Aggregation | "Scrape Beijing Python backend positions from Boss Zhipin, filter by 5+ years" |
| Real Estate | "Second-hand houses in Chaoyang on Ke, sort by total price, top 50" |
| Social Listening | "Top 20 posts about Brand X on Xiaohongshu by likes" |
| Data Export | "What did we scrape last time? Export that task to Excel" |
| Anti-bot | `proxy_rotate()` to switch IP; `solve_captcha()` for auto OCR |
| SPA Speedup | `mock_add(matcher=r"/api/products", body='...')` intercepts API |

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Agent Framework | LangGraph 1.x (create_agent) |
| LLM Adapter | LangChain 1.x (OpenAI / DeepSeek / any compatible API) |
| Browser Engine | Playwright 1.x (Chromium) |
| Browser Service | Node.js 18 + Express |
| Backend | FastAPI + Uvicorn |
| Database | SQLite (default) / PostgreSQL (production optional) |
| ORM | SQLAlchemy 2.x async |
| Streaming | SSE (Server-Sent Events) |
| Inter-process | HTTP + JSON (Python ↔ Node.js) |

---

## ❓ FAQ

**Q: Can I use this without Node.js?**
A: No. Browser automation runs in Node.js (Playwright is natively more stable there). Python only acts as an HTTP client.

**Q: Which LLMs are supported?**
A: Any OpenAI-compatible API — DeepSeek, OpenAI, Anthropic, Azure, Ollama local, Alibaba Bailian, etc. Configure `LLM_BASE_URL` + `LLM_API_KEY`.

**Q: Can it bypass anti-bot?**
A: Heuristic rule-based bots (5-second shield, slider, basic captcha) — yes, mostly. Advanced ML-based behavioral detection — no, needs human intervention.

**Q: How does resume-from-breakpoint work?**
A: LangGraph Checkpointer persists all messages. On restart, `run_manager.input()` checks the checkpoint count and injects `[Resume Hint: N items saved]` as SystemMessage. Agent continues from the next one.

**Q: Where is data stored?**
A: SQLite/PostgreSQL `crawl_results` table per `task_id`. Conversation history is in LangGraph checkpointer.

**Q: Can multiple tasks run concurrently?**
A: Yes, each task has its own thread_id and context. Single Chromium instance shared across all tasks.

---

## 📄 License

MIT

## 📬 Contact

WeChat: **Speaker-AI**

Questions, suggestions, collaborations — feel free to reach out.

---

**Keywords**: AI crawler, LLM agent, web scraping, browser automation, LangGraph, Playwright, FastAPI, anti-bot bypass, multi-tool agent, auto-pagination, breakpoint resume, SPA interception, proxy rotation

**Suggested GitHub Topics**: `ai-crawler` `llm-agent` `web-scraping` `browser-automation` `langgraph` `playwright` `fastapi` `antibot-bypass` `multi-tool-agent` `universal-crawler`
