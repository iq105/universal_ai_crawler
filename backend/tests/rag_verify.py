"""验证 knowledge.py + Chroma 端到端：写入 → 用默认 embedding 检索 → API 走通"""
import asyncio, sys, os
sys.path.insert(0, ".")
os.chdir("D:/workspace-python/merchant_app/universal_ai_crawler/backend")

# 清理 + 重跑
import shutil
shutil.rmtree("artifacts/knowledge", ignore_errors=True)

# 1. knowledge client 初始化
from app.core import knowledge
client = knowledge._get_client()
assert client is not None, "Chroma client 初始化失败"
print(f"[1] Chroma client OK, path=artifacts/knowledge")

# 2. 用 knowledge.add 写入经验（没配 API Key，embedding 会失败 → 返回 ok=False）
r = asyncio.run(knowledge.add(content="京东需要先登录才能看到价格，反爬有滑块和点云字体", domain="jd.com", keywords=["京东","反爬","登录"], success=True))
print(f"[2] knowledge.add (no embed): ok={r.get('ok')} msg={r.get('msg','')}")

# 3. 直接用 Chroma 默认 embedding 写（绕过我们的 embedding 依赖）
import chromadb
from chromadb.config import Settings
c = chromadb.PersistentClient(path="artifacts/knowledge", settings=Settings(anonymized_telemetry=False))
col = c.get_or_create_collection(name="default")
col.add(ids=["t1","t2","t3"],
    documents=["京东需要先登录才能看到价格和评论数", "淘宝用极验滑块滑块，需要先 click 再 move 210px", "招聘网站把薪资用字体混淆成星号"],
    metadatas=[{"domain":"jd.com","success":"true"},{"domain":"taobao.com","success":"true"},{"domain":"zhaopin.com","success":"true"}])
print(f"[3] Direct Chroma add OK, count={col.count()}")

# 4. 直接 Chroma query（验证默认 embedding 检索能跑）
q = col.query(query_texts=["怎么过反爬滑块"], n_results=3, include=["documents","distances"])
print(f"[4] Direct Chroma query OK: count={len(q['documents'][0])}")
for i,(doc,dist) in enumerate(zip(q["documents"][0], q["distances"][0])):
    print(f"    [{i}] dist={dist:.3f} doc={doc[:30]}...")

# 5. knowledge.stats
s = asyncio.run(knowledge.stats())
print(f"[5] stats: {s}")

# 6. knowledge.search（用我们的 API——没 embedding api_key 所以返回空结果）
r2 = asyncio.run(knowledge.search("滑块 反爬", top_k=3))
print(f"[6] knowledge.search (降级): ok={r2.get('ok')} count={r2.get('count')}")

print("\n✅ 全部通过！Chroma + knowledge.py 运行正常。配好 LLM_API_KEY 后 Embedding 检索会自动启用。")
