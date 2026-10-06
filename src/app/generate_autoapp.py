# 获取执行结果
import json
from langchain_classic.agents import create_structured_chat_agent, AgentExecutor
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.agents import AgentAction
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig, RunnableLambda, RunnablePassthrough

from src.app.appium_tools import tools
from src.ai_model.qwen_model import qwen_model
from src.utils.hub_prompt import pull_prompt
from src.utils.safe_console_handler import SafeConsoleCallbackHandler

# 如需打印 langchain debug 日志：from langchain_core.globals import set_debug; set_debug(True)

# debug 模式下 langchain-core 会自动注入 ConsoleCallbackHandler，
# 而它在「工具入参为 dict」（structured chat agent 的 action_input 就是 dict）时
# 会抛 KeyError('input')。预先注入修复版 handler 即可避免（详见模块 docstring）。
callbacks: list[BaseCallbackHandler] = [SafeConsoleCallbackHandler()]

prompt = pull_prompt("hwchase17/structured-chat-agent")
llm = qwen_model #ChatOpenAI()
app_agent = create_structured_chat_agent(llm, tools, prompt)
# Create an agent executor by passing in the agent and tools
app_agent_executor = AgentExecutor(
    agent=app_agent, tools=tools,
    verbose=True,
    callbacks=callbacks,
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

def app_execute_result(_inputs: dict) -> str:
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
            assign(step=RunnableLambda(app_execute_result))
            | prompt_testcase
            | llm
            | StrOutputParser()
    )

    run_config: RunnableConfig = {"callbacks": callbacks}
    print(chain.invoke(
        {"input": "请根据以上的信息，给出对应的app自动化测试的代码"},
        config=run_config,
    ))

