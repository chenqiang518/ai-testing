import os

from langchain_community.chat_models.tongyi import ChatTongyi


model_name = "qwen-plus"

# 设置环境变量（替换为你的 DashScope API Key）
os.environ["DASHSCOPE_API_KEY"] = "sk-6ccd2cddff28409b9060a7636e6ac58c"
os.environ["DASHSCOPE_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 初始化大模型（支持 function calling）
qwen_model = ChatTongyi(model=model_name)

