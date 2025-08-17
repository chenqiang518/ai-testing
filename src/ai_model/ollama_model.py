from langchain_ollama import ChatOllama

ollama_model= ChatOllama(
    model="qwen3:latest",
    base_url='http://127.0.0.1:11434',
    # think=False,
)
