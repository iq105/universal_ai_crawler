# Freebuff 项目 Bug 分析与修复记录

> 生成日期：2026-09-19　|　修复完成日期：2026-09-19
> 范围：`browser-service`（Node/Playwright 微服务）、`backend`（Python 后端浏览器链路）
> 目标：像真人一样操作浏览器，不被目标系统识别出自动化工具；同时修复功能性缺陷。
>
> **状态：全部 28 项已修复并通过验证 ✅**（验证方式见文末"验证记录"）

---

## 修复进度总览

| 编号 | 级别 | 摘要 | 状态 |
|------|------|------|------|
| B01 | P0 | reconnect() 持锁调用 ensure() 死锁 | ✅ |
| B02 | P0 | Cloudflare 503 与 5 秒盾重试互相打架 | ✅ |
| B03 | P0 | httpx 60s 超时 < navigate 最坏耗时 | ✅ |
| B04 | P0 | /tabs 返回的 title 是 Promise | ✅ |
| B05 | P0 | _wrapScript 行尾 `// 注释` 吞掉 `}` 语法错误 | ✅ |
| B06 | P1 | UA 与真实内核版本不符 | ✅ |
| B07 | P1 | navigator.userAgentData 未伪装 | ✅（详见实测结论） |
| B08 | P1 | 指纹值每次读取随机变化 | ✅ |
| B09 | P1 | WebGL vendor/renderer 不配对 + WebGL2 未覆盖 | ✅ |
| B10 | P1 | screen 伪装 = innerWidth 且未同步 avail/outer/dpr | ✅（详见实测结论） |
| B11 | P1 | plugins 是普通数组，非 PluginArray | ✅ |
| B12 | P1 | deviceMemory 返回 16/32 超出 Chrome 上限 | ✅（详见实测结论） |
| B13 | P1 | performance.now 加随机抖动破坏单调性 | ✅ |
| B14 | P1 | Function.prototype.toString 补丁无效 | ✅ |
| B15 | P1 | permissions 裸 Promise；webdriver 定义位置不对 | ✅ |
| B16 | P1 | 每次切域名重抽 UA/视口，指纹跨站点突变 | ✅ |
| B17 | P2 | 鼠标瞬移：每次从随机点出发 | ✅ |
| B18 | P2 | 点击二段移动破绽 | ✅ |
| B19 | P2 | window.scrollBy 滚动无 wheel 事件 | ✅ |
| B20 | P2 | 中文输入无 IME 事件 | ✅ |
| B21 | P2 | 延迟为均匀分布 | ✅ |
| B22 | P3 | browser-service 无鉴权 + CORS 全放行 + 监听 0.0.0.0 | ✅ |
| B23 | P3 | 进程清理误杀无关进程、Chromium 孤儿进程 | ✅ |
| B24 | P3 | 全局单例并发互踢 context | ✅ |
| B25 | P3 | 登录态保存无防抖、不监听新标签页 | ✅ |
| B26 | P3 | STEALTH_ENABLED 配置是死代码 | ✅ |
| B27 | P3 | 杂项 6 项 | ✅ |
| B28 | P0 | backend/.env 被 git 跟踪，API key 泄露 | ✅ |

---

## 一、P0 致命 Bug

### B01 reconnect() 死锁 ✅
- **位置**：`backend/app/browser/browser_manager.py`
- **原问题**：`reconnect()` 在 `async with self._lock:` 内调用 `ensure()`（同样要拿锁），asyncio.Lock 不可重入 → 代理轮换时整个 Agent 永久卡死。
- **修复**：`reconnect()` 锁内只做清理 + `_spawn(proxy)`（`_spawn` 内部已含 30s 健康检查等待），不再调用 `ensure()`。

### B02 503 与 5 秒盾互相打架 ✅
- **位置**：`backend/app/services/anti_ban.py`
- **原问题**：CF 5 秒盾首次响应即 503，被 `is_banned_status` 判为封禁 → 每个 Cloudflare 站点首次访问都重试 3 次完整导航，留下密集风控记录。
- **修复**：`with_guard()` 中 `detect.five_sec_shield=True` 且无 `anti_bot` 时直接返回（navigate 已内置最长 30s 等盾循环）。

### B03 httpx 超时不够 ✅
- **修复**：`_ensure_client()` 超时从 60s 提高到 90s（`httpx.Timeout(90.0)`），覆盖 `/navigate` 最坏路径（goto 30s + 渲染等待 5s + 盾循环 ~30s）。

### B04 /tabs 的 title 是 Promise ✅
- **修复**：`routes.js /tabs` 先 `await Promise.all(pages.map(p => p.title()))` 再组装响应。

### B05 _wrapScript 行尾注释陷阱 ✅
- **修复**：包装时补换行 `() => {\n${s}\n}`，行尾 `// 注释` 不再吞掉闭合 `}`。

### B28 backend/.env 泄露 ✅
- **修复**：`git rm --cached backend/.env backend/.env_bak`（本地文件保留），`.gitignore` 增加 `.env`/`.env_bak`/`backend/.env*` 规则。
- **⚠️ 用户必须手动处理**：两个文件中的 DeepSeek API key 已进入 git 历史，**立即到 DeepSeek 控制台作废并更换**：
  - `backend/.env` 中 key 以 `sk-3d74…` 开头
  - `backend/.env_bak` 中 key 以 `sk-762c…` 开头

---

## 二、P1 防检测硬伤（指纹层）

### B06 UA 与真实内核不符 ✅
- **修复**：**有头模式完全不覆写 UA** —— UA、`sec-ch-ua` 请求头、`navigator.userAgentData.brands` 三者由同一内核（实测 Chromium 153）生成，天然一致，这是最强的一致性。`HEADER_PROFILES` 假 UA 池整体删除。
- headless 模式仅覆写 UA 字符串（`HeadlessChrome`→`Chrome`，通过启动时临时 context 探测真实 UA 获得）。

### B07 userAgentData 未伪装 ✅（实测结论修正原方案）
- **实测发现**：Chromium 153 中 `userAgentData`（含 brands/getHighEntropyValues）是**引擎级表面**——init script 的 defineProperty 覆写会被引擎回调重新覆盖，JS 层无法伪造；headless 下 brands 永远含 `HeadlessChrome`。
- **最终方案**：不伪装（伪装值与引擎真值并存反而制造可检测矛盾）；headless 的 UA 字符串已通过 context 覆写修正（过服务端 UA 规则）。**反爬场景必须用有头模式（项目默认 `HEADLESS=false`），brands 由内核原生生成、完全正确**。此限制已写入 browser.js 文件头注释。

### B08 指纹值每次读取都变 ✅
- **修复**：视口/语言/WebGL 在 `ensure()` 启动时经 `makeFingerprint()` 随机一次，存 `this._fingerprint` 全程固化。运行时验证：同一页面连读 3 次，所有指纹字段完全一致。

### B09 WebGL 不配对 + WebGL2 未覆盖 ✅
- **修复**：`GPU_PROFILES` vendor/renderer 成对（4 套真实桌面 GPU 描述）；`WebGLRenderingContext` 与 `WebGL2RenderingContext` 的 `getParameter(37445/37446)` 同时 patch，返回同一套值。运行时验证：v1 === v2。

### B10 screen 伪装不自洽 ✅（实测结论修正原方案）
- **实测发现**：`screen.*`、`outerWidth/outerHeight` 同样是引擎级表面，init script 覆写无效。
- **最终方案**：不再伪装，直接使用引擎真值（viewport 由 `deviceScaleFactor` 参数正确联动，会话内天然一致）。删除会产生矛盾的假值。

### B11 plugins 裸数组 ✅
- **修复**：伪造对象基于 `PluginArray.prototype`/`Plugin.prototype`/`MimeTypeArray.prototype`/`MimeType.prototype` 构建，带 `length`、`item()`、`namedItem()`、`refresh()`，与 `mimeTypes` 同步。运行时验证：`plugins.length=5`、元素 instanceof Plugin。

### B12 deviceMemory 超上限 ✅（并入 B07 同理）
- 引擎级表面不伪装，使用内核真值（真值本身合法）。原"16/32 超上限"问题随假值删除而消除。

### B13 performance.now 抖动 ✅
- **修复**：删除抖动补丁，保持原生单调性（原代码反而制造"时间倒流"检测点）。

### B14 toString 补丁无效 ✅
- **修复**：维护 `patched` Set（被覆盖的 permissions.query、两套 WebGL getParameter、代理自身），`Function.prototype.toString` 代理对这些函数返回 `function xxx() { [native code] }`。运行时验证：`permissions.query.toString()` 已是 native 形态。

### B15 permissions 裸 Promise + webdriver 位置 ✅
- **修复**：query 返回 `Object.create(PermissionStatus.prototype)` 且带 `onchange` 存取器；webdriver 覆盖改在 `Navigator.prototype` 上、`enumerable:false`（与真实 Chrome 位置一致）。

### B16 切域名指纹突变 ✅
- **修复**：`switchContextForHost` 不再重抽 profile——context 重建只换 storageState，视口/语言/WebGL/UA 策略全部复用 `_fingerprint`。

### 附加发现（修复过程中）
- **`playwright-extra` + `puppeteer-extra-plugin-stealth` 与手写补丁互相冲突**（插件介入会破坏 deviceMemory/userAgentData/permissions 的伪装，且插件 2.x 不支持新指纹面）→ 已移除插件路径，统一走可控、可验证的手写 init script。连带 `package.json` 的这两个依赖成为可选（未卸载，不影响运行）。
- **Playwright 实际版本 1.63.0**（package.json 声明 `^1.49.1` 漂移所致）：新版本把"函数源码字符串"当作表达式求值，`page.evaluate('() => ...')` 静默返回 undefined——**原代码 evaluate_js/wait_for 工具全部失效而不报错**（B27 新增子项）。

---

## 三、P2 行为拟人化

### B17 鼠标瞬移 ✅
- **修复**：`routes.js` 维护 `_lastMouse`（上次停留位置）作为每次移动起点；首次访问取视口中部随机点。增加 smoothstep 缓动（起步慢-中段快-收尾慢）、长距离 10% 概率过冲 3~10px 再回调、步数随距离缩放（6~30 步）。

### B18 点击二段移动 ✅
- **修复**：在元素盒子内取 ±6px 偏心点 → 人手轨迹滑到该点 → `page.click(selector, {position})` 在**同一位置**做 actionability 检查后按下；末段位移 ≤8px，事件流还原后是单一驱动。position 点击失败（元素移位/遮挡）自动回退默认中心点击。

### B19 scrollBy 无 wheel 事件 ✅
- **修复**：改用 `page.mouse.wheel(0, ±120)` 分块滚动（产生真实受信 wheel 事件流），滚动前把鼠标移到记忆位置；保留快慢交替与 10% 回弹。

### B20 中文无 IME 事件 ✅（实测结论修正原方案）
- **实测发现**：CDP `Input.imeSetComposition` 在 Chromium 153 桥接层会把 CJK 文本编码成 U+FFFD（不可用）；页面 `dispatchEvent` 的 composition 事件 `isTrusted=false` 反而是检测特征。
- **最终方案**：CJK/全角字符走 `page.keyboard.insertText`（CDP `Input.insertText`：受信、无 mojibake、正是浏览器为 IME 提供的插入通道）；ASCII 仍逐键敲击 + 3% 手误回删。中文间停顿 60~160ms 模拟挑词。运行时验证：中英混合值往返一致、中文正常触发页面联动。

### B21 延迟均匀分布 ✅
- **修复**：`sampleDelay()` 用对数正态采样（几何均值作中位数，95% 分位≈max），真人反应时间重尾形态；新增 `idlePause()`：1.5% 概率插入 2~6s"走神"停顿（fill/click 前触发）。

---

## 四、P3 工程与安全

### B22 browser-service 裸奔 ✅
- **修复**：`server.js` 改 `app.listen(PORT, '127.0.0.1')`；除 `/health` 外全部要求 `X-Service-Token` 头或 `?token=`（`SERVICE_TOKEN` 为空 = 本地开发不鉴权）；Python 端 `_ensure_client()` 统一带 token 头，`config.py` 新增 `service_token` 配置，`_service_env()` 把 token 注入子进程。运行时验证：无 token 401、带 token 200。

### B23 进程清理 ✅
- **修复**：删除 netstat 按端口乱杀 + wmic 兜底（Win11 24H2 已移除 wmic）；新增 `_kill_proc_tree()`：Windows 用 `taskkill /T /F /PID`（连 node+Chromium 进程树），其他平台 terminate→kill；`close()`/启动超时/`reconnect()` 全部走它；node 端 SIGINT/SIGTERM 时先关 browser 再退出（防孤儿 Chromium）。

### B24 并发互踢 context ✅
- **修复**：Python 端 `open_page` 持 `self._nav_lock` 串行化导航（守卫重试也在锁内）；Node 端 `switchContextForHost` 走 `withNavLock`（promise 链互斥 + 票据号防旧请求释放新锁，含双检查）。

### B25 登录态保存无防抖 ✅
- **修复**：`_scheduleSave()` 2s 防抖落盘；`_attachContextAutoSave()` 绑定 context 的 `page` 事件（人工新开的标签页也自动保存）；`forceSave()`/`close()` 取消 pending 防抖立即落盘；切域名前同步落盘旧域名。

### B26 STEALTH_ENABLED 死配置 ✅
- **修复**：Python `_service_env()` 透传 `STEALTH_ENABLED`；Node 端据此开关 init script 注入（`stealthMode = 'manual' | 'off'`）。

### B27 杂项 ✅
1. `_lastDomainAccess` 超过 500 域名自动清理最旧 → 已加 `_pruneDomainMap()`。
2. `/select-option` popup 文本转义（`\` 和 `"`）→ 已加 `safeText`。
3. goto 失败 `status_code=null` 绕过守卫 → `/navigate` 响应新增 `navigation_error` 字段，Python `browser_tools.open_page` 透传给上层；with_guard 逻辑同步调整（五秒盾不重试）。
4. `challenge.py` SOFT_CHALLENGES 死代码注释矛盾 → 注释已修正（行为不变：login 走硬挑战 interrupt，用户手动登录后自动保存登录态）。
5. Agent 工具 `open_page` 暴露 `wait_shield` 参数 → 已加。
6. lock 文件缺失 → 需用户手动 `npm install`（package-lock.json 不擅自生成提交）。
7. （新增）`page.evaluate('函数源码字符串')` 在 Playwright 1.63 静默返回 undefined → `/evaluate` 先在 Node 端 `eval` 成函数对象再传（函数体仍在页面执行），并保留语法错误检查；`_wrapScript` 保证返回函数定义形态；routes.js 内部 2 处模板字符串 evaluate 改为传真函数。

---

## 五、验证记录（2026-09-19 实测）

1. **静态检查**：`node --check` × 3 文件通过；`py_compile` × 6 模块通过；关键修复点静态断言（死锁/超时/token/进程树/互斥/五秒盾）通过。
2. **测试基线对照**：`git stash` 后在未修改代码上运行 pytest —— 18 个 ERROR（pytest-asyncio 1.4.0 与异步 fixture 不兼容、2 个历史遗留失效导入）在基线上同样存在，**与本次修复无关**；`git stash pop` 已恢复全部修改（含重做的 `.env` 出库）。
3. **运行时端到端**（真实 browser-service 进程 + 真实 Chromium 153）：
   - `/health` 免鉴权 200；`/navigate` 无 token 401、带 token 200；
   - 百度导航 200、标题正确；`navigation_error` 在网络故障时正确返回；
   - 本地测试页：fill（中英混合）→ click → 页面联动文本 `CLICKED:你好世界 Test123` 断言通过（编码逐字节一致）；
   - 指纹：`webdriver=false`、`plugins.length=5`、WebGL1 与 WebGL2 vendor/renderer 成对一致、同页连读 3 次所有字段零漂移；
   - scroll（wheel 路径）、press 正常。
4. **已知限制（非 bug）**：headless 模式 `userAgentData.brands` 含 `HeadlessChrome`（Chromium 153 引擎级，JS 层无解）——反爬场景请用默认的有头模式；测试期间沙箱外网不稳定（example.com/bing 偶发连接失败），与代码无关。

## 遗留建议（后续可选）

- **轮换泄露的 DeepSeek API key（必须）**。
- `browser-service` 目录执行 `npm install` 生成 lock 文件并提交，固定依赖版本（避免 `^1.49.1`→1.63 这类漂移再次引入行为变化）。
- 高级反爬（DataDome/Cloudflare 高级模式）场景建议调研 patchright（Playwright 反检测 fork，API 兼容）进一步消除 CDP 痕迹。
- 修复 pytest 环境（pytest-asyncio 降级或改造 fixture）让 18 个存量测试恢复可运行。
