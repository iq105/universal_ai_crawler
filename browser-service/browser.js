// browser.js — Playwright JS 浏览器单例管理
// 有头/无头、隐身指纹、会话持久化（storage_state）
//
// 防检测设计要点（对应 docs/BUG_ANALYSIS_AND_FIXES.md B06-B16/B24/B25/B26）：
// 1. 不覆写 UA（headed 模式）—— UA / sec-ch-ua 头 / navigator.userAgentData 三者由同一
//    内核生成，天然一致。headless 模式仅覆写 UA 字符串去掉 HeadlessChrome（过服务端
//    UA 规则）；但 userAgentData.brands 是 Chromium 153 引擎级表面，JS 层无法伪装，
//    反爬场景请用有头模式（项目默认 HEADLESS=false）。
// 2. 视口/语言/WebGL 在启动时随机一次并固化（this._fingerprint），切域名复用同一指纹
//    —— 真实用户的设备指纹不会在几分钟内突变。screen/硬件数等引擎级表面直接用真值
//    （会话内天然一致，伪装反而制造矛盾）。
// 3. init script 里所有伪装值固定，不随读取次数变化；WebGL vendor/renderer 成对出现；
//    performance.now 保持单调（不做抖动）；toString 对被覆盖函数返回 native code。
const path = require('path');
const fs = require('fs');

const STEALTH_ENABLED = String(process.env.STEALTH_ENABLED || 'true').toLowerCase() !== 'false';

// 地域画像：locale/timezone/--lang 必须一致，且最好与代理出口 IP 地域匹配
// （默认中文站场景为 zh-CN / Asia/Shanghai；做海外站时请通过 env 覆盖）
const LOCALE = process.env.LOCALE || 'zh-CN';
const TIMEZONE = process.env.TIMEZONE || 'Asia/Shanghai';

// 注：不再使用 playwright-extra + puppeteer-extra-plugin-stealth。
// 实测（Playwright 1.63）插件补丁与下方手写 init script 互相覆盖冲突
// （会破坏 deviceMemory / userAgentData 的伪装），且插件 2.x 不支持
// userAgentData / 新版指纹面。手写脚本已完整覆盖所有常见检测点，且可控可验证。
const chromium = require('playwright').chromium;

// ===== 指纹池（启动时随机一次，之后全程固化；B16：切域名不再重抽） =====
// 视口池（真实桌面用户分辨率分布）
const VIEWPORT_POOL = [
  { width: 1920, height: 1080 },  // 最常见
  { width: 1536, height: 864 },   // 笔记本缩放下
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
  { width: 1600, height: 900 },
  { width: 1920, height: 1200 },
  { width: 2560, height: 1440 },
];

// Accept-Language 池（不再绑定 UA 版本；首项须与 locale 一致）
const LANG_PROFILES = [
  'zh-CN,zh;q=0.9,en;q=0.8',
  'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
  'zh-CN,zh-Hans;q=0.9,en;q=0.8',
];

// WebGL vendor/renderer 必须成对（B09）
const GPU_PROFILES = [
  { vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)' },
  { vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Super Direct3D11 vs_5_0 ps_5_0, D3D11)' },
  { vendor: 'Google Inc. (AMD)', renderer: 'ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
  { vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x000046A6) Direct3D11 vs_5_0 ps_5_0, D3D11)' },
];

const HARDWARE_POOL = {
  dpr: [1, 1, 1.25, 1.5],      // Windows 常见缩放
};

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

/** 由 Accept-Language 字符串推导 navigator.languages —— 两者必须一一对应，
 * 否则高级风控会比对请求头与 JS 表面发现矛盾（真实 Chrome 中 languages 恰是
 * Accept-Language 的去权重 list）。 */
function parseLanguages(lang) {
  return String(lang || '')
    .split(',')
    .map((s) => s.trim().split(';')[0])
    .filter(Boolean);
}

/** 生成一次并固化的指纹（进程生命周期内不变）
 * 注：screen/cores/memory 等引擎级表面不在此列（伪装无效，直接用引擎真值），
 * 这里只生成“JS 层可控”+“Playwright context 参数可控”的字段。
 */
function makeFingerprint() {
  const viewport = pick(VIEWPORT_POOL);
  const dpr = pick(HARDWARE_POOL.dpr);
  const gpu = Math.floor(Math.random() * GPU_PROFILES.length);
  const lang = pick(LANG_PROFILES);
  return {
    viewport,
    dpr,
    lang,
    langs: parseLanguages(lang),  // 与 Accept-Language 请求头严格一致
    gpuIndex: gpu,                // WebGL1/2 用同一套（同机不可能两套 GPU）
  };
}

// ===== 手动指纹伪装（仅覆盖 JS 层可靠可控的表面）=====
// 实测（Chromium 153 / Playwright 1.63）：
// - navigator.userAgentData / deviceMemory / hardwareConcurrency / screen / outerWidth
//   已是引擎级表面：init script 覆写会被引擎回调重新覆盖（写入后读回仍是引擎值），
//   且 headless 下 brands 永远含 HeadlessChrome（JS 层无解）。
//   → 引擎真值在会话内天然一致（不随读取变化），伪装它们反而制造矛盾，故不碰。
// - headless 模式对高级风控（检查 userAgentData.brands）天生劣势，反爬场景请用有头模式
//   （HEADLESS=false，项目默认值）：UA/userAgentData/屏幕全部原生一致。
// 以下表面在 JS 层可稳定伪装且与真值不冲突：
//   webdriver / plugins / mimeTypes / languages / window.chrome / WebGL vendor+renderer /
//   permissions / Function.prototype.toString
function manualStealthInitScript(fp) {
  const gpu = GPU_PROFILES[fp.gpuIndex] || GPU_PROFILES[0];

  return `
(() => {
  const gpu1 = ${JSON.stringify(gpu)};
  const langs = ${JSON.stringify(fp.langs || ['zh-CN', 'zh', 'en'])};

  // === 1. webdriver（真实 Chrome 中定义在 Navigator.prototype 上且不可枚举）===
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false,
      configurable: true,
      enumerable: false,
    });
  } catch (e) {}

  // === 2. plugins / mimeTypes：带方法与 length 的对象，非裸数组 ===
  try {
  const pluginData = [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 2, mimes: ['application/pdf', 'text/pdf'] },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 2, mimes: ['application/pdf', 'text/pdf'] },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 2, mimes: ['application/pdf', 'text/pdf'] },
    { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 2, mimes: ['application/pdf', 'text/pdf'] },
    { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 2, mimes: ['application/pdf', 'text/pdf'] },
  ];
  const mimeTypeOf = (p, m) => Object.create(MimeType.prototype, {
    type: { get: () => m }, suffixes: { get: () => 'pdf' },
    description: { get: () => p.description }, enabledPlugin: { get: () => p },
  });
  const fakePlugins = pluginData.map(d => Object.create(Plugin.prototype, {
    name: { get: () => d.name }, filename: { get: () => d.filename },
    description: { get: () => d.description }, length: { get: () => d.length },
    item: { value: (i) => (i >= 0 && i < d.length) ? mimeTypeOf(d, d.mimes[i]) : null },
    namedItem: { value: (n) => d.mimes.includes(n) ? mimeTypeOf(d, n) : null },
  }));
  const pluginArray = Object.create(PluginArray.prototype);
  for (let i = 0; i < fakePlugins.length; i++) {
    Object.defineProperty(pluginArray, i, { get: () => fakePlugins[i], enumerable: true, configurable: true });
    Object.defineProperty(pluginArray, fakePlugins[i].name, { get: () => fakePlugins[i], enumerable: false, configurable: true });
  }
  Object.defineProperty(pluginArray, 'length', { get: () => fakePlugins.length, configurable: true });
  pluginArray.item = (i) => fakePlugins[i] || null;
  pluginArray.namedItem = (n) => fakePlugins.find(p => p.name === n) || null;
  pluginArray.refresh = () => {};
  Object.defineProperty(navigator, 'plugins', { get: () => pluginArray, configurable: true });
  const mimeArray = Object.create(MimeTypeArray.prototype);
  const flatMimes = [];
  fakePlugins.forEach(p => p.mimes.forEach(m => flatMimes.push(mimeTypeOf(p, m))));
  for (let i = 0; i < flatMimes.length; i++) {
    Object.defineProperty(mimeArray, i, { get: () => flatMimes[i], enumerable: true, configurable: true });
  }
  Object.defineProperty(mimeArray, 'length', { get: () => flatMimes.length, configurable: true });
  mimeArray.item = (i) => flatMimes[i] || null;
  mimeArray.namedItem = (n) => flatMimes.find(m => m.type === n) || null;
  Object.defineProperty(navigator, 'mimeTypes', { get: () => mimeArray, configurable: true });
  } catch (e) {}

  // === 3. languages（与 Accept-Language 请求头一致，非硬编码）===
  try {
    Object.defineProperty(navigator, 'languages', { get: () => langs, configurable: true });
  } catch (e) {}

  // === 4. window.chrome（headless 下可能只有空壳 {}，逐项补齐）===
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
    if (!window.chrome.loadTimes) window.chrome.loadTimes = () => ({ commitLoadTime: Date.now() / 1000 });
    if (!window.chrome.csi) window.chrome.csi = () => ({ onloadT: Date.now() });
    if (!window.chrome.app) {
      window.chrome.app = {
        isInstalled: false,
        InstallState: { INSTALLED: 'installed', NOT_INSTALLED: 'not_installed', DISABLED: 'disabled' },
        RunningState: { RUNNING: 'running', CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run' },
        getDetails: () => null, getIsInstalled: () => false, runningState: () => 'cannot_run',
      };
    }
    if (!window.chrome.webstore) window.chrome.webstore = {};
  } catch (e) {}

  // === 5. WebGL：vendor/renderer 成对固化，WebGL1 与 WebGL2 同时覆盖 ===
  try {
    const patchGL = (proto) => {
      if (!proto) return;
      const _getParameter = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return gpu1.vendor;           // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return gpu1.renderer;         // UNMASKED_RENDERER_WEBGL
        return _getParameter.call(this, p);
      };
    };
    if (window.WebGLRenderingContext) patchGL(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchGL(WebGL2RenderingContext.prototype);
  } catch (e) {}

  // === 6. permissions：被覆盖的 query 对外表现为 native code ===
  try {
    const _origQuery = navigator.permissions.query.bind(navigator.permissions);
    const _patchedQuery = function query(descriptor) {
      const name = (descriptor && descriptor.name) || '';
      let state = null;
      if (name === 'notifications') state = 'denied';
      else if (name === 'geolocation') state = 'prompt';
      else if (name === 'camera' || name === 'microphone') state = 'prompt';
      else if (name === 'clipboard-read' || name === 'clipboard-write') state = 'denied';
      if (state === null) return _origQuery(descriptor);
      const result = Object.create(PermissionStatus ? PermissionStatus.prototype : Object.prototype, {
        state: { get: () => state, enumerable: true },
        onchange: { get: () => null, set: () => {}, enumerable: true },
      });
      return Promise.resolve(result);
    };
    navigator.permissions.query = _patchedQuery;
  } catch (e) {}

  // === 7. 被覆盖函数的 toString 返回 native code ===
  try {
    const patched = new Set([navigator.permissions.query]);
    if (window.WebGLRenderingContext) patched.add(WebGLRenderingContext.prototype.getParameter);
    if (window.WebGL2RenderingContext) patched.add(WebGL2RenderingContext.prototype.getParameter);
    const _origToString = Function.prototype.toString;
    const toStringProxy = function toString() {
      if (patched.has(this)) return 'function ' + (this.name || 'anonymous') + '() { [native code] }';
      return _origToString.call(this);
    };
    Object.defineProperty(Function.prototype, 'toString', {
      value: toStringProxy, writable: true, configurable: true,
    });
    patched.add(toStringProxy);  // 自身也要过检
  } catch (e) {}
})();
`;
}

// 代理配置解析（支持 socks5 / http / https），来自环境变量 PROXY 或 PROXIES（逗号分隔轮换池）
function _parseProxy(raw) {
  if (!raw) return null;
  const s = String(raw).trim();
  const m = s.match(/^(\w+):\/\/(?:(.*?)@)?([^/]+)$/);
  if (!m) return null;
  const scheme = m[1];
  let server = '';
  let port = '';
  if (m[3].includes(':')) {
    const parts = m[3].split(':');
    server = parts[0];
    port = parts[1];
  } else {
    server = m[3];
  }
  let u = { scheme, server, port };
  if (m[2]) u = { ...u, ..._parseAuth(m[2]) };
  return u;
}
function _parseAuth(auth) {
  const i = auth.indexOf(':');
  if (i === -1) return { username: auth };
  return { username: auth.slice(0, i), password: auth.slice(i + 1) };
}

// === 请求拦截默认规则 ===
// 屏蔽对爬虫无意义的资源（字体/图标/广告/埋点），加速加载 + 减少被识别特征
const DEFAULT_BLOCK_PATTERNS = [
  /\.(woff2?|ttf|otf|eot)$/i,    // 字体
  /\.ico$/i,                      // 图标
  /(?:cdn|tracker|analytics|advert|banner|pixel|hotjar|mixpanel|segment)/i,  // 埋点/广告
  /googletagservices|doubleclick|adsbygoogle/i,  // Google 广告
];

class BrowserManager {
  constructor() {
    this.browser = null;
    this.context = null;
    this.page = null;
    this.stealthMode = STEALTH_ENABLED ? 'manual' : 'off';
    // 进程生命周期内固化的指纹（B16：切域名不重抽）
    this._fingerprint = null;
    // 登录态防抖落盘（B25）
    this._saveTimer = null;
    this._saving = false;
    // 导航互斥（B24）：同一时刻只允许一个导航/关context型操作
    this._navLock = Promise.resolve();
    this._navLockOwner = 0;

    // === 按域名分文件存储登录态 ===
    const legacyPath = process.env.STORAGE_STATE_PATH || '';
    this.storageDir = process.env.STORAGE_STATE_DIR ||
      (legacyPath ? path.dirname(legacyPath) : path.join('artifacts', 'browser', 'storage'));
    fs.mkdirSync(this.storageDir, { recursive: true });

    // 当前 context 绑定的域名
    this._currentHost = '';

    // === Proxy 轮换池 ===
    const proxyRaw = process.env.PROXIES || process.env.PROXY || '';
    this._proxyPool = proxyRaw
      .split(/[,;\n]/)
      .map((s) => _parseProxy(s.trim()))
      .filter(Boolean);
    this._proxyIndex = 0;
    this._perHostProxy = new Map();  // hostname → proxy（同一域名固定用同一代理，减少指纹突变）

    // === 请求拦截规则 ===
    this._blockPatterns = [...DEFAULT_BLOCK_PATTERNS];
    this._routeMockers = [];  // [{ matcher: RegExp|string, status, headers, body }]
    this._routeInstalled = false;
  }

  /** 当前应该用的代理：同一 hostname 固定一个代理，跨 hostname 轮换 */
  _currentProxy(hostname) {
    if (this._proxyPool.length === 0) return null;
    const normalized = BrowserManager._normalizeHost(hostname);
    if (!normalized) {
      // 无 hostname 时（首次创建 context），用第一个
      return this._proxyPool[0] || null;
    }
    if (!this._perHostProxy.has(normalized)) {
      // 新域名 → 轮换下一个代理
      const p = this._proxyPool[this._proxyIndex % this._proxyPool.length];
      this._proxyIndex++;
      this._perHostProxy.set(normalized, p);
      console.log(`[browser] 域名 ${normalized} 绑定代理 ${p.scheme}://${p.server}:${p.port}`);
    }
    return this._perHostProxy.get(normalized);
  }

  get headless() {
    return String(process.env.HEADLESS || 'false').toLowerCase() === 'true';
  }

  /** 把任意 hostname 归一化成主域名（eTLD+1），跨子域名共享 */
  static _normalizeHost(hostname) {
    const h = String(hostname || '').trim().replace(/:\d+$/, '');
    if (!h) return '';
    const parts = h.split('.').filter(Boolean);
    if (parts.length <= 2) return parts.join('.');
    if (parts.every((p) => /^\d+$/.test(p))) return h;
    const last = parts[parts.length - 1].toLowerCase();
    const second = parts[parts.length - 2].toLowerCase();
    const ccTLDS = new Set(['uk', 'jp', 'br', 'au', 'ru', 'de', 'fr', 'it', 'es', 'nz', 'ca', 'us', 'in', 'mx', 'sg', 'hk', 'tw', 'cn']);
    const gTLDS = new Set(['com', 'net', 'org', 'gov', 'edu', 'co', 'me', 'ne', 'or', 'gen', 'sch']);
    if (ccTLDS.has(last) && gTLDS.has(second)) {
      return parts.slice(-3).join('.');
    }
    return parts.slice(-2).join('.');
  }

  /** 把 hostname 归一化成安全文件名（基于主域名） */
  _hostFile(hostname) {
    const key = BrowserManager._normalizeHost(hostname)
      .replace(/[^a-zA-Z0-9.-]/g, '_');
    if (!key) return path.join(this.storageDir, '_default.json');
    return path.join(this.storageDir, `${key}.json`);
  }

  /** 列出所有已保存的登录态文件 */
  listStates() {
    if (!fs.existsSync(this.storageDir)) return [];
    return fs.readdirSync(this.storageDir)
      .filter(f => f.endsWith('.json'))
      .map(f => {
        const full = path.join(this.storageDir, f);
        const stat = fs.statSync(full);
        return { file: f, host: f.replace(/\.json$/, ''), size: stat.size, mtime: stat.mtimeMs };
      })
      .sort((a, b) => b.mtime - a.mtime);
  }

  // ===== 导航互斥（B24）：把导航/切context操作串行化，避免并发任务互踢 context =====
  async withNavLock(fn) {
    const ticket = ++this._navLockOwner;
    let release;
    const prev = this._navLock;
    this._navLock = new Promise(r => { release = r; });
    await prev;                       // 等前一个持有者完成
    try {
      return await fn();
    } finally {
      if (ticket === this._navLockOwner) release();  // 只由最新调用者释放（旧请求超时放弃也不影响新请求）
      else release();                    // 旧票据放弃时也放行，避免死锁
    }
  }

  async ensure() {
    if (this.browser) return;

    this._fingerprint = makeFingerprint();
    const fp = this._fingerprint;
    const viewport = fp.viewport;

    const launchOpts = {
      headless: this.headless,
      args: [
        '--disable-blink-features=AutomationControlled',
        // 注意：不再禁用 site-per-process/IsolateOrigins —— 真实 Chrome 默认开启，
        // 禁用反而制造特征（旧代码为绕 iframe 限制加的，addInitScript 已覆盖 iframe）
        '--disable-dev-shm-usage',
        '--no-first-run',
        '--no-default-browser-check',
        `--lang=${LOCALE}`,
        '--start-maximized',
      ],
    };
    this.browser = await chromium.launch(launchOpts);
    console.log(`[browser] launched mode=${this.stealthMode} headless=${this.headless} viewport=${viewport.width}x${viewport.height}`);

    // headless 时用临时 context 探测真实 UA（Browser 无 userAgent() API），
    // 只把 HeadlessChrome 换成 Chrome，其余保持与内核一致（B06/B07）
    if (this.headless) {
      try {
        const probe = await this.browser.newContext();
        const ppage = await probe.newPage();
        const realUa = await ppage.evaluate(() => navigator.userAgent);
        fp.uaOverride = String(realUa).replace(/HeadlessChrome/g, 'Chrome');
        await probe.close();
      } catch (e) {
        console.warn('[browser] UA 探测失败，headless 将不覆写 UA:', e.message);
      }
    }

    // 启动时创建"干净"初始 context（无站点 storage）；首次 navigate 再按域名重建
    this.context = await this._createContext(fp, '');
    this.page = await this.context.newPage();
    this._attachContextAutoSave(this.context);
    this._currentHost = '';
  }

  async _createContext(fp, storageFile = '', hostname = '') {
    // ⚠️ 不覆写 userAgent（B06）：headed 模式 UA 与内核一致，sec-ch-ua/userAgentData 天然自洽。
    // headless 模式覆写 UA 只为过服务端 UA 规则（brands 引擎级无法伪装，见文件头注释）。
    const headless = this.headless;
    // fp.uaOverride 已在 ensure() 里探测（headless 才有值）
    const uaOverride = headless ? fp.uaOverride : undefined;
    const contextOpts = {
      viewport: fp.viewport,
      locale: LOCALE,
      timezoneId: TIMEZONE,
      colorScheme: 'light',
      acceptDownloads: true,
      deviceScaleFactor: fp.dpr,
      extraHTTPHeaders: {
        'Accept-Language': fp.lang,
      },
    };
    if (uaOverride) contextOpts.userAgent = uaOverride;

    // === Proxy 轮换池：同一 hostname 固定一个代理，跨 hostname 轮换 ===
    const proxy = this._currentProxy(hostname);
    if (proxy) {
      const { scheme, server, port, username, password } = proxy;
      contextOpts.proxy = { server: `${scheme}://${server}:${port}` };
      if (username) contextOpts.proxy.username = username;
      if (password) contextOpts.proxy.password = password;
      console.log(`[browser] context 使用代理 ${scheme}://${server}:${port}`);
    }

    if (storageFile) {
      if (fs.existsSync(storageFile)) {
        console.log(`[browser] 加载 storage: ${storageFile}`);
        contextOpts.storageState = storageFile;
      } else {
        console.log(`[browser] 无已保存的登录态: ${storageFile}（首次访问该域名）`);
      }
    }
    let newCtx;
    try {
      newCtx = await this.browser.newContext(contextOpts);
    } catch (e) {
      // storageState 文件损坏/中途被删 → 去掉登录态重建，避免该域名永久 500
      console.warn(`[browser] 创建 context 失败（可能 storageState 损坏），无状态重建: ${e.message}`);
      delete contextOpts.storageState;
      newCtx = await this.browser.newContext(contextOpts);
    }
    if (this.stealthMode === 'manual') {
      // 有头/无头都注入：webdriver/plugins/chrome/WebGL/permissions/toString
      // （这些表面 JS 层可控；userAgentData/硬件数等引擎级表面不伪装，见文件头注释）
      await newCtx.addInitScript(manualStealthInitScript(fp));
    }

    // === page.route 请求拦截 ===
    newCtx.on('page', (page) => this._installRouteHandlers(page));
    // 已存在的 page 也装
    for (const p of newCtx.pages()) this._installRouteHandlers(p);

    return newCtx;
  }

  /** 在 page 上安装 route handlers：屏蔽无意义资源 + 应用自定义 mock */
  _installRouteHandlers(page) {
    // 只装一次
    if (page._routesInstalled) return;
    page._routesInstalled = true;

    // 默认：屏蔽无意义资源（加速 + 减少被识别特征）
    page.route('**/*', async (route) => {
      const url = route.request().url();
      for (const pat of this._blockPatterns) {
        if (pat.test(url)) {
          // 204 No Content：告诉浏览器"请求成功但无数据"，不浪费带宽
          return route.fulfill({ status: 204 });
        }
      }
      route.continue();
    });

    // 自定义 mock（优先级高于 block，因为 mock 是用户显式指定的）
    for (const mock of this._routeMockers) {
      const matcher = typeof mock.matcher === 'string'
        ? new RegExp(mock.matcher)
        : mock.matcher;
      if (matcher instanceof RegExp) {
        page.route(matcher, async (route) => {
          const opts = { status: mock.status || 200 };
          if (mock.headers) opts.headers = mock.headers;
          if (mock.body) opts.body = typeof mock.body === 'string' ? mock.body : JSON.stringify(mock.body);
          if (mock.contentType) opts.contentType = mock.contentType;
          await route.fulfill(opts);
        });
      }
    }
  }

  /** context 级自动保存绑定：所有 page（含新开标签）framenavigated 都触发防抖保存（B25） */
  _attachContextAutoSave(context) {
    const onNav = () => this._scheduleSave();
    context.on('page', (page) => {
      // 新标签页也绑定导航保存
      page.on('framenavigated', onNav);
      page.on('close', () => {
        try { page.removeListener('framenavigated', onNav); } catch (e) {}
      });
    });
    // 已存在的 page 也要绑
    for (const p of context.pages()) p.on('framenavigated', onNav);
  }

  /** 防抖落盘：2s 内多次导航只写一次盘（B25） */
  _scheduleSave() {
    if (this._saveTimer) clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(async () => {
      this._saveTimer = null;
      if (this._saving) return;    // 上一次还在写，跳过本轮（下轮导航再存）
      this._saving = true;
      try { await this._saveCurrentHost(); } catch (e) {}
      this._saving = false;
    }, 2000);
    this._saveTimer.unref?.();
  }

  /** 切换到指定 hostname 的登录态 context（主域名变了才重建，子域名复用） */
  async switchContextForHost(hostname) {
    if (!this.browser) await this.ensure();
    const normalized = BrowserManager._normalizeHost(hostname);
    if (normalized === this._currentHost && this.context) {
      return this.page;
    }

    // 导航互斥：切域名会关旧 context，必须串行（B24）
    return this.withNavLock(async () => {
      // 双检：等锁期间可能已被同域名请求切换完成
      if (normalized === this._currentHost && this.context) return this.page;

      // 切域名前，先把旧域名的登录态立即落盘（不走防抖）
      if (this._currentHost && this.context) {
        await this._saveCurrentHost().catch(() => {});
      }

      if (this.context) {
        try { await this.context.close(); } catch (e) {}
        this.context = null;
        this.page = null;
      }

      const fp = this._fingerprint || makeFingerprint();
      const storageFile = this._hostFile(normalized);
      this.context = await this._createContext(fp, storageFile, normalized);
      this.page = await this.context.newPage();
      this._attachContextAutoSave(this.context);
      this._currentHost = normalized;

      console.log(`[browser] 切换到域名 ${normalized}（storage=${path.basename(storageFile)}，指纹复用）`);
      return this.page;
    });
  }

  async _saveCurrentHost() {
    if (!this.context || !this._currentHost) return;
    const file = this._hostFile(this._currentHost);
    try {
      await this.context.storageState({ path: file });
    } catch (e) {
      /* 忽略保存失败 */
    }
  }

  /** 手动强制保存（外部调用，比如用户手动登录完后） */
  async forceSave() {
    // 取消 pending 的防抖，立即写
    if (this._saveTimer) { clearTimeout(this._saveTimer); this._saveTimer = null; }
    await this._saveCurrentHost();
    return { host: this._currentHost, file: this._hostFile(this._currentHost) };
  }

  async getPage() {
    await this.ensure();
    return this.page;
  }

  // ============ 多标签页管理 ============

  /** 列出所有标签页 */
  listTabs() {
    if (!this.context) return [];
    return this.context.pages().map((p, i) => ({
      index: i,
      url: p.url(),
      title: p.title(),
      is_active: p === this.page,
    }));
  }

  /** 新开标签页（可选立即导航） */
  async openTab(url = '') {
    if (!this.context) await this.ensure();
    const page = await this.context.newPage();
    if (url) {
      try { await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 }); } catch (e) {}
    }
    await page.bringToFront();
    this.page = page;
    return {
      index: this.context.pages().length - 1,
      url: page.url(),
      title: page.title(),
    };
  }

  /** 切到指定标签页（按 index 或 url/title 关键字） */
  async switchTab(target) {
    if (!this.context) throw new Error('context 未初始化');
    const pages = this.context.pages();
    let page = null;

    if (typeof target === 'number') {
      if (target < 0 || target >= pages.length) throw new Error(`标签页 index 越界: ${target}/${pages.length}`);
      page = pages[target];
    } else if (typeof target === 'string') {
      page = pages.find(p => p.url().includes(target) || p.title().includes(target));
      if (!page) throw new Error(`未找到匹配标签页: ${target}`);
    } else {
      throw new Error('target 必须是 number(index) 或 string(url/title 关键字)');
    }

    await page.bringToFront();
    this.page = page;
    return { url: page.url(), title: page.title() };
  }

  /** 关闭指定标签页 */
  async closeTab(target = 'current') {
    if (!this.context) throw new Error('context 未初始化');
    const pages = this.context.pages();
    let page = null;

    if (target === 'current') {
      page = this.page;
    } else if (typeof target === 'number') {
      if (target < 0 || target >= pages.length) throw new Error(`标签页 index 越界`);
      page = pages[target];
    }

    if (!page) throw new Error('没有可关闭的标签页');
    if (pages.length <= 1) {
      throw new Error('只剩一个标签页，无法关闭（请用 close 关闭 context）');
    }

    const idx = pages.indexOf(page);
    await page.close();

    const remaining = this.context.pages();
    const nextIdx = Math.min(idx, remaining.length - 1);
    this.page = remaining[nextIdx];
    if (this.page) await this.page.bringToFront();

    return { closed_url: page.url(), now_active: this.page ? this.page.url() : null };
  }

  // ============ 鼠标悬停 ============

  async hoverElement(selector, text = '') {
    const page = await this.getPage();
    let el;
    if (selector) {
      el = page.locator(selector).first();
    } else if (text) {
      el = page.getByText(text, { exact: false }).first();
    } else {
      throw new Error('selector 和 text 至少传一个');
    }
    await el.waitFor({ state: 'visible', timeout: 10000 });
    await el.hover({ timeout: 10000 });
    return { ok: true, hovered: selector || text };
  }

  // ============ iframe 切换 ============

  /** 切进 iframe。target 可以是 CSS 选择器、iframe name、或 frame url 关键字；不传则切回主 frame */
  async switchFrame(target = null) {
    const page = await this.getPage();
    if (!target) {
      this._frame = null;
      return { ok: true, frame: 'main' };
    }
    let frame = null;
    // 1. 按 CSS 选择器找 iframe 元素
    try {
      const iframe = page.locator(target).first();
      if (await iframe.count() > 0) {
        frame = page.frame({ element: iframe });
      }
    } catch (e) {}
    // 2. 按 name 或 url 找（Playwright 原生支持）
    if (!frame) {
      frame = page.frame(target);
    }
    // 3. 遍历所有 frame 按 url 关键字匹配
    if (!frame) {
      const frames = page.frames();
      frame = frames.find(f => f.url().includes(target));
    }
    if (!frame) throw new Error(`未找到 iframe: ${target}`);
    await frame.waitForLoadState('domcontentloaded', { timeout: 10000 });
    this._frame = frame;
    return { ok: true, frame: frame.url() || frame.name() || target };
  }

  /** 切回主 frame */
  async switchBackFromFrame() {
    this._frame = null;
    return { ok: true, frame: 'main' };
  }

  /** 获取当前活跃 frame（供其他方法内部调用） */
  _activeFrame() {
    return this._frame || this.page;
  }

  // ============ 文件下载 ============

  /**
   * 触发下载并保存到本地。
   * trigger: {selector, text, url} 三者选一——selector/text 触发点击，url 直接下载
   * filename: 可选，指定保存文件名；不传则用浏览器建议名
   * timeout: 下载超时（毫秒）
   */
  async downloadFile({ trigger = {}, filename = '', timeout = 60000 } = {}) {
    const page = await this.getPage();
    const saveDir = path.resolve(process.env.DOWNLOAD_DIR || path.join(__dirname, '..', 'artifacts', 'downloads'));
    if (!fs.existsSync(saveDir)) fs.mkdirSync(saveDir, { recursive: true });

    const { selector, text, url } = trigger;

    if (url) {
      // 直接下载 URL（用 context.request）
      const resp = await this.context.request.get(url, { timeout });
      if (!resp.ok()) throw new Error(`下载失败 HTTP ${resp.status()}`);
      const suggested = filename || (() => {
        try { return new URL(url).pathname.split('/').pop() || 'download'; } catch { return 'download'; }
      })();
      const savePath = path.join(saveDir, suggested);
      await resp.saveAs(savePath);
      const stat = fs.statSync(savePath);
      return { ok: true, path: savePath, filename: path.basename(savePath), size: stat.size };
    }

    // 通过点击触发下载（监听 download 事件）
    let downloadPromise;
    let triggered = false;

    if (selector) {
      const el = page.locator(selector).first();
      downloadPromise = page.waitForEvent('download', { timeout });
      await el.click({ timeout: 10000 });
      triggered = true;
    } else if (text) {
      const el = page.getByText(text, { exact: false }).first();
      downloadPromise = page.waitForEvent('download', { timeout });
      await el.click({ timeout: 10000 });
      triggered = true;
    }

    if (!triggered) throw new Error('必须提供 selector、text 或 url');

    const download = await downloadPromise;
    let savePath;
    if (filename) {
      savePath = path.join(saveDir, filename);
    } else {
      savePath = path.join(saveDir, download.suggestedFilename());
    }
    await download.saveAs(savePath);
    const stat = fs.statSync(savePath);
    return { ok: true, path: savePath, filename: path.basename(savePath), size: stat.size };
  }

  async close() {
    // 关闭前把 pending 的防抖保存立即落盘
    if (this._saveTimer) { clearTimeout(this._saveTimer); this._saveTimer = null; }
    if (this.browser) {
      await this._saveCurrentHost();
      await this.browser.close().catch(() => {});
      this.browser = null;
      this.context = null;
      this.page = null;
      this._currentHost = '';
    }
  }

  // ============ 请求拦截控制 ============

  /** 替换整个屏蔽规则列表 */
  setBlockPatterns(patterns = []) {
    this._blockPatterns = patterns.map((p) => (p instanceof RegExp ? p : new RegExp(p, 'i')));
    return { count: this._blockPatterns.length };
  }

  /** 追加一条屏蔽规则 */
  addBlockPattern(pattern) {
    const re = pattern instanceof RegExp ? pattern : new RegExp(pattern, 'i');
    this._blockPatterns.push(re);
    return { count: this._blockPatterns.length };
  }

  /** 清空所有屏蔽规则（只留默认） */
  resetBlockPatterns() {
    this._blockPatterns = [...DEFAULT_BLOCK_PATTERNS];
    return { count: this._blockPatterns.length };
  }

  /** 添加一个请求 mock（匹配 URL 返回自定义内容） */
  addRouteMock({ matcher, status = 200, headers = {}, body = '', contentType = '' } = {}) {
    if (!matcher) throw new Error('matcher 必须提供（RegExp 或字符串正则）');
    this._routeMockers.push({ matcher, status, headers, body, contentType });
    // 对已存在的 page 重新装 handlers（覆盖旧 route）
    if (this.context) {
      for (const p of this.context.pages()) this._installRouteHandlers(p);
    }
    return { mock_count: this._routeMockers.length };
  }

  /** 清空所有自定义 mock */
  clearRouteMocks() {
    this._routeMockers = [];
    return { mock_count: 0 };
  }

  /** 查看当前代理池和已绑定 */
  getProxyStatus() {
    return {
      pool_size: this._proxyPool.length,
      pool: this._proxyPool.map((p) => `${p.scheme}://${p.server}:${p.port}`),
      per_host: Object.fromEntries(
        [...this._perHostProxy.entries()].map(([h, p]) => [h, `${p.scheme}://${p.server}:${p.port}`])
      ),
      current_host: this._currentHost,
    };
  }

  /** 强制轮换下一个代理（当前域名也换） */
  rotateProxy() {
    if (this._proxyPool.length === 0) return { ok: false, msg: '未配置代理池' };
    // 清空 perHost 绑定，让下次 switchContext 重新分配
    this._perHostProxy.clear();
    this._proxyIndex = (this._proxyIndex + 1) % this._proxyPool.length;
    // 如果有活跃 context，标记需要重建
    if (this._currentHost) {
      const next = this._proxyPool[this._proxyIndex];
      console.log(`[browser] 强制轮换代理 → ${next.scheme}://${next.server}:${next.port}（切域名后生效）`);
    }
    return { ok: true, next: this._currentProxy(this._currentHost) };
  }
}

module.exports = new BrowserManager();
