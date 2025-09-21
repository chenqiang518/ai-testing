#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
# 提前设置 UA
os.environ["USER_AGENT"] = "MyLangChainBot/1.0"

import time
from math import ceil
from pathlib import Path
from uuid import uuid4
import faiss

from langchain_community.document_loaders import SitemapLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain.chains import create_retrieval_chain

# 引入你的嵌入模型和大模型
from src.ai_model.qwen_embedding import qwen_embeddings
from src.ai_model.qwen_model import qwen_model

# 向量库存储目录
INDEX_DIR = Path("faiss_index")
batch_size = 10

if INDEX_DIR.exists():
    print(f"📂 检测到已有向量库: {INDEX_DIR}，直接加载...")
    start_time = time.time()
    vector = FAISS.load_local(
        folder_path=str(INDEX_DIR),
        embeddings=qwen_embeddings,
        allow_dangerous_deserialization=True
    )
    print(f"✅ 向量库加载完成，用时 {time.time() - start_time:.2f} 秒")
else:
    print("📭 未发现已有向量库，开始抓取 + 构建...")

    # 1. 抓取数据
    print("🔍 抓取 sitemap 中的页面，请稍候...")
    loader = SitemapLoader("https://docs.langchain.com/sitemap.xml")
    docs = loader.load()
    print(f"✅ 抓取完成，页面总数: {len(docs)}")

    # 2. 文档切分
    print("✂️ 正在切分文档...")
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    documents = text_splitter.split_documents(docs)
    print(f"✅ 切分完成，总 chunk 数量: {len(documents)}")

    # 3. 创建一个空 FAISS（重要修复）
    embedding_dim = len(qwen_embeddings.embed_query("test"))
    index = faiss.IndexFlatL2(embedding_dim)
    vector = FAISS(
        embedding_function=qwen_embeddings,
        index=index,
        docstore=InMemoryDocstore(),
        index_to_docstore_id={}
    )

    # 4. 分批添加文档
    total_batches = ceil(len(documents) / batch_size)
    print(f"🚀 开始向量化并存储到 FAISS，每批 {batch_size} 条，总批次 {total_batches}...")

    start_time = time.time()
    for i in range(0, len(documents), batch_size):
        batch = documents[i:i+batch_size]
        vector.add_documents(batch)
        print(f"  📝 已处理批次 {i // batch_size + 1} / {total_batches}")
    print(f"✅ 向量化完成，总用时 {time.time() - start_time:.2f} 秒")

    # 5. 保存向量库
    print(f"💾 保存向量库到 {INDEX_DIR} ...")
    vector.save_local(str(INDEX_DIR))
    print("✅ 向量库保存完成")

# 6. 创建检索问答链
prompt = ChatPromptTemplate.from_template("""
Answer the following question based only on the provided context:
<context>
{context}
</context>
Question: {input}
""".strip())

llm = qwen_model
document_chain = create_stuff_documents_chain(llm, prompt)
retriever = vector.as_retriever()
retrieval_chain = create_retrieval_chain(retriever, document_chain)

# 7. 测试
question = "how can langsmith help with testing?"
print(f"\n❓ 提问: {question}")
response = retrieval_chain.invoke({"input": question})

print("\n🤖 回答：")
print(response["answer"])
