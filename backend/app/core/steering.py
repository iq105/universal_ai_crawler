"""运行中对话指挥：进程内 per-task 消息队列

用户通过 /api/chat 对运行中的任务发消息时，消息入队；
agent_think 节点每轮 drain 队列，把新指令注入 Agent 对话。
"""
import asyncio


class SteeringManager:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}

    def _queue_for(self, task_id: str) -> asyncio.Queue:
        if task_id not in self._queues:
            self._queues[task_id] = asyncio.Queue()
        return self._queues[task_id]

    async def push(self, task_id: str, message: str) -> None:
        await self._queue_for(task_id).put(message)

    async def drain(self, task_id: str) -> list[str]:
        """取出当前所有未消费的用户消息"""
        q = self._queue_for(task_id)
        out: list[str] = []
        while not q.empty():
            try:
                out.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    def remove(self, task_id: str) -> None:
        self._queues.pop(task_id, None)


steering = SteeringManager()
