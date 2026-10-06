from pprint import pp
from typing import Any, TypedDict, cast

import requests
from bs4 import BeautifulSoup
from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import set_verbose, set_debug
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from src.ai_model.ollama_model import ollama_model
from src.utils.safe_console_handler import SafeConsoleCallbackHandler

set_verbose(True)
set_debug(True)

# langgraph 的 ToolNode 同样以 dict 形式传参调用工具，
# debug 模式下 langchain-core 自带的 ConsoleCallbackHandler 会抛 KeyError('input')，
# 这里注入修复版 handler（详见模块 docstring）。
callbacks: list[BaseCallbackHandler] = [SafeConsoleCallbackHandler()]


class AgentInput(TypedDict):
    """create_agent 的入参状态。

    只需提供 messages，其余状态（如 remaining_steps）由图内部维护。
    显式声明 TypedDict 可满足 CompiledStateGraph.invoke 对 InputT 的泛型约束，
    避免直接传 dict 字面量时的类型告警。
    """

    messages: list[BaseMessage]

class TestCaseModel(BaseModel):

    case_name: str
    priority: int
    steps: list[str]
    expected_results: str
    tags: list[str]
    # description: str


def get(url: str):
    """
    发起网络请求，获取网页的基本结构
    :param url:
    :return:
    """
    text = requests.get(url).text
    html = BeautifulSoup(text, 'html.parser')
    result = ""
    for tag in html.select('input'):
        result += str(tag)

    print("html result")
    print(result)
    return result


def read_file(path: str):
    """
    读取文件内容
    :param path:
    :return:
    """
    ...


testcase_list_store = []


def testcase_save(testcase_list: list[TestCaseModel]):
    """
    保存所有的测试用例
    :param testcase_list:
    :return:
    """
    global testcase_list_store
    testcase_list_store += testcase_list

    # 把testcase_list_store中的数据保存到excel中，使用pandas库
    import pandas as pd
    df = pd.DataFrame(testcase_list_store, columns=['case_name', 'priority', 'steps', 'expected_results', 'tags'])

    df.to_excel('testcase.xlsx', index=False)
    print("testcase saved to excel")

    ...


tools = [ get,read_file, testcase_save ]
agent = create_agent(
    model=ollama_model,
    tools=tools,
    system_prompt="""
    你是软件测试工程师，你擅长做自动化测试。
    你可以根据用户提供的网址，进行网页分析，并仅编写完整的测试用例，不执行自动化测试。
    """,
)


def test_gen():
    query = """
    https://www.baidu.com/
    """
    state: AgentInput = {"messages": [HumanMessage(query)]}
    run_config: RunnableConfig = {
        "recursion_limit": 100,
        "callbacks": callbacks,
    }
    response = agent.invoke(
        # langgraph 把 invoke 的 InputT 约束为 TypedDictLike 协议，PyCharm 无法把
        # 具体的 TypedDict 结构匹配上去（属静态检查局限，运行时完全正常），故显式 cast
        input=cast(Any, state),
        config=run_config
    )
    pp(response, indent=2)


# agent工具
# model_structured = model_ollama.with_structured_output(TestCaseModel)
# agent2 = create_react_agent(
#     model=model_structured,
#     tools=[],
# )

# 结构化输出
# model_structured.invoke()

#
# def test_get():
#     r = get('https://www.baidu.com')
#     print(r)
