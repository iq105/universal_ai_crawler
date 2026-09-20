"""知识库（RAG 向量检索）

Chromadb + LangChain DashScopeEmbeddings，复用 LLM 同 base_url + api_key。
每个域名一个 collection（按 hostname 隔离检索范围）+ 一个 __default__ 全局 collection。

核心 API：
  knowledge.add(content, domain, keywords, ...)   → 写一条经验
  knowledge.search(query, domain, top_k)            → 检索（自动搜 default + 指定 domain）
  knowledge.reflect(task_id, final_summary)        → 任务结束时 LLM 反思入库

⚠️ 完全异步：所有公开方法都是 async def，内部 await 所有 Chroma 操作。
⚠️ 懒加载：embedding model 和 Chroma client 首次调用才初始化（避免 .env 没配就崩）。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_client = None           # ChromaDB HttpClient（持久化）
_embeddings = None       # LangChain Embeddings 实例
_embedding_dim = 1024    # text-embedding-v3 是 1024；bge-m3 也是 1024


def _ensure_dir() -> None:
    os.makedirs(settings.knowledge_dir, exist_ok=True)


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.embedding_enabled:
        return None
    _ensure_dir()
    try:
        import chromadb
        from chromadb.config import Settings
        _client = chromadb.PersistentClient(
            path=settings.knowledge_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        logger.info("[knowledge] Chroma 已初始化 path=%s", settings.knowledge_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[knowledge] Chroma 初始化失败（RAG 功能将不可用）：%s", exc)
        _client = None
    return _client


def _get_embeddings():
    global _embeddings
    if _embeddings is not None:
        return _embeddings
    if not settings.embedding_enabled or not settings.llm_api_key:
        logger.warning("[knowledge] embedding 未启用（embedding_enabled=%s / api_key=%s）",
                       settings.embedding_enabled, bool(settings.llm_api_key))
        return None

    # 优先用 DashScope / 百炼（跟 LLM 同一套 Key）
    try:
        from langchain_community.embeddings import DashScopeEmbeddings
        _embeddings = DashScopeEmbeddings(
            model=settings.embedding_model,
            dashscope_api_key=settings.llm_api_key,
            dashscope_base_url=settings.llm_base_url,
        )
        logger.info("[knowledge] embedding 已启用 model=%s provider=DashScope", settings.embedding_model)
        return _embeddings
    except Exception as exc:  # noqa: BLE001
        logger.info("[knowledge] DashScopeEmbeddings 不可用（%s），尝试 langchain-openai ...", exc)

    # fallback：OpenAI 兼容 embeddings（DeepSeek 等）
    try:
        from langchain_openai import OpenAIEmbeddings
        _embeddings = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        logger.info("[knowledge] embedding 已启用 model=%s provider=OpenAI-compat", settings.embedding_model)
        return _embeddings
    except Exception as exc:  # noqa: BLE001
        logger.warning("[knowledge] embedding 初始化全部失败：%s", exc)
        # 兜底：用 Chroma 内置的 DefaultEmbeddingFunction（all-MiniLM-L6-v2），零配置任何环境都能跑
        try:
            import chromadb.utils.embedding_functions as ef
            _embeddings = ef.DefaultEmbeddingFunction()
            logger.info("[knowledge] embedding 已启用 model=all-MiniLM-L6-v2 provider=Chroma-builtin (fallback)")
            return _embeddings
        except Exception as exc2:  # noqa: BLE001
            logger.warning("[knowledge] Chroma 内置 embedding 也不可用：%s", exc2)
            return None


def _domain_collection(domain: str) -> str:
    """把 hostname 转成安全的 collection 名（Chroma collection 名不能含点/斜杠）"""
    safe = "".join(c for c in domain if c.isalnum() or c in ("_", "-")).strip("_")
    return safe or "default"


def _encode(messages: str | list[str]) -> list[list[float]] | None:
    """调用 embedding model，失败返回 None（不抛异常，让上层处理）。

    兼容两种接口：
      - LangChain embeddings:  emb.embed_documents(list[str]) → list[list[float]]
      - Chroma DefaultEmbeddingFunction: emb(list[str]) → list[list[float]]
    """
    emb = _get_embeddings()
    if emb is None:
        return None
    texts = [messages] if isinstance(messages, str) else list(messages)
    try:
        # LangChain 接口优先
        if hasattr(emb, "embed_documents"):
            return emb.embed_documents(texts)  # type: ignore[attr-defined]
        # Chroma DefaultEmbeddingFunction 接口：直接 call
        if hasattr(emb, "__call__"):
            return emb(texts)  # type: ignore[misc]
        # Chroma 的 embed_query（单条）
        if hasattr(emb, "embed_query") and len(texts) == 1:
            v = emb.embed_query(texts[0])  # type: ignore[attr-defined]
            return [v]
        logger.warning("[knowledge] embedding 对象无法识别：%s", type(emb))
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[knowledge] embedding 编码失败：%s", exc)
        return None


# ─────────────────────────── 公开 API ───────────────────────────

async def add(content: str, domain: str = "", keywords: list[str] | None = None,
              success: bool = True, source_task_id: str = "") -> dict:
    """存一条经验。自动写 __default__ + 对应 domain collection（便于全局搜索 + 域名定向搜索）。

    返回 {"ok": True, "ids": [...], "collections": [...]}
    """
    client = _get_client()
    if client is None:
        return {"ok": False, "msg": "知识库未初始化（embedding_enabled=false 或 chromadb 未安装）"}

    emb = _encode(content)
    if emb is None:
        return {"ok": False, "msg": "embedding 编码失败"}

    col_names = ["default"]
    if domain:
        col_names.append(_domain_collection(domain))

    kw_str = ",".join(keywords) if keywords else ""
    metadata = {
        "domain": domain or "",
        "success": "true" if success else "false",
        "keywords": kw_str,
        "task_id": source_task_id,
        "ts": int(time.time()),
        "model": settings.embedding_model,
    }
    doc_id = f"k_{int(time.time() * 1000) % 10_000_000:07d}_{hash(content) % 10000:04x}"

    written: list[str] = []
    for name in col_names:
        try:
            col = client.get_or_create_collection(name=name)
            col.add(
                ids=[doc_id],
                embeddings=emb,  # type: ignore[list-item]
                documents=[content],
                metadatas=[metadata],
            )
            written.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[knowledge] add → %s 失败：%s", name, exc)

    return {"ok": True, "id": doc_id, "collections": written, "doc": content[:120], "domain": domain}


async def search(query: str, top_k: int = 5, domain: str = "", min_score: float = 0.3) -> dict:
    """检索经验。自动搜 __default__ + 指定 domain collection，合并去重。

    min_score: 相似度阈值（Chroma distance → 1 - distance，越低越严）。默认 0.3 很宽松，
               因为经验内容比较杂，太严容易漏结果。
    """
    client = _get_client()
    if client is None:
        return {"ok": False, "msg": "知识库未初始化", "results": []}

    emb = _encode(query)
    if emb is None:
        return {"ok": False, "msg": "embedding 编码失败", "results": []}

    col_names = ["default"]
    if domain:
        col_names.append(_domain_collection(domain))

    seen_ids: set[str] = set()
    results: list[dict[str, Any]] = []

    for name in col_names:
        try:
            col = client.get_or_create_collection(name=name)
            data = col.query(query_embeddings=emb, n_results=top_k, include=["documents", "metadatas", "distances"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[knowledge] search → %s 失败：%s", name, exc)
            continue

        docs = data.get("documents", [[]])
        metas = data.get("metadatas", [[]])
        distances = data.get("distances", [[]])
        ids = data.get("ids", [[]])

        for i, doc in enumerate(docs[0]):
            doc_id = ids[0][i] if ids and ids[0] else ""
            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)
            # Chroma 的 distance 是 L2 距离（0=完全相同，∞=完全不同），转成"相似度"
            dist = distances[0][i] if distances and distances[0] else 1.0
            similarity = 1.0 / (1.0 + dist)  # 简单归一化到 [0, 1]
            if similarity < min_score:
                continue
            results.append({
                "content": doc,
                "similarity": round(similarity, 3),
                "metadata": metas[0][i] if metas and metas[0] else {},
                "collection": name,
            })

    # 相似度降序
    results.sort(key=lambda r: r["similarity"], reverse=True)
    results = results[:top_k]

    return {"ok": True, "results": results, "count": len(results), "query": query}


async def reflect(task_id: str, final_summary: str) -> dict:
    """任务结束后让 Agent 反思：从对话历史里提取反爬经验 / DOM 选择器 / 分页策略 / 踩坑点，自动入库。

    输入 final_summary 是 run_manager._consume 里拿到的最终 assistant_final 事件文本。
    这个 summary 是自然语言，knowledge.reflect 直接把它作为经验写入库——
    未来 Agent 搜"京东 爬虫经验"就能召回这些反思结果。
    """
    if not final_summary or len(final_summary.strip()) < 10:
        return {"ok": True, "skipped": True, "reason": "summary 太短"}

    # 从 summary 里抽出关键词（简单分词：按标点/空格切，取前 15 个非停用词）
    stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "但", "不过", "或", "如果", "因为", "所以", "我们", "他们", "你好", "谢谢"}
    import re
    tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", final_summary)
    keywords = [t for t in tokens if t not in stop_words and len(t) >= 2][:15]

    return await add(
        content=final_summary,
        domain="",  # reflect 不绑特定域名，放 __default__ 全局
        keywords=keywords,
        success=True,
        source_task_id=task_id,
    )


async def stats() -> dict:
    """知识库统计"""
    client = _get_client()
    if client is None:
        return {"ok": False}

    out: dict[str, int] = {}
    for col_name in ["default"] + [c.name for c in client.list_collections() if c.name != "default"]:
        try:
            col = client.get_collection(col_name)
            out[col_name] = col.count()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "collections": out, "total": sum(out.values())}
