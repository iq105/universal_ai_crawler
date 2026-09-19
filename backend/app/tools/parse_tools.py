"""页面解析工具：选择器提取 + LLM 结构化提取（兜底）"""
from app.browser.browser_manager import browser_manager
from app.core.event_bus import get_ctx
from app.core.llm import llm_json
from app.prompts import EXTRACT_ITEMS_SYSTEM, EXTRACT_ITEMS_USER

# 选择器批量提取的 JS 模板（Playwright evaluate 以函数形式注入）
_EXTRACT_JS = """(scheme) => {
  const containerSel = (scheme.container || '').trim();
  const itemSel = (scheme.item_selector || '').trim();
  let nodes = [];
  if (containerSel && itemSel) {
    const c = document.querySelector(containerSel);
    if (c) nodes = Array.from(c.querySelectorAll(itemSel));
    // 容器选择器可能命中 header 等无关区域：无条目时回退全局匹配
    if (nodes.length === 0) nodes = Array.from(document.querySelectorAll(itemSel));
  } else if (itemSel) {
    nodes = Array.from(document.querySelectorAll(itemSel));
  } else if (containerSel) {
    nodes = Array.from(document.querySelectorAll(containerSel + ' > *'));
  } else {
    nodes = [document.body];
  }
  const fields = Array.isArray(scheme.fields) ? scheme.fields : [];
  const items = [];
  for (const n of nodes) {
    if (items.length >= (scheme.max_items || 200)) break;
    const item = {};
    for (const f of fields) {
      let el = n;
      if (f.selector) el = n.querySelector(f.selector);
      if (!el || !el.getAttribute) { item[f.name] = null; continue; }
      const extract = f.extract || 'text';
      if (extract === 'attribute' || extract === 'href' || extract === 'src') {
        const attr = f.attr || (extract === 'href' ? 'href' : extract === 'src' ? 'src' : '');
        item[f.name] = el.getAttribute(attr);
      } else {
        item[f.name] = (el.innerText || el.textContent || '').trim();
      }
    }
    items.push(item);
  }
  return items;
}"""


async def extract_by_scheme(scheme: dict) -> list[dict]:
    """按方案跑 CSS 选择器批量提取，返回条目列表"""
    # BrowserManager 是 HTTP 客户端，Playwright page 在 Node.js 进程里
    # 所以用 evaluate() 转发 JS 代码到 Node.js 执行
    js = f"({_EXTRACT_JS})(scheme)"
    items = await browser_manager.evaluate(js, js_args={"scheme": scheme})
    return items or []


async def llm_extract(content: str, fields: list, item_hint: str = "") -> list[dict]:
    """LLM 结构化提取兜底：直接传页面内容让 LLM 提取条目"""
    ctx = get_ctx()
    fields_desc = ", ".join(
        f["name"] if isinstance(f, dict) else str(f) for f in (fields or [])
    )
    hint = f"额外提示：{item_hint}\n" if item_hint else ""
    result = await llm_json(
        [("user", EXTRACT_ITEMS_USER.format(limit=12000, content=content[:12000], fields=fields_desc, hint=hint))],
        ctx,
        system_hint=EXTRACT_ITEMS_SYSTEM,
    )
    items = result.get("items") or []
    return items
