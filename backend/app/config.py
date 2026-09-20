"""全局配置：pydantic-settings 从 .env 读取"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Universal Crawler"

    # LLM（OpenAI 兼容）
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.0

    # 数据库 / Redis
    database_url: str = "sqlite+aiosqlite:///./crawler.db"
    redis_url: str = ""

    # 爬虫
    max_pages: int = 5
    max_extract_retries: int = 2
    headless: bool = False  # 默认有头，人工介入可直接操作弹出窗口
    stealth_enabled: bool = True
    # 浏览器会话持久化：按域名分文件保存 cookies / localStorage / sessionStorage
    # Playwright storage_state JSON 格式，Node browser-service 读写
    # 每个域名自动生成一个文件，如 jd.com.json、taobao.com.json
    storage_state_dir: str = str(Path("artifacts") / "browser" / "storage")
    # 旧的单文件路径（兼容迁移用，新逻辑优先 storage_state_dir）
    storage_state_path: str = ""
    force_interrupt_at: str = ""
    max_steps: int = 30
    media_dir: str = "artifacts/media"
    ffmpeg_path: str = ""

    # 浏览器服务（Node.js Playwright 微服务）
    browser_service_url: str = "http://127.0.0.1:8765"
    browser_service_auto_start: bool = True
    # B22：browser-service 共享鉴权 token（留空 = 不启用鉴权；生产建议设置）
    service_token: str = ""
    # 地域画像：locale / timezone / --lang 必须一致，并尽量与代理出口 IP 地域匹配
    # （做海外站或挂国外代理时覆盖，避免中文 locale + 国外 IP 的地域矛盾）
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"

    # 反封禁 / 代理池（逗号分隔，如 "socks5://127.0.0.1:1080,http://user:pass@host:port"）
    proxy_list: str = ""
    anti_ban_max_retries: int = 3

    # RAG / Embedding（复用 LLM 同 base_url + api_key，换 model 名即可）
    embedding_model: str = "text-embedding-v3"  # 百炼 v3；或 "bge-m3" 配合本地 Ollama
    embedding_enabled: bool = True  # 关了就不建 Chroma，不存不检索
    knowledge_dir: str = str(Path("artifacts") / "knowledge")  # Chroma 数据目录

    # 服务
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # 确保 storage_state 目录存在（Playwright 写文件需要目录）
    target_dir = s.storage_state_dir or (str(Path(s.storage_state_path).parent) if s.storage_state_path else "")
    if target_dir:
        Path(target_dir).mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
