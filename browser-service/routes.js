// routes.js — 原子操作路由（供 Python 后端通过 HTTP 调用）
//
// 拟人化设计（对应 docs/BUG_ANALYSIS_AND_FIXES.md B17-B21/B27）：
// - 鼠标有"记忆"：起点永远是上一次停留的位置，不再瞬移（B17）
// - 点击全程一套驱动：人手轨迹滑到目标点后，用 position 精修点击，
//   不再出现"移到 A 点再跳到中心点点击"的两段式破绽（B18）
// - 滚动用 page.mouse.wheel，产生真实 wheel 事件流（B19）
// - 中文/全角输入走 page.keyboard.insertText（CDP Input.insertText，受信、无 mojibake，B20）
// - 延迟服从对数正态（重尾），偶发"走神"长停顿（B21）
const express = require('express');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const browserManager = require('./browser');

const router = express.Router();
router.use(express.json({ limit: '5mb' }));

// ============ 延迟采样：对数正态（重尾），真人反应时间分布（B21） ============
function _randn() {
  let u = 0, v = 0;
  while (!u) u = Math.random();
  while (!v) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
function sampleDelay(min, max) {
  min = Math.max(1, min); max = Math.max(min + 1, max);
  const med = Math.sqrt(min * max);                       // 几何均值作中位数
  const sigma = Math.max(0.25, Math.log(max / med) / 1.645); // 95% 分位 ≈ max
  const v = med * Math.exp(_randn() * sigma);
  return Math.round(Math.min(max, Math.max(min, v)));
}
const humanDelay = (min = 150, max = 600) =>
  new Promise((r) => setTimeout(r, sampleDelay(min, max)));

// 偶发"走神"：1.5% 概率长停顿 2~6 秒（喝水/被消息打断）
async function idlePause() {
  if (Math.random() < 0.015) {
    await humanDelay(2000, 6000);
  }
}

// === 域名级 QPS 限速 ===
// 同一域名连续请求间隔不低于 1500ms（可配置），避免触发反爬
const _lastDomainAccess = new Map();  // hostname -> timestamp
const MIN_DOMAIN_INTERVAL = parseInt(process.env.MIN_DOMAIN_INTERVAL || '1500', 10);

function _pruneDomainMap() {
  // B27：防止 Map 无限增长（长期运行内存泄漏）
  if (_lastDomainAccess.size > 500) {
    const entries = [..._lastDomainAccess.entries()].sort((a, b) => a[1] - b[1]);
    for (const [host] of entries.slice(0, entries.length - 250)) {
      _lastDomainAccess.delete(host);
    }
  }
}

async function respectDomainRateLimit(page) {
  if (!page) return;
  let host = '';
  try {
    const url = page.url();
    host = new URL(url).hostname;
  } catch { return; }
  if (!host) return;
  _pruneDomainMap();
  const now = Date.now();
  const last = _lastDomainAccess.get(host) || 0;
  const elapsed = now - last;
  if (elapsed < MIN_DOMAIN_INTERVAL) {
    const wait = MIN_DOMAIN_INTERVAL - elapsed + Math.random() * 300;
    await humanDelay(wait, wait + 200);
  }
  _lastDomainAccess.set(host, Date.now());
}

// ============ 鼠标轨迹：带"记忆"的贝塞尔弧线（B17） ============
// 起点 = 上一次停留位置（首次为视口中心附近），终点 = 目标点。
// 长距离移动 10% 概率轻微过冲再回调（真人瞄准惯性）。
const _lastMouse = { x: null, y: null };

function _viewportOf(page) {
  const vp = page.viewportSize();
  return vp || { width: 1366, height: 768 };
}

function _ensureLastMouse(page) {
  if (_lastMouse.x === null || _lastMouse.y === null) {
    const vp = _viewportOf(page);
    _lastMouse.x = vp.width * (0.35 + Math.random() * 0.3);
    _lastMouse.y = vp.height * (0.35 + Math.random() * 0.3);
  }
}

async function humanMouseMove(page, targetX, targetY) {
  try {
    _ensureLastMouse(page);
    const startX = _lastMouse.x;
    const startY = _lastMouse.y;
    const dist = Math.hypot(targetX - startX, targetY - startY);
    // 步数随距离缩放：近处 6~10 步，远处最多 30 步
    const steps = Math.max(6, Math.min(30, Math.round(dist / 35) + Math.floor(Math.random() * 4)));

    // 贝塞尔控制点（制造弧线）
    const ctrlOffsetX = (Math.random() - 0.5) * Math.min(200, dist * 0.3);
    const ctrlOffsetY = (Math.random() - 0.5) * Math.min(200, dist * 0.3);
    const ctrlX = (startX + targetX) / 2 + ctrlOffsetX;
    const ctrlY = (startY + targetY) / 2 + ctrlOffsetY;

    // 过冲：长距离 10% 概率冲过目标 3~10px 再回来
    let finalX = targetX, finalY = targetY;
    let overshoot = 0;
    if (dist > 250 && Math.random() < 0.1) {
      overshoot = 3 + Math.random() * 7;
      const ang = Math.atan2(targetY - startY, targetX - startX);
      finalX = targetX + Math.cos(ang) * overshoot;
      finalY = targetY + Math.sin(ang) * overshoot;
    }

    await page.mouse.move(startX, startY);

    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const ex = t * t * (3 - 2 * t);   // smoothstep 缓动：起步慢-中段快-收尾慢
      const cx = (1 - ex) * (1 - ex) * startX + 2 * (1 - ex) * ex * ctrlX + ex * ex * finalX;
      const cy = (1 - ex) * (1 - ex) * startY + 2 * (1 - ex) * ex * ctrlY + ex * ex * finalY;
      // 微抖动（人手不可能完全精确），末步不抖
      const jitter = (i === steps) ? 0 : (Math.random() - 0.5) * 1.5;
      await page.mouse.move(cx + jitter, cy + jitter);
      // 步间停顿 5-20ms，指数分布感（快段更快）
      await humanDelay(5, 20);
    }
    // 过冲回调
    if (overshoot) {
      await humanDelay(30, 80);
      await page.mouse.move(targetX + (Math.random() - 0.5), targetY + (Math.random() - 0.5));
    }
    _lastMouse.x = targetX;
    _lastMouse.y = targetY;
  } catch (e) {
    /* 鼠标轨迹失败不应阻断操作 */
  }
}

// 找到元素盒子与中心坐标（供 humanMouseMove / position 点击使用）
async function getElementCenter(page, selector, text) {
  try {
    let box = null;
    if (selector) {
      const loc = page.locator(selector).first();
      box = await loc.boundingBox();
    } else if (text) {
      const loc = page.getByText(text, { exact: false }).first();
      box = await loc.boundingBox();
    }
    if (box) {
      return { x: box.x + box.width / 2, y: box.y + box.height / 2, box };
    }
  } catch (e) { /* ignore */ }
  return null;
}

function cleanText(text, maxLen) {
  let t = String(text || '');
  t = t.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  if (maxLen && t.length > maxLen) t = t.slice(0, maxLen);
  return t;
}

// 页面挑战检测：登录墙/验证码/5秒盾/滑块/反爬
async function detectChallenges(page) {
  let url = '';
  let title = '';
  let bodyText = '';
  let mainContentLen = 0;  // 页面主要业务区域的文本长度
  let statusCode = null;
  try {
    url = page.url();
    title = await page.title().catch(() => '');
    bodyText = await page.evaluate(() => (document.body ? document.body.innerText : '')).catch(() => '');
    // 检查主要业务区域（main / article / [role=main] / 列表容器）有没有实际文本
    mainContentLen = await page.evaluate(() => {
      const selectors = [
        'main', 'article', '[role="main"]', '[class*="list"]', '[class*="grid"]',
        '[class*="feed"]', '[class*="content"]', '[class*="result"]', '[class*="card"]',
        '[class*="item"]', '[class*="post"]', '[class*="video"]', '[class*="product"]',
      ];
      let maxLen = 0;
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.children.length >= 2) {
          const t = (el.innerText || '').trim();
          if (t.length > maxLen) maxLen = t.length;
        }
      }
      return maxLen;
    }).catch(() => 0);
  } catch (e) {
    /* ignore */
  }
  bodyText = cleanText(bodyText, 6000).toLowerCase();

  const detect = {
    login: false,
    captcha: false,
    five_sec_shield: false,
    slider: false,
    anti_bot: false,
  };
  const inIframe = async (srcRe) => {
    try {
      const frames = page.frames();
      for (const f of frames) {
        if (srcRe.test(f.url())) return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  };

  // 登录墙检测：先排除"页面已有实际业务内容"的情况
  const hasLoginKeyword = /登录|登入|log in|sign in|请先登录/.test(bodyText);
  const urlLoginish = /\/login|\/signin|\/auth|\/signup/.test(url);
  const bodyShort = bodyText.length < 2000;
  const mainAreaEmpty = mainContentLen < 100;
  if (urlLoginish && bodyShort && mainAreaEmpty) detect.login = true;
  if (hasLoginKeyword && mainAreaEmpty && bodyShort) detect.login = true;

  if (/(captcha|recaptcha|geetest|验证码|人机验证|安全验证)/.test(bodyText)) detect.captcha = true;
  if (await inIframe(/(captcha|recaptcha|geetest|hcaptcha|turnstile)/)) detect.captcha = true;

  if (/(just a moment|checking your browser|正在检查您的浏览器|验证浏览器|五秒盾|5秒盾|cf-challenge|__cf_chl|enable javascript and cookies|稍候|challenge)/.test(bodyText)) {
    detect.five_sec_shield = true;
  }
  if (await inIframe(/(challenge-platform|cf-chl|cloudflare)/)) detect.five_sec_shield = true;

  if (/(滑块|滑动验证|拖动滑块|slide to|geetest_slider|请按住滑块|完成拼图)/.test(bodyText)) detect.slider = true;
  if (await inIframe(/geetest/)) detect.slider = true;

  if (/(blocked|access denied|访问被拒绝|请求被拒绝|访问过于频繁|禁止访问|您的请求已被拦截)/.test(bodyText)) detect.anti_bot = true;

  return { url, title, status_code: statusCode, detect, body_preview: bodyText.slice(0, 500) };
}

// ============ 打字：ASCII 键盘敲击 + CJK 走受信 insertText（B20） ============
// 注：实测 Chromium 153 的 CDP Input.imeSetComposition 会把 CJK 文本错误编码成 U+FFFD，
// 不可用；且页面派发（dispatchEvent）的 composition 事件 isTrusted=false 反而是检测特征。
// 折中方案：CJK 用 keyboard.insertText（CDP Input.insertText，受信、无 mojibake、
// 正是浏览器为 IME 提供的插入通道）；ASCII 仍走逐键敲击 + 手误模拟。
async function typeText(page, value) {
  const text = String(value || '');
  const isCjkChar = (ch) => /[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]/.test(ch);

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];

    if (isCjkChar(ch)) {
      // 中文/全角字符：受信 insertText（与真人 IME 上屏等效），无 keydown 噪声
      try {
        await page.keyboard.insertText(ch);
      } catch (e) {
        await page.keyboard.type(ch);
      }
      // 中文字之间停顿更久（挑词节奏）
      await humanDelay(60, 160);
      continue;
    }

    // ASCII：3% 概率打错再删（模拟真人手误）
    if (Math.random() < 0.03 && i < text.length - 1) {
      const wrongKey = String.fromCharCode(97 + Math.floor(Math.random() * 26));
      await page.keyboard.type(wrongKey);
      await humanDelay(50, 120);
      await page.keyboard.press('Backspace');
      await humanDelay(50, 100);
    }
    await page.keyboard.type(ch);
    // 随机停顿：英文/数字 20-60ms，偶尔长停顿（模拟思考）
    if (Math.random() < 0.05) {
      await humanDelay(300, 700);
    } else {
      await humanDelay(20, 60);
    }
  }
}

router.get('/health', (req, res) => {
  res.json({ ok: true, pid: process.pid, headless: browserManager.headless });
});

// 打开页面：可等待 5 秒盾自动完成后再判定
router.post('/navigate', async (req, res) => {
  try {
    const { url, wait_until = 'domcontentloaded', timeout = 30000, wait_shield = true } = req.body || {};
    if (!url) return res.status(400).json({ error: '缺少 url' });

    // === 域名级 context 切换 ===
    let hostname = '';
    try { hostname = new URL(url).hostname; } catch (e) {}
    const page = await browserManager.switchContextForHost(hostname);
    await respectDomainRateLimit(page);

    let statusCode = null;
    let gotoError = '';
    try {
      const resp = await page.goto(url, { waitUntil: wait_until, timeout });
      statusCode = resp ? resp.status() : null;
    } catch (e) {
      // 超时/网络错误：页面可能仍部分加载；把错误带回，Python 层守卫决定是否重试（B27）
      gotoError = String(e.message || e);
    }

    // === 等 CSS + 首次渲染完成 ===
    try {
      await page.waitForFunction(() => {
        if (!document.body || document.body.offsetHeight === 0) return false;
        const hasContent = document.body.children.length > 3;
        const bodyText = (document.body.innerText || '').trim();
        const hasText = bodyText.length > 10;
        const firstEl = document.body.querySelector('*');
        const hasStyle = firstEl && window.getComputedStyle(firstEl).color !== 'rgba(0, 0, 0, 0)';
        return hasContent || hasText || hasStyle;
      }, { timeout: 5000 });
    } catch {
      // 5 秒没等到 → 继续（纯 HTML 页面或网络慢）
    }
    // 再等一会儿让延迟样式/字体加载，避免"先裸后美"
    await humanDelay(500, 800);

    if (wait_shield) {
      // Cloudflare 5 秒盾通常 5-15 秒自动通过，最久等 30 秒
      for (let i = 0; i < 20; i++) {
        await humanDelay(800, 1500);
        const st = await detectChallenges(page);
        const d = st.detect;
        if (!d.five_sec_shield) break;
        if (d.anti_bot || d.slider || d.login || d.captcha) break;
      }
    } else {
      await humanDelay(600, 1200);
    }
    const st = await detectChallenges(page);
    st.status_code = statusCode;
    if (gotoError) st.navigation_error = gotoError;
    res.json(st);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 页面状态（含挑战检测）
router.post('/page-state', async (req, res) => {
  try {
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    const st = await detectChallenges(page);
    res.json(st);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 可见文本快照
router.post('/snapshot', async (req, res) => {
  try {
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await humanDelay(100, 300);
    const maxLen = (req.body || {}).max_text_len || 6000;
    const text = await page.evaluate(() => (document.body ? document.body.innerText : '')).catch(() => '');
    res.json({ text: cleanText(text, maxLen) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 简化 HTML（去脚本/样式/iframe）
router.post('/html', async (req, res) => {
  try {
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await humanDelay(100, 300);
    const maxLen = (req.body || {}).max_len || 20000;
    const html = await page.evaluate(() => {
      const clone = document.body ? document.body.cloneNode(true) : document.documentElement.cloneNode(true);
      clone.querySelectorAll('script,style,noscript,svg,canvas,iframe').forEach(e => e.remove());
      return clone.outerHTML;
    }).catch(() => '');
    res.json({ html: cleanText(html, maxLen) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 滚动（拟人节奏：wheel 事件流 + 快慢交替 + 偶尔回弹）（B19）
router.post('/scroll', async (req, res) => {
  try {
    const { direction = 'down', amount = 1200, max_scrolls = 10 } = req.body || {};
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await humanDelay(200, 500);
    let reachedBottom = false;

    // 鼠标移到视口中央（wheel 事件落在鼠标下的元素上，中央最接近真人习惯）
    const vp = _viewportOf(page);
    _ensureLastMouse(page);
    const cx = Math.min(vp.width - 4, Math.max(4, _lastMouse.x ?? vp.width / 2));
    const cy = Math.min(vp.height - 4, Math.max(4, _lastMouse.y ?? vp.height / 2));
    await humanMouseMove(page, cx, cy).catch(() => {});

    for (let i = 0; i < max_scrolls; i++) {
      // 快慢交替：奇数步快速（0.6-1.0x），偶数步慢速（1.2-1.8x），偶尔反向（10%概率）
      let dy = Math.abs(amount);
      if (i % 2 === 0) dy = dy * (0.6 + Math.random() * 0.4);
      else dy = dy * (1.2 + Math.random() * 0.6);
      if (direction === 'up') dy = -dy;
      else if (Math.random() < 0.1 && i > 0) dy = -Math.abs(dy) * 0.3;  // 偶尔回弹

      // wheel 事件（受信），一次滚轮 = 多个较小 delta 更真实
      const chunk = Math.sign(dy) * 120;
      let remaining = Math.abs(dy);
      while (remaining > 0) {
        const stepDelta = Math.min(remaining, 120) * Math.sign(dy);
        await page.mouse.wheel(0, stepDelta);
        remaining -= Math.abs(stepDelta);
        await humanDelay(10, 40);
      }

      const bottom = await page.evaluate(() => {
        return (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 5);
      });
      if (bottom && direction !== 'up') { reachedBottom = true; break; }
      // 停顿也快慢交替
      if (i % 2 === 0) await humanDelay(150, 400);
      else await humanDelay(400, 900);
    }
    res.json({ ok: true, scrolled_to_bottom: reachedBottom });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 点击元素：全程一套鼠标驱动（B17/B18）
router.post('/click', async (req, res) => {
  try {
    const { selector = '', text = '', timeout = 5000 } = req.body || {};
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await idlePause();  // 偶发"走神"（B21）

    // 1. 找到元素盒子
    const info = await getElementCenter(page, selector, text);

    if (info && info.box) {
      // 2. 在盒子内选一个偏心点（±6px，真人不会总点正中心）
      const jx = (Math.random() - 0.5) * 12;
      const jy = (Math.random() - 0.5) * 12;
      const tx = info.x + jx;
      const ty = info.y + jy;
      // 3. 人手轨迹滑到该点（终点=点击点，鼠标有记忆）
      await humanMouseMove(page, tx, ty);
      await humanDelay(50, 150);
      // 4. 用 position 让 Playwright 在同一位置做 actionability 检查后点击，
      //    最终移动距离 ≤8px，不会出现"移到 A 点再跳到中心点"的破绽
      const position = {
        x: Math.max(1, Math.min(info.box.width - 1, info.box.width / 2 + jx)),
        y: Math.max(1, Math.min(info.box.height - 1, info.box.height / 2 + jy)),
      };
      try {
        if (selector) {
          await page.click(selector, { timeout, position });
        } else {
          await page.getByText(text, { exact: false }).first().click({ timeout, position });
        }
      } catch (e) {
        // position 点击失败（元素被遮挡/移动）→ 回退到默认中心点击
        if (selector) await page.click(selector, { timeout });
        else await page.getByText(text, { exact: false }).first().click({ timeout });
      }
    } else {
      // 拿不到坐标：纯停顿兜底后按默认方式点
      await humanDelay(200, 500);
      if (selector) {
        await page.click(selector, { timeout });
      } else if (text) {
        await page.getByText(text, { exact: false }).first().click({ timeout });
      } else {
        return res.json({ ok: false, msg: '需要提供 selector 或 text' });
      }
    }
    await humanDelay(500, 1200);  // 点击后思考/页面响应
    res.json({ ok: true, msg: '点击成功' });
  } catch (e) {
    res.json({ ok: false, msg: e.message });
  }
});

// 输入文本（鼠标弧线 → 聚焦 → 清空 → IME/逐字打字）（B17/B18/B20）
router.post('/fill', async (req, res) => {
  try {
    const { selector, value } = req.body || {};
    if (!selector) return res.json({ ok: false, msg: '缺少 selector' });
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await idlePause();  // 偶发"走神"（B21）

    // 鼠标弧线移到输入框内偏心点
    const info = await getElementCenter(page, selector, '');
    let clicked = false;
    if (info && info.box) {
      const jx = (Math.random() - 0.5) * 10;
      const jy = (Math.random() - 0.5) * 10;
      await humanMouseMove(page, info.x + jx, info.y + jy);
      await humanDelay(50, 150);
      const position = {
        x: Math.max(1, Math.min(info.box.width - 1, info.box.width / 2 + jx)),
        y: Math.max(1, Math.min(info.box.height - 1, info.box.height / 2 + jy)),
      };
      try {
        await page.click(selector, { timeout: 5000, position });
        clicked = true;
      } catch (e) {
        clicked = false;
      }
    }
    if (!clicked) await page.click(selector, { timeout: 5000 });

    await humanDelay(100, 300);
    // 清空已有内容（Control+A 后 Backspace，比直接 value='' 更像真人）
    await page.keyboard.press('Control+A');
    await humanDelay(30, 100);
    await page.keyboard.press('Backspace');
    await humanDelay(30, 100);

    // IME/逐字打字（B20）
    await typeText(page, value);
    await humanDelay(200, 500);
    res.json({ ok: true });
  } catch (e) {
    res.json({ ok: false, msg: e.message });
  }
});

// 下拉框选择（原生 <select> 和 常见自定义下拉）
router.post('/select-option', async (req, res) => {
  try {
    const { selector, value = '', label = '', index = -1, timeout = 5000 } = req.body || {};
    if (!selector) return res.json({ ok: false, msg: '缺少 selector' });
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);

    // 策略 1：原生 <select> — Playwright 的 selectOption 支持 value / label / index
    const el = page.locator(selector).first();
    const tagName = await el.evaluate(el => el.tagName ? el.tagName.toLowerCase() : '');

    if (tagName === 'select') {
      const opt = { timeout };
      if (index >= 0) opt.index = index;
      else if (value) opt.value = value;
      else if (label) opt.label = label;
      else return res.json({ ok: false, msg: '原生 select 需提供 value/label/index 之一' });

      await el.selectOption(opt);
      await humanDelay(200, 500);
      const selected = await el.evaluate(s => ({
        value: s.value,
        text: s.options[s.selectedIndex]?.text || '',
        selectedIndex: s.selectedIndex,
      }));
      return res.json({ ok: true, method: 'native_select', selected });
    }

    // 策略 2：自定义下拉（antd Select / el-select / 各种 div 模拟的 select）
    const info = await getElementCenter(page, selector, '');
    if (info && info.box) {
      const jx = (Math.random() - 0.5) * 10;
      const jy = (Math.random() - 0.5) * 10;
      await humanMouseMove(page, info.x + jx, info.y + jy);
      await humanDelay(50, 150);
      const position = {
        x: Math.max(1, Math.min(info.box.width - 1, info.box.width / 2 + jx)),
        y: Math.max(1, Math.min(info.box.height - 1, info.box.height / 2 + jy)),
      };
      try {
        // 与 /click 一致：position 精修点击，避免"移到偏心点又跳回中心"的二段式破绽（B18）
        await page.click(selector, { timeout, position });
      } catch (e) {
        await page.click(selector, { timeout }).catch(() => {});
      }
    } else {
      await page.click(selector, { timeout }).catch(() => {});
    }
    await humanDelay(300, 600);

    // 在展开的下拉面板里找匹配项：先按文本，再按 value 特征
    const targetText = label || value;
    // B27：转义引号/反斜杠，防注入与语法错误
    const safeText = String(targetText).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    let clicked = false;

    if (targetText) {
      const popupSelectors = [
        `[role="option"]:has-text("${safeText}")`,
        `.ant-select-item:has-text("${safeText}")`,
        `.el-select-dropdown__item:has-text("${safeText}")`,
        `.el-select-dropdown__item.selected:has-text("${safeText}")`,
        `.vxe-select-option:has-text("${safeText}")`,
        `li:has-text("${safeText}")`,
        `[class*="option"]:has-text("${safeText}")`,
        `[class*="item"]:has-text("${safeText}")`,
        `text=${targetText}`,
      ];
      for (const popSel of popupSelectors) {
        try {
          const loc = page.locator(popSel).first();
          if (await loc.count() > 0) {
            await loc.click({ timeout: 3000 });
            clicked = true;
            break;
          }
        } catch {}
      }
    }

    if (!clicked && index >= 0) {
      const fallbackLoc = page.locator('[role="option"], [class*="option"], [class*="item"]').nth(index);
      try {
        await fallbackLoc.click({ timeout: 3000 });
        clicked = true;
      } catch {}
    }

    await humanDelay(200, 500);

    if (clicked) {
      return res.json({ ok: true, method: 'custom_dropdown' });
    }
    await page.keyboard.press('Escape').catch(() => {});
    return res.json({ ok: false, msg: `未找到匹配的选项：target=${targetText || 'index=' + index}` });
  } catch (e) {
    res.json({ ok: false, msg: e.message });
  }
});

// 文件上传（<input type="file">）
router.post('/upload-file', async (req, res) => {
  try {
    const { selector, file_path } = req.body || {};
    if (!selector) return res.json({ ok: false, msg: '缺少 selector' });
    if (!file_path) return res.json({ ok: false, msg: '缺少 file_path' });

    if (!fs.existsSync(file_path)) {
      return res.json({ ok: false, msg: `文件不存在: ${file_path}` });
    }

    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);

    const input = page.locator(selector).first();
    await input.setInputFiles(file_path);
    await humanDelay(300, 700);

    // 尝试自动触发 change/input 事件（有些框架需要手动触发）
    try {
      await input.evaluate(el => {
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('input', { bubbles: true }));
      });
    } catch {}

    await humanDelay(200, 400);
    res.json({ ok: true, file_path, file_size: fs.statSync(file_path).size });
  } catch (e) {
    res.json({ ok: false, msg: e.message });
  }
});

// 按键（Enter/Escape 等）
router.post('/press', async (req, res) => {
  try {
    const { key = 'Enter' } = req.body || {};
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await humanDelay(100, 300);
    await page.keyboard.press(key);
    await humanDelay(500, 1200);
    res.json({ ok: true });
  } catch (e) {
    res.json({ ok: false, msg: e.message });
  }
});

// 执行任意 JS
router.post('/evaluate', async (req, res) => {
  try {
    const { script, args = null } = req.body || {};
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await humanDelay(80, 200);
    // 自动包装大模型生成的脚本（顶层 return / 缺 async）
    const safeScript = _wrapScript(script);

    // ⚠️ Playwright >=1.5x 行为变更：传"函数源码字符串"会被当作表达式求值，
    // 结果是函数对象 → JSON 序列化成 undefined（静默丢结果）。
    // 这里先在 Node 端把字符串 eval 成真正的函数对象再传给 page.evaluate
    // （函数体最终仍在页面里执行，Node 上下文不会泄漏进页面；新旧 Playwright 版本都兼容）。
    let fn = null, evalErr = null;
    try { fn = eval(`(${safeScript})`); } catch (e) { evalErr = e; }

    if (typeof fn !== 'function') {
      // eval 失败 = 脚本本身语法错误（1.63 下字符串表达式求值会静默丢结果，不如直接报错）
      return res.status(500).json({ error: `JS 语法错误: ${(evalErr && evalErr.message) || '无法解析为函数'}` });
    }
    const result = await page.evaluate(fn, args);
    res.json({ result, wrapped: safeScript !== script });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ---------- JS 经验库：自动包装大模型生成的脚本 ----------
// 返回值保证是"函数定义源码"（绝不返回 IIFE 形态），调用方会 eval 成函数对象：
// - 函数定义在 Node 端 eval 是安全的（不会执行）
// - IIFE 若直接 eval 会在 Node 端执行产生副作用，所以包一层让它在页面里执行
function _wrapScript(script) {
  if (!script || typeof script !== 'string') return script;
  const s = script.trim();

  // 函数定义形态（箭头函数 / function / async function）→ 原样
  if (/^async\s*\(/.test(s) || /^function\s*\(/.test(s) || /^async\s+function\s*\(/.test(s)) {
    return s;
  }
  if (/^\(/.test(s)) {
    // IIFE `(() => {...})()` → 包一层函数，在页面里执行
    if (/\)\s*\(\s*\)\s*;?\s*$/.test(s)) {
      return `() => {\n${s}\n}`;
    }
    // 其他括号开头（如 (a,b) => ... 的参数括号）→ 原样按函数表达式处理
    return s;
  }

  // 含顶层 return / await → 包装成 async function
  // B05：末尾补换行再闭合，防止脚本最后一行是 // 注释时吞掉 }
  if (/\breturn\b/.test(s) || /\bawait\b/.test(s)) {
    return `async () => {\n${s}\n}`;
  }

  return `() => {\n${s}\n}`;
}

// 截图
router.post('/screenshot', async (req, res) => {
  try {
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await humanDelay(100, 300);
    const outDir = (req.body || {}).dir || 'artifacts/screenshots';
    fs.mkdirSync(outDir, { recursive: true });
    const file = path.join(outDir, `${crypto.randomUUID()}.png`);
    await page.screenshot({ path: file });
    res.json({ saved_path: file });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 元素截图（用于验证码识别：返回 base64 图片数据）
router.post('/screenshot-element', async (req, res) => {
  try {
    const { selector = '', index = 0 } = req.body || {};
    const page = await browserManager.getPage();
    await respectDomainRateLimit(page);
    await humanDelay(100, 300);
    let target = null;
    if (selector) {
      const loc = page.locator(selector);
      target = await loc.nth(index).first().catch(() => null);
    }
    if (!target) {
      const buf = await page.screenshot({ fullPage: true });
      const data = 'data:image/png;base64,' + buf.toString('base64');
      return res.json({ base64: data, scope: 'full' });
    }
    const buf = await target.screenshot();
    const data = 'data:image/png;base64,' + buf.toString('base64');
    res.json({ base64: data, scope: 'element', selector });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/close', async (req, res) => {
  await browserManager.close();
  res.json({ ok: true });
});

// === 登录态持久化接口 ===

// 手动保存当前域名的登录态（用户手动登录完后调用）
router.post('/save-state', async (req, res) => {
  try {
    await browserManager.ensure();
    const result = await browserManager.forceSave();
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 列出所有已保存的登录态
router.get('/state-list', (req, res) => {
  try {
    const list = browserManager.listStates();
    res.json({ ok: true, count: list.length, states: list });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 当前状态（当前域名、storage 文件、是否检测到登录墙）
router.get('/state-info', async (req, res) => {
  try {
    await browserManager.ensure();
    let currentUrl = '';
    let detect = null;
    try {
      currentUrl = browserManager.page ? browserManager.page.url() : '';
      if (browserManager.page) {
        const st = await detectChallenges(browserManager.page);
        detect = st.detect;
      }
    } catch (e) {}
    const host = browserManager._currentHost || '';
    const storageFile = host ? browserManager._hostFile(host) : '';
    res.json({
      ok: true,
      current_host: host,
      current_url: currentUrl,
      storage_file: storageFile,
      storage_exists: storageFile ? fs.existsSync(storageFile) : false,
      detect,
      saved_states: browserManager.listStates(),
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 删除指定域名的登录态（storage 文件）
router.delete('/state/:host', async (req, res) => {
  try {
    const host = req.params.host;
    const file = browserManager._hostFile(host);
    if (fs.existsSync(file)) {
      fs.unlinkSync(file);
      if (browserManager._currentHost === host) {
        browserManager._currentHost = '';
      }
      res.json({ ok: true, deleted: file });
    } else {
      res.json({ ok: false, msg: '文件不存在', host });
    }
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ============ 多标签页管理 ============

router.get('/tabs', (req, res) => {
  try {
    const list = browserManager.listTabs();
    res.json({ ok: true, tabs: list });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/tabs/open', async (req, res) => {
  try {
    await browserManager.ensure();
    const { url = '' } = req.body || {};
    const result = await browserManager.openTab(url);
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/tabs/switch', async (req, res) => {
  try {
    await browserManager.ensure();
    const { target } = req.body || {};
    if (target === undefined || target === null) {
      return res.status(400).json({ error: 'target 是必填的（number 或 string）' });
    }
    // JSON 里数字会变 number，字符串保持 string，直接传
    const result = await browserManager.switchTab(target);
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/tabs/close', async (req, res) => {
  try {
    await browserManager.ensure();
    const { target = 'current' } = req.body || {};
    const result = await browserManager.closeTab(target);
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ============ 鼠标悬停 ============

router.post('/hover', async (req, res) => {
  try {
    await browserManager.ensure();
    const { selector = '', text = '' } = req.body || {};
    const result = await browserManager.hoverElement(selector, text);
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ============ iframe 切换 ============

router.post('/switch-frame', async (req, res) => {
  try {
    await browserManager.ensure();
    const { target = null } = req.body || {};
    const result = await browserManager.switchFrame(target);
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ============ 文件下载 ============

router.post('/download', async (req, res) => {
  try {
    await browserManager.ensure();
    const { trigger = {}, filename = '', timeout = 60000 } = req.body || {};
    const result = await browserManager.downloadFile({ trigger, filename, timeout });
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// === Proxy 管理 ===
router.get('/proxy-status', (req, res) => {
  res.json(browserManager.getProxyStatus());
});

router.post('/proxy-rotate', (req, res) => {
  res.json(browserManager.rotateProxy());
});

// === 请求拦截（屏蔽规则 + mock）===
router.post('/block-patterns/set', (req, res) => {
  const { patterns = [] } = req.body || {};
  res.json(browserManager.setBlockPatterns(patterns));
});

router.post('/block-patterns/add', (req, res) => {
  const { pattern } = req.body || {};
  if (!pattern) return res.status(400).json({ error: 'pattern 必填' });
  res.json(browserManager.addBlockPattern(pattern));
});

router.post('/block-patterns/reset', (req, res) => {
  res.json(browserManager.resetBlockPatterns());
});

router.post('/route-mock/add', (req, res) => {
  try {
    res.json(browserManager.addRouteMock(req.body || {}));
  } catch (e) {
    res.status(400).json({ error: e.message });
  }
});

router.post('/route-mock/clear', (req, res) => {
  res.json(browserManager.clearRouteMocks());
});

module.exports = router;
