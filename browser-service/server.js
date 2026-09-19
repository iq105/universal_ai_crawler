// server.js — browser-service 入口
// Node.js Playwright 微服务，供 Python 后端通过 HTTP 调用
//
// 安全（B22）：
// - 只绑定 127.0.0.1（用户浏览器里的恶意网页无法跨端口打到该服务）
// - 共享 token 鉴权（X-Service-Token 头或 ?token= 查询参数），与 Python 端 SERVICE_TOKEN 一致
// - /health 免鉴权（供探活）
// 退出清理（B23）：SIGINT/SIGTERM 时关闭 browser，避免 Chromium 孤儿进程
const express = require('express');
const cors = require('cors');
const routes = require('./routes');
const browserManager = require('./browser');

const PORT = Number(process.env.PORT || process.env.BROWSER_SERVICE_PORT || 8765);
const BIND_HOST = process.env.BIND_HOST || '127.0.0.1';
// 与 Python 端约定：SERVICE_TOKEN 为空 = 不启用鉴权（本地开发兼容）
const SERVICE_TOKEN = String(process.env.SERVICE_TOKEN || '');

function checkToken(req) {
  if (!SERVICE_TOKEN) return true;
  const h = req.get('X-Service-Token');
  if (h && h === SERVICE_TOKEN) return true;
  const q = req.query && req.query.token;
  return q === SERVICE_TOKEN;
}

const app = express();
app.use(cors());

// 鉴权：除 /health 外全部要求 token（B22）
app.use((req, res, next) => {
  if (req.path === '/health' || req.path === '/') return next();
  if (checkToken(req)) return next();
  res.status(401).json({ error: 'unauthorized: 缺少或错误的 X-Service-Token' });
});

app.use(routes);

// 兜底：未捕获的同步/异步异常不让进程崩掉
process.on('uncaughtException', (err) => {
  console.error('[browser-service] uncaughtException:', err);
});
process.on('unhandledRejection', (reason) => {
  console.error('[browser-service] unhandledRejection:', reason);
});

// 退出清理（B23）：避免 Chromium 孤儿进程
let closing = false;
async function shutdown(signal) {
  if (closing) return;
  closing = true;
  console.log(`[browser-service] received ${signal}, closing browser...`);
  try { await browserManager.close(); } catch (e) { /* ignore */ }
  process.exit(0);
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

const server = app.listen(PORT, BIND_HOST, () => {
  console.log(`[browser-service] listening on http://${BIND_HOST}:${PORT}`);
  console.log(`[browser-service] HEADLESS=${String(process.env.HEADLESS || 'false')} AUTH=${SERVICE_TOKEN ? 'on' : 'off'} STORAGE_DIR=${process.env.STORAGE_STATE_DIR || '(default)'}`);
});

// 端口被占时明确报错退出（而不是静默挂死）
server.on('error', (err) => {
  console.error(`[browser-service] 监听失败（端口 ${PORT}）：${err.message}`);
  process.exit(1);
});
