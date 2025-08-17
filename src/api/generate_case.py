from langchain_community.document_loaders.text import TextLoader
from langchain_core.output_parsers import JsonOutputParser, StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough

from src.ai_model.qwen_model import qwen_model
from src.utils.langchain_debug import langchain_debug

langchain_debug()
llm = qwen_model


def get_by_filename(filename):
    info = TextLoader(f'./data/{filename}')
    return info.load()


def get_case_data(filename):
    template = """
        你是一个自动化测试工程师，你非常熟悉requests库
        {context}
        Question: {input}
        请根据传入的接口信息提取request中的 ip 、 url 、method、json、params、 headers
        key值为前面提到的字段，如果没有则无需添加。如果有则提取对应的value。
        要求返回的格式为json格式
        """
    prompt = PromptTemplate.from_template(template=template, )
    data_chain = (
            RunnablePassthrough.assign(context=lambda x: get_by_filename(filename), )
            | prompt
            | llm
            | JsonOutputParser()
    )
    return data_chain


def get_case(case_filename, api_filename):
    """
    通过大模型生成测试数据。
    :return:
    """
    template = """
        你是一个自动化测试工程师，精通的技术栈为 Python pytest requests库
        这个接口的具体信息为 {context}
        请求的参数信息将输入一个字典，输入的内容为 {request}
        Question: {input}"""
    # 模板提示，输出 json 格式的回答
    prompt = PromptTemplate.from_template(
        template=template)
    chain = (
            RunnablePassthrough.
            assign(context=lambda x: get_by_filename(case_filename),
                   request=lambda  x : get_case_data(api_filename))
            | prompt
            | llm
            | StrOutputParser()
    )

    input_template = """
    根据每条测试用例的测试步骤，生成对应的测试数据信息，
    每条测试用例要求都有一条对应的单独的pytest函数
    """
    print(chain.invoke({"input": input_template}))


if __name__ == '__main__':
    case_doc = '用例文档.md'
    api_doc = 'ip.har'
    get_case(case_filename=case_doc, api_filename=api_doc)
