import json

from langchain import hub
from langchain.agents import create_structured_chat_agent, AgentExecutor
from langchain.globals import set_debug
from langchain_core.agents import AgentAction
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI

from src.ai_model.ollama_model import ollama_model
from src.ai_model.qwen_model import qwen_model
from src.web.selenium_tools import tools

# 启用langchain的debug模式
set_debug(True)

prompt = hub.pull ("hwchase17/structured-chat-agent")
llm  = qwen_model # ChatOpenAI()

web_agent = create_structured_chat_agent(llm, tools, prompt)
# Create an agent executor by passing in the agent and tools
web_agent_executor = AgentExecutor(
    agent=web_agent, tools=tools,
    verbose=True,
    return_intermediate_steps=True,
    handle_parsing_errors=True)


query ="""
你是一个自动化测试工程师，接下来需要根据测试步骤，
每一步如果定位则都是根据上一步返回的html操作完成，
执行对应的测试用例，测试步骤如下:
1. 打开 https://litemall.hogwarts.ceshiren.com/#/login?redirect=%2Fdashboard
2. 输入用户名 hogwarts
3. 输入密码 test12345
4. 点击登录按钮
5. 进入主页，获取此时的url
6. 执行完成，退出浏览器
"""

def web_execute_result(self):
    # 获取执行结果
    r = web_agent_executor.invoke({"input": query})
    # 获取执行记录
    steps = r["intermediate_steps"]
    steps_info = []
    # 遍历执行步骤，获取每一步的执行步骤以及输入信息
    for step in steps:
        action = step[0]
        if isinstance(action, AgentAction):
            steps_info.append({'tool': action.tool, 'input': action.tool_input})

    print(steps_info)
    return json.dumps(steps_info)

prompt = PromptTemplate.from_template("""
你是一个web自动化测试工程师，主要应用的技术栈为pytest + selenium。
以下为web自动化测试的测试步骤，测试步骤由json结构体描述
{step}

{input}
""")

chain = (
        RunnablePassthrough.assign(step=web_execute_result)
        | prompt
        | llm
        | StrOutputParser()
)

print(chain.invoke({"input": "请根据以上的信息，给出对应的web自动化测试的代码"}))
