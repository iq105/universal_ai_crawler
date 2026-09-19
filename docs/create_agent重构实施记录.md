# Universal Crawler · create_agent 重构实施记录

> 本文件是本次重构的**唯一推进依据**：所有改造按「步骤索引」依次执行，
> 每完成一步在本文件「验证登记」中打勾并记录结果，然后再进入下一步。

---

## 0. 版本基线（已验证 2026-09-16）

| 包 | 已安装 | requirements.txt 下限 | 结论 |
| --- | --- | --- | --- |
| langchain | 1.4.0 | >=0.2.10（过期） | 需同步下限 >=1.0 |
| langgraph | 1.2.11 | >=0.2.60（过期） | 需同步下限 >=1.0 |
| langchain-openai | 1.6.2 | >=0.1.10（过期） | 同步 >=1.0 |
| langgraph-checkpoint-sqlite | 3.1.1 | >=2.0.0 | 同步 >=3.1 |
| langgraph-checkpoint-postgres | 3.1.2 | >=2.0.0 | 同步 >=3.1 |

关键 API 在 1.x 下编译/import 验证：
- [x] `from langchain.agents import create_agent`
- [x] `from langchain.agents.middleware import HumanInTheLoopMiddleware / ToolRetryMiddleware / ModelRetryMiddleware / ContextEditingMiddleware / ToolErrorMiddleware / AgentMiddleware`
- [x] `from langgraph.graph import END, START, StateGraph`；`from langgraph.types import Command, interrupt`
- [x] `from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver`；`from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver`
- [x] `from langchain.tools import tool`；`from langgraph.prebuilt import ToolNode`
- [x] `backend/app` 全量 `compileall` 通过；`app.core.checkpointer / app.services.crawler_graph / app.services.agent_nodes / app.tools` import 通过

> `create_agent` 在 1.x 的签名要点：`create_agent(model, tools, *, system_prompt=None, middleware=(), response_format=None, state_schema=None, context_schema=None, checkpointer=None, store=None, interrupt_before/after=None, transformers=None)`，返回 `CompiledStateGraph`。

---

## 1. 背景与目标

现有 Agent 模式（`services/agent_nodes.py` 的 `agent_think ⇄ agent_act`）是手写的 ReAct 循环：
消息组装、工具调用配对、DSML 兜底、挑战检测、各种守卫全部散落在节点函数里，
加上 `ToolRegistry.execute` 里的审计逻辑，横切关注点与主流程强耦合。

**目标**：用 `create_agent` 的框架能力替换手写循环，把所有横切逻辑收进中间件，
`pipeline` 分支与旧 checkpoint 兼容保留。产出物直接可由顶层图复用。

**非目标**：不改 pipeline 节点逻辑；不引入 RAG/VectorStore；不换前端。

---

## 2. 目标架构

```
crawler_graph.py（大 StateGraph，双入口，保留）
├─ pipeline 分支（intent_understand … store，原样不动）
└─ agent 分支 ──► agent_builder.build_agent() 返回的 create_agent 子图（作为单节点挂入）
        ├─ model        = init_chat_model(...)                       → 从 core/llm
        ├─ tools        = [@tool 重写的 24 个工具]                    → 从 tools/
        ├─ state_schema = CrawlerAgentState（CrawlerState 扩展）      → services/agent_builder
        ├─ context_schema = CrawlerRunContext（task_id/bus/steering） → services/agent_builder
        ├─ checkpointer = get_checkpointer()                         → core/checkpointer 原样
        └─ middleware   = [Audit, Steering, Challenge, Guard, DSML兜底, Retry/Limit/Summarization…]
                                → services/middleware/*.py（新目录）
```

`create_agent` 内置的模型→tool_calls→ToolNode→消息合并循环，替代：
`agent_nodes._to_messages`、`agent_think` 选工具、`agent_act` 执行、工具消息配对。

---

## 3. 现状盘点（将被替换的代码）

| 文件 | 现状 | 去向 |
| --- | --- | --- |
| `services/agent_nodes.py` | 手写 ReAct 循环 + 全部守卫 | 循环逻辑删；守卫迁中间件；挑战文案 `CHALLENGE_MESSAGES`、DSML 解析保留给中间件 |
| `core/registry.py` | ToolRegistry + `execute()` 审计 | 改造为持有 `list[BaseTool]` 的注册表，保留 `.schemas()` 供 pipeline 用；审计迁 AuditMiddleware |
| `tools/__init__.py` | `TOOL_SCHEMAS`(dict) + `TOOL_EXECUTORS` | 迁移为 `@tool` 函数；`TOOL_SCHEMAS` 保留为 pipeline 选择器提取所用（可选） |
| `tools/*.py` | executor 函数 | 加 `@tool`，`get_ctx()` 改为 `runtime.context` 注入（context_schema） |
| `core/event_bus.py` | contextvar RunContext | 保留（pipeline 仍在用）；agent 子图内用 `context_schema` |
| `core/steering.py` | 进程内队列 | 保留，注入 `context_schema`，SteeringMiddleware 消费 |
| `core/llm.py` | `llm_json/llm_chat`（pipeline 用） | 保留；新增 `init_chat_model()` 供 create_agent 使用 |
| `services/anti_ban.py` | 手写退避/代理轮换 | 重试层 `ToolRetryMiddleware`；`browser_manager.reconnect` 保留作自定义回调 |
| `core/run_manager.py` `_consume` | 手工解析 `astream updates` | 迁移为消费子图输出流 + `StreamTransformer` |
| `services/crawler_graph.py` | `agent_think/agent_act` 节点 + `route_entry` | 节点替换为一个 `agent` 子图节点；resume/human_intervene 复用 |

---

## 4. 步骤索引（按此顺序依次执行）

| 步 | 内容 | 涉及文件 | 验证标准 | 完成 |
| --- | --- | --- | --- | --- |
| 1 | 同步依赖下限到 1.x | `requirements.txt` | pip 解析通过 | ☑ |
| 2 | 工具层迁移 `@tool` + 注册表改造（pipeline 兼容）| `core/registry.py`、`tools/*.py` | pipeline `tool_registry.execute` 行为不变；agent 侧 `tools=[...]` 可用 | ☑ |
| 3 | `services/agent_builder.py`：最小 create_agent 子图 | 新建 + `core/llm.py` | 图可编译；空工具 remove 调用即终态；checkpointer 复用 | ☑ |
| 4 | 中间件落地（按表 5.1，先基础后增强）| 新建 `services/middleware/*.py` | 各中间件用 mock 验证触发时序 | ☑ |
| 5 | SSE 事件流：context_schema + transformers | `agent_builder.py`、`core/run_manager.py` | `assistant_delta/reasoning/tool_start/tool_result` 事件还原 | ☑ |
| 6 | 顶层图集成：`route_entry` agent 分支指向子图；`human_intervene`/resume 兼容 | `services/crawler_graph.py` | 双入口可跑；旧 `waiting_interrupt` 任务 resume 通过 | ☑ |
| 7 | 回归验证全场景 | 三条 API 路径 + smoke | 见 §8 验证登记 | ☑ |

> 每步完成必须在本文件的「完成」打 ☑，并补登记到 §8。

---

## 5. 中间件设计清单

### 5.1 中间件注册顺序与职责

`create_agent(middleware=[...])`，**列表靠前者为最外层**（wrap 型 hook 先进入、最后回归），
按以下顺序注册（外层→内层）：

| 序 | 中间件 | hook | 职责 | 替代的现有代码 |
| --- | --- | --- | --- | --- |
| 1 | `AuditMiddleware` | `wrap_tool_call` / `after_agent` | tool_start/tool_result 事件、task_log | `registry.py:46-56` |
| 2 | `SteeringMiddleware` | `wrap_model_call`（前置注入） | drain steering 队列注入 user 消息 | `agent_nodes.py:117-130` |
| 3 | `ChallengeInterceptor` | `wrap_tool_call`（open_page 等结果）| 挑战检测→`Command(interrupt())` 中断交人工 | `agent_nodes.py:366-373` |
| 4 | `GuardMiddleware` | `wrap_tool_call` + `wrap_model_call` | 截屏频率守卫 / 导出无数据守卫 | `agent_nodes.py:143-170,287-339` |
| 5 | `DSMLFallback` | `wrap_model_call` | 无 tool_calls 时解析 DeepSeek DSML 标记兜底 | `agent_nodes.py:236-264` |
| 6 | `HumanInTheLoopMiddleware` | 预置 | 人工介入（结合 ChallengeInterceptor 的 interrupt）| `crawler_graph.py:276-314` |
| 7 | `ToolRetryMiddleware` | 预置 | 429/403/反爬指数退避重试 | `services/anti_ban.py` |
| 8 | `ToolCallLimitMiddleware` | 预置 | `max_steps` 步数上限 | `agent_nodes.py:376-378` |
| 9 | `ContextEditingMiddleware` / `SummarizationMiddleware` | 预置 | 上下文裁剪 | `agent_nodes.py:158-170` |

### 5.2 context_schema（`CrawlerRunContext`）

```python
class CrawlerRunContext(TypedDict):
    task_id: str
    bus: RunEventBus          # SSE 事件通道
    steering: SteeringManager # 运行中指挥
    settings: Settings        # max_steps/max_pages 等
```

工具与中间件一律改从 `request.runtime.context` / `tool` 注入参数读取，替代 `get_ctx()`。

---

## 6. 工具迁移对照（tools/ → @tool）

| 工具 | 现定义 | 迁移要点 |
| --- | --- | --- |
| open_page / get_page_snapshot / get_simplified_html / scroll_page / click_element / evaluate_js / fill_input / screenshot / get_page_state / wait_for / check_challenges / press_key | `browser_tools.py` | `@tool`，参数走 docstring 生成 schema；`get_ctx()` 改参数注入 `context: CrawlerRunContext` |
| extract_by_scheme / llm_extract | `parse_tools.py` | 同上 |
| save_items / get_task / list_items / export_data / list_tasks | `storage_tools.py` | `task_id` 缺省时从 `context.task_id` 取 |
| probe_media / download_media / solve_captcha / smart_extract / resolve_target_tool | `smart_tools.py` | 同上 |

注册表：
- 新增 `registry.as_tools() -> list[BaseTool]` 供 `create_agent(tools=...)`
- 保留 `registry.execute(name, ctx, ...)` 与 `schemas()` 供 **pipeline 分支**继续使用（pipeline 不是 agent，不经过 create_agent）

---

## 7. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 升级大版本破坏现有运行 | 当前 smoke（import/compile）已通过；每次改动前保留 `git stash`/快照；逐步验证 |
| create_agent 子图 interrupt 穿透到顶层 | 验证 `interrupt_before/after` 与 resume 语义，用现有 `build_resume_input` 复用 |
| pipeline 分支被误伤 | pipeline 不经过 create_agent，工具注册表保持双通道 |
| 工具 schema 生成与旧 dict 不一致 | 迁移后跑 `registry.schemas()` 对照 diff |
| DSML 兜底失真 | 兜底逻辑完整保留进中间件，回归用例不删 |

回滚：`git checkout -- backend/app` 恢复代码；依赖已装（无需回滚，1.x 与旧限制并存时只改 requirements 文本）。

---

## 8. 验证登记（每步完成后打勾）

| 场景 | 方式 | 结果 |
| --- | --- | --- |
| 依赖下限同步 | `pip install --dry-run -r requirements.txt` | ☑ 2026-09-16 `pip check` 无 broken；dry-run 无 conflict/升级 |
| pipeline 工具审计回归 | 现有 `_e2e.py`/`_smoke_*.py` | ☑ 2026-09-16 `compileall` 通过；`get_agent_tools()` 返回 24 个 StructuredTool；`tool_registry.schemas()` 数量正确 |
| agent 最小闭环 | 无工具调用即终态；带 mock 输出 | ☑ 2026-09-16 `create_agent` 编译通过；图节点 `['model','tools','ToolCallLimitMiddleware.after_model']`；已并入 `get_agent_tools()`（24 个 StructuredTool）|
| 中间件时序 | 单测/mock model 输出 | ☑ 2026-09-16 mock 组合验证：Steering 首轮注入任务描述（HumanMessage+task_log）；Audit 推 tool_start/tool_result；Challenge 检测登录墙 → `before_model` interrupt 暂停图（checkpoint `next` 非空可 resume）；Guard 导出空数据 → 注入 SystemMessage + `jump_to=end` + `done=True`（model 仅 1 次调用）；DSMLFallback 解析 DSML 标记 → tool 执行成功；`compileall` 通过。修复：Challenge 的 interrupt 只能在 graph 节点 hook 调用（awrap_tool_call 内会被 Audit 捕获吞掉）、守卫判空条件 `last_tool_result is None`（空 list 属 falsy）、guard 用 `hook_config(can_jump_to=["end"])` |
| SSE 事件 | agent 任务事件流 | ☑ 2026-09-16 `run_manager._consume` 改用 `stream_mode=["updates","messages"]`：model 节点 token 流经 `_forward_model_stream` 转发 `assistant_delta`（content）+ `assistant_reasoning`（reasoning_content），非 model 节点消息忽略（不与 steering/守卫重复）；updates 仍原样 → `_graph_chunk` 兼容旧消费者；`langgraph_node` 过滤 `endswith("model")`，pipeline 图无 model 节点故零影响；mock 验证 delta/reasoning 事件正确 |
| SSE 事件 | 跑一次 agent 任务看事件流 | ☐ |
| 双入口 + 旧任务 resume | `FORCE_INTERRUPT_AT=page_load` | ☑ 2026-09-17 顶层图集成验证：`build_graph` 用 `g.add_node("agent", build_agent(checkpointer))` 单节点嵌入子图；`route_entry` agent→`agent`、pipe→`intent_understand`；删 `agent_think/agent_act/route_after_agent_think/route_after_agent_act`，`route_after_agent` 恒 END；`route_after_intervene` agent→`agent`。mock 验证：①子图在顶层图内完整跑（中间件 hook 齐全，messages 以 node=agent 整条上浮，FinalAnswer 置 `done/final_answer/status=success`）②Challenge 触发（get_page_state 返回 `detect.login`）→ 顶层收到 `__interrupt__`（payload `{'type':'challenge','reason':'login','message':...}`，`next=['agent']`）→ `Command(resume=...)` 续跑直达 done ③终态任务 resume 走空图 `_graph_end` 不报错。附带：新增 `FinalAnswerMiddleware`（`services/middleware/final_answer.py`，`aafter_model` 判完成语义→emit chat_ack + `done/final_answer/status`），注册进 agent_builder 链；`CrawlerAgentState` 加 `status` 字段；`run_manager._consume` 用 `stream_mode=["updates","messages"]`（updates 保留 `_graph_chunk` 兼容，messages 走 `_forward_model_stream` 转发整条 AIMessage 为 `assistant_delta`）。`compileall` 通过 |
| 全回归 | 文档《Agent自主爬虫重构方案》验证表 | ☑ 2026-09-17 步骤 7 回归：**修复真实 bug**——langgraph 1.2.11 的 `ToolNode` 不注入裸 `InjectedToolArg`（`_inject_tool_args` 只注入 `InjectedState/InjectedStore/ToolRuntime`，裸 `InjectedToolArg()` 仅被剥离 → 报 `'InjectedToolArg' object has no attribute 'bus'`）。6 个需 context 的工具（llm_extract/save_items/get_task/list_items/export_data/smart_extract）改标注 `runtime: ToolRuntime`，用 `runtime.context` → `ctx_from_context/restore_ctx` 桥接。**第二次 bug**：langgraph-core issue #35931——`@tool` 推断 schema 时 `runtime: ToolRuntime = None` 抛 `PydanticInvalidForJsonSchema(CallableSchema)`（`_filter_schema_args` 未剔除 `_DirectlyInjectedToolArg`，官方 PR #35929/#7227 未合入当前版本）。修复：`agent_tools.py` 顶部给 `ToolRuntime` 补 `__get_pydantic_core_schema__`（`classmethod` 返回 `nullable_schema(any_schema())`）→ schema 生成成功、`_injected_args_keys={'runtime'}` 正确识别、`convert_to_openai_tool` 模型侧 schema 已剔除 runtime 字段。回归全绿：`compileall` 通过；24 个工具 schema 全部可生成（errors=0）；`step7_run_manager_smoke` PASS（事件序列 `tool_start→tool_result→chat_ack→assistant_delta×2→_graph_chunk→_graph_end`，done=True/status=success）；`step7_save_items_smoke` PASS（`{"saved":1}` + items_batch）；`challenge_top_verify` PASS（Challenge interrupt→resume 不受影响）；pipeline 模式 LLM-free 冒烟 PASS（`build_graph` 含 pipeline 全节点；`initial_state_for(graph_mode="pipeline")` instruction 走 state 字段、不注入 messages，与 chat 的 `user_message` 参数区分）。待补（live 环境）：`SSE 事件\|跑一次 agent 任务看事件流` |