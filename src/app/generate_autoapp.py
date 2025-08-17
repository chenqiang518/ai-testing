# 获取执行结果
import json
from langchain import hub
from langchain.agents import create_structured_chat_agent, AgentExecutor
from langchain.globals import set_debug
from langchain_core.agents import AgentAction
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough

from src.app.appium_tools import tools
from src.ai_model.qwen_model import qwen_model

# set_debug(True)

prompt = hub.pull("hwchase17/structured-chat-agent")
llm = qwen_model #ChatOpenAI()
app_agent = create_structured_chat_agent(llm, tools, prompt)
# Create an agent executor by passing in the agent and tools
app_agent_executor = AgentExecutor(
    agent=app_agent, tools=tools,
    verbose=True,
    return_intermediate_steps=True,
    handle_parsing_errors=True)

query = """
你是一个app自动化测试工程师，接下来需要根据测试步骤，
每一步如果定位都是根据上一步的返回的html操作完成
执行对应的测试用例，测试步骤如下
1. 打开  app activity ".Settings" , app package "com.android.settings"
2. 滚动到页面 直至找到 省电与电池
3. 点击 省电与电池
4. 获取 剩余电量
5. 返回上一级页面
"""

def app_execute_result(self):
    # 获取执行结果
    r = app_agent_executor.invoke({"input": query})
    # 获取执行记录
    steps = r["intermediate_steps"]
    steps_info = []
    # 遍历执行步骤，获取每一步的执行步骤以及输入的信息。
    for step in steps:
        action = step[0]
        if isinstance(action, AgentAction):
            steps_info.append({'tool': action.tool, 'input': action.tool_input})
    return json.dumps(steps_info)


if __name__ == '__main__':
    prompt_testcase = PromptTemplate.from_template("""
    你是一个app自动化测试工程师，主要应用的技术栈为pytest + appium。
    以下为app自动化测试的测试步骤，测试步骤由json结构体描述

    {step}

    {input}

    """)

    chain = (
            RunnablePassthrough.
            assign(step=app_execute_result)
            | prompt_testcase
            | llm
            | StrOutputParser()
    )

    print(chain.invoke({"input": "请根据以上的信息，给出对应的app自动化测试的代码"}))

