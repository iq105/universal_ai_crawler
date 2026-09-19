"""任务相关 Pydantic 模型"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    url: str = Field(..., description="目标 URL")
    instruction: str = Field("", description="用户指令，如：抓取文章标题、链接、发布时间")
    max_pages: int | None = Field(None, ge=1, le=50, description="最大翻页数，默认取配置")


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    instruction: str | None
    max_pages: int | None
    status: str
    progress: int
    parse_scheme: dict | None
    total_items: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class TaskDetailOut(TaskOut):
    logs: list = []
    result_count: int = 0


class ResumeIn(BaseModel):
    value: dict | str = Field(..., description="人工介入的回复内容")
