import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 允许直接以脚本方式运行：python src/web/generate_autoweb.py
# 此时 sys.path[0] 是 src/web，`import src.*` 会失败（IDE 里运行则由 IDE 注入根目录），
# 这里把仓库根目录补进 sys.path，保证两种运行方式一致。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_classic.agents import (
    AgentExecutor,
    create_openai_tools_agent,
    create_structured_chat_agent,
)
from langchain_core.agents import AgentAction
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig, RunnableLambda, RunnablePassthrough

from src.ai_model.qwen_model import qwen_model
from src.web.selenium_tools import tools, web
from src.utils.script_tools import SCRIPTS_DIR, script_tools
from src.utils.hub_prompt import pull_prompt
from src.utils.debug_events import DebugEventFilter
from src.utils.langchain_debug import (
    configure_langchain_logging,
    debug_enabled,
    describe_logging,
    resolve_event_filter,
)

# langchain 控制台日志开关：**默认打开**，控制台会打印
#   1) llm / tool 两类 tracer 日志：[llm/start]（发给模型的 prompt 消息）、
#      [llm/end]（模型返回内容与 tool_calls）、[tool/start]（调了哪个工具、传了什么入参）、
#      [tool/end]（工具返回值）；
#   2) agent 步骤行：「> Entering new AgentExecutor chain...」「Invoking: `xxx` with ...」
#      「responded: ...」「> Finished chain.」（来自 verbose 注入的 StdOutCallbackHandler）。
# 只想看干净输出（业务 print + 最终答案）时二选一关闭：
#     python src/web/generate_autoweb.py --quiet
#     LANGCHAIN_DEBUG=0 python src/web/generate_autoweb.py
#
# chain 类 tracer 日志默认**不**打印（白名单见下方 DEFAULT_DEBUG_EVENTS）：
# 实测一次运行会产生 30 条 [chain/start] + 30 条 [chain/end]，
# 且绝大多数是 RunnableSequence / RunnableLambda / ChatPromptTemplate 这类 LCEL 包装层，
# 只是把同一份 input/output 层层转述一遍，对排查没帮助却能把 tool/llm 日志冲散。
# 需要看 chain 时显式覆盖白名单即可（命令行 / 环境变量都优先于这个默认值）：
#     --debug-events=all                 # 9 类事件全开（含 chain）
#     --debug-events=chain,tool          # 自己指定组合
#     --hide-debug-events=llm/end        # 在默认 tool,llm 基础上再去掉大模型返回内容
#     LANGCHAIN_DEBUG_EVENTS=chain       # 环境变量写法，命令行优先级更高
#
# handler 必须由本模块注入**修复版**，而不是让框架自动注入原版 ConsoleCallbackHandler：
#   - 原版 `_on_tool_start` 写死 `run.inputs["input"]`，遇到「工具入参为 dict」
#     （structured chat agent 的 action_input 就是 dict）会抛 KeyError('input')，
#     控制台只留一行 Error in ConsoleCallbackHandler.on_tool_start callback 并丢失 [tool/start]；
#   - 原版 chat model 的 [llm/start] 会被回退成**一行转义过的 prompt 字符串**：
#     tracer 默认 _schema_format="original" -> _create_chat_model_run 抛 NotImplementedError
#     -> handle_event 回退成 on_llm_start(prompts=[get_buffer_string(messages)])。
#     本项目用的 ChatTongyi 是 chat model，system prompt + 工具清单 + agent_scratchpad
#     轻松上千字，全挤成一行、\n 变字面量，根本没法读；
#   - 原版 [llm/end] 直接 dump 整个 LLMResult，嵌套 JSON 里翻不出一句模型回复。
#   三点都已在 SafeConsoleCallbackHandler 里修好（详见 src/utils/safe_console_handler.py）。
# 它同时故意不开全局 debug：debug 与 verbose 同时为真时，langchain 会跳过
# StdOutCallbackHandler，「Invoking: ...」这类 agent 步骤行反而会消失（详见该函数 docstring）。

# tracer 日志的默认白名单：只打印 tool 与 llm 两类事件
# （chain 需要显式 --debug-events=all 或 LANGCHAIN_DEBUG_EVENTS=chain 才会出现）
DEFAULT_DEBUG_EVENTS: str = "tool,llm"
# 默认打印调试日志（agent 步骤行 + llm/tool tracer 日志）；--quiet / LANGCHAIN_DEBUG=0 关闭
DEBUG_LOGGING: bool = debug_enabled(default=True)
EVENT_FILTER: DebugEventFilter = resolve_event_filter(default_only=DEFAULT_DEBUG_EVENTS)
callbacks: list[BaseCallbackHandler] = configure_langchain_logging(
    DEBUG_LOGGING, event_filter=EVENT_FILTER
)
# 只提示一行当前日志配置（含被隐藏了哪些事件、怎么改），避免使用者不知道日志能开关。
# 提示语里刻意不写 [chain/start] 这类字面量，否则会污染对日志的 grep 统计
print(describe_logging(DEBUG_LOGGING, EVENT_FILTER))

# 回调必须通过 invoke(config=...) 下发，**不能**只写 AgentExecutor(callbacks=...)：
# langchain_classic.chains.base.Chain.invoke 里是
#     CallbackManager.configure(callbacks_from_config, self.callbacks, self.verbose, ...)
# 即构造参数走 local_callbacks -> add_handler(handler, inherit=False)，handler 只挂在
# executor 自己这一层，**到不了嵌套的 chat model / tool run**，于是 [llm/start] /
# [llm/end] / [tool/start] / [tool/end] 一条都不打印（实测：构造传 + invoke 不带 config
# => 0 条 llm/tool 日志，只剩 agent 步骤行；改成 config 传 => 全部正常）。
# 反过来「构造传 + config 也传」会让根节点事件重复打印（同一 handler 被 add 两次，
# 实测 --debug-events=all 时 [chain/start] 18 条 vs 只用 config 传 13 条），
# 所以统一只在这里定义一份 run_config，两个 executor 都不再写 callbacks= 参数。
# --quiet 关闭日志时 callbacks 是空列表，这里归一成 None（RunnableConfig.callbacks
# 允许 None），语义即「不额外挂任何回调」，一行 tracer 日志都不打。
run_config: RunnableConfig = {"callbacks": callbacks or None}

# hub 的 structured-chat prompt 会被两个 agent 复用（第一环操作浏览器、第二环读写脚本）。
# 变量名特意不叫 prompt：历史上第二环用 `prompt = PromptTemplate.from_template(...)`
# 把它遮蔽了，导致 hub prompt 只在第一环生效。
agent_prompt = pull_prompt("hwchase17/structured-chat-agent")
llm  = qwen_model # ChatOpenAI()

web_agent = create_structured_chat_agent(llm, tools, agent_prompt)
# Create an agent executor by passing in the agent and tools
web_agent_executor = AgentExecutor(
    agent=web_agent, tools=tools,
    # verbose 会额外注入 StdOutCallbackHandler，打印
    # 「> Entering new AgentExecutor chain...」「Invoking: `tool` with ...」等 agent 步骤行，
    # 与 tracer 日志共用同一个总开关（默认打开，--quiet / LANGCHAIN_DEBUG=0 关闭）。
    # 回调**不在这里传**：构造参数是不可继承的 local_callbacks，嵌套的 llm / tool run
    # 收不到，统一改由 invoke(config=run_config) 下发（见 run_config 处的说明）。
    verbose=DEBUG_LOGGING,
    return_intermediate_steps=True,
    # 步骤变多（新增 get_page_source / assert_contains 等）后，默认 15 轮容易不够
    max_iterations=25,
    handle_parsing_errors=True)


# 目标脚本名：第一环「是否需要采集步骤」与第二环「落盘/执行哪个文件」共用这一个常量。
# 两处各写一份迟早会漏改（漏改后「已存在」判断恒为假，于是每次运行都重新登录采集一遍）。
SCRIPT_NAME = "首页登录.py"


def target_script_path() -> Path:
    """目标脚本的绝对路径。

    与 script_tools 里 write_script / run_script 的落点保持同源（都取以 REPO_ROOT
    锚定的 SCRIPTS_DIR），因此与进程 CWD 无关，IDE 运行与命令行运行结论一致。
    """
    return SCRIPTS_DIR / SCRIPT_NAME


def target_script_exists() -> bool:
    """目标脚本是否已存在且非空。

    空文件 / 只有空白 / 解码失败都视同「不存在」：这种情况下第二环既拿不到步骤 json、
    又读不到可用代码，而 NO_STEPS_NOTE 明确禁止它凭空生成脚本，agent 会被卡死；
    判定为「不存在」就能让第一环重新采集步骤，走完整的首次生成流程。
    """
    path = target_script_path()
    if not path.is_file():
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def force_collect() -> bool:
    """是否强制重新采集浏览器步骤（页面改版、想推倒重建脚本时用）。

    两种触发方式：`python src/web/generate_autoweb.py --force-collect`
    或 `FORCE_COLLECT=1 python src/web/generate_autoweb.py`。
    在调用时读取（而非 import 时固化成常量），便于测试里 monkeypatch 切换。
    """
    if "--force-collect" in sys.argv:
        return True
    return os.getenv("FORCE_COLLECT", "").strip().lower() in {"1", "true", "yes", "on"}


query ="""
你是一个自动化测试工程师，接下来需要根据测试步骤，
每一步骤的定位前提条件都是上一步骤操作完成返回的html，
执行测试用例 -> 首页登录，测试步骤如下:
1. 打开 https://litemall.hogwarts.ceshiren.com/#/login?redirect=%2Fdashboard
2. 输入用户名 hogwarts
3. 输入密码 test12345
4. 点击登录按钮
5. 进入主页后，断言主页左侧导航栏包含"首页"、"商场管理"、"商品管理"
6. 执行完成，退出浏览器

执行约束（务必遵守）:
- css 选择器只能取自工具返回的 html 摘要中真实存在的标签与属性，禁止凭经验臆测类名或层级；
- 页面跳转后、或某一步定位失败后，先调用 get_page_source 重新获取当前页面元素，再继续下一步；
- 断言统一使用 assert_contains 工具，多个期望文本用「、」分隔，
  例如 assert_contains(text="首页、商场管理、商品管理")；
- 每次只输出一个 action；全部步骤执行完成后必须调用 quit 关闭浏览器，然后给出 Final Answer。
"""

# ---- 步骤缓存：让「浏览器采集」这件事只发生一次 ----
# 第一环的唯一产出是「页面上真实存在、且已验证可用的 css 选择器」，代价却是把整个
# 登录流程在浏览器里真跑一遍；而第二环 run_script 验证生成的脚本时还要再登录一次。
# 于是「脚本不存在」时每次运行都要登录两遍（探索一遍 + 验证一遍）—— 其中探索那一遍
# 在测试步骤没变的前提下纯属重复劳动。把采集结果落盘缓存即可吸收掉：
#     首次运行：采集(登录 1 次) + run_script(登录 1 次)
#     之后运行：读缓存(登录 0 次) + run_script(登录 1 次)
# 缓存放在 src/web/.steps/ 而不是 scripts/ 里：scripts 是 pytest 的收集目录，不该混进
# 非脚本产物；该目录已加入 .gitignore（缓存内容取决于线上页面的实时结构，不宜提交）。
STEPS_CACHE_DIR = Path(__file__).resolve().parent / ".steps"


def steps_cache_path() -> Path:
    """当前用例的步骤缓存文件路径（按脚本名区分，多个用例互不覆盖）。"""
    return STEPS_CACHE_DIR / f"{Path(SCRIPT_NAME).stem}.steps.json"


def query_fingerprint() -> str:
    """测试步骤（query）的指纹，用于判断缓存是否还对应同一份用例。

    query 一改（换 URL、换账号、改断言文本），旧缓存里的选择器与步骤就可能失效，
    必须自动作废重采，否则会拿旧步骤去生成新用例的脚本。
    """
    return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:16]


def steps_complete(steps_info: object) -> bool:
    """采集结果是否完整到值得缓存 / 复用。

    三个条件缺一不可：
      1. 非空列表，且不含 error 记录 —— 带异常的部分步骤（如迭代次数用尽）会误导代码生成；
      2. 至少有一次 open —— 没打开过页面就谈不上「页面上真实存在的选择器」；
      3. 至少有一次交互或断言（send_keys / click / assert_contains）——
         只 open 不操作，说明采集中途就断了。
    """
    if not isinstance(steps_info, list) or not steps_info:
        return False
    if not all(isinstance(step, dict) for step in steps_info):
        return False
    if any("error" in step for step in steps_info):
        return False
    used = {step.get("tool") for step in steps_info}
    return "open" in used and bool(used & {"send_keys", "click", "assert_contains"})


def load_cached_steps() -> str | None:
    """读取可复用的步骤缓存（json 字符串）；不可用时返回 None，由调用方去真采集。

    缓存只是优化、不是主流程依赖，所以任何异常都在此降级成「缓存不可用」，
    绝不冒泡终止 chain（与 selenium_tools / script_tools 的约定一致）。
    """
    path = steps_cache_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"步骤缓存读取失败，将重新采集：{type(exc).__name__}: {exc}")
        return None

    if not isinstance(payload, dict):
        print(f"步骤缓存格式不正确，将重新采集：{path}")
        return None
    if payload.get("query_fingerprint") != query_fingerprint():
        print(f"测试步骤（query）已变更，步骤缓存作废，将重新采集：{path}")
        return None
    steps = payload.get("steps")
    if not steps_complete(steps):
        print(f"步骤缓存内容不完整，将重新采集：{path}")
        return None
    return json.dumps(steps, ensure_ascii=False)


def save_steps_cache(steps_info: list[dict]) -> None:
    """把本次真实采集到的步骤写入缓存，供后续运行复用（不再重复登录采集）。

    写失败只打印不抛：缓存缺失最多让下一次运行重新采集一遍，不影响本次结果。
    注意「采集不完整就不写」：半截步骤（带 error）一旦落盘，后续运行会拿它生成
    缺胳膊少腿的脚本，比每次重采更难排查。
    """
    if not steps_complete(steps_info):
        print("本次采集结果不完整，不写入步骤缓存（下次运行会重新采集）")
        return
    path = steps_cache_path()
    payload = {
        "script_name": SCRIPT_NAME,
        "query_fingerprint": query_fingerprint(),
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "steps": steps_info,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        print(f"步骤缓存写入失败（已忽略）：{type(exc).__name__}: {exc}")
        return
    print(f"已缓存本次采集的 {len(steps_info)} 个步骤：{path}"
          f"（此后运行不再打开浏览器重复执行首页登录采集）")


def resolve_steps() -> tuple[str, str]:
    """决定第一环本轮的产出，返回 (steps_json, source)。

    source 取值（按代价从低到高）：
        "skip"    目标脚本已存在：步骤 json 用不上，第二环直接跑现有脚本；
        "cache"   命中步骤缓存：用上次采集到的真实步骤生成脚本，本轮不打开浏览器；
        "browser" 前两者都不成立：必须真的采集一次（本轮唯一的探索性登录）。

    只有 "browser" 会触发第一环的首页登录，因此登录次数从「每次运行 2 次」降为
    「首次 2 次、之后每次 1 次」—— 剩下的那一次是 run_script 跑用例本身，不能省。
    """
    if force_collect():
        return "", "browser"
    if target_script_exists():
        return "[]", "skip"
    cached = load_cached_steps()
    if cached is not None:
        return cached, "cache"
    return "", "browser"


def collect_steps_in_browser() -> str:
    """真的打开浏览器跑一遍测试步骤，采集每一步的工具名与入参（含真实可用的 css 选择器）。

    这是整个流程里唯一一次「探索性登录」：采集成功即写入步骤缓存，后续运行直接复用，
    不再为了同一份测试步骤反复登录。

    这里必须自己兜异常：agent 内部工具虽然已把 selenium 异常降级成 Observation，
    但仍可能因为迭代次数用尽、模型返回不可解析等原因抛错；一旦异常冒泡，
    整个 chain 会直接以 exit code 1 结束，浏览器也不会被关闭。
    """
    steps_info = []
    error = ""
    try:
        # 获取执行结果（config=run_config：把修复版 handler 作为**可继承**回调传下去，
        # 否则这一环的 [llm/*] / [tool/*] 日志一条都不会打印）
        r = web_agent_executor.invoke({"input": query}, config=run_config)
        # 获取执行记录
        steps = r["intermediate_steps"]
        # 遍历执行步骤，获取每一步的执行步骤以及输入信息
        for step in steps:
            action = step[0]
            if isinstance(action, AgentAction):
                steps_info.append({'tool': action.tool, 'input': action.tool_input})
    except Exception as exc:  # noqa: BLE001 - 保证外层 chain 与浏览器清理仍能继续
        error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        print(f"web agent 执行异常，已降级为部分步骤结果：{error}")
    finally:
        # 兜底关闭浏览器，避免异常路径下 chrome / chromedriver 进程泄漏
        web.quit()

    if error:
        steps_info.append({'error': error})

    # 是否真的落盘由 steps_complete 判定：带 error 的半截步骤绝不缓存
    save_steps_cache(steps_info)

    print(f'获取到的每一步的测试步骤以及输入信息: \n{steps_info}')
    return json.dumps(steps_info, ensure_ascii=False)


def web_execute_result(_inputs: dict) -> str:
    """第一环入口：按「脚本已存在 / 命中步骤缓存 / 需要真采集」三种情况给出步骤 json。

    只有第三种情况会打开浏览器执行首页登录，前两种都是 0 次登录，
    因此不会与第二环 run_script 的验证登录叠加成「一次运行登录两遍」。
    """
    steps, source = resolve_steps()
    if source == "skip":
        print(f"目标脚本已存在：{target_script_path()}，跳过浏览器步骤采集"
              f"（避免与 run_script 重复登录一次；需要重采请加 --force-collect）")
        return steps
    if source == "cache":
        print(f"命中步骤缓存：{steps_cache_path()}，本轮不打开浏览器重复执行首页登录"
              f"（页面改版导致选择器失效时，加 --force-collect 重新采集）")
        return steps
    print("目标脚本与步骤缓存都不存在，本轮打开浏览器采集一次真实步骤（探索性登录）")
    return collect_steps_in_browser()


# ---- 第二环：代码生成 agent（带文件系统工具，负责落盘 / 执行 / 修复）----
# 历史坑 1：这一环曾是 `prompt | llm | StrOutputParser()` 的纯文本调用，llm 没有绑定
#   任何工具，「保存到 scripts 文件夹下」只是 prompt 里的一句空话——模型只能把代码
#   当字符串吐回来，被 print 到控制台就丢弃了，src/web/scripts/ 里始终只有一个空
#   __init__.py；「查找是否存在 -> 存在则执行 -> 失败则修复」更无从谈起。
# 历史坑 2：改成 structured chat agent 后仍然写不出文件——它要求模型把 action_input
#   以 JSON 文本形式输出，而 write_script 的 code 参数是**多行代码**，模型会在 JSON
#   字符串里直接敲裸换行，产生非法 JSON，实测连续 7 次 OUTPUT_PARSING_FAILURE，
#   handle_parsing_errors 只能把它变成 Observation，模型下一轮仍犯同样的错。
# 因此第二环改用 create_openai_tools_agent（原生 function calling，ChatTongyi 支持
# bind_tools）：工具入参由模型侧按 JSON Schema 生成，多行代码作为字符串参数可正确传递。
# 第一环仍保留 structured chat（其入参都是短字符串，且已验证可用）。
codegen_prompt = pull_prompt("hwchase17/openai-tools-agent")
codegen_agent = create_openai_tools_agent(llm, script_tools, codegen_prompt)
codegen_executor = AgentExecutor(
    agent=codegen_agent, tools=script_tools,
    # 同上：verbose 的「Invoking: `write_script` with ...」会把整段生成代码打一遍
    # （[tool/start] 里也会有一份，两份内容一致、属预期）；嫌长可只关这一类日志：
    #     --hide-debug-events=tool/start   或   --quiet 全关
    verbose=DEBUG_LOGGING,
    # 回调同样只走 invoke(config=run_config)，见 run_config 处的说明
    # 一轮「生成 -> 执行 -> 修复」约 3~4 个 action，15 轮足够跑完 2 轮修复
    max_iterations=15,
    handle_parsing_errors=True)

# 代码规范 + 工具使用流程。原来这些要求写在 PromptTemplate 里，模型没有工具只能
# 「口头答应」；现在作为 agent 的任务指令，每一条都有对应工具可以真正执行。
# 注意：本模板用 str.format 渲染，除下面 4 个占位符外不要再出现花括号。
CODEGEN_TASK = """
    你是一个web自动化测试工程师，主要应用的技术栈为pytest + selenium。
    你的任务：把下面这次真实执行过的测试步骤，落成一个可重复运行的自动化测试脚本。
    
    {task}
    
    目标脚本：{scripts_dir} 目录下的 {script_name}
    
    本次真实执行过的测试步骤（json 数组，tool 是工具名，input 是工具入参，
    其中的 css 都是当时页面上真实存在、且已经验证可用的选择器）；
    如果下面给出的不是步骤 json，而是一段「本轮未采集步骤」的说明，则以该说明为准：
    {step}
    
    必须严格按以下流程使用工具，不要臆测文件是否存在：
    1. 先调用 list_scripts，确认 {script_name} 是否已经存在；
    2. 已存在：调用 read_script 读取内容，再调用 run_script 执行验证；执行通过就不必重写；
    3. 不存在：按下面的「代码规范」生成完整脚本，调用 write_script 保存，再调用 run_script 验证；
    4. run_script 失败时按工具返回的提示区分处理：脚本步骤失败（定位/超时/语法/导入错误）
       必须 read_script 后用 write_script 写入修复后的完整代码并重跑，最多修复 2 轮；
       若同一个原因连续失败两次，说明是环境/被测站点问题，立即停止修复并在 Final Answer 中说明，
       不要重复写入内容相同的代码；断言失败说明脚本本身跑得通，不要改脚本；
       若是「元素定位不到 / 等待超时」连续失败两次，很可能是页面结构改版、上面的步骤已过期，
       此时必须在 Final Answer 中提示：用 `python src/web/generate_autoweb.py --force-collect`
       重新采集步骤（不要自己臆测新的 css 选择器）；
    5. 结束后给出 Final Answer，说明脚本绝对路径、执行结论（通过 / 断言失败 / 修复了几轮）与关键改动。

    代码规范：
    - 用 pytest 组织：driver 放在 fixture 里，yield 之后 driver.quit()，保证异常路径也能关浏览器；
      测试函数必须以 test_ 开头，否则 pytest 收集不到用例；
    - fixture 里创建 driver 后必须调用 driver.maximize_window()：采集步骤时浏览器是全屏的
      （这一步不会记录在步骤 json 里），而 element-plus 是响应式布局，窗口过窄会把侧边栏
      菜单折叠掉，导致 li[role='menuitem'] 这类元素定位超时；
    - 只使用显式等待（WebDriverWait + expected_conditions），禁止 time.sleep 硬等待；
      点击/跳转之后的每一步定位都要用显式等待，禁止裸用 driver.find_element 直接取元素；
    - 向输入框输入前**必须先清空**：对显式等待拿到的元素先 element.clear() 再
      element.send_keys(text)。页面输入框可能已有默认值或上次输入的残留，不清空会拼成
      「旧值+新值」，导致登录提交失败；禁止用 send_keys 覆盖输入而省略 clear()；
    - 点击前用 EC.element_to_be_clickable 等待，
      仅用 presence_of_element_located 拿到元素就 click，会在 Vue 绑定事件前点到，登录不会真正提交；
    - 点击登录之后必须先等页面跳转完成再断言，例如
          WebDriverWait(driver, 20).until(EC.url_contains("#/dashboard"))
      不要在 click() 后立刻 assert driver.current_url——本用例登录页 URL 本身就带
      redirect=%2Fdashboard，写 assert 'dashboard' in driver.current_url 是**永真断言**（假阳性）；
    - css 选择器必须原样照抄步骤 json 里的值，用双引号包裹即可（如 "li[role='menuitem']"）；
      禁止改写成等价形式，尤其禁止 find_elements(By.TAG_NAME, 'li') 再靠
      get_attribute('attributes') 过滤——该属性在 Selenium 中恒为 None，且登录页没有 li 会等到超时；
    - 启动浏览器必须显式指定本地驱动，否则会触发 Selenium Manager 联网下载驱动、卡到执行超时：
          from selenium.webdriver.chrome.service import Service
          from src.web.web_framework import resolve_chromedriver
          driver_path = resolve_chromedriver()
          driver = webdriver.Chrome(service=Service(driver_path)) if driver_path else webdriver.Chrome()
      禁止使用 webdriver.Chrome(executable_path=...)——Selenium 4 已移除该参数，会直接 TypeError；
      也不要 import 了 resolve_chromedriver 却不使用；
    - 元素定位的 css 选择器只能取自上面步骤 json 中真实出现过的 css 值，禁止凭经验臆测类名或层级；
    - 步骤 json 里 assert_contains 的 text 参数是「用、分隔的多个期望文本」，生成代码时必须拆成
      多个独立断言（逐个判断文本是否出现在页面/元素中），不要把整串当成一个文本来匹配；
      css 参数（若存在）表示断言范围的选择器，该选择器通常匹配**多个**元素，必须用
      driver.find_elements（复数）取全部元素、聚合它们的 text 之后再逐个断言；
      用 find_element（单数）只会拿到第一个元素，必然出现 assert '商场管理' in '首页' 这种假失败，
      这属于脚本缺陷、必须修，不要当成被测系统的问题；
    - f-string 里的变量占位符只写一层花括号（写成 f"缺少 {{{{text}}}}" 是错的，应写 f"缺少 {{text}}"）；
    - 调用 write_script 时 code 参数必须是完整可运行的 Python 代码：不要 markdown 围栏、不要解释文字。
"""

# 第一环跳过采集时（脚本已存在），填进 CODEGEN_TASK 里 {step} 位置的替代说明。
# 不能直接把空数组 `[]` 丢给模型：模板里「css 选择器必须原样照抄步骤 json」等约束
# 会失去依据，模型很可能凭经验重写脚本 —— 重写后照样要跑一遍登录，等于白折腾一轮，
# 又退回到「重复执行首页登录」的老问题。这里把本轮任务明确收窄成「验证 + 按需修复」。
NO_STEPS_NOTE = (
    f"（本轮未采集浏览器步骤：目标脚本 {SCRIPT_NAME} 已存在，第一环被主动跳过，"
    f"目的就是不让同一次运行里重复执行一遍首页登录。）\n"
    f"因此本轮只做「验证 + 按需修复」，请以现有脚本为唯一事实来源：\n"
    f"- 先 list_scripts 确认，再 read_script 读出现有内容，然后 run_script 执行验证；\n"
    f"- 执行通过、或只是断言失败：直接给出 Final Answer，**不要**调用 write_script；\n"
    f"- 仅当出现脚本步骤失败（定位/超时/语法/导入错误）时，才在现有代码基础上做最小化修复，"
    f"用 write_script 写回完整代码后重跑，最多 2 轮；\n"
    f"- 本轮没有步骤 json，禁止凭空生成新脚本、禁止臆测或改写现有 css 选择器，"
    f"也不要为了「补采集」而重复执行登录流程。"
)


def persist_and_verify(inputs: dict) -> str:
    """把第一环收集到的真实步骤交给代码生成 agent：生成 -> 落盘 -> 执行 -> 失败则修复。

    第一环跳过采集时（脚本已存在，step 为空数组）改用 NO_STEPS_NOTE，
    让 agent 只「跑现有脚本 + 按需修复」，不再重写脚本。

    与 web_execute_result 同理，这里必须自己兜异常：agent 仍可能因迭代次数用尽、
    模型输出不可解析等原因抛错，一旦冒泡整个 chain 会以 exit code 1 结束。
    """
    step = (inputs.get("step") or "").strip()
    if step in ("", "[]", "{}"):
        step = NO_STEPS_NOTE
    task = CODEGEN_TASK.format(
        task=inputs.get("input", ""),
        step=step,
        script_name=SCRIPT_NAME,
        scripts_dir=SCRIPTS_DIR,
    )
    try:
        # config=run_config 把修复版 handler 作为**可继承**回调传下去（构造参数是不可继承的
        # local_callbacks，见 run_config 处的说明）。本函数常被单独 import 调用
        # （`from src.web.generate_autoweb import persist_and_verify`），那种情况下没有
        # 外层 chain 的环境上下文可继承，显式传 config 才拿得到 llm/tool 日志。
        result = codegen_executor.invoke({"input": task}, config=run_config)
        return result.get("output", "")
    except Exception as exc:  # noqa: BLE001 - 保证外层 chain 仍能给出结论
        message = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        print(f"代码生成 agent 执行异常：{message}")
        return f"脚本生成失败：{message}"

chain = (
        RunnablePassthrough.assign(step=RunnableLambda(web_execute_result))
        | RunnableLambda(persist_and_verify)
)


def main() -> None:
    """完整链路：（仅在脚本缺失时）跑浏览器 agent 收集真实步骤 -> 代码生成 agent 落盘/执行/修复。

    脚本已存在时第一环会被跳过、脚本不存在但命中步骤缓存时也不会打开浏览器
    （见 resolve_steps），于是「一次运行只登录一次」：登录只发生在 run_script 执行
    脚本时。需要重新采集步骤（页面改版、想重建脚本）请加 --force-collect，
    或设置环境变量 FORCE_COLLECT=1。

    控制台日志**默认打开**：会打印 agent 步骤行（「> Entering new AgentExecutor
    chain...」「Invoking: `run_script` with ...」「> Finished chain.」）与 llm / tool
    两类 tracer 日志（[llm/start] 发给模型的 prompt、[llm/end] 模型返回、
    [tool/start] 工具入参、[tool/end] 工具返回值）；chain 类日志默认隐藏，
    加 --debug-events=all 可以一并显示。想要干净输出（只有业务 print 与最终答案）
    就加 --quiet，或设环境变量 LANGCHAIN_DEBUG=0；这些开关与 --force-collect
    可叠加使用。

    包在 main() + __main__ 守卫里（与 generate_autoapp.py 的约定一致）：
    这样其他模块可以 `from src.web.generate_autoweb import persist_and_verify`
    单独验证第二环，而不会在 import 时就拉起浏览器。
    """
    print(chain.invoke(
        {"input":
             f"""请根据以上的信息，给出对应的web自动化测试的代码: 
             首先在 `{SCRIPTS_DIR}` 文件夹下查找是否存在对应自动化脚本 `{SCRIPT_NAME}`，
             如果存在则直接执行，执行时如果是脚本执行步骤失败(断言成功/失败不在判断范围内)，则修复脚本直到除断言之外的执行步骤全部成功
             如果不存在则按照测试步骤生成自动化测试脚本且保存在: {SCRIPTS_DIR} 文件夹下，名称为 {SCRIPT_NAME}
            """
         },
        # run_config 带上修复版 handler（可继承）：外层 chain 的 tracer 日志不会触发原版
        # ConsoleCallbackHandler 的 KeyError('input')，并顺着 LCEL 的环境上下文继承给
        # 两个 RunnableLambda 里的 executor.invoke（它们自己也显式传了同一份 config，
        # 单独 import persist_and_verify 调用时同样有日志）。
        # --quiet 时 callbacks 为 None，外层 chain 一行 tracer 日志都不打。
        config=run_config,
    ))


if __name__ == "__main__":
    main()

