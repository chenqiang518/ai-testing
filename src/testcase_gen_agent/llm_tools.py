from langchain_ollama.chat_models import ChatOllama
from src.ai_model.ollama_model import model_ollama

# model = init_chat_model("gpt-4o-mini", model_provider="openai")


model = ChatOllama(
    model="qwen3:4b",
    base_url='http://127.0.0.1:11433',
    # think=False,
)


def get_weather(city: str):
    return f'{city} is sunny'


tools = [get_weather]
model_tools = model_ollama.bind_tools(tools, tool_choice="auto")
response = model_tools.invoke("北京的天气如何")
print(response.model_dump_json(indent=2))

response = model_tools.invoke("1+1=？")
print(response.model_dump_json(indent=2))
