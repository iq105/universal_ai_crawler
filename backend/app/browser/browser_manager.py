"""浏览器管理器：HTTP 客户端，调用 Node.js browser-service（Playwright JS 版）

Python 后端不再直接依赖 playwright，浏览器操作全部转发到 browser-service 进程，
有头模式下人工可直接操作弹出窗口。

修复记录（对应 docs/BUG_ANALYSIS_AND_FIXES.md）：
- B01: reconnect() 不再持锁调用 ensure()（死锁）
- B03: httpx 超时提高到 90s（/navigate 最坏路径 65~75s）
- B22: 请求带 X-Service-Token（与 browser-service 端 SERVICE_TOKEN 一致）
- B23: 进程清理不再 netstat/wmic 误杀，改为仅 taskkill 自身子进程树
- B24: 进程内导航互斥锁，避免并发任务互踢浏览器 context
- B26: 透传 STEALTH_ENABLED 给 browser-service
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()          # 保护 spawn/健康检查
        self._nav_lock = asyncio.Lock()      # B24：导航型操作互斥（切 context 会杀页面）
        self._proxy: str = ""

    @property
    def base_url(self) -> str:
        return settings.browser_service_url.rstrip("/")

    def _headers(self) -> dict:
        """B22：browser-service 开启鉴权时带上共享 token"""
        token = (settings.service_token or "").strip()
        return {"X-Service-Token": token} if token else {}

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(90.0),  # B03：/navigate 最坏 65~75s，60s 会误杀
                headers=self._headers(),
            )
        return self._client

    async def ensure(self) -> None:
        """确保 browser-service 可达；配置开启时自动拉起 Node 进程"""
        async with self._lock:
            try:
                client = await self._ensure_client()
                r = await client.get("/health", timeout=3.0)
                if r.status_code == 200:
                    return  #如果系统活着，不调用 await self._spawn()
            except Exception as exc:  # noqa: BLE001
                logger.debug("browser-service /health 不可达：%s", exc)
            if not settings.browser_service_auto_start:
                raise ConnectionError(f"browser-service 不可达（{self.base_url}），请先启动："
                                      "cd browser-service && npm start")
            await self._spawn()

    def _service_env(self, proxy: str = "") -> dict:
        env = dict(os.environ)
        env["HEADLESS"] = "true" if settings.headless else "false"
        # B26：stealth 开关透传
        env["STEALTH_ENABLED"] = "true" if settings.stealth_enabled else "false"
        env["LOCALE"] = settings.locale
        env["TIMEZONE"] = settings.timezone
        # 优先用目录模式（按域名分文件），兼容旧的单文件路径
        storage_dir = settings.storage_state_dir or (
            str(Path(settings.storage_state_path).parent) if settings.storage_state_path else ""
        )
        if storage_dir:
            env["STORAGE_STATE_DIR"] = storage_dir
        elif settings.storage_state_path:
            env["STORAGE_STATE_PATH"] = settings.storage_state_path
        env["PORT"] = self.base_url.rsplit(":", 1)[-1]
        # B22：共享 token（为空时 browser-service 不启用鉴权）
        if settings.service_token:
            env["SERVICE_TOKEN"] = settings.service_token
        if proxy:
            env["PROXY"] = proxy
        return env

    async def _spawn(self, proxy: str = "") -> None:
        """作用 ： 拉起 Node.js browser-service 子进程 ——它是 Python 端操控 Chromium 的唯一入口。整个方法就是一个"进程生命周期管理器"。"""
        cwd = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "browser-service", )
        if not os.path.isdir(cwd):
            raise ConnectionError(f"browser-service 目录不存在（查找于 {cwd}），请检查项目结构")
        env = self._service_env(proxy)
        self._proxy = proxy
        # B23：只清理自己拉起的旧子进程，不再按端口/wmic 误杀无关进程。
        # 端口被其他实例占用时，listen 会报错退出，由健康检查超时报错提示用户。
        if self._proc is not None:
            await self._kill_proc_tree(self._proc)

        self._proc = await asyncio.create_subprocess_exec("node", "server.js", cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, )
        # 等待健康检查就绪（最多 30 秒）
        stderr_tail: list[str] = []
        for i in range(60):
            await asyncio.sleep(0.5)
            # 同时读一下 stderr
            if self._proc and self._proc.stderr:
                try:
                    line = await asyncio.wait_for(self._proc.stderr.readline(), timeout=0.1)
                    if line:
                        txt = line.decode(errors="replace").strip()
                        stderr_tail.append(txt)
                        if len(stderr_tail) > 20:
                            stderr_tail.pop(0)
                except Exception:
                    pass
            try:
                client = await self._ensure_client()
                r = await client.get("/health", timeout=2.0)
                if r.status_code == 200:
                    logger.info("browser-service 就绪（第 %d 次探测通过）", i + 1)
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("等待 browser-service 就绪中 (#%d): %s", i + 1, exc)
            # 检查进程是否还活着
            if self._proc and self._proc.returncode is not None:
                stderr_full = "\n".join(stderr_tail) if stderr_tail else "(空)"
                raise ConnectionError(f"browser-service 进程已退出（returncode={self._proc.returncode}）。"
                                      f"\n最近 stderr：\n{stderr_full}")
        # 超时：读一下 stderr 末尾
        stderr_full = "\n".join(stderr_tail) if stderr_tail else "(空)"
        if self._proc:
            try:
                await self._kill_proc_tree(self._proc)
            except Exception:
                pass
        raise ConnectionError(f"browser-service 自动启动超时（30 秒）。最近 stderr：\n{stderr_full}\n"
                              f"请手动执行：cd browser-service && npm start")

    @staticmethod
    async def _kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
        """B23：杀掉自己拉起的 node 及其子进程树（Chromium 是 node 的子进程）。

        Windows：taskkill /T /F 连进程树；其他平台 terminate → kill。
        不再解析 netstat/wmic，避免误杀占用端口的无关进程。
        """
        if proc is None or proc.returncode is not None:
            return
        pid = proc.pid
        try:
            if sys.platform == "win32":
                import subprocess as _sp
                _sp.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                        capture_output=True, timeout=5)
            else:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
        except Exception as exc:  # noqa: BLE001
            logger.debug("终止 browser-service 进程树失败：%s", exc)
        finally:
            # Windows 下不同事件循环 wait() 可能报错，兜底同步等待
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass

    async def _post(self, path: str, **body) -> dict:
        """BrowserManager 的 底层基石 ——所有高层 API（`open_page` /`evaluate` /`click` /`screenshot` ...）最终都调它。"""
        await self.ensure()# ① 先确保 Node.js 进程活着（端口 8765 可达）
        client = await self._ensure_client()# ② 拿到 httpx.AsyncClient（异步 HTTP 客户端）
        r = await client.post(path, json=body or {})# ③ 发 POST，body 序列化成 JSON
        data = r.json()# ④ 响应反序列化
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or f"browser-service {path} 返回 {r.status_code}")
        return data

    async def _get(self, path: str, **params) -> dict:
        """_post 的 GET 版本"""
        await self.ensure()
        client = await self._ensure_client()
        r = await client.get(path, params=params or None)
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or f"browser-service {path} 返回 {r.status_code}")
        return data

    # ---------------- 原子操作 ----------------

    async def open_page(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000,
            wait_shield: bool = True) -> dict:
        # 包一层反封禁守卫：429/403/反爬时指数退避 + 代理轮换
        from app.services.anti_ban import with_guard

        # B24：导航会切换/重建 context，与其他任务的导航互斥
        async with self._nav_lock:
            return await with_guard(
                lambda: self._post("/navigate", url=url, wait_until=wait_until, timeout=timeout,
                                   wait_shield=wait_shield))

    async def page_state(self) -> dict:
        return await self._post("/page-state")

    async def snapshot(self, max_text_len: int = 6000) -> str:
        data = await self._post("/snapshot", max_text_len=max_text_len)
        return data.get("text") or ""

    async def html(self, max_len: int = 20000) -> str:
        data = await self._post("/html", max_len=max_len)
        return data.get("html") or ""

    async def scroll(self, direction: str = "down", amount: int = 1200, max_scrolls: int = 10) -> dict:
        return await self._post("/scroll", direction=direction, amount=amount, max_scrolls=max_scrolls)

    async def click(self, selector: str = "", text: str = "", timeout: int = 5000) -> dict:
        return await self._post("/click", selector=selector, text=text, timeout=timeout)

    async def fill(self, selector: str, value: str) -> dict:
        return await self._post("/fill", selector=selector, value=value)

    async def select_option(self, selector: str, value: str = "", label: str = "",
                            index: int = -1, timeout: int = 5000) -> dict:
        """下拉框选择（原生 <select> 和 自定义下拉）。

        原生 select：value/label/index 三选一即可。
        自定义下拉（antd/el-select 等）：先点开，再按文本匹配点击。
        """
        return await self._post("/select-option", selector=selector, value=value,
                                label=label, index=index, timeout=timeout)

    async def upload_file(self, selector: str, file_path: str) -> dict:
        """文件上传（<input type="file">）。Playwright 的 setInputFiles 对隐藏 input 也有效。"""
        return await self._post("/upload-file", selector=selector, file_path=file_path)

    async def press(self, key: str = "Enter") -> dict:
        return await self._post("/press", key=key)

    async def evaluate(self, script: str, js_args: dict | None = None) -> object:
        data = await self._post("/evaluate", script=script, args=js_args)
        return data.get("result")

    async def screenshot(self, dir: str = "artifacts/screenshots") -> dict:
        return await self._post("/screenshot", dir=dir)

    async def screenshot_element(self, selector: str = "", index: int = 0) -> dict:
        """元素截图（验证码用）：返回 base64 图片数据；无选择器则整页截图"""
        data = await self._post("/screenshot-element", selector=selector, index=index)
        return {"base64": data.get("base64") or "", "scope": data.get("scope") or "",
                "selector": data.get("selector") or ""}

    # ============ 多标签页管理 ============

    async def tabs_list(self) -> list:
        """列出所有标签页"""
        await self.ensure()
        client = await self._ensure_client()
        r = await client.get("/tabs")
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or "tabs-list 失败")
        return data.get("tabs", [])

    async def tabs_open(self, url: str = "") -> dict:
        """新开标签页（可选立即导航）"""
        return await self._post("/tabs/open", url=url)

    async def tabs_switch(self, target) -> dict:
        """切到指定标签页。target 可以是 int(index) 或 str(url/title 关键字)"""
        return await self._post("/tabs/switch", target=target)

    async def tabs_close(self, target="current") -> dict:
        """关闭标签页。target='current' 或 int(index)"""
        return await self._post("/tabs/close", target=target)

    # ============ 鼠标悬停 ============

    async def hover(self, selector: str = "", text: str = "") -> dict:
        """鼠标悬停触发下拉菜单/tooltip"""
        return await self._post("/hover", selector=selector, text=text)

    # ============ iframe 切换 ============

    async def switch_frame(self, target: str | None = None) -> dict:
        """切进 iframe。target=null 切回主 frame；可以是 CSS 选择器、iframe name、url 关键字"""
        return await self._post("/switch-frame", target=target)

    # ============ 文件下载 ============

    async def download_file(
        self,
        trigger_selector: str = "",
        trigger_text: str = "",
        url: str = "",
        filename: str = "",
        timeout: int = 60000,
    ) -> dict:
        """触发下载并保存到本地。三种触发方式：selector（点元素）/ text（按文本点）/ url（直接下载）"""
        trigger = {}
        if selector:
            trigger["selector"] = selector
        if trigger_text:
            trigger["text"] = trigger_text
        if url:
            trigger["url"] = url
        return await self._post("/download", trigger=trigger, filename=filename, timeout=timeout)

    def _terminate(self) -> None:
        """终止当前 node 子进程（同步接口，尽力而为）"""
        if self._proc is not None and self._proc.returncode is None:
            try:
                if sys.platform == "win32":
                    import subprocess as _sp
                    _sp.run(["taskkill", "/T", "/F", "/PID", str(self._proc.pid)],
                            capture_output=True, timeout=5)
                else:
                    self._proc.terminate()
            except Exception:
                pass

    async def reconnect(self, proxy: str = "") -> None:
        """以指定代理重启浏览器会话（用于代理轮换 / 反封禁自愈）

        B01 修复：不再调用 ensure()（它也要拿 self._lock，会造成不可重入死锁）。
        _spawn() 内部已含健康检查等待，效果等同。
        """
        async with self._lock:
            # 1. 关闭旧连接与进程
            try:
                if self._client is not None:
                    await self._client.post("/close")
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭旧浏览器会话失败：%s", exc)
            if self._proc is not None:
                await self._kill_proc_tree(self._proc)
            self._proc = None
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            # 2. 用新代理重新拉起（_spawn 内部等待 /health 就绪）
            await self._spawn(proxy=proxy)

    async def close(self) -> None:
        try:
            if self._client is not None:
                await self._client.post("/close")
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭浏览器会话失败：%s", exc)
        finally:
            if self._proc is not None:
                # B23：杀整个进程树（node + Chromium），避免孤儿浏览器进程
                await self._kill_proc_tree(self._proc)
            self._proc = None
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    # ---------------- 登录态持久化（按域名） ----------------

    async def save_state(self) -> dict:
        """强制保存当前域名的登录态（用户手动登录完后调用）"""
        await self.ensure()
        client = await self._ensure_client()
        r = await client.post("/save-state")
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or "save-state 失败")
        logger.info("登录态已保存 host=%s file=%s", data.get("host"), data.get("file"))
        return data

    async def state_info(self) -> dict:
        """获取当前状态（当前域名、storage 文件、是否有登录墙、所有已保存域名）"""
        await self.ensure()
        client = await self._ensure_client()
        r = await client.get("/state-info")
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or "state-info 失败")
        return data

    async def state_list(self) -> list[dict]:
        """列出所有已保存的登录态文件"""
        await self.ensure()
        client = await self._ensure_client()
        r = await client.get("/state-list")
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or "state-list 失败")
        return data.get("states", [])

    async def delete_state(self, host: str) -> dict:
        """删除指定域名的登录态"""
        await self.ensure()
        client = await self._ensure_client()
        r = await client.delete(f"/state/{host}")
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or "delete-state 失败")
        logger.info("登录态已删除 host=%s", host)
        return data

    # ---------------- Proxy 轮换池 ----------------

    async def proxy_status(self) -> dict:
        """查看代理池状态"""
        return await self._get("/proxy-status")

    async def proxy_rotate(self) -> dict:
        """强制轮换下一个代理"""
        return await self._post("/proxy-rotate")

    # ---------------- 请求拦截（屏蔽 + mock） ----------------

    async def block_set(self, patterns: list[str]) -> dict:
        """替换整个屏蔽规则列表"""
        return await self._post("/block-patterns/set", patterns=patterns)

    async def block_add(self, pattern: str) -> dict:
        """追加一条屏蔽规则"""
        return await self._post("/block-patterns/add", pattern=pattern)

    async def block_reset(self) -> dict:
        """清空屏蔽规则，恢复默认"""
        return await self._post("/block-patterns/reset")

    async def mock_add(self, matcher: str, status: int = 200, body: str = "",
                       headers: dict | None = None, content_type: str = "") -> dict:
        """添加一个请求 mock"""
        return await self._post(
            "/route-mock/add",
            matcher=matcher, status=status, body=body,
            headers=headers or {}, contentType=content_type,
        )

    async def mock_clear(self) -> dict:
        """清空所有 mock"""
        return await self._post("/route-mock/clear")


browser_manager = BrowserManager()
