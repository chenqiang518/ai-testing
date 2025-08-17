import os

from langchain_community.document_loaders import TextLoader
from langchain_community.vectorstores.faiss import FAISS
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain import hub
from plantuml import PlantUML

# 配置请求openai的key和地址
os.environ['OPENAI_API_KEY']='536ee59f7b7bf5f5347298593828fcaб'
os.environ['OPENAI_API_BASE']='https://apitoken.ceba.ceshiren.com/openai/v1/'
# 声明模型
llm = ChatOpenAI()
# # 1. 读取文件。
loader = TextLoader("./需求文档.md")
data = loader.load()
# 3. embedding
embeddings = OpenAIEmbeddings()
# # 4. 向量存储
vector = FAISS.from_documents(data, embeddings)
retriever = vector.as_retriever()

from langchain.tools.retriever import create_retriever_tool

retriever_tool = create_retriever_tool(
    retriever,
    "search_demand",
    "找到需求文档中具体说明需求的地方",
)
@tool
def generate_png(uml_code, filename):
    """输入plantuml代码生成图像并保存为文件"""
    plantuml = PlantUML(url='https://plantuml.ceshiren.com/img/')
    image_bytes = plantuml.processes(uml_code)
    with open(f'{filename}.png', 'wb') as f:
        f.write(image_bytes)

tools = [retriever_tool, generate_png]
llm_with_tools = llm.bind_tools(tools)

prompt = hub.pull("hwchase17/openai-tools-agent")
agent = create_openai_tools_agent(llm, tools, prompt, )
agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)
result = agent_executor.invoke({
    "input": """我是一个测试工程师，我需要从以上的需求文档中梳理出来需求信息，请帮我将所有的需求梳理出来，"
             "思维导图的第一级是需求文档中的4.x开头的标题信息，表示功能模块，第二级是该功能模块的测试点，"
             "请先输出一个 plantuml 格式的源码，源码格式如代码内所示
            @startmindmap
            * root node
                * some first level node
                    * second level node
                    * another second level node
                * another first level node
            @endmindmap
             然后再根据源码信息输出一个plantuml格式的思维导图文件。生成一个图片文件，文件名为 hogwarts加任意随机数"""
})
