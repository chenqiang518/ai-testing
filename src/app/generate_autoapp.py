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
from src.utils.script_tools import SCRIPTS_DIR

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

# 目标脚本名：第一环「是否需要采集步骤」与第二环「落盘/执行哪个文件」共用这一个常量。
# 两处各写一份迟早会漏改（漏改后「已存在」判断恒为假，于是每次运行都重新登录采集一遍）。
SCRIPT_NAME = "检查电源.py"

query = """
    你是一个app自动化测试工程师，接下来需要根据测试步骤，
    每一步骤的定位前提条件都是上一步骤操作完成返回的html，
    执行测试用例 -> 检查电源，测试步骤如下:
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
    你的任务：把下面这次真实执行过的测试步骤，落成一个可重复运行的自动化测试脚本。

    {step}

    目标脚本：{scripts_dir} 目录下的 {script_name}
    
    本次真实执行过的测试步骤（json 数组，tool 是工具名，input 是工具入参，
    其中的 css 都是当时页面上真实存在、且已经验证可用的选择器）；
    如果下面给出的不是步骤 json，而是一段「本轮未采集步骤」的说明，则以该说明为准：

    {input}

    必须严格按以下流程使用工具，不要臆测文件是否存在：
    1. 先调用 list_scripts，确认 {script_name} 是否已经存在；
    2. 已存在：调用 read_script 读取内容，再调用 run_script 执行验证；执行通过就不必重写；
    3. 不存在：按下面的「代码规范」生成完整脚本，调用 write_script 保存，再调用 run_script 验证；
    4. run_script 失败时按工具返回的提示区分处理：脚本步骤失败（定位/超时/语法/导入错误）
       必须 read_script 后用 write_script 写入修复后的完整代码并重跑，最多修复 2 轮；
       若同一个原因连续失败两次，说明是环境/被测站点问题，立即停止修复并在 Final Answer 中说明，
       不要重复写入内容相同的代码；断言失败说明脚本本身跑得通，不要改脚本；
       若是「元素定位不到 / 等待超时」连续失败两次，很可能是页面结构改版、上面的步骤已过期，
       此时必须在 Final Answer 中提示：用 `python src/web/generate_autoapp.py --force-collect`
       重新采集步骤（不要自己臆测新的 xpath 选择器）；
    5. 结束后给出 Final Answer，说明脚本绝对路径、执行结论（通过 / 断言失败 / 修复了几轮）与关键改动。
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
        {"input":
             f"""请根据以上的信息，给出对应的web自动化测试的代码: 
             首先在 `{SCRIPTS_DIR}` 文件夹下查找是否存在对应自动化脚本 `{SCRIPT_NAME}`，
             如果存在则直接执行，执行时如果是脚本执行步骤失败(断言成功/失败不在判断范围内)，则修复脚本直到除断言之外的执行步骤全部成功
             如果不存在则按照测试步骤生成自动化测试脚本且保存在: {SCRIPTS_DIR} 文件夹下，名称为 {SCRIPT_NAME}
            """
         },
        config=run_config,
    ))

