from pprint import pp

import requests
from bs4 import BeautifulSoup
from langchain.globals import set_verbose, set_debug
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

from src.ai_model.ollama_model import model_ollama

set_verbose(True)
set_debug(True)

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
agent = create_react_agent(
    model=model_ollama,
    tools=tools,
    prompt="""
    你是软件测试工程师，你擅长做自动化测试。
    你可以根据用户提供的网址，进行网页分析，并仅编写完整的测试用例，不执行自动化测试。
    """
)


def test_gen():
    query = """
    https://www.baidu.com/
    """
    response = agent.invoke(
        input={
            "messages": [
                ("user", query)
            ]
        },
        config={
            "recursion_limit": 100
        }
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
