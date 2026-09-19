"""RunManager：任务运行生命周期管理

把「图的消费」与「SSE 连接」解耦：
- /api/chat 创建任务后立即后台启动 run（不依赖任何 SSE 连接）
- /run、/resume、/stream 都可挂到同一 run 的事件总线
- 中断载荷持久化在内存，SSE 重连时可重放
- graph 结束（无论有无订阅者）都会把最终状态写入数据库
"""
import asyncio
import traceback
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command

from app.core.event_bus import RunContext, RunEventBus, reset_run_context, set_run_context
from app.core.events import EVENT_ASSISTANT_DELTA, EVENT_ASSISTANT_REASONING
from app.core.steering import steering
from app.core.logger import get_logger
from app.core.trace import ensure_trace_id
_log = get_logger("run_manager")


class RunManager:
    def __init__(self) -> None:
        self._runs: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def start(self, task_id: str, graph, config: dict, initial_state: dict | None = None, is_resume: bool = False,
            resume_value=None, ) -> RunEventBus:
        """启动（或复用）一次运行，返回事件总线,**"创建并启动一个 Agent 运行实例"**"""
        async with self._lock:
            entry = self._runs.get(task_id)
            if entry and not entry["finished"]:
                return entry["bus"]
            bus = RunEventBus()
            entry = {"bus": bus, "finished": False, "paused": False, "interrupt_payload": None,
                "graph": graph, "config": config, }
            self._runs[task_id] = entry

        ctx = RunContext(bus=bus, task_id=task_id)
        token = set_run_context(ctx)
        try:
            from app.services.anti_ban import reset_for_task

            reset_for_task()
            # AGENT.md #5：非 HTTP 入口（Gradio/后台）兜底生成 trace_id，
            # create_task 会复制当前 context，运行链自动继承
            ensure_trace_id()
            task = asyncio.create_task(self._consume(entry, task_id, initial_state, is_resume, resume_value))
            entry["task"] = task
            _log.info("[run_manager] 任务开始运行 task_id=%s is_resume=%s", task_id, is_resume)
        except Exception:
            reset_run_context(token)
            raise
        finally:
            reset_run_context(token)
        _log.info("[run_manager] 已创建运行总线 task_id=%s", task_id)
        return bus

    async def _forward_model_stream(self, token: tuple, bus: RunEventBus) -> None:
        """把 graph 的 messages 流（model 输出）转发为 assistant_delta / assistant_reasoning。

        关键过滤：有 tool_calls 的 AIMessageChunk 是「推理/规划」，不是用户可读的回复。
        这类消息只含工具规划文本（"Let me try..."、"I'll check..."），对前端无价值，
        直接跳过不发，避免 think-stream 里一堆垃圾英文。
        只有最终回复阶段（无 tool_calls、纯 content）才发 assistant_delta。
        """
        try:
            message, meta = token
            from langchain_core.messages import AIMessage, AIMessageChunk

            if not isinstance(message, (AIMessage, AIMessageChunk)):
                return
            # 有 tool_calls → 推理/规划 → 不发 delta
            tc = getattr(message, "tool_calls", None) or []
            if tc:
                return
            content = getattr(message, "content", None) or ""
            if isinstance(content, str) and content.strip():
                await bus.emit(EVENT_ASSISTANT_DELTA, text=content)
            reasoning = (message.additional_kwargs or {}).get("reasoning_content")
            if reasoning:
                await bus.emit(EVENT_ASSISTANT_REASONING, text=reasoning)
        except Exception as _e:  # noqa: BLE001 —— 事件转发失败不影响主流程
            traceback.print_exc()
            _log.warning("[run_manager] 模型流转发失败：%s", _e)

    # ── 辅助：检测 updates chunk 是否来自 tool 节点 ──
    @staticmethod
    def _is_tool_chunk(keys: list[str]) -> bool:
        """LangGraph 的 tool 节点在 updates 模式下的 key 可能是：
        - 'tools'（LangGraph 内置 ToolNode 默认名）
        - 'ToolNode'（某些自定义子图）
        - 具体工具名如 'open_page'（如果 agent 把工具当独立节点）
        宽松匹配：任何 key 含 'tool' 或等于内置工具节点名都算。
        """
        for k in keys:
            lk = k.lower()
            if k in ("tools", "ToolNode", "__pregel_tasks"):
                return True
            if "tool" in lk:
                return True
        return False

    # ── 辅助：清理 checkpoint 里已完成的推理 AIMessage ──
    async def _cleanup_inference_messages(self, graph, config: dict, task_id: str) -> None:
        """每次 tool 执行完后立即清理。

        LangGraph agent 每轮循环：model(推理+tool_calls) → tool(执行) → model(新推理)...
        如果只在最后清一次，中间 10 轮就积累 10 条推理 AIMessage，每条可能含 200-500 token，
        下一次模型调用时全部喂进去白白消耗。
        这里在 tool 执行完后立即清掉：
        - 有 tool_calls 的 AIMessage → content 清空
        - 最新一轮 AIMessage（刚发出 tool_calls，还没被 tool result 回应）保留
        """
        try:
            snap = await graph.aget_state(config)
            vals = snap.values or {}
            msgs = list(vals.get("messages", []))
            if not msgs:
                return
            from langchain_core.messages import AIMessage

            # 从后往前找第一条有 tool_calls 的 AIMessage → 它是最新一轮推理，不动
            # 它之前的所有有 tool_calls 的 AIMessage 全部清 content
            latest_tool_call_idx = -1
            for i in range(len(msgs) - 1, -1, -1):
                m = msgs[i]
                if isinstance(m, AIMessage) and (getattr(m, "tool_calls", None) or []):
                    latest_tool_call_idx = i
                    break

            cleaned_count = 0
            for i, m in enumerate(msgs):
                if i >= latest_tool_call_idx:
                    break  # 最新一轮及之后不动
                if isinstance(m, AIMessage) and (getattr(m, "tool_calls", None) or []):
                    tc = m.tool_calls
                    msgs[i] = AIMessage(content="", tool_calls=tc, id=m.id)
                    cleaned_count += 1

            if cleaned_count > 0:
                await graph.aupdate_state(config, {"messages": msgs})
                _log.info("[run_manager] 增量清理 %d 条推理 AIMessage task_id=%s", cleaned_count, task_id)
        except Exception as _e:  # noqa: BLE001
            _log.warning("[run_manager] 增量清理 checkpoint 失败（不影响主流程）：%s", _e)

    async def _consume(self, entry: dict, task_id: str, initial_state, is_resume: bool, resume_value) -> None:
        bus = entry["bus"]
        graph = entry["graph"]
        config = entry["config"]
        final_status = "failed"
        try:
            if is_resume:
                # 统一用 Command(resume=...)：create_agent 的 checkpointer 自动从 checkpoint 恢复
                graph_input = resume_value if isinstance(resume_value, Command) else Command(resume=resume_value)
                _log.info("[run_manager] resume Command(resume=%r)", graph_input.resume)
            else:
                graph_input = initial_state
            _log.info("[run_manager] 启动 astream task_id=%s is_resume=%s graph_input=%s graph_input_type=%s", task_id, is_resume,graph_input,
                type(graph_input).__name__)
            from app.core.context import CrawlerRunContext

            run_ctx = CrawlerRunContext(task_id=task_id, bus=bus, steering=steering, settings=None)
            chunk_count = 0 # 自动把 context run_ctx对象注入到每个节点/中间件的最后一个参数
            async for mode, chunk in graph.astream(graph_input, config=config, context=run_ctx,
                    stream_mode=["updates", "messages"]):
                if mode == "messages":
                    await self._forward_model_stream(chunk, bus)
                    continue
                chunk_count += 1
                keys = list(chunk.keys())
                _log.info("[run_manager] chunk #%d keys=%s", chunk_count, keys)
                if "__interrupt__" in chunk:
                    try:
                        entry["interrupt_payload"] = chunk["__interrupt__"][0].value
                    except (KeyError, IndexError, TypeError):
                        entry["interrupt_payload"] = None
                    _log.info("[run_manager] interrupt 触发 task_id=%s", task_id)

                # ── 每轮 tool 执行完后立即清理 checkpoint 里的推理 AIMessage ──
                # 检测方式：chunk 的 key 包含 LangGraph tool 节点名
                if self._is_tool_chunk(keys):
                    await self._cleanup_inference_messages(graph, config, task_id)

                await bus.emit("_graph_chunk", chunk=chunk)
            _log.info("[run_manager] astream 结束，共 %d 个 chunk，task_id=%s", chunk_count, task_id)
            # ✅ 从 state 里提取最终回答（最后一个 AIMessage）当 agent_final 事件
            try:
                snap = await graph.aget_state(config)
                vals = snap.values or {}
                msgs = vals.get("messages", [])
                from langchain_core.messages import AIMessage
                final_text = ""
                for m in reversed(msgs):
                    if isinstance(m, AIMessage) and m.content:
                        final_text = m.content if isinstance(m.content, str) else str(m.content)
                        break
                if final_text:
                    await bus.emit("agent_final", text=final_text)
                    _log.info("[run_manager] agent_final 已 emit 长度=%d", len(final_text))
                else:
                    _log.info("[run_manager] agent 没有最终 AIMessage（可能纯工具调用无文本回复）")

                # ✅ 清理 checkpoint：有 tool_calls 的 AIMessage（推理/规划）清 content
                # 只保留 HumanMessage + ToolMessage + 最终 AIMessage 的完整 content
                before_count = len(msgs)
                cleaned_count = 0
                cleaned = []
                for m in msgs:
                    if isinstance(m, AIMessage):
                        tc = getattr(m, "tool_calls", None) or []
                        if tc:
                            # 有 tool_calls → 推理/规划 → content 清空（保留 tool_calls 字段）
                            cleaned.append(AIMessage(content="", tool_calls=tc, id=m.id))
                            cleaned_count += 1
                        else:
                            # 无 tool_calls → 最终答案 → 完整保留
                            cleaned.append(m)
                    else:
                        # HumanMessage / ToolMessage / SystemMessage → 原样保留
                        cleaned.append(m)
                if len(cleaned) != before_count:
                    _log.warning("[run_manager] checkpoint 清理前后数量异常 %d→%d", before_count, len(cleaned))
                # 只有当有推理 AIMessage 被清了才更新 checkpoint
                if cleaned_count > 0:
                    await graph.aupdate_state(config, {"messages": cleaned})
                    _log.info("[run_manager] checkpoint 已清理 %d 条 AIMessage 推理内容（总消息 %d 条）", cleaned_count, before_count)
            except Exception as exc:  # noqa: BLE001
                _log.warning("[run_manager] agent_final 提取失败: %s", exc)

            # 落定最终状态（不再手动判 success/failed——用户自己看数据）
            if entry.get("interrupt_payload") is not None:
                final_status = "waiting_interrupt"
            else:
                final_status = "done"
            await bus.emit("_graph_end")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            final_status = "failed"
            _log.error("[run_manager] 任务运行异常 task_id=%s: %s", task_id, exc)
            await bus.emit("_graph_error", error=repr(exc))
        finally:
            # 硬取消（暂停）时 CancelledError 继承 BaseException，不会被上面的 except Exception 捕获，
            # final_status 仍为初始值；这里按 pause 标记纠正为 paused
            if entry.get("paused"):
                final_status = "paused"
            _log.info("[run_manager] 任务结束 task_id=%s final=%s（浏览器保持打开）", task_id, final_status)
            # 注意：浏览器在所有任务结束后仍保持打开，只有用户说"关闭浏览器"或显式调
            # browser_manager.close() 时才关闭。避免了多任务之间浏览器反复重启、登录态丢失。
            entry["finished"] = True

    async def pause(self, task_id: str) -> bool:
        """暂停运行中的任务：硬取消 asyncio 任务（checkpoint 仍在，可续跑），保留浏览器。

        返回 True 表示确实停掉了一个运行中的任务。
        """
        async with self._lock:
            entry = self._runs.get(task_id)
            if not entry or entry.get("finished"):
                return False
            entry["paused"] = True
            task = entry.get("task")
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 —— 等待任务落地（CancelledError 等），忽略
                traceback.print_exc()
        _log.info("[run_manager] pause 完成 task_id=%s", task_id)
        return True

    async def input(self, task_id: str, graph, config: dict, message: str,
                    initial_state: dict | None = None) -> RunEventBus:
        """统一入口：不管任务什么状态，只传用户消息进来。

        判断逻辑（按优先级）：
        1. _runs 里有 run 且正在跑（未暂停）→ steering.push（不中断 graph）
        2. checkpoint 里有历史（LangGraph 存的）→ resume Command(resume=message)
           （包括：interrupt 待恢复 / paused 被硬停 / 进程重启后）
        3. 全新任务 → start(initial_state)
        """
        # 1. 内存里还有活的 run 吗？
        async with self._lock:
            entry = self._runs.get(task_id)
            has_entry = bool(entry and not entry.get("finished"))
            is_actually_running = False
            if has_entry:
                task = entry.get("task")
                is_actually_running = bool(task and not task.done() and not entry.get("paused"))

        if has_entry and is_actually_running:
            # ✅ 运行中 → steering（不中断正在跑的 graph）
            _log.info("[run_manager] input → steering task_id=%s", task_id)
            await steering.push(task_id, message)
            return entry["bus"]

        # 2+3. 内存没有 run → 查 LangGraph checkpoint 能不能 resume
        #    关键：不是有 values 就能 resume！必须有 pending interrupt 或 next 节点。
        #    终态 checkpoint（success/failed）有 values 但没 tasks/next → 应该 start 新 graph
        has_pending = False
        try:
            snap = await graph.aget_state(config)
            tasks = list(snap.tasks or [])
            has_pending = bool(tasks or snap.next)
            _log.info("[run_manager] snapshot tasks=%s next=%s values=%s",
                      [getattr(t, 'name', '?') for t in tasks], snap.next, bool(snap.values))
        except Exception as _e:  # noqa: BLE001
            _log.warning("[run_manager] checkpoint 查询失败: %s", _e)

        if has_entry or has_pending:
            # ── 分支 1：有 pending interrupt / 内存 run 被暂停 ──
            # Command(resume=...) 是 LangGraph 恢复 interrupt 的正确方式
            cmd = Command(resume=message)
            _log.info("[run_manager] input → resume_interrupt task_id=%s has_entry=%s has_pending=%s",
                      task_id, has_entry, has_pending)
            return await self.start(task_id, graph, config, is_resume=True, resume_value=cmd)
        elif snap.values:
            # ── 分支 2：checkpoint 有历史但已终态 ──
            # 正常 start，只传新 HumanMessage。LangGraph 的 add_messages reducer
            # 会自动从 checkpoint 加载旧 messages + 追加新消息 → 大模型能看到完整上下文。
            # === 断点续爬增强：注入已存数据量 ===
            extra_msgs = []
            try:
                from app.services import result_store
                saved_count = await result_store.count_results(task_id)
                if saved_count > 0:
                    extra_msgs.append(SystemMessage(
                        content=f"[断点续爬提示] 本任务已保存 {saved_count} 条数据，"
                                f"请从第 {saved_count + 1} 条继续，不要重复抓取。"
                                f"可以调用 list_items 查看已存数据。"))
            except Exception:
                pass
            init = {"messages": extra_msgs + [HumanMessage(content=message)]}
            _log.info("[run_manager] input → continue_from_finished task_id=%s saved_count_injected=%d",
                      task_id, len(extra_msgs))
            return await self.start(task_id, graph, config, initial_state=init)
        else:
            # ── 分支 3：真·全新任务（checkpoint 里啥也没有）──
            init = initial_state or {"messages": [HumanMessage(content=message)]}
            _log.info("[run_manager] input → fresh_start task_id=%s", task_id)
            return await self.start(task_id, graph, config, initial_state=init)

    async def get_bus(self, task_id: str) -> RunEventBus | None:
        entry = self._runs.get(task_id)
        return entry["bus"] if entry else None

    async def is_finished(self, task_id: str) -> bool:
        entry = self._runs.get(task_id)
        return bool(entry and entry["finished"])

    async def get_interrupt(self, task_id: str):
        entry = self._runs.get(task_id)
        return entry["interrupt_payload"] if entry else None

    async def is_running(self, task_id: str) -> bool:
        entry = self._runs.get(task_id)
        if not entry or entry["finished"]:
            return False
        # 暂停中的 run 不算"在运行"（中断等用户处理 → steering 没人消费）
        if entry.get("paused"):
            return False
        task = entry.get("task")
        return bool(task and not task.done())

    async def remove(self, task_id: str) -> None:
        steering.remove(task_id)
        entry = self._runs.pop(task_id, None)
        if entry and entry.get("task") and not entry["task"].done():
            entry["task"].cancel()


run_manager = RunManager()
