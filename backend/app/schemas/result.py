"""结果相关 Pydantic 模型"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str
    item_index: int
    page_no: int
    data: dict
    created_at: datetime


class ResultPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[ResultOut]
