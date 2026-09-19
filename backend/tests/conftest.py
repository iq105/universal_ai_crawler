"""pytest 公共夹具：路径注入 + 临时数据库隔离（AGENT.md #50）。

- 把 backend 目录加入 sys.path，保证 `import app.*` 可用（无需安装包）
- 每个测试会话使用独立的临时 SQLite，绝不污染开发库
- DB 引擎在每个测试所属事件循环内初始化，避免跨 loop 绑定问题
- 外部依赖（LLM、浏览器）在各自测试内 Mock
"""
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_TMPDIR = tempfile.mkdtemp(prefix="crawler_test_").replace("\\", "/")
_DB_FILE = f"{_TMPDIR}/test.db"


@pytest.fixture(scope="session", autouse=True)
def _db_path():
    """会话级：把数据库重定向到临时文件（仅设置，不建引擎）。"""
    from app.config import settings

    settings.database_url = f"sqlite+aiosqlite:///{_DB_FILE}"
    yield


@pytest.fixture(autouse=True)
async def _init_db(_db_path):
    """每个测试在当前事件循环内初始化引擎并建表（幂等）。"""
    from app.core import db

    db._engine = None
    db._session_factory = None
    await db.init_db()
    yield