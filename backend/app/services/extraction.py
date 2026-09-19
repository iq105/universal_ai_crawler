"""智能提取降级链：schema → 自动结构识别 → 正则 → LLM

供 Agent 的 smart_extract 工具与旧流水线复用。
"""
import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------- 正则层：常见字段 ----------
_REGEX_PATTERNS = {"url": re.compile(r"https?://[^\s<>\"']+"), "email": re.compile(r"[\w.\-+]+@[\w.\-]+\.[a-zA-Z]{2,}"),
    "phone": re.compile(r"(?:\+?86[\s-]?)?1[3-9]\d{9}|(?:\d{3,4}[\s-]?){2}\d{4}"),
    "price": re.compile(r"[¥￥]?\s?\d+(?:\.\d{1,2})?"),
    "date": re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}月\d{1,2}日"), }


def _relevant_text(node, max_len=2000) -> str:
    t = (node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)) or ""
    return t[:max_len]


def _auto_list_schema(container):
    """基于结构启发式：找出条目内重复的字段元素（用于生成 items 字段）"""
    # 直接子元素作为字段候选（a/span/td/div/img/time/strong 等）
    field_tags = ["a", "td", "li", "div", "span", "img", "time", "strong", "b", "em", "p", "h1", "h2", "h3"]

    def field_key(el):
        cls = " ".join(el.get("class", []))
        return el.name + ("." + cls if cls else "")

    seen = {}
    for child in container.find_all(field_tags, recursive=False):
        key = field_key(child)
        if key not in seen:
            seen[key] = {"name": child.name, "class": " ".join(child.get("class", [])), "count": 0,
                "text": _relevant_text(child, 60), "href": child.get("href") if child.name == "a" else "",
                "src": child.get("src") if child.name == "img" else "", }
        seen[key]["count"] += 1
    # 无直接子元素时，回退到任意层级的前 N 个同类字段
    if not seen:
        for child in container.find_all(field_tags)[:12]:
            key = field_key(child)
            if key not in seen:
                seen[key] = {"name": child.name, "class": " ".join(child.get("class", [])), "count": 1,
                    "text": _relevant_text(child, 60), "href": child.get("href") if child.name == "a" else "",
                    "src": child.get("src") if child.name == "img" else "", }
            else:
                seen[key]["count"] += 1
    fields = list(seen.values())
    # 字段命名：保留标签名 + 流传 class 去空格
    for f in fields:
        cls = f["class"].replace(" ", "-")
        f["name"] = f["name"] + (f"_{cls}" if cls and cls not in f["name"] else "")
        f.pop("class", None)
    return fields


def find_list_container(html: str, max_rows: int = 200) -> dict | None:
    """自动识别列表容器与条目选择器（启发式）。"""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:  # noqa: BLE001
            return None

    best = None
    for tag in ["ul", "ol", "tbody", "table"]:
        for container in soup.find_all(tag):
            rows = [c for c in container.find_all(recursive=False, name=tag)]
            if tag in ("ul", "ol"):
                rows = container.find_all("li", recursive=False)
            resume_rows = [r for r in (container.find_all(["tr", "li", "div"]) if not rows else rows)]
            rows = resume_rows or rows
            if len(rows) >= 3 and len(rows) <= max_rows:
                schema = _auto_list_schema(rows[0])
                if schema:
                    # 逐行提取验证
                    sample = []
                    for r in rows[:5]:
                        row = {}
                        for f in schema:
                            row[f["name"]] = _relevant_text(r, 60)
                        sample.append(row)
                    # 简单评分：能提取出非空文本的字段数
                    score = sum(1 for f in schema if f["count"] >= 3)
                    if best is None or score > best["score"]:
                        best = {"score": score, "container_tag": tag, "schema": schema, "row_count": len(rows),
                            "sample": sample, "container_html": clean_html(str(container)[:3000]), }
    return best


def clean_html(html: str, max_len: int = 20000) -> str:
    t = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S)
    return t[:max_len]


def regex_extract(text: str, fields: list | None = None) -> list[dict]:
    """针对单条文本用正则提取常见字段（address/email/phone/price 等）。"""
    fields = fields or list(_REGEX_PATTERNS.keys())
    item = {}
    for f in fields:
        pat = _REGEX_PATTERNS.get(f.lower())
        if pat:
            m = pat.search(text or "")
            item[f] = m.group(0) if m else None
    return item


def smart_extract_local(html: str, item_selector: str = "", fields: list | None = None) -> dict:
    """本地（无 LLM）智能提取：结构识别 + 正则。返回 {items, source}。"""
    fields = fields or []
    if item_selector:
        # 提供显式选择器：直接按选择器提取行文本
        try:
            soup = BeautifulSoup(html, "lxml")
            rows = soup.select(item_selector)
            items = []
            for r in rows[:200]:
                item = {}
                if fields:
                    for f in fields:
                        key = f.get("name") if isinstance(f, dict) else str(f)
                        sel = f.get("selector") if isinstance(f, dict) else ""
                        sub = r.select_one(sel) if sel else None
                        item[key] = _relevant_text(sub if sub is not None else r, 120)
                else:
                    item["text"] = _relevant_text(r, 120)
                items.append(item)
            if items:
                return {"items": items, "source": "schema"}
        except Exception as exc:  # noqa: BLE001
            logger.debug("schema 提取失败，降级到结构识别：%s", exc)

    found = find_list_container(html)
    if found:
        # 结构识别成功 → 用识别到的字段批量提取
        return {"items": found["sample"], "source": "structure"}

    # 正则层：从整页文本抽取常见字段
    try:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        text = re.sub(r"<[^>]+>", " ", html)
    reg_items = []
    for chunk in re.split(r"\s{2,}", text)[:200]:
        item = regex_extract(chunk, [f.get("name") for f in fields] if fields else None)
        if any(item.values()):
            reg_items.append(item)
    if reg_items:
        return {"items": reg_items, "source": "regex"}
    return {"items": [], "source": "none"}
