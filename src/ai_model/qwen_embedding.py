import os

from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.documents import Document

model_name = "text-embedding-v4"

# 设置环境变量（替换为你的 DashScope API Key）
os.environ["DASHSCOPE_API_KEY"] = "sk-6ccd2cddff28409b9060a7636e6ac58c"
os.environ["DASHSCOPE_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 初始化大模型（支持 function calling）
# 初始化 Qwen Embedding 模型
qwen_embeddings = DashScopeEmbeddings(
    model=model_name   # 这是 Qwen 向量模型
)


if __name__ == "__main__":
    # 向量化测试
    query = "LangChain 是什么？"
    vector = qwen_embeddings.embed_query(query)
    print(len(vector), vector[:10])  # 打印向量长度和前10个数
    from langchain_core.vectorstores import InMemoryVectorStore

    in_memory_vector_store = InMemoryVectorStore(qwen_embeddings)
    print(in_memory_vector_store.embeddings)


    from langchain_chroma import Chroma

    chroma_vector_store = Chroma(
        collection_name="example_collection",
        embedding_function=qwen_embeddings,
        persist_directory="./chroma_langchain_db",  # Where to save data locally, remove if not necessary
    )
    print(chroma_vector_store.embeddings)
    document = Document(
        page_content="I have a bad feeling I am going to get deleted :(",
        metadata={"source": "tweet"},
    )
    documents = [
        document,
    ]

    chroma_vector_store.add_documents(documents=documents)

    results = chroma_vector_store.similarity_search(
        "LangChain provides abstractions to make working with LLMs easy",
        k=2,
        filter={"source": "tweet"},
    )
    for res in results:
        print(f"* {res.page_content} [{res.metadata}]")
