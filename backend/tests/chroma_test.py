import chromadb;

print(chromadb.__version__)

"""
ChromaDB 增删改查完整示例
依赖: pip install chromadb
"""

import chromadb
from chromadb.config import Settings

# ========== 1. 初始化持久化客户端 ==========
client = chromadb.PersistentClient(path="./artifacts/chroma_data",  # 数据落盘目录，重启不丢
    settings=Settings(anonymized_telemetry=False),  # 关闭遥测
)

# 获取或创建 collection
# 注意: 同一个 collection 内, embedding 维度必须一致
collection = client.get_or_create_collection(name="demo_collection", metadata={"hnsw:space": "cosine"},
    # 可选: cosine / l2 / ip
)


# ========== 2. 增 (Add) ==========
def add_documents():
    """批量写入文档。embedding 若不传, Chroma 会用默认模型自动生成。"""
    collection.add(ids=["doc1", "doc2", "doc3"],
        documents=["Python 是一种解释型、面向对象的高级编程语言。", "ChromaDB 是一个开源的向量数据库, 专为 AI 应用设计。",
            "RAG 是检索增强生成, 结合检索与生成来提升回答质量。", ],
        metadatas=[{"category": "language", "year": 1991}, {"category": "database", "year": 2022},
            {"category": "architecture", "year": 2020}, ], # embeddings=[[0.1, 0.2, ...], ...],  # 可选: 传入自定义向量
    )
    print(f"[ADD] 当前文档数: {collection.count()}")


# ========== 3. 查 (Query / Get) ==========
def query_documents():
    """语义查询: 传入查询文本, 返回最相似的 N 条。"""
    results = collection.query(query_texts=["什么是向量数据库?"], n_results=2, where={"category": "database"},
        # 可选: 元数据过滤
        include=["documents", "metadatas", "distances"], )
    print("\n[QUERY] 语义检索结果:")
    for i, doc in enumerate(results["documents"][0]):
        meta = results["metadatas"][0][i]
        dist = results["distances"][0][i]
        print(f"  - {doc} | {meta} | distance={dist:.4f}")


def get_documents():
    """按 ID 或条件精确获取, 不做语义检索。"""
    # 按 ID 获取
    by_id = collection.get(ids=["doc1"], include=["documents", "metadatas"])
    print("\n[GET by id] doc1:", by_id["documents"])

    # 按元数据条件获取
    by_meta = collection.get(where={"year": {"$gte": 2021}}, include=["documents"])
    print("[GET by where] year>=2021:", by_meta["documents"])

    # 获取全部
    all_docs = collection.get(include=["documents"])
    print(f"[GET all] 共 {len(all_docs['ids'])} 条")


# ========== 4. 改 (Update / Upsert) ==========
def update_documents():
    """update: 更新已存在的 ID; upsert: 存在则更新, 不存在则插入。"""
    # 更新文档内容和元数据
    collection.update(ids=["doc1"], documents=["Python 是一种广泛用于数据科学和 AI 的高级语言。"],
        metadatas=[{"category": "language", "year": 1991, "updated": True}], )
    print("\n[UPDATE] doc1 已更新")

    # upsert: doc4 不存在会被插入
    collection.upsert(ids=["doc3", "doc4"],
        documents=["RAG 通过检索外部知识来减少大模型幻觉。", "LangChain 是构建 LLM 应用的流行框架。", ],
        metadatas=[{"category": "architecture", "year": 2020}, {"category": "framework", "year": 2022}, ], )
    print(f"[UPSERT] 当前文档数: {collection.count()}")


# ========== 5. 删 (Delete) ==========
def delete_documents():
    """支持按 ID、按条件、按整集合删除。"""
    # 按 ID 删除
    collection.delete(ids=["doc4"])
    print("\n[DELETE by id] doc4 已删除")

    # 按元数据条件删除
    collection.delete(where={"category": "language"})
    print("[DELETE by where] category=language 已删除")

    print(f"[DELETE] 当前文档数: {collection.count()}")


# ========== 6. 清理: 删除整个 collection ==========
def drop_collection():
    client.delete_collection(name="demo_collection")
    print("\n[DROP] collection 已删除")


if __name__ == "__main__":
    # 先清空, 保证每次运行结果一致
    try:
        client.delete_collection(name="demo_collection")
    except Exception:
        pass
    collection = client.get_or_create_collection(name="demo_collection", metadata={"hnsw:space": "cosine"}, )

    add_documents()
    query_documents()
    get_documents()
    update_documents()
    delete_documents()  # drop_collection()  # 需要时再开
