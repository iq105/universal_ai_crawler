"""本地启动入口（FastAPI + 纯 HTML 前端）

用法：
    cd backend
    python run_server.py

然后浏览器打开 http://127.0.0.1:8899/
"""
import asyncio
import logging
import subprocess

from app.core.logger import setup_logging

setup_logging()

logger = logging.getLogger("run_server")
import app.tools  # noqa: F401  确保工具注册


def _kill_port(port: int) -> None:
    """杀掉占用指定端口的进程（Windows netstat + taskkill）"""
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
                pid = parts[4]
                if pid.isdigit():
                    logger.info("清理残留进程 PID=%s（占用端口 %d）", pid, port)
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=3)
    except Exception as _e:
        logger.debug("清理端口 %d 失败：%s", port, _e)


async def _init():
    # ---- 启动前先清理残留 ----
    # 1. 杀掉旧的 browser-service（端口 8765）
    _kill_port(8765)
    # 2. 杀掉可能残留的旧 FastAPI（端口 8899）
    _kill_port(8899)
    await asyncio.sleep(0.5)  # 等端口释放

    # ---- 拉起 browser-service ----
    try:
        from app.browser.browser_manager import browser_manager
        await browser_manager.ensure()
        logger.info("browser-service 已就绪（%s）", browser_manager.base_url)
    except Exception as exc:
        logger.warning("browser-service 未能自动拉起，将延迟到首次抓取时再尝试：%s", exc)


if __name__ == "__main__":
    asyncio.run(_init())
    import uvicorn
    logger.info("FastAPI 服务已构建，端口 8899，正在启动…")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8899, reload=False, log_level="info")
