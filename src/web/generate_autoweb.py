import ast
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

# 允许直接以脚本方式运行：python src/web/generate_autoweb.py
# 此时 sys.path[0] 是 src/web，`import src.*` 会失败（IDE 里运行则由 IDE 注入根目录），
# 这里把仓库根目录补进 sys.path，保证两种运行方式一致。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_classic.agents import (
    AgentExecutor,
    create_structured_chat_agent,
)
from langchain_classic.agents.format_scratchpad.openai_tools import (
    format_to_openai_tool_messages,
)
from langchain_classic.agents.output_parsers.openai_tools import (
    OpenAIToolsAgentOutputParser,
)
from langchain_core.agents import AgentAction
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig, RunnableLambda, RunnablePassthrough
from langchain_core.utils.function_calling import convert_to_openai_tool

from src.ai_model.qwen_model import qwen_model
from src.web.selenium_tools import tools, web
from src.utils.script_tools import REPO_ROOT, SCRIPTS_DIR, run_script, script_tools
from src.utils.hub_prompt import pull_prompt
from src.utils.json_repair import (
    repair_json_arguments,
    repair_json_string,  # noqa: F401 - 与 web 版历史调用点保持同名可用
    repair_tool_call_arguments,
)
from src.utils.debug_events import DebugEventFilter
from src.utils.langchain_debug import (
    configure_langchain_logging,
    debug_enabled,
    describe_logging,
    resolve_event_filter,
)
from src.utils.testcase_md import (
    TestCase,
    describe_test_cases,
    find_test_case,
    load_test_cases,
    select_test_cases,
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

# ---- 第一环上下文瘦身：不裁剪就会把模型输入顶穿，整轮采集报废 ----
# 实测 `--case 行政区域/区域名称`：每一步的 Observation 都是「当前页面可交互元素摘要」，
# 单条上限 MAX_SOURCE_LENGTH = 6000 字符（见 src/web/web_framework.py），而第一环
# max_iterations = 25，scratchpad 会把**每一步**的摘要全量塞进下一轮 prompt。
# 走到行政区域页（整张中国行政区表格，td 元素上百个）时累积输入直接超过模型上限：
#     ValueError: ... InternalError.Algo.InvalidParameter:
#                 Range of input length should be [1, 30720]
# invoke 一抛异常，intermediate_steps 就拿不到 -> 本轮采集结果全丢 -> 第二环无步骤可用。
#
# 因此给 AgentExecutor 挂 trim_intermediate_steps 回调：**头部若干步 + 尾部若干步保留原文，
# 中间步骤压成「工具名(入参)」一行摘要**。两个极端都不行：
#   - 简单写 trim_intermediate_steps=N（只留最后 N 步）：scratchpad 是第一环唯一的
#     「我做到第几步了」的记忆，砍光会让模型重复登录、反复点同一个菜单，白烧 max_iterations；
#   - 完全不裁剪：就是上面那次 30720 报错。
# 裁剪只影响**喂给模型的 scratchpad**，不影响采集结果：langchain_classic 的
# AgentExecutor._prepare_intermediate_steps 是对局部变量重新赋值，
# 返回的 output["intermediate_steps"] 仍是完整列表。
WEB_TRIM_HEAD: int = 2   # 开头保留原始 Thought/Action 的步数（open + 第一步登录，含真实 URL / 账号）
WEB_TRIM_TAIL: int = 3   # 结尾保留原始 Thought/Action 的步数（最近几步决定下一步动作）
# Observation 只保留**最后一条**原文：它是唯一还有效的页面快照（决定下一步的 css 选择器）；
# 更早的摘要都已被后续操作改变，留着既没用又会顶穿上下文长度
WEB_OBSERVATION_NOTE = """（该步返回的页面元素摘要已省略：页面此后已发生变化、摘要已失效；\
    需要当前页面元素时重新调用 get_page_source）"""
# 被压掉的中间步骤由这一条合成 action 代表；它不是真实工具名，
# StepRecorder 按 tools 白名单过滤，绝不会混进采集到的步骤 json
TRIMMED_HISTORY_TOOL = "__trimmed_history__"


def summarize_action(action: AgentAction, *, limit: int = 120) -> str:
    """把一个 action 压成一行「tool(入参)」摘要（入参里的 css 选择器原样保留）。"""
    raw = action.tool_input
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"{action.tool}({text})"


def trim_web_steps(steps: list[tuple[AgentAction, str]]) -> list[tuple[AgentAction, str]]:
    """第一环 scratchpad 的裁剪策略（传给 AgentExecutor.trim_intermediate_steps）。

    两条规则，都是「保住进度记忆、砍掉过期大块文本」：
      1. Observation 只保留最后一条原文，其余换成一行 WEB_OBSERVATION_NOTE —— 每步摘要
         最多 MAX_SOURCE_LENGTH=6000 字符（见 src/web/web_framework.py），是上下文膨胀的
         唯一来源；
      2. 步数超过 WEB_TRIM_HEAD + WEB_TRIM_TAIL + 1 时，中间那段再压成一条合成 action：
         log 写进度说明、observation 写逐行「tool(入参)」摘要；
    format_log_to_str 渲染成 `log\\nObservation: 摘要\\nThought: `，模型仍读得懂
    「前面已经做过哪些操作、用过哪些真实选择器」，而 token 从 N×6000 字符降到几百字符。

    只裁剪喂给模型的 scratchpad，不动 AgentExecutor 自己维护的完整 intermediate_steps，
    因此步骤采集拿到的仍是每一步的真实入参（见 collect_steps_in_browser / StepRecorder）。
    """
    if not steps:
        return steps
    # 规则 1：除最后一步外，Observation 一律换成一行说明（原始 Thought/Action 全部保留）
    trimmed: list[tuple[AgentAction, str]] = [
        (action, WEB_OBSERVATION_NOTE) for action, _ in steps[:-1]
    ]
    trimmed.append(steps[-1])
    if len(trimmed) <= WEB_TRIM_HEAD + WEB_TRIM_TAIL + 1:
        return trimmed  # 步数不多时不再压缩中间步骤，免得无谓丢信息
    # 规则 2：中间那段压成一条合成 action
    head = trimmed[:WEB_TRIM_HEAD]
    middle = trimmed[WEB_TRIM_HEAD:-WEB_TRIM_TAIL]
    tail = trimmed[-WEB_TRIM_TAIL:]
    summary = "\n".join(
        f"{index}. {summarize_action(action)}"
        for index, (action, _) in enumerate(middle, start=WEB_TRIM_HEAD + 1)
    )
    marker = AgentAction(
        tool=TRIMMED_HISTORY_TOOL,
        tool_input="",
        log=f"""（第 {WEB_TRIM_HEAD + 1}~{len(steps) - WEB_TRIM_TAIL} 步此前已执行完成，\
为控制上下文长度只保留工具调用摘要；需要页面元素时重新调用 get_page_source）""",
    )
    return [*head, (marker, summary), *tail]


def step_failure_reason(tool: str, observation: Any) -> str:
    """从工具返回的 Observation 判断这一步当时是否失败：失败返回一行原因，成功返回 ""。

    判据来自 selenium_tools._execute 的固定输出格式 —— 异常被降级成
    「<tool> 执行失败：<异常类型>: <一行原因>。<纠错提示>」；assert_contains 不抛异常，
    失败时返回以「断言失败」开头的结论串。
    """
    text = observation if isinstance(observation, str) else str(observation or "")
    head = re.sub(r"\s+", " ", text).strip()
    if not head:
        return ""
    marker = f"{tool} 执行失败："
    if head.startswith(marker):
        # 只留「异常类型 + 一行原因」：后面的纠错提示（_RETRY_HINT，以「请先调用 get_page_source」
        # 开头）是给采集 agent 看的，对代码生成没有价值，带进步骤 json 只会白白撑大第二环的 prompt。
        # 用正则而不是 split("。请先调用")：异常消息自身可能带句号/换行，分隔符不一定是「。」
        reason = re.split(r"。?\s*请先调用 get_page_source", head[len(marker):])[0]
        return reason.strip(" 。;；")[:160]
    if head.startswith("断言失败"):
        return head[:160]
    if "is not a valid tool" in head:
        return "无效的工具名"
    return ""


class StepRecorder(BaseCallbackHandler):
    """边跑边记第一环的每一步工具调用（on_agent_action 回调），作为步骤 json 的来源。

    为什么不只用返回值的 intermediate_steps：invoke 中途抛异常（迭代次数用尽、模型输入
    超长、输出不可解析）时**拿不到返回值**，已经真跑过的十几步（含真实可用的 css 选择器）
    会全部丢失，只能整轮重采。回调在异常路径下同样已经记到了步骤，至少能留下
    「跑到哪一步、用过哪些选择器」，便于定位问题，也能配合 --force-collect 之外的排查。

    除了「调用了什么」，还会用 on_tool_end 给每一步标注**当时是否真的跑通**（failed 字段）：
    步骤 json 是第二环写代码的唯一事实来源，而采集过程里必然掺着大量失败尝试
    （实测「行政区域 / 区域名称」那轮 25 步里有 13 步是非法选择器 `td:contains('北京市') + td button`
    的重复失败）。不标注的话，第二环会把这些废选择器当成「已验证可用」照抄进脚本，
    脚本执行验证时直接 InvalidSelectorException；标注后 prompt 里明确要求「failed 的步骤禁止照抄」。
    """

    def __init__(self, tool_names: frozenset[str]) -> None:
        self.tool_names = tool_names
        self.steps: list[dict] = []

    def on_agent_action(self, action: AgentAction, **kwargs: Any) -> None:
        # 只记真实工具：解析失败时 AgentExecutor 会合成占位 action，
        # trim_web_steps 也会插入一条 TRIMMED_HISTORY_TOOL 合成 action，都要排除
        if action.tool in self.tool_names:
            self.steps.append({"tool": action.tool, "input": action.tool_input})

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        """给刚跑完的那一步补上 failed 标记（AgentExecutor 串行执行，最后一条就是本次）。"""
        name = kwargs.get("name")
        if name is not None and name not in self.tool_names:
            return  # InvalidTool 之类的兜底 observation，不对应任何真实步骤
        if not self.steps:
            return
        step = self.steps[-1]
        if name is not None and name != step.get("tool"):
            return  # 顺序对不上（嵌套 / 并发）时宁可不标，避免把成功步骤误判成失败
        reason = step_failure_reason(str(step.get("tool", "")), output)
        if reason:
            step["failed"] = reason

    @property
    def failed_steps(self) -> list[dict]:
        """本次采集里没跑通的步骤（打印用，不进第二环的 prompt）。"""
        return [step for step in self.steps if step.get("failed")]


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
    # 步骤变多（新增 get_page_source / assert_contains 等）后，默认 15 轮容易不够；
    # 带前提条件的用例（如「行政区域 / 区域名称」要先做完整个登录前置）实测 25 轮仍会耗尽
    # —— agent 在第 25 轮随便给个 Final Answer 收场，采集到的步骤缺最后几步（连 quit 都没有）。
    # scratchpad 已由 trim_web_steps 收敛成常数级，多给轮次不会再顶穿模型输入长度。
    max_iterations=35,
    # scratchpad 裁剪：不裁剪会把模型输入顶穿（详见 trim_web_steps 的说明）
    trim_intermediate_steps=trim_web_steps,
    handle_parsing_errors=True)


# ---- 用例参数化：用例名 / 前提条件 / 测试步骤 / 预期结果 全部取自 markdown 用例文档 ----
# 历史问题：`SCRIPT_NAME = "首页登录.py"` 与 query 里的 6 条测试步骤都是硬编码字符串，
# 和 src/web/testcase/home_page.md 构成两份事实来源，必然漂移：md 改了步骤，生成脚本
# 用的还是旧步骤；md 里新增的用例（如「行政区域 / 区域名称」）永远没人跑。
# 现在统一由用例文档派生（解析逻辑见 src/utils/testcase_md.py）：
#   用例名 = 各级标题去掉编号前缀后用 `_` 连接，即「一级标题_二级标题_三级标题_...」
#            （`# 1. 首页登录` -> 首页登录；`# 2. 行政区域` + `## 2.1 区域名称`
#              -> 行政区域_区域名称；三、四级标题依次往下拼）；
#   脚本名 = 用例名 + ".py"（行政区域_区域名称.py）；
#   query  = 前提条件 + 测试步骤 + 预期结果（由 build_query 渲染，见下）。
#
# 选哪条用例（命令行优先于环境变量，与 --force-collect 的约定一致）：
#   python src/web/generate_autoweb.py                        # 文档里的**全部**用例（默认）
#   python src/web/generate_autoweb.py --all-cases             # 同上，把「跑全部」显式写出来
#   python src/web/generate_autoweb.py --first-case            # 只跑文档里的第一条（调试用）
#   python src/web/generate_autoweb.py --case 行政区域_区域名称  # 指定用例
#   python src/web/generate_autoweb.py --case 区域名称          # 末级标题也能匹配
#   WEB_TESTCASE=行政区域_区域名称 python src/web/generate_autoweb.py
#   python src/web/generate_autoweb.py --list-cases            # 只列用例清单，不调大模型
#   python src/web/generate_autoweb.py --case-file src/app/testcase/ip.md   # 换用例文档
#
# 为什么默认是「全部」而不是「第一条」（本轮修复）：
#   旧行为是 `select_test_cases(..., select_all=False)` -> 未指定 --case 时只取文档里的
#   第一条用例，于是 `python src/web/generate_autoweb.py` 跑完「# 1. 首页登录」就结束，
#   「# 2. 行政区域 / ## 2.1 区域名称1」以及文档里后续新增的用例**一条都不会执行**。
#   这种现象很容易被误判成「用例解析不到 / 场景丢了」，其实解析是好的（启动日志里
#   「解析到 2 条用例」、--list-cases 也能完整列出来），只是没被选中。
#   用例文档既然是唯一事实来源，「文档里有几条就跑几条」才符合直觉；要缩小范围就用
#   --case <名称> / --first-case 显式表达。
DEFAULT_TESTCASE_FILE: Path = Path(__file__).resolve().parent / "testcase" / "home_page.md"
CASE_FILE_FLAG = "--case-file"
CASE_FLAG = "--case"
ALL_CASES_FLAGS = ("--all-cases", "--all")
# 只跑文档里的第一条用例（即旧默认行为）：调试单条用例、又不想敲用例名时用
FIRST_CASE_FLAGS = ("--first-case", "--first")
LIST_CASES_FLAGS = ("--list-cases", "--list")
# 打印用法后直接退出。这里的开关是手写 sys.argv 解析（没用 argparse，见 _cli_flag /
# _cli_option），所以 argparse 自带的 -h/--help 并不存在：不加这一条的话 `--help`
# 会被当成无意义参数**静默忽略**，然后照常跑起完整流水线（开浏览器 + 调大模型 +
# 烧 token），实测踩过。帮助文本见文件末尾的 USAGE / print_usage。
HELP_FLAGS = ("--help", "-h")
CASE_FILE_ENV = "WEB_TESTCASE_FILE"
CASE_ENV = "WEB_TESTCASE"
# 前置用例（前提条件）准备开关：默认**开** —— 开跑本用例前，会先把「前提条件」引用的
# 前置脚本准备成「可直接 import 复用」的状态（已存在即复用、缺入口函数即最小重构、
# 不存在即重新生成，见 ensure_precondition_scripts）。只想跑当前用例、不想让程序顺带
# 生成/重构前置脚本时二选一关闭：
#     python src/web/generate_autoweb.py --case 行政区域/区域名称 --no-deps
#     SKIP_PRECONDITION=1 python src/web/generate_autoweb.py --all-cases
SKIP_PRECONDITION_FLAGS: tuple[str, ...] = ("--no-deps", "--no-precondition", "--skip-precondition")
SKIP_PRECONDITION_ENV = "SKIP_PRECONDITION"


def _cli_option(flag: str) -> str | None:
    """读取 `--flag=value`（也兼容 `--flag value`）；没配则返回 None。

    写法与 src/utils/langchain_debug.py 里的 `_read_cli_option` 一致（那是日志开关的
    私有实现，这里不跨模块借私有函数，保持本模块自洽）。

    空格形式只吃「不像开关」的下一个参数：`--case --all-cases` 若把 `--all-cases`
    当成用例名，报错会变成「找不到用例 --all-cases」，指不到真正的问题（漏了值）。
    """
    prefix = flag + "="
    for index, arg in enumerate(sys.argv):
        if arg.startswith(prefix):
            return arg[len(prefix):].strip()
        if arg == flag and index + 1 < len(sys.argv):
            value = sys.argv[index + 1].strip()
            return None if value.startswith("-") else value
    return None


def _cli_flag(*flags: str) -> bool:
    """命令行里是否出现了任一不带值的开关（如 --all-cases / --list-cases）。"""
    return any(flag in sys.argv for flag in flags)


def resolve_testcase_file(cli: Optional[str] = None) -> Path:
    """用例文档路径：`--case-file` / `WEB_TESTCASE_FILE`，缺省 DEFAULT_TESTCASE_FILE。

    相对路径按仓库根目录（REPO_ROOT）解析，与 SCRIPTS_DIR 同源，
    因此在仓库任意子目录下运行、IDE 运行与命令行运行结论都一致。
    """
    raw = (cli if cli is not None
           else (_cli_option(CASE_FILE_FLAG) or os.getenv(CASE_FILE_ENV, "").strip()))
    if not raw:
        return DEFAULT_TESTCASE_FILE
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path)


# 解析在 import 时做一次即可：用例文档是仓库里的静态文件，一次运行内不会变。
# 文档缺失 / 一条用例都解析不出来时，load_test_cases 直接抛带提示的异常（快速失败）——
# SCRIPT_NAME 要从第一条用例推导，返回空列表只会在下游变成难排查的 IndexError。
TESTCASE_FILE: Path = resolve_testcase_file()
TEST_CASES: list[TestCase] = load_test_cases(TESTCASE_FILE)
# 本轮要跑哪些用例：
#   --case <名称> / WEB_TESTCASE=<名称> -> 只跑这一条；
#   --first-case                        -> 只跑文档里的第一条（旧默认行为）；
#   其余情况（什么都不带、或显式 --all-cases）-> 文档里的**全部**用例，一条都不漏。
CASE_KEYWORD: str = _cli_option(CASE_FLAG) or os.getenv(CASE_ENV, "").strip()
RUN_ALL_CASES: bool = (
    _cli_flag(*ALL_CASES_FLAGS)
    or not (CASE_KEYWORD or _cli_flag(*FIRST_CASE_FLAGS))
)
SELECTED_CASES: list[TestCase] = select_test_cases(
    TEST_CASES,
    CASE_KEYWORD,
    select_all=RUN_ALL_CASES,
)
# 本轮「主用例」：模块级函数的默认参数（单独 import 本模块验证时也用它），
# 以及 --case / --first-case 只跑一条时的目标。
# 一次跑多条（默认 / --all-cases）时，main() 会把当前用例放进 chain 的 inputs["case"]，
# 由 _case_of() 取用（见 web_execute_result / persist_and_verify）。
CASE: TestCase = SELECTED_CASES[0]
# 目标脚本名 = 用例名 + ".py"：第一环「是否需要采集步骤」与第二环「落盘/执行哪个文件」
# 共用这一个常量。两处各写一份迟早会漏改（漏改后「已存在」判断恒为假，
# 于是每次运行都重新登录采集一遍）。
SCRIPT_NAME: str = CASE.script_name


def _case_of(inputs: Optional[dict]) -> TestCase:
    """从 chain 的 inputs 里取「当前用例」，没传就用模块级默认 CASE。

    RunnablePassthrough.assign 会把 invoke 时给的键原样透传，所以 main() 放进
    inputs["case"] 的用例，两个 RunnableLambda 都能拿到；而单独 import
    persist_and_verify 调用（不带 case）时行为与改造前完全一致。
    """
    case = (inputs or {}).get("case")
    return case if isinstance(case, TestCase) else CASE


# 只提示两行本轮的用例上下文（文档、选中用例、目标脚本），与 describe_logging 的
# 做法一致：让人一眼知道「这次到底在跑 md 里的哪条用例」，不必翻代码。
print(f"测试用例文档：{TESTCASE_FILE}（解析到 {len(TEST_CASES)} 条用例；"
      f"--list-cases 查看全部）")
if len(SELECTED_CASES) == 1:
    print(f"""本轮用例：{CASE.name} -> 目标脚本 {SCRIPTS_DIR / CASE.script_name}\
（--case <名称> 指定用例，--first-case 只跑第一条，--case-file <md> 换文档）""")
else:
    # 多条用例时逐条列出「用例名 -> 脚本名」，目标脚本一行只写一个反而会误导
    print(f"本轮用例：共 {len(SELECTED_CASES)} 条（文档里的全部用例），顺序执行于 {SCRIPTS_DIR}")
    for _index, _case in enumerate(SELECTED_CASES, start=1):
        print(f"  {_index}. {_case.name} -> {_case.script_name}")
    print("""（不带开关即跑全部用例；--case <名称> 只跑一条，--first-case 只跑第一条，\
--list-cases 查看全部，--case-file <md> 换文档）""")


def target_script_path(case: TestCase = CASE) -> Path:
    """目标脚本的绝对路径。

    与 script_tools 里 write_script / run_script 的落点保持同源（都取以 REPO_ROOT
    锚定的 SCRIPTS_DIR），因此与进程 CWD 无关，IDE 运行与命令行运行结论一致。
    """
    return SCRIPTS_DIR / case.script_name


def target_script_exists(case: TestCase = CASE) -> bool:
    """目标脚本是否已存在且非空。

    空文件 / 只有空白 / 解码失败都视同「不存在」：这种情况下第二环既拿不到步骤 json、
    又读不到可用代码，而 NO_STEPS_NOTE 明确禁止它凭空生成脚本，agent 会被卡死；
    判定为「不存在」就能让第一环重新采集步骤，走完整的首次生成流程。
    """
    path = target_script_path(case)
    if not path.is_file():
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


# ---- 前提条件（前置用例）：能复用已有脚本就绝不重新生成一遍 ----
#
# 背景：`行政区域_区域名称` 的前提条件写着「首页登录」。改造前只有 build_query 里那句
# 「前提条件里的前置操作要在浏览器里先做完」，于是**每条用例的脚本都把登录抄一遍**
# （账号密码、选择器、等跳转全重复）：登录页一改版要改 N 个脚本，每次执行脚本还要
# 多跑 N 遍登录。现在的处理规则（开跑本用例前由 ensure_precondition_scripts 落地）：
#   1. 前提条件为空 / 只有「1. 」这类空占位 -> 什么都不做，也不给脚本补登录；
#   2. 前提条件能在用例文档里匹配到一条用例（如「首页登录」-> 首页登录.py）：
#        a. `src/web/scripts/首页登录.py` 已存在，且有**可复用入口函数**（如 `login(driver)`）
#           -> 直接复用：本用例脚本 `from src.web.scripts.首页登录 import login`，
#              严禁把登录流程重写一遍；
#        b. 已存在，但只有 `test_login(driver)`（没有入口函数）-> 先做一次**确定性最小重构**
#           补出入口函数（add_reusable_entry，用 ast 改名 + 加转发包装，不走 LLM，
#           结果先过 ast.parse 语法自检）。这类脚本本轮没有真跑过，所以重构后会
#           run_script 复核一次（verify=True），结构性失败自动回滚原文件；
#           而「刚生成的脚本」一律 verify=False —— 用例已在真实浏览器里跑过一遍，
#           落盘后再执行就是重复执行；
#        c. 不存在 -> 把前置用例当一条独立用例跑完整 chain（必要时开浏览器采集步骤），
#           **重新生成**出带入口函数的 首页登录.py，再复用；
#   3. 前提条件在文档里找不到对应用例（如「已有一条商品数据」这类环境/数据描述）
#      -> 按文字含义在本用例脚本内自行实现，不产生独立脚本。
#
# 「入口函数」用 ast 静态解析判定（find_reusable_entry）：模块顶层、不以 `test_` 开头、
# 不带 fixture 装饰器、第一个位置参数是 driver。用 ast 而不是让 LLM 猜：判定结果直接决定
# import 语句怎么写，必须确定、可重复，且不需要把 selenium 脚本 import 进当前进程。
#
# 前置用例自己也可能有前提条件，因此 ensure_precondition_scripts 是递归的；
# 环形引用（A 依赖 B、B 依赖 A）靠 preparing 祖先链截断，另有 MAX_PRECONDITION_DEPTH 兜底。

# 前提条件里的占位写法：md 手写时常留一行空的 `1.`，或写「无」，都不指向真实前置用例
PLACEHOLDER_PRECONDITIONS: frozenset[str] = frozenset({
    "", "无", "none", "n/a", "na", "null", "nil", "-", "--", "暂无", "不涉及", "见测试步骤",
})
# 入口函数（以及可安全重构的 test 函数）的首参名：web 领域约定是 selenium 的 driver
ENTRY_DRIVER_ARGS: tuple[str, ...] = ("driver", "webdriver", "browser", "d")
# 前置用例递归准备的最大层数：防止环形 / 超深依赖把一次运行拖成没完没了
MAX_PRECONDITION_DEPTH: int = 5
# 前提条件 -> 处理状态的中文说明（describe_preconditions / build_precondition_note 共用）
PRECONDITION_STATES: dict[str, str] = {
    "reuse": "脚本已存在且有入口函数 -> 直接 import 复用",
    "refactor": "脚本已存在但没有入口函数 -> 先最小重构补入口，再 import 复用",
    "generate": "脚本不存在 -> 先重新生成前置脚本，再 import 复用",
    "inline": "文档里没有对应的独立用例 -> 按文字在本用例脚本内自行实现",
}


def script_module_name(script_path: Optional[Path]) -> str:
    """脚本绝对路径 -> import 用的模块路径，如 `src.web.scripts.首页登录`。

    中文模块名在 Python 3 是合法标识符，`from src.web.scripts.首页登录 import login`
    可以直接写（已在真实脚本上验证过）；这里只做路径 -> 点号的换算，不校验可导入性。
    """
    if not script_path:
        return ""
    try:
        relative = script_path.relative_to(REPO_ROOT)
    except ValueError:  # 脚本目录被挪到仓库外（正常不会发生，兜底而已）
        return script_path.stem
    return ".".join(relative.with_suffix("").parts)


@dataclass(frozen=True)
class PreconditionRef:
    """md 里一条前提条件的解析结果：它指向哪个用例、脚本在不在、能不能直接复用。

    frozen dataclass（与 TestCase 同样的约定）：可以安全地跨函数传递，
    也能整体塞进 prompt 渲染函数，不用担心被中途改掉。
    """

    text: str                             # md 里的原文，如「首页登录」
    case: Optional[TestCase] = None       # 在用例文档里匹配到的前置用例；None 表示纯文字描述
    script_path: Optional[Path] = None    # 前置用例的脚本绝对路径（匹配到用例才有）
    entry: Optional[str] = None           # 前置脚本里可复用的入口函数名；None 表示没有

    @property
    def exists(self) -> bool:
        """前置脚本是否已存在（实时查文件系统，不用缓存值：可能刚被上一步生成出来）。"""
        return bool(self.script_path and self.script_path.is_file())

    @property
    def reusable(self) -> bool:
        """是否已经处于「可直接 import 复用」的状态：脚本存在且有入口函数。"""
        return self.exists and bool(self.entry)

    @property
    def state(self) -> str:
        """处理状态：reuse（直接复用）/ refactor（补入口）/ generate（重新生成）/ inline。"""
        if self.case is None:
            return "inline"
        if self.reusable:
            return "reuse"
        return "refactor" if self.exists else "generate"

    @property
    def module_name(self) -> str:
        """前置脚本的 import 路径，如 `src.web.scripts.首页登录`（中文模块名在 Python 3 合法）。"""
        return script_module_name(self.script_path)

    @property
    def import_line(self) -> str:
        """本用例脚本里应该写的那行 import；没有入口函数时为空串。"""
        return f"from {self.module_name} import {self.entry}" if self.module_name and self.entry else ""

    @property
    def call_line(self) -> str:
        """本用例脚本里应该写的那行调用；没有入口函数时为空串。"""
        return f"{self.entry}(driver)" if self.entry else ""


def _is_fixture_decorator(decorator: ast.expr) -> bool:
    """判断装饰器是不是 pytest fixture（`@pytest.fixture` / `@fixture` / `@pytest.fixture(...)`）。

    fixture 由 pytest 注入，import 过来直接调用没有意义（拿到的不是 driver 而是生成器），
    因此不能被当成「可复用入口函数」。
    """
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(node, ast.Attribute):
        return node.attr == "fixture"
    return isinstance(node, ast.Name) and node.id == "fixture"


def _business_entry_of_tests(tree: ast.Module) -> Optional[str]:
    """找出顶层 `test_*(driver)` 用例实际调用的那个业务函数名（即真正的复用入口）。

    为什么要单独找：一个脚本里可能有**多个**「首参是 driver」的顶层函数，例如
    实测生成的 `行政区域_区域名称.py`：
        def region_name_verification(driver): ...   # 只有业务步骤，**不含登录**
        def region_name(driver):                    # 前置登录 + 业务步骤，才是完整入口
            login(driver)
            region_name_verification(driver)
        def test_region_name(driver):
            region_name(driver)
    按「文件里第一个首参为 driver 的函数」挑会选中 region_name_verification，
    后续用例把它当前提条件 import 复用时就**少了登录**，脚本必然定位超时。
    而 pytest 入口调用的那个函数，语义上就是「跑完整条用例」的入口，最可靠。

    只在能唯一确定时返回：test 函数体里调用了 0 个或 2 个以上同模块函数时返回 None，
    交给调用方回落到原有启发式（宁可不猜，也不要给出错误的复用入口）。
    """
    top_level = {node.name for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    called: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        for child in ast.walk(node):
            # 只认「裸函数名调用」：self.xxx() / module.xxx() 都不是同模块的业务入口
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                if child.func.id in top_level and not child.func.id.startswith("test_"):
                    called.add(child.func.id)
    if len(called) == 1:
        return next(iter(called))
    return None


def find_reusable_entry(script_path: Optional[Path]) -> Optional[str]:
    """ast 静态解析脚本，找出可被其它脚本 import 复用的入口函数名；找不到返回 None。

    挑选顺序：
      1. 顶层 `test_*(driver)` 用例实际调用的那个业务函数（见 _business_entry_of_tests）——
         语义上就是「跑完整条用例」的入口，含前置登录，最可靠；
      2. 回落启发式：模块顶层函数、不以 `test_` 开头、不是 dunder、不带 pytest fixture
         装饰器、至少有一个位置参数；首参名是 driver / browser 这类约定名的**优先**返回，
         否则退而求其次返回第一个满足条件的函数名。

    只做静态解析、不 import 目标脚本：selenium 脚本 import 进来会连带解析 chromedriver，
    纯属浪费；而且解析失败（语法错误 / 编码问题）时返回 None 就好，绝不能抛异常打断链路。
    """
    if not script_path or not script_path.is_file():
        return None
    try:
        tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"解析脚本失败，按「没有入口函数」处理：{script_path.name} -> {type(exc).__name__}: {exc}")
        return None

    reusable: set[str] = set()
    fallback: Optional[str] = None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        if name.startswith(("test_", "__")):
            continue  # pytest 用例自己会起 driver，不能当入口复用
        if any(_is_fixture_decorator(decorator) for decorator in node.decorator_list):
            continue
        positional = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
        if not positional:
            continue  # 无参函数复用价值不大（多半是工具函数），也不符合「传 driver 就能跑」的形态
        reusable.add(name)
        if positional[0] in ENTRY_DRIVER_ARGS and fallback is None:
            fallback = name

    # 优先「test_ 用例调用的那个业务函数」，但它必须本身也满足可复用条件
    preferred = _business_entry_of_tests(tree)
    if preferred in reusable:
        return preferred
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in reusable
                and [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)][0] in ENTRY_DRIVER_ARGS):
            return node.name
    return fallback


def _precondition_keyword(text: str) -> str:
    """把一条前提条件的文字裁成 find_test_case 能用的关键词。

    去掉 `.py` 后缀、去掉包裹的引号 / 书名号 / 括号、去掉首尾标点与空白：
    md 是手写的，「首页登录」「首页登录.py」「`首页登录`」「首页登录（账号 hogwarts）」
    都应该指向同一条前置用例。
    """
    keyword = re.sub(r"\.py$", "", (text or "").strip(), flags=re.IGNORECASE).strip()
    keyword = keyword.strip("「」『』\"'`【】[]（）() ")
    return re.sub(r"[\s。．.，,；;：:、！!？?]+$", "", keyword).strip()


def is_placeholder_precondition(text: str) -> bool:
    """前提条件是不是空占位（`1. ` 这种空列表项、或写「无」）：这类行不指向任何前置用例。

    md 里 `- 前提条件:` 下面留一行空的 `1.` 是常见写法（testcase_md 已经过滤了**完全空**的项，
    但「无」「暂无」这类文字还在），这里统一判掉，免得下游把「无」当成一个叫「无」的用例去找。
    """
    keyword = _precondition_keyword(text)
    if not keyword:
        return True
    normalized = re.sub(r"[\s_\-*.、，,。.;；:：]+", "", keyword).lower()
    return normalized in PLACEHOLDER_PRECONDITIONS


def find_precondition_case(text: str, cases: Sequence[TestCase] = TEST_CASES) -> Optional[TestCase]:
    """把一条前提条件的文字（如「首页登录」）解析成用例文档里的 TestCase；解析不出返回 None。

    两级匹配：
      1. 借用 find_test_case 的宽松匹配（用例名 / 各级标题 / 末级标题 / 带 .py 都认）；
         它命中多条（歧义）或一条都没命中时会抛 ValueError，这里捕获后走第 2 级；
      2. 兜底子串匹配：前提条件常写成一句话（「首页登录（账号 hogwarts）」「先完成 首页登录」），
         这时用例名 / 各级标题是这句话的子串，取**命中标题最长**的那条用例（最具体优先）。
    都匹配不上 -> None，说明这只是环境 / 数据描述，不是可复用的前置脚本。

    注意：本函数不排除「自引用」（某条用例把自己写进前提条件），
    由 resolve_preconditions 统一过滤 —— 只有它才知道「本用例是谁」。
    """
    keyword = _precondition_keyword(text)
    if not keyword or is_placeholder_precondition(keyword):
        return None
    try:
        return find_test_case(cases, keyword)
    except ValueError:
        pass  # 歧义 / 未命中 -> 走子串兜底

    best: Optional[tuple[int, TestCase]] = None
    source = text or keyword
    for candidate in cases:
        hit = max((len(title) for title in {candidate.name, *candidate.title_path}
                   if title and title in source), default=0)
        if hit and (best is None or hit > best[0]):
            best = (hit, candidate)
    return best[1] if best else None


def resolve_preconditions(
    case: TestCase = CASE,
    cases: Sequence[TestCase] = TEST_CASES,
) -> tuple[PreconditionRef, ...]:
    """解析一条用例的全部前提条件 -> PreconditionRef 元组（含脚本是否存在 / 能否直接复用）。

    每次都实时查文件系统与 ast 解析（不缓存）：前置脚本可能在同一次运行里刚被生成 / 重构出来，
    缓存会让「生成完还是 generate 状态」这种错误结论一直传到 prompt 里。
    代价是每条前提条件一次 is_file + 一次小文件解析，可以忽略。
    """
    refs: list[PreconditionRef] = []
    for raw in case.preconditions:
        if is_placeholder_precondition(raw):
            continue  # 空占位 / 「无」：不算前置用例
        matched = find_precondition_case(raw, cases)
        if matched is not None and matched.name == case.name:
            print(f"用例「{case.name}」的前提条件指向了自己（「{raw.strip()}」），已忽略")
            continue
        if matched is None:
            refs.append(PreconditionRef(text=raw.strip()))
            continue
        script_path = SCRIPTS_DIR / matched.script_name
        refs.append(PreconditionRef(
            text=raw.strip(),
            case=matched,
            script_path=script_path,
            entry=find_reusable_entry(script_path),
        ))
    return tuple(refs)


def summarize_preconditions(refs: Sequence[PreconditionRef]) -> str:
    """把前提条件解析结果压成一行（控制台进度提示用）。"""
    if not refs:
        return "前提条件：无"
    parts: list[str] = []
    for ref in refs:
        if ref.state == "reuse":
            parts.append(f"「{ref.text}」复用 {ref.script_path.name}::{ref.entry}(driver)")
        elif ref.state == "refactor":
            parts.append(f"「{ref.text}」{ref.script_path.name} 缺入口函数，需先重构")
        elif ref.state == "generate":
            parts.append(f"「{ref.text}」{ref.case.script_name} 不存在，需先生成")
        else:
            parts.append(f"「{ref.text}」脚本内自行实现")
    return "前提条件：" + "；".join(parts)


def describe_preconditions(case: TestCase = CASE,
                            cases: Sequence[TestCase] = TEST_CASES,
                            refs: Optional[Sequence[PreconditionRef]] = None) -> str:
    """把前提条件解析结果渲染成多行文本（--list-cases 用：不调大模型就能看清依赖关系）。

    refs 给了就直接渲染（调用方已经解析过，避免重复查文件系统），否则用 (case, cases) 现解析。
    """
    resolved = resolve_preconditions(case, cases) if refs is None else tuple(refs)
    if not resolved:
        return "    前提条件：无（不会给脚本补登录等任何前置操作）"
    lines = [f"    前提条件（{len(resolved)} 条）："]
    for index, ref in enumerate(resolved, start=1):
        target = ref.script_path.name if ref.script_path else "-"
        entry = f"，入口 {ref.entry}(driver)" if ref.entry else ""
        lines.append(f"        {index}. 「{ref.text}」-> {target}{entry}"
                     f"：{PRECONDITION_STATES[ref.state]}")
    return "\n".join(lines)


def force_collect() -> bool:
    """是否强制重新采集浏览器步骤（页面改版、想推倒重建脚本时用）。

    两种触发方式：`python src/web/generate_autoweb.py --force-collect`
    或 `FORCE_COLLECT=1 python src/web/generate_autoweb.py`。
    在调用时读取（而非 import 时固化成常量），便于测试里 monkeypatch 切换。
    """
    if "--force-collect" in sys.argv:
        return True
    return os.getenv("FORCE_COLLECT", "").strip().lower() in {"1", "true", "yes", "on"}


def skip_precondition() -> bool:
    """是否跳过「前置脚本自动准备」（--no-deps / SKIP_PRECONDITION=1）。

    与 force_collect 一样在**调用时**读取（不固化成 import 期常量）：便于测试里
    monkeypatch sys.argv / 环境变量来切换行为，也避免 import 顺序影响取值。
    """
    if _cli_flag(*SKIP_PRECONDITION_FLAGS):
        return True
    return os.getenv(SKIP_PRECONDITION_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def build_precondition_steps(case: TestCase = CASE,
                             refs: Optional[Sequence[PreconditionRef]] = None,
                             *,
                             cases: Sequence[TestCase] = TEST_CASES) -> str:
    """把「前提条件」引用的前置用例**展开成可照着做的具体步骤**（第一环浏览器 agent 用）。

    为什么必须展开：md 里本用例的前提条件往往只写了「首页登录」四个字，而第一环要真的
    在浏览器里把登录做完，才能走到本用例要测的页面。不给展开步骤，模型只能猜 URL / 账号
    —— 实测它会 open `http://example.com/login`，然后反过来向使用者索要登录地址，
    采集结果只剩一条无用步骤（steps_complete 判定不完整，连缓存都不写），
    第二环拿到这种垃圾步骤只能凭经验臆造选择器，生成的脚本必然跑不通。

    与 build_precondition_note 的分工（同一个「前提条件」，两个环节要的东西不同）：
        本函数                  给**浏览器 agent**：前置操作要「亲手做一遍」-> 给步骤原文；
        build_precondition_note 给**代码生成 agent**：前置操作要「import 复用」-> 给脚本路径 + 入口函数名。

    Returns:
        展开后的「前置操作」段落；本用例没有前提条件时返回空串（调用方不要塞空行）。
    """
    resolved = resolve_preconditions(case, cases) if refs is None else tuple(refs)
    if not resolved:
        return ""

    blocks: list[str] = []
    for index, ref in enumerate(resolved, start=1):
        if ref.case is None:
            blocks.append(f"""【前置 {index}】「{ref.text}」：用例文档里没有对应的独立用例，\
                按文字含义在当前浏览器里准备好即可（能用测试步骤覆盖的不要额外造数据）。""")
            continue
        # 逐行缩进两格：让展开内容与本用例正文在视觉上分开，模型不会把两者混成一份步骤
        detail = "\n".join(f"  {line}" for line in ref.case.render().splitlines())
        blocks.append(f"""【前置 {index}】「{ref.text}」-> 用例「{ref.case.name}」\
            （{ref.case.hierarchy}），它在用例文档里的原始内容如下，
              必须照做（URL / 账号 / 密码只能照抄下面的原文，禁止臆造）：
            {detail}""")
    return """前置操作（本用例「前提条件」引用到的内容，请先在当前浏览器里按顺序做完，\
        含其中的断言，再开始执行本用例的测试步骤）：
        注意：前置操作里「执行完成，退出浏览器」这类**收尾步骤一律跳过**——浏览器要留给\
        本用例的测试步骤继续使用，等本用例全部步骤（含断言）做完后再统一调用 quit。
        """ + "\n\n".join(blocks)


def build_query(case: TestCase = CASE) -> str:
    """把 md 里的一条用例渲染成第一环（操作浏览器的 agent）的任务 prompt。

    用例内容（前提条件 / 测试步骤 / 预期结果）来自 `case.render()`，本函数只补上
    「怎么操作浏览器」的执行约束——两者分开，改 md 不必动 prompt 骨架，
    改 prompt 骨架也不会影响用例内容。

    注意：这段文本同时是步骤缓存的指纹来源（见 query_fingerprint），
    因此用例内容一变，旧缓存会自动作废重采。
    """
    # 「前提条件」引用的前置用例要先在浏览器里亲手做一遍，所以这里必须把它们的
    # 步骤原文展开进 prompt（只写「首页登录」四个字，模型会去猜 URL，详见
    # build_precondition_steps）；为空时不塞空行，免得模型以为有内容没渲染出来。
    precondition_steps = build_precondition_steps(case)
    precondition_block = f"\n{precondition_steps}\n" if precondition_steps else ""
    return f"""
        你是一个自动化测试工程师，接下来需要根据测试步骤，
        每一步骤的定位前提条件都是上一步骤操作完成返回的html，
        执行测试用例 -> {case.name}（取自用例文档 {TESTCASE_FILE}，标题层级：{case.hierarchy}），
        用例内容如下:
        {case.render()}
        {precondition_block}
        执行约束（务必遵守）:
        - 严格按「测试步骤」的顺序逐步执行，不要跳步、不要自行增删步骤；
        - 「前提条件」里若引用了其它用例（如「首页登录」），必须先在当前浏览器里把这些前置操作
          全部做完（含其中的断言），再开始执行本用例的测试步骤；前提条件为「无」时不要凭空补登录；
          前置操作的内容以上面「前置操作」段落给出的步骤原文为准（URL / 账号 / 密码照抄原文，
          禁止臆造 example.com 这类占位地址）；前置操作里的「退出浏览器 / quit」一律跳过，
          浏览器要留给本用例继续使用；该段落不存在时说明本用例没有前置操作，直接从第 1 步开始；
        - 「预期结果」以及步骤里写明「断言 ...」的内容，都必须真正调用工具验证，不要只在回答里口头判断；
        - 定位表达式只能取自工具返回的 html 摘要中真实存在的标签与属性，禁止凭经验臆测类名或层级；
        - css **不支持按文本定位**：`:contains()` / `:has-text()` 是 jQuery、Playwright 的语法，
          Selenium 会直接抛 InvalidSelectorException，原样重试永远失败（实测有 agent 连试 7 次，
          把整轮采集的轮次全烧光）。需要「点击『北京市』那一行左边的展开箭头」这类按可见文本定位时，
          必须改用 xpath（css 参数以 // 开头即按 xpath 处理），把「行文本」与「目标小部件的 class」组合起来，例如
              click(css="//tr[.//td[contains(., '北京市')]]//div[contains(@class, 'el-table__expand-icon')]")
          目标小部件的 class 从 get_page_source 的摘要里找（摘要已包含 expand / arrow / switch 这类结构性元素）；
        - 同一个定位表达式失败过一次就**不要原样重试**：换一种写法（css <-> xpath）、或先 get_page_source
          确认元素是否真的存在；连续两次同样失败说明思路错了，必须改换定位方式而不是继续重试；
        - 页面跳转后、或某一步定位失败后，先调用 get_page_source 重新获取当前页面元素，再继续下一步；
          但同一页面不要连续重复调用 get_page_source：它每次都会返回一大段元素摘要，既浪费轮次也容易
          顶穿模型上下文长度；已经知道选择器时直接 find / click / send_keys，只有选择器失效时才重新取摘要；
        - 断言统一使用 assert_contains 工具，多个期望文本用「、」分隔后一次传入
          （期望有 3 项时形如 assert_contains(text="第一项、第二项、第三项")），
          需要限定断言范围时再传 css 参数（如左侧导航栏容器）；
        - 每次只输出一个 action；全部步骤执行完成后必须调用 quit 关闭浏览器，然后给出 Final Answer。
        """


# 默认用例（不带 --case 时选中的那条）的 prompt；单独 import 本模块验证时也用它。
query: str = build_query(CASE)


# ---- 步骤缓存：让「浏览器采集」这件事只发生一次 ----
# 第一环的唯一产出是「页面上真实存在、且已验证可用的 css 选择器」，代价却是把整个
# 登录流程在浏览器里真跑一遍。第二环的 write_script 只落盘、不执行（理由见
# src/utils/script_tools.py 顶部注释），所以「生成脚本」这件事本身是 0 次登录；
# 只有「脚本已存在 -> run_script 复核」「复核失败 -> 修复后再确认」才会再登录一次。
# 在测试步骤没变的前提下，探索那一遍纯属重复劳动，把采集结果落盘缓存即可吸收掉：
#     首次运行：采集(登录 1 次) + write_script 落盘(登录 0 次)
#     之后运行：跳过采集(登录 0 次) + run_script 复核已存在脚本(登录 1 次)
# 缓存放在 src/web/.steps/ 而不是 scripts/ 里：scripts 是 pytest 的收集目录，不该混进
# 非脚本产物；该目录已加入 .gitignore（缓存内容取决于线上页面的实时结构，不宜提交）。
STEPS_CACHE_DIR = Path(__file__).resolve().parent / ".steps"


def steps_cache_path(case: TestCase = CASE) -> Path:
    """某条用例的步骤缓存文件路径（按用例名区分，多条用例互不覆盖）。"""
    return STEPS_CACHE_DIR / f"{case.name}.steps.json"


def query_fingerprint(case: TestCase = CASE) -> str:
    """该用例 prompt（测试步骤）的指纹，用于判断缓存是否还对应同一份用例。

    query 一改（换 URL、换账号、改断言文本，或干脆换了一条用例），旧缓存里的选择器
    与步骤就可能失效，必须自动作废重采，否则会拿旧步骤去生成新用例的脚本。
    """
    return hashlib.sha256(build_query(case).strip().encode("utf-8")).hexdigest()[:16]


def steps_complete(steps_info: object) -> bool:
    """采集结果是否完整到值得缓存 / 复用。

    四个条件缺一不可：
      1. 非空列表，且不含 error 记录 —— 带异常的部分步骤（如迭代次数用尽、模型输入超长）
         会误导代码生成；
      2. 至少有一次 open —— 没打开过页面就谈不上「页面上真实存在的选择器」；
      3. 至少有一次**成功的**交互或断言（send_keys / click / assert_contains 且没标 failed）
         —— 只 open 不操作、或每次操作都失败，说明采集中途就断了；
      4. 调用过 quit —— build_query 明确要求「全部步骤执行完成后必须调用 quit 关闭浏览器」，
         没有 quit 说明 agent 是**半路放弃**的（选择器一直定位不到，把 max_iterations 烧光后
         随便给个 Final Answer 收场）。实测「行政区域 / 区域名称」就是这样：25 步里
         13 步是 `td:contains('北京市') + td button` 这种非法 css 的重复失败，既没真点开
         展开箭头、也没有最终断言，却因为「有 open + 有 click」被判为完整、缓存了下来，
         第二环照着它生成脚本，脚本一执行必然 InvalidSelectorException / TimeoutException。
    """
    if not isinstance(steps_info, list) or not steps_info:
        return False
    if not all(isinstance(step, dict) for step in steps_info):
        return False
    if any("error" in step for step in steps_info):
        return False
    used = {step.get("tool") for step in steps_info}
    if "open" not in used or "quit" not in used:
        return False
    return any(step.get("tool") in {"send_keys", "click", "assert_contains"}
               and "failed" not in step for step in steps_info)


def parse_steps(steps_json: str) -> Optional[list]:
    """把步骤 json 解析成 list；不是 json（例如 skip 分支给的说明文字）时返回 None。"""
    if not steps_json or not steps_json.strip():
        return None
    try:
        parsed = json.loads(steps_json)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def first_step_error(steps_json: str) -> Optional[str]:
    """采集结果里的第一条 error 记录（有则说明第一环中途炸了）；没有 error 返回 None。

    与 steps_complete 的分工：steps_complete 回答「这份步骤能不能拿去生成脚本」，
    本函数回答「第一环是不是**失败了**」—— 失败时必须让第二环直接停手，理由见下。

    为什么不能把带 error 的半截结果交给第二环：`collect_steps_in_browser` 只在
    `web_agent_executor.invoke` 抛异常时才追加 error 记录（迭代次数用尽、模型输入超长
    `Range of input length should be [1, 30720]`、输出不可解析……），此时
    intermediate_steps 拿不到，steps 往往只剩 `[{"error": ...}]`，一个真实选择器都没有。
    实测模型拿到它会一边说「缺少 CSS 选择器」一边照样 write_script 落一份满是
    `pass` / 「待补充」的占位脚本；而脚本一旦存在，后续运行就走
    「已存在 -> 跳过采集 -> 只验证修复」的分支，占位脚本再也不会被真实步骤替换掉
    （除非 --force-collect 或手工删）—— 静默降级比明确失败更难排查。
    """
    parsed = parse_steps(steps_json)
    if parsed is None:
        return None  # 不是 json（例如调用方直接传了说明文字），交给下游按原样处理
    for step in parsed:
        if isinstance(step, dict) and step.get("error"):
            return str(step["error"])
    return None


def steps_blocking_reason(steps_json: str) -> Optional[str]:
    """第二环开工前对步骤 json 的准入检查：返回 None 表示可以拿去生成脚本。

    两类情况必须拦下（都不写任何文件，直接返回结论让人去修采集）：
      1. 采集中途炸了（步骤里带 error 记录）—— 见 first_step_error 的说明；
      2. 采集半路放弃（步骤不完整：没 open / 没 quit / 没有一次成功的交互）——
         这种步骤里往往全是失败尝试（非法选择器反复重试），照着生成的脚本必然跑不通；
         而脚本一旦落盘，后续运行会走「已存在 -> 跳过采集 -> 只验证修复」的分支，
         再也不会被真实步骤替换掉（除非 --force-collect 或手工删）。
    """
    error = first_step_error(steps_json)
    if error:
        return f"浏览器步骤采集失败：{error}"
    parsed = parse_steps(steps_json)
    if parsed is None:
        return None  # 不是步骤 json（skip 分支的说明文字等），交给下游按原样处理
    if not steps_complete(parsed):
        failed = sum(1 for step in parsed if isinstance(step, dict) and step.get("failed"))
        return f"""浏览器步骤采集不完整（共 {len(parsed)} 步，其中 {failed} 步当时就没跑通）：\
        缺少 open / quit，或没有任何一次成功的交互与断言，说明 agent 半路放弃了"""
    return None


def load_cached_steps(case: TestCase = CASE) -> str | None:
    """读取可复用的步骤缓存（json 字符串）；不可用时返回 None，由调用方去真采集。

    缓存只是优化、不是主流程依赖，所以任何异常都在此降级成「缓存不可用」，
    绝不冒泡终止 chain（与 selenium_tools / script_tools 的约定一致）。
    """
    path = steps_cache_path(case)
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
    if payload.get("query_fingerprint") != query_fingerprint(case):
        print(f"用例「{case.name}」的测试步骤（query）已变更，步骤缓存作废，将重新采集：{path}")
        return None
    steps = payload.get("steps")
    if not steps_complete(steps):
        print(f"步骤缓存内容不完整，将重新采集：{path}")
        return None
    return json.dumps(steps, ensure_ascii=False)


def save_steps_cache(steps_info: list[dict], case: TestCase = CASE) -> None:
    """把本次真实采集到的步骤写入缓存，供后续运行复用（不再重复登录采集）。

    写失败只打印不抛：缓存缺失最多让下一次运行重新采集一遍，不影响本次结果。
    注意「采集不完整就不写」：半截步骤（带 error）一旦落盘，后续运行会拿它生成
    缺胳膊少腿的脚本，比每次重采更难排查。
    """
    if not steps_complete(steps_info):
        print("本次采集结果不完整，不写入步骤缓存（下次运行会重新采集）")
        return
    path = steps_cache_path(case)
    payload = {
        "case_name": case.name,
        "script_name": case.script_name,
        "testcase_file": str(TESTCASE_FILE),
        "query_fingerprint": query_fingerprint(case),
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
    print(f"""已缓存本次采集的 {len(steps_info)} 个步骤：{path}\
    （此后运行不再打开浏览器重复执行「{case.name}」采集）""")


def resolve_steps(case: TestCase = CASE) -> tuple[str, str]:
    """决定第一环本轮的产出，返回 (steps_json, source)。

    source 取值（按代价从低到高）：
        "skip"    目标脚本已存在：步骤 json 用不上，第二环直接跑现有脚本；
        "cache"   命中步骤缓存：用上次采集到的真实步骤生成脚本，本轮不打开浏览器；
        "browser" 前两者都不成立：必须真的采集一次（本轮唯一的探索性登录）。

    只有 "browser" 会触发第一环的页面探索；而第二环的 write_script 只落盘、不执行脚本，
    所以「脚本不存在」的那一轮全程只登录 1 次（就是采集那一次）。
    脚本已存在时（"skip"）本轮不采集，那一次登录发生在 run_script 复核既有脚本时。
    """
    if force_collect():
        return "", "browser"
    if target_script_exists(case):
        return "[]", "skip"
    cached = load_cached_steps(case)
    if cached is not None:
        return cached, "cache"
    return "", "browser"


def collect_steps_in_browser(case: TestCase = CASE) -> str:
    """真的打开浏览器跑一遍测试步骤，采集每一步的工具名与入参（含真实可用的 css 选择器）。

    这是整个流程里唯一一次「探索性执行」：采集成功即写入步骤缓存，后续运行直接复用，
    不再为了同一份测试步骤反复登录。

    步骤由 StepRecorder 回调**边跑边记**，而不是从返回值的 intermediate_steps 里取：
    invoke 抛异常（迭代用尽 / 模型输入超长 / 输出不可解析）时拿不到返回值，
    已跑过的真实步骤会全丢，只剩一条 error，第二环就只能凭空臆造选择器。
    异常路径下回调里至少还留着「跑到哪一步、用过哪些选择器」，排查与重采都有依据。

    这里必须自己兜异常：agent 内部工具虽然已把 selenium 异常降级成 Observation，
    但仍可能因为迭代次数用尽、模型返回不可解析等原因抛错；一旦异常冒泡，
    整个 chain 会直接以 exit code 1 结束，浏览器也不会被关闭。
    """
    recorder = StepRecorder(frozenset(item.name for item in tools))
    # run_config 里已有 tracer handler（--quiet 时是 None）；把 StepRecorder 追加进同一次
    # invoke 的 config：既保留 [llm/*] / [tool/*] 日志，又能**边跑边记**工具调用。
    config: RunnableConfig = {
        **run_config,
        "callbacks": [*(run_config.get("callbacks") or []), recorder],
    }
    result: Optional[dict] = None
    error = ""
    try:
        # 获取执行结果（config 必须传：回调是**可继承**的，构造参数走 local_callbacks，
        # 嵌套的 llm / tool run 收不到，日志一条都不会打印）
        result = web_agent_executor.invoke({"input": build_query(case)}, config=config)
    except Exception as exc:  # noqa: BLE001 - 保证外层 chain 与浏览器清理仍能继续
        error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        print(f"web agent 执行异常，保留回调已记录的 {len(recorder.steps)} 步部分结果：{error}")
    finally:
        # 兜底关闭浏览器，避免异常路径下 chrome / chromedriver 进程泄漏
        web.quit()

    # 步骤以回调记录为准：它是异常路径下唯一还完整的来源（invoke 抛错时拿不到返回值）。
    # 回调一条都没记到时回落到 intermediate_steps，兼容换 executor 实现的情况。
    steps_info: list[dict] = list(recorder.steps)
    if not steps_info and isinstance(result, dict):
        steps_info = [{'tool': action.tool, 'input': action.tool_input}
                      for action, _ in result.get("intermediate_steps", [])
                      if isinstance(action, AgentAction) and action.tool in recorder.tool_names]

    if error:
        steps_info.append({'error': error})

    # 失败尝试也一并打印（步骤 json 里带 failed 字段）：它们不会进第二环的代码，
    # 但人排查时最需要知道「agent 在哪个选择器上反复摔跤」
    failed = [step for step in steps_info if isinstance(step, dict) and step.get("failed")]
    if failed:
        print(f"本次采集有 {len(failed)} 步没跑通（已标 failed，第二环禁止照抄其选择器）：")
        for step in failed:
            print(f"  - {step.get('tool')} {json.dumps(step.get('input'), ensure_ascii=False)[:120]}"
                  f" => {step.get('failed')}")

    # 是否真的落盘由 steps_complete 判定：带 error / 半路放弃的步骤绝不缓存
    save_steps_cache(steps_info, case)

    print(f'获取到的每一步的测试步骤以及输入信息: \n{steps_info}')
    return json.dumps(steps_info, ensure_ascii=False)


def web_execute_result(inputs: dict) -> str:
    """第一环入口：按「脚本已存在 / 命中步骤缓存 / 需要真采集」三种情况给出步骤 json。

    只有第三种情况会打开浏览器，前两种都是 0 次登录；第二环 write_script 只落盘不执行，
    因此也不会叠加成「一次运行登录两遍」（只有脚本已存在时的 run_script 复核会登录一次）。

    用例取自 inputs["case"]（--all-cases 时由 main() 逐条传入），
    缺省回落到模块级 CASE，所以单独 import 本模块调用也是可用的。
    """
    case = _case_of(inputs)
    steps, source = resolve_steps(case)
    if source == "skip":
        print(f"""目标脚本已存在：{target_script_path(case)}，跳过浏览器步骤采集\
            （避免与第二环 run_script 复核重复登录一次；需要重采请加 --force-collect）""")
        return steps
    if source == "cache":
        print(f"""命中步骤缓存：{steps_cache_path(case)}，本轮不打开浏览器重复执行\
            用例「{case.name}」的前置探索\
            （页面改版导致选择器失效时，加 --force-collect 重新采集）""")
        return steps
    print(f"""目标脚本与步骤缓存都不存在，本轮打开浏览器采集一次真实步骤\
        （探索性执行用例「{case.name}」）""")
    return collect_steps_in_browser(case)


# ---- 第二环：代码生成 agent（带文件系统工具，负责落盘 / 执行 / 修复）----
# 历史坑 1：这一环曾是 `prompt | llm | StrOutputParser()` 的纯文本调用，llm 没有绑定
#   任何工具，「保存到 scripts 文件夹下」只是 prompt 里的一句空话——模型只能把代码
#   当字符串吐回来，被 print 到控制台就丢弃了，src/web/scripts/ 里始终只有一个空
#   __init__.py；「查找是否存在 -> 存在则执行 -> 失败则修复」更无从谈起。
# 历史坑 2：改成 structured chat agent 后仍然写不出文件——它要求模型把 action_input
#   以 JSON 文本形式输出，而 write_script 的 code 参数是**多行代码**，模型会在 JSON
#   字符串里直接敲裸换行，产生非法 JSON，实测连续 7 次 OUTPUT_PARSING_FAILURE，
#   handle_parsing_errors 只能把它变成 Observation，模型下一轮仍犯同样的错。
# 因此第二环改用原生 function calling（ChatTongyi 支持 bind_tools）：工具入参由模型侧
#   按 JSON Schema 生成，多行代码作为字符串参数可正确传递。
# 历史坑 3（本次新增修复）：function calling 也**不是**万无一失——生成的 Python 代码里
#   单引号很多（xpath 的 contains(., '北京市')），Qwen 会按 Python 的习惯把它们转义成
#   `\'`，而 JSON 里 `\'` 是**非法转义**，langchain 的 parse_ai_message_to_tool_action
#   直接抛 OutputParserException("Could not parse tool input")；handle_parsing_errors
#   把它变成 Observation 后，模型下一轮还是同样的写法，实测连撞 39 次、把 15 轮
#   max_iterations 全烧光，脚本一个字都没落盘。与其指望模型改习惯，不如在解析前
#   把这类**可机械修复**的坏转义修掉（见 repair_json_arguments）。
# 第一环仍保留 structured chat（其入参都是短字符串，且已验证可用）。
# 上面提到的三类坏转义 / 裸双引号的**机械修复**已抽到 src/utils/json_repair.py：
# web 与 app 两个生成域共用同一份实现（app 版早先自己写过一个只扫到第一个换行的
# _repair_illegal_tool_args，对多行 code 永远返回原文，等于兜底形同虚设 —— 抽出来
# 正是为了不再出现这种「一个域修好了、另一个域还留在老坑里」的漂移）。

codegen_prompt = pull_prompt("hwchase17/openai-tools-agent")
# 与 create_openai_tools_agent 等价的组装，只是在 llm 与解析器之间插了一步
# repair_tool_call_arguments（历史坑 3）；官方工厂函数没有留解析器/中间步骤的注入点。
codegen_agent = (
    RunnablePassthrough.assign(
        agent_scratchpad=lambda x: format_to_openai_tool_messages(x["intermediate_steps"]),
    )
    | codegen_prompt
    | llm.bind(tools=[convert_to_openai_tool(tool) for tool in script_tools])
    | RunnableLambda(repair_tool_call_arguments)
    | OpenAIToolsAgentOutputParser()
)
codegen_executor = AgentExecutor(
    agent=codegen_agent, tools=script_tools,
    # 同上：verbose 的「Invoking: `write_script` with ...」会把整段生成代码打一遍
    # （[tool/start] 里也会有一份，两份内容一致、属预期）；嫌长可只关这一类日志：
    #     --hide-debug-events=tool/start   或   --quiet 全关
    verbose=DEBUG_LOGGING,
    # 回调同样只走 invoke(config=run_config)，见 run_config 处的说明
    # 一轮「生成 -> 落盘」只需 2~3 个 action（list_scripts -> write_script -> Final Answer）；
    # write_script 不执行脚本，复核 / 修复场景才会多用几轮，15 轮足够跑完 2 轮修复
    max_iterations=15,
    handle_parsing_errors=True)

# 代码规范 + 工具使用流程。原来这些要求写在 PromptTemplate 里，模型没有工具只能
# 「口头答应」；现在作为 agent 的任务指令，每一条都有对应工具可以真正执行。
# 注意：本模板用 str.format 渲染，除下面 7 个占位符（{task} / {case} / {testcase_file}
# / {script_name} / {scripts_dir} / {step} / {precondition}）外不要再出现花括号。
# {precondition} 由 build_precondition_note 渲染：把「前提条件」逐条翻译成
# 「直接 import 复用已有前置脚本 / 先补入口函数 / 先重新生成前置脚本 / 脚本内自行实现」
# 四种可执行指令，这是「登录只写一次、其它用例都复用」能落地的关键。
CODEGEN_TASK = """
    你是一个web自动化测试工程师，主要应用的技术栈为pytest + selenium。
    你的任务：把下面这次真实执行过的测试步骤，落成一个可重复运行的自动化测试脚本。
    
    {task}
    
    本条用例在用例文档 {testcase_file} 里的原始描述（前提条件 / 测试步骤 / 预期结果）：
    {case}
    
    目标脚本：{scripts_dir} 目录下的 {script_name}
    
    {precondition}
    
    本次真实执行过的测试步骤（json 数组，tool 是工具名，input 是工具入参，
    其中的 css 是当时页面上真实出现过的选择器，**没有额外字段的步骤都已验证可用**；
    带 `"failed": "原因"` 的步骤是当时没跑通的尝试，其 css 值禁止照抄）；
    如果下面给出的不是步骤 json，而是一段「本轮未采集步骤」的说明，则以该说明为准：
    {step}
    
    必须严格按以下流程使用工具，不要臆测文件是否存在：
    1. 先调用 list_scripts，确认 {script_name} 是否已经存在；
    2. 已存在（说明本轮没有重新采集步骤）：调用 read_script 读取内容，再调用 run_script
       执行验证；执行通过就不必重写；
    3. 不存在：按下面的「代码规范」生成完整脚本，调用 write_script 保存，**保存完就结束** ——
       write_script 只做语法检查 + 落盘，不会执行脚本；保存之后也**不要**再调用 run_script 去跑它。
       原因：本轮的步骤 json 就是第一环在真实浏览器里把这条用例（含断言）完整跑通后记录下来的，
       脚本里每个选择器都刚刚验证过；落盘后再执行一遍等于把同一条用例重复跑一次
       （多登录一次被测站点、多烧一轮 LLM），信息量几乎为零，还可能因为环境抖动
       把本来正确的脚本误判成有问题；
       实测还踩过：模型把 run_script 的 file_name 抄错一个字（行政区域_区域名称.py 抄成
       行政区域_地区名称.py），白跑一轮只拿到「脚本不存在」；
    4. 只有下面两种情况才需要处理失败，且都要按工具返回的提示区分类型：
       a) write_script 回执报「代码语法检查未通过」-> 修正代码后重新 write_script；
       b) 第 2 步用 run_script 复核**已存在**的脚本时失败 -> 脚本步骤失败（定位/超时/语法/导入错误）
          必须 read_script 后用 write_script 写入修复后的完整代码，再 run_script 确认，最多修复 2 轮；
       若同一个原因连续失败两次，说明是环境/被测站点问题，立即停止修复并在 Final Answer 中说明，
       不要重复写入内容相同的代码；断言失败说明脚本本身跑得通，不要改脚本；
       若是「元素定位不到 / 等待超时」连续失败两次，很可能是页面结构改版、上面的步骤已过期，
       此时必须在 Final Answer 中提示：用 `python src/web/generate_autoweb.py --force-collect`
       重新采集步骤（不要自己臆测新的 css 选择器）；
    5. 结束后给出 Final Answer，说明脚本绝对路径、执行结论（本轮新生成的脚本写「已按第一环
       真实执行过的步骤落盘，未重复执行」；复核已有脚本则写 通过 / 断言失败 / 修复了几轮）与关键改动。

    代码规范：
    - 用 pytest 组织：driver 放在 fixture 里，yield 之后 driver.quit()，保证异常路径也能关浏览器；
      测试函数必须以 test_ 开头，否则 pytest 收集不到用例；
    - 步骤 json 末尾那次 `quit` 是采集结束时关浏览器用的，脚本里**一律不要写** driver.quit()：
      driver 的生命周期只由 fixture 管（yield 之后已经 quit 过）。实测踩过：业务函数结尾多写一句
      driver.quit()，fixture 收尾再 quit 一次会抛 InvalidSessionIdException，把本来跑通的用例判成失败；
      更糟的是这个函数还要给别的脚本 import 复用（如 login(driver)），提前 quit 会把调用方的
      driver 一起关掉，后续步骤全部定位不到元素；
    - 脚本自身也要能被**后续用例复用**：把业务主流程（含断言）抽成一个不带 `test_` 前缀、
      不带 fixture 装饰器的函数，第一个参数固定为 driver，名字用语义化英文（登录用例就叫 `login`），
      `test_xxx(driver)` 只保留一行调用它。这样别的用例脚本才能直接
      `from src.web.scripts.<脚本名> import login` 复用，而不是把这段流程再抄一遍；
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
    - 定位表达式必须原样照抄步骤 json 里 css 字段的值，用双引号包裹即可（如 "li[role='menuitem']"）；
      禁止改写成等价形式，尤其禁止 find_elements(By.TAG_NAME, 'li') 再靠
      get_attribute('attributes') 过滤——该属性在 Selenium 中恒为 None，且登录页没有 li 会等到超时；
    - css 字段里的值**可能是 xpath**（采集时按文本定位只能用 xpath）：以 `//`、`./`、`(` 开头的
      必须用 By.XPATH，其余用 By.CSS_SELECTOR。把 xpath 塞进 By.CSS_SELECTOR、或把 css 塞进
      By.XPATH，运行时会直接抛 InvalidSelectorException（本仓库已踩过：
      `td:contains('北京市') + td button` 被当成 css 传进 Selenium）。稳妥写法是按前缀分流：
          locator = "//tr[.//td[contains(., '北京市')]]//div[contains(@class, 'el-table__expand-icon')]"
          by = By.XPATH if locator.startswith(("//", "./", "(")) else By.CSS_SELECTOR
      也可以直接用框架里现成的判定：`from src.web.web_framework import by_of`（by_of(locator)）；
    - 步骤 json 里带 `"failed"` 字段的步骤，是采集当时**没有跑通**的尝试（选择器非法 / 元素不存在 /
      断言不通过），它的 css 值**禁止照抄**进脚本；这类步骤要么改用同一环节里其它成功步骤的表达式，
      要么在 Final Answer 里如实说明「该步缺少可用选择器，需要 --force-collect 重新采集」，
      不要自己臆造一个看起来合理的 xpath / css；
    - 启动浏览器必须显式指定本地驱动，否则会触发 Selenium Manager 联网下载驱动、卡到执行超时：
          import pytest
          from selenium import webdriver
          from selenium.webdriver.chrome.service import Service
          from src.web.web_framework import resolve_chromedriver
          driver_path = resolve_chromedriver()
          driver = webdriver.Chrome(service=Service(driver_path)) if driver_path else webdriver.Chrome()
      `from selenium import webdriver` 这一行**必须写**：实测两次生成都只 import 了 Service
      与 resolve_chromedriver、漏掉 webdriver 本身，fixture 里 webdriver.Chrome(...) 直接
      NameError，白白多花一轮修复；
      禁止使用 webdriver.Chrome(executable_path=...)——Selenium 4 已移除该参数，会直接 TypeError；
      也不要 import 了 resolve_chromedriver / by_of 却不使用；
    - 元素定位的表达式只能取自上面步骤 json 中真实出现过、且**没有标 failed** 的 css 值，
      禁止凭经验臆测类名或层级；
    - 步骤 json 里的步骤要**按原顺序逐条**落进脚本，禁止合并、禁止只保留其中一条：
      用例文档里的一个「测试步骤」在真实页面上常常要连续点好几次（例如「点击左侧导航栏
      商场管理 -> 行政区域」= 先点父级菜单 li[role='menuitem'].el-submenu 把子菜单展开，
      再点子菜单 a[href='#/mall/region'] 才真正跳转），两次的 css 都在步骤 json 里，就都要写。
      实测踩过：模型只保留了第一次 click，页面停在 dashboard 没跳转，后面
      //tr[.//td[contains(., '北京市')]] 一直等到超时，自动验证的失败点看起来像「选择器过期」，
      很容易把人误导去 --force-collect 重采（其实选择器是对的，是少点了一次）；
    - 脚本必须覆盖上面用例描述里的**全部**「测试步骤」，一条都不能漏；「前提条件」里的前置用例
      按上面给出的处理方式执行 —— 能 import 复用就只调用它的入口函数（如 `login(driver)`），
      **不要**在本脚本里把前置流程重写一遍；断言必须与「预期结果」逐条对应；
      步骤 json 与用例描述不一致时以用例描述为准，
      但要如实说明哪一步缺少可用的选择器，不要为此臆造选择器；
    - 步骤 json 里 assert_contains 的 text 参数是「用、或 , 分隔的多个期望文本」
      （框架按 [,，、|;；\\n]+ 切分后逐项判断），生成代码时必须拆成同样多个**独立**断言，
      不要把整串当成一个文本来匹配 —— 实测踩过：text="省,110000" 被写成
      `assert '省,110000' in row.text`，而页面上该行的文本是 '北京市\\n省\\n110000'（换行分隔），
      必然 AssertionError；正确写法是先归一再逐项断言：
          row_text = row_element.text.replace("\\n", "")
          assert '省' in row_text, f'缺少 省'
          assert '110000' in row_text, f'缺少 110000'
      css 参数（若存在）表示断言范围的选择器，该选择器通常匹配**多个**元素：必须用
      driver.find_elements（复数）取全部元素，**先把它们的 text 聚合成一个字符串，
      再对每个期望文本各断言一次**；用 find_element（单数）只会拿到第一个元素，
      必然出现 assert '商场管理' in '首页' 这种假失败，这属于脚本缺陷、必须修，
      不要当成被测系统的问题。正确写法（先聚合、再逐项断言）：
          rows = driver.find_elements(by_of(locator), locator)
          assert rows, '断言范围内一个元素都没匹配到：' + locator
          aggregated = ''.join(row.text.replace("\\n", "") for row in rows)
          assert '市' in aggregated, '缺少 市'
          assert '110100' in aggregated, '缺少 110100'
      **禁止**写成「对每个匹配元素逐个断言」（for row in rows: assert '市' in row.text）：
      element-ui 的固定列会把同一行再渲染一份副本，副本里的 text 常常是空串，
      循环到它就变成 assert '市' in '' 直接失败。实测踩过：
      `//tr[.//td[contains(., '市辖区')]]` 匹配到 2 个 tr（正文表格 + 固定列副本），
      逐个断言时第二个的 text 为空，脚本报 AssertionError: 缺少 市 —— 页面数据其实是对的，
      纯属脚本写法缺陷，必须改成聚合后断言；
    - f-string 里的变量占位符只写一层花括号（写成 f"缺少 {{{{text}}}}" 是错的，应写 f"缺少 {{text}}"）；
    - code 参数是以 JSON 字符串传的：**代码里的字符串字面量内部不要再出现双引号**
      （如 f'未找到含有"东城区"的文本'），需要强调就用中文引号「东城区」或干脆不加引号，
      否则 JSON 字符串会被提前截断、整轮迭代白白浪费在入参解析失败上；
      定位表达式本身用双引号包裹是安全的（如 "//tr[.//td[contains(., '北京市')]]"，里面只有单引号）；
    - 调用 write_script 时 code 参数必须是完整可运行的 Python 代码：不要 markdown 围栏、不要解释文字；
      write_script 只做语法检查 + 落盘、**不会执行脚本**，本轮新生成的脚本保存后也不要再调用
      run_script 重复执行（这些步骤刚刚已在真实浏览器里跑通过一遍）。
"""

# 第一环跳过采集时（脚本已存在），填进 CODEGEN_TASK 里 {step} 位置的替代说明。
# 不能直接把空数组 `[]` 丢给模型：模板里「css 选择器必须原样照抄步骤 json」等约束
# 会失去依据，模型很可能凭经验重写脚本 —— 重写后照样要跑一遍登录，等于白折腾一轮，
# 又退回到「重复执行前置流程」的老问题。这里把本轮任务明确收窄成「验证 + 按需修复」。
# 脚本名 / 用例名都从 case 派生（不同用例的提示不能互相串味），故做成函数。
def build_no_steps_note(case: TestCase = CASE) -> str:
    """「本轮未采集步骤」的替代说明（按用例渲染）。"""
    return f"""（本轮未采集浏览器步骤：目标脚本 {case.script_name} 已存在，第一环被主动跳过，\
    目的就是不让同一次运行里重复执行一遍「{case.name}」的前置流程。）
    因此本轮只做「验证 + 按需修复」，请以现有脚本为唯一事实来源：
    - 先 list_scripts 确认，再 read_script 读出现有内容，然后 run_script 执行验证；
    - 执行通过、或只是断言失败：直接给出 Final Answer，**不要**调用 write_script；
    - 仅当出现脚本步骤失败（定位/超时/语法/导入错误）时，才在现有代码基础上做最小化修复，\
    用 write_script 写回完整代码后再 run_script 确认修复生效，最多 2 轮\
    （本轮没有重新采集步骤，这次执行属于必要复核，不算重复执行）；
    - 本轮没有步骤 json，禁止凭空生成新脚本、禁止臆测或改写现有 css 选择器，\
    也不要为了「补采集」而重复执行登录流程。"""


# 默认用例的那份说明；沿用旧名字，方便既有代码 / 笔记引用。
NO_STEPS_NOTE: str = build_no_steps_note(CASE)


def build_precondition_note(case: TestCase = CASE,
                            refs: Optional[Sequence[PreconditionRef]] = None,
                            *,
                            cases: Sequence[TestCase] = TEST_CASES) -> str:
    """渲染 CODEGEN_TASK 里 `{precondition}` 的内容：本用例的前提条件该怎么落到代码上。

    与 ensure_precondition_scripts 的分工：
        ensure_*  在**开跑前**把前置脚本准备成「可直接 import 复用」的状态（能自动做的都自动做掉）；
        本函数    只负责把结论**告诉模型**，并对每种状态给出可直接照抄的写法约束。
    两者分开的好处：即使加了 --no-deps、或有人 `from src.web.generate_autoweb import
    persist_and_verify` 单独调用（前置脚本压根没准备），prompt 里仍然带着
    「脚本不存在就先生成、缺入口函数就先补上」的兜底指令，不会退化成把登录重写一遍。
    """
    resolved = resolve_preconditions(case, cases) if refs is None else tuple(refs)
    if not resolved:
        return """本用例的「前提条件」为空：脚本里**不要**补任何前置操作（尤其是登录），\
直接从测试步骤的第 1 步开始写。"""

    lines = ["本用例「前提条件」里引用到的前置用例，按下面逐条处理（能复用已有脚本就绝不重写）："]
    for index, ref in enumerate(resolved, start=1):
        if ref.state == "reuse":
            lines.extend([
                f"""{index}. 「{ref.text}」-> 前置脚本已存在：`{ref.script_path}`，\
                可复用入口函数 `{ref.entry}(driver)`，必须直接复用：""",
                f"   - 在本用例脚本顶部写 `{ref.import_line}`（中文模块名在 Python 3 是合法的，照抄即可）；",
                f"   - 在 driver fixture 之后、本用例第 1 步测试步骤之前调用 `{ref.call_line}`；",
                """   - **严禁**把前置流程（打开登录页 / 输入账号密码 / 点击登录 / 等跳转）在本脚本里重写一遍，\
                也不要复制它的选择器、等待逻辑与账号常量；""",
                """   - 步骤 json 里属于该前置用例的那几步（通常是开头的登录步骤）由这行调用整体覆盖，\
                不要再把它们逐条落成代码；本脚本只写前置完成**之后**的步骤与断言。""",
            ])
        elif ref.state == "refactor":
            lines.extend([
                f"""{index}. 「{ref.text}」-> 前置脚本已存在：`{ref.script_path}`，\
                但还没有可复用的入口函数（只有一个 test_* 函数）：""",
                f"""   - 先 read_script 读出它，把业务流程（含断言）原样抽成一个不带 `test_` 前缀、\
                不带 fixture 装饰器的函数，第一个参数是 driver，名字语义化（登录流程就叫 `login`）；\
                原来的 test_* 函数只保留一行调用；driver fixture / import / 选择器 / 等待**全部保持原样**；""",
                f"""   - write_script 覆盖写回**同名文件** `{ref.script_path.name}`，\
                再 run_script 确认它仍然通过（重构改的是既有脚本、本轮没有真实执行过它，\
                这次验证是必要的）；""",
                f"""   - 然后在本用例脚本里写 `from {ref.module_name} import <入口函数名>` 并调用它\
                （入口函数名就是上一步抽出来的那个，如 `login`），同样禁止重写前置流程。""",
            ])
        elif ref.state == "generate":
            lines.extend([
                f"{index}. 「{ref.text}」-> 前置脚本 `{ref.case.script_name}` 在 `{SCRIPTS_DIR}` 下不存在：",
                f"""   - 先按前置用例「{ref.case.name}」（{ref.case.hierarchy}）的测试步骤生成 \
                `{ref.case.script_name}`，同样要带可复用入口函数（如 `login(driver)`），\
                write_script 保存即可（它的第一环已在浏览器里真实跑通，不要再 run_script 重复执行）；""",
                f"""   - 再在本用例脚本里 import `{ref.module_name}` 的入口函数复用，\
                禁止把前置流程抄进本脚本。""",
            ])
        else:  # inline：文档里没有对应的独立用例，只是环境 / 数据描述
            lines.extend([
                f"{index}. 「{ref.text}」-> 用例文档里没有对应的独立用例（属于环境 / 数据准备描述）：",
                """   - 按文字含义在本用例脚本内自行实现；能用测试步骤覆盖的就不要额外造数据，\
                也不要为它单独生成脚本文件。""",
            ])
    return "\n".join(lines)


def persist_and_verify(inputs: dict) -> str:
    """把第一环收集到的真实步骤交给代码生成 agent：生成 -> 落盘 ->（按需）执行 -> 失败则修复。

    write_script 只做「语法自检 -> 落盘」，**不会执行脚本**（理由见 src/utils/script_tools.py
    顶部注释）：本轮步骤已在真实浏览器里完整跑通过一遍（含断言），落盘后再执行一次就是把
    同一条用例重复跑一遍。只有「脚本已存在时的复核」与「修复之后的确认」这两种
    本轮没有真跑过用例的情况，才由 agent 显式调用 run_script。

    第一环跳过采集时（脚本已存在，step 为空数组）改用 build_no_steps_note(case)，
    让 agent 只「跑现有脚本 + 按需修复」，不再重写脚本。

    用例（脚本名 / 测试步骤 / 预期结果）取自 inputs["case"]，缺省用模块级 CASE，
    因此 --all-cases 顺序跑多条用例时，每一环看到的都是同一条用例。

    与 web_execute_result 同理，这里必须自己兜异常：agent 仍可能因迭代次数用尽、
    模型输出不可解析等原因抛错，一旦冒泡整个 chain 会以 exit code 1 结束。
    """
    case = _case_of(inputs)
    step = (inputs.get("step") or "").strip()
    # 第一环的产出不可用时直接停手，不把半截步骤交给代码生成 agent：原因与后果见
    # steps_blocking_reason / first_step_error 的说明。这里返回结论文本而不是抛异常，
    # 与本模块「任何一环都不让异常冒泡打断 chain」的约定一致。
    # 只在**真的采集到了步骤**时才做准入检查：第一环「跳过采集」分支（目标脚本已存在）
    # 给出的也是 "[]"，那种情况本轮只做「验证 + 按需修复」，没有步骤可校验，必须放行。
    collect_error = steps_blocking_reason(step) if parse_steps(step) else None
    if collect_error:
        retry = f"python src/web/generate_autoweb.py --case {case.name} --force-collect"
        print(f"第一环步骤采集结果不可用，本轮跳过代码生成（不落占位脚本）：{collect_error}")
        print(f"排查/修复采集失败的原因后重跑：{retry}")
        return f"""脚本未生成：{collect_error}。
        为避免落下只有 pass / 「待补充」的占位脚本（脚本一旦存在，后续运行就会走\
        「已存在 -> 跳过采集 -> 只验证修复」的分支，占位脚本不会被真实步骤替换掉），\
        本轮不调用代码生成 agent，也不写任何文件。
        重跑命令：{retry}"""
    if step in ("", "[]", "{}"):
        step = build_no_steps_note(case)
    # 前提条件（前置用例）实时解析一次：结果既渲染进 prompt（{precondition}），
    # 也打印一行摘要，让人一眼看清「本用例复用了哪个前置脚本 / 还差哪个」。
    # 正常情况下 main() 已经用 ensure_precondition_scripts 把前置脚本准备好了，
    # 这里看到的应该是 reuse；单独 import 本函数调用（或加了 --no-deps）时可能是
    # generate / refactor，prompt 里对应的兜底指令会让 agent 自己先生成 / 先补入口。
    refs = resolve_preconditions(case)
    print(f"目标脚本：{target_script_path(case)}（{'已存在' if target_script_exists(case) else '待生成'}）")
    print(summarize_preconditions(refs))
    task = CODEGEN_TASK.format(
        task=inputs.get("input", ""),
        case=case.render(),
        testcase_file=TESTCASE_FILE,
        step=step,
        script_name=case.script_name,
        scripts_dir=SCRIPTS_DIR,
        precondition=build_precondition_note(case, refs),
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


# ---- 前置脚本的「入口函数」补全：确定性重构，不走 LLM ----
# 场景：`src/web/scripts/首页登录.py` 这类**老脚本**只有一个 `test_login(driver)`，
# 别的用例脚本没法 import 复用（import test_login 再调用虽然也能跑，但语义上是
# 「调用一条 pytest 用例」，而且以后想给它加参数/返回值都别扭）。
# 这一步只是「改名 + 加一层转发」，用 ast 精确定位、按行替换就够了：
# 交给模型重写整个文件反而可能顺手「优化」掉选择器或等待，把本来能跑的登录脚本弄坏。
def _entry_refactor_plan(text: str) -> tuple[str, str, int, str]:
    """分析脚本文本，给出「补入口函数」的重构方案。

    Returns:
        (入口函数名, 原 test 函数名, 原 test 函数 def 所在行号(1-based), 追加到文件末尾的包装代码)

    Raises:
        ValueError: 脚本不满足「可证明安全」的重构条件（原因在异常消息里，调用方打印后保持原文件不动）：
            1. 语法解析不了；
            2. 顶层没有 / 有多于一个 `test_*` 函数（多个时不知道哪段才是主流程）；
            3. 该函数带装饰器（改名会丢 @pytest.mark.xxx 之类语义）；
            4. 参数形态不是「单个 driver 参数」（无参 / 多参 / 带默认值的都不是传 driver 就能跑的形态）；
            5. 去掉 `test_` 前缀后的名字不是合法标识符，或模块里已被占用。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ValueError(f"脚本语法无法解析（第 {exc.lineno} 行：{exc.msg}），不做重构") from exc

    tests = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name.startswith("test_")]
    if not tests:
        raise ValueError("脚本里没有顶层 test_* 函数，无法确定要复用哪段流程")
    if len(tests) > 1:
        names = "、".join(node.name for node in tests)
        raise ValueError(f"脚本里有多个 test_* 函数（{names}），无法确定主流程，需人工或模型重构")

    node = tests[0]
    if node.decorator_list:
        raise ValueError(f"{node.name} 带装饰器，改名会丢失装饰器语义，不做重构")
    args = node.args
    positional = [arg.arg for arg in (*args.posonlyargs, *args.args)]
    if args.vararg or args.kwarg or args.kwonlyargs or len(positional) != 1:
        raise ValueError(f"{node.name} 的参数不是「单个 driver」（实际：{', '.join(positional) or '无参'}）")
    if positional[0] not in ENTRY_DRIVER_ARGS:
        raise ValueError(f"{node.name} 的首参是 {positional[0]}，不是 driver，不做重构")

    entry_name = node.name[len("test_"):]
    if not entry_name.isidentifier():
        raise ValueError(f"去掉 test_ 前缀后的 {entry_name!r} 不是合法标识符")
    defined = {child.name for child in tree.body
               if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    defined |= {target.id for child in tree.body if isinstance(child, ast.Assign)
                for target in child.targets if isinstance(target, ast.Name)}
    if entry_name in defined:
        raise ValueError(f"模块里已存在同名对象 {entry_name}，改名会冲突")

    driver_arg = positional[0]
    wrapper = (f"\n\ndef {node.name}({driver_arg}):\n"
               f'    """pytest 入口：业务流程复用 {entry_name}({driver_arg})，'
               f'便于其它用例脚本 import 复用（勿删）。"""\n'
               f"    {entry_name}({driver_arg})\n")
    return entry_name, node.name, node.lineno, wrapper


def _entry_refactor_broke(output: str) -> bool:
    """重构后 run_script 的输出是不是「结构性失败」（必须回滚原文件）。

    断言失败不算：那说明脚本本来就没跑通（被测站点 / 数据问题），与「改名 + 加转发」无关，
    回滚只会把问题藏起来。执行通过 / 出现 passed 也不算。其余一律回滚最保险
    （导入错误、语法错误、收集不到用例、超时……都是重构可能引入的）。
    """
    text = output or ""
    if "执行通过" in text or "passed" in text:
        return False
    if "AssertionError" in text or re.search(r"^E\s+assert", text, re.M):
        return False
    return True


def _entry_refactor_reason(output: str) -> str:
    """从 run_script 输出里挑出**真正的失败原因**那一行。

    不能直接取最后一个非空行：run_script 的失败输出末尾固定跟着 _fix_hint 的通用建议，
    实测拿到的就是「点击没反应：点击前要用 EC.element_to_be_clickable ...」这类文案，
    真实的 ImportError / TimeoutException 反而被淹没，把人带偏去查选择器。
    这里优先取 pytest 的 `E ` 错误行 / 首个含 Error|Exception 的行，
    都没有就退回首行结论（`执行失败（exit code N）：<路径>`）。
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return (output or "").strip()
    for line in lines:
        if line.startswith("E ") or "Error" in line or "Exception" in line:
            return line
    return lines[0]


def add_reusable_entry(case: TestCase, label: str = "前置脚本", *,
                       verify: bool = False) -> Optional[str]:
    """给「只有 test_xxx(driver)、没有可复用入口」的脚本补一个入口函数。

    改动只有两处（业务逻辑、fixture、import、选择器全部原样不动）：
        1. 唯一的 `test_login(driver)` 原地改名为 `login(driver)`；
        2. 文件末尾追加转发包装，保证 pytest 仍然收集得到这条用例：
               def test_login(driver):
                   login(driver)

    重构结果一律先过 `ast.parse` 语法自检，不通过就保持原文件不动。

    verify=False（默认）：**到此为止，不执行脚本**。这是绝大多数场景的正确选择 ——
    两个调用点（第二环刚结束的目标脚本、刚由第一环真实步骤生成的前置脚本）本轮都已经
    把这条用例在真实浏览器里跑过一遍了，而重构只是「改名 + 加转发」的纯机械变换，
    再跑一遍等于把同一条用例重复执行（多登录一次被测站点、多等几十秒）。
    实测还踩过更糟的：重构后那次执行因为环境抖动 / 站点数据变化没通过，
    `_entry_refactor_broke` 判成结构性失败 -> **把这次有用的重构回滚掉了**，
    后续用例照样 import 不到入口函数，问题反而是这次「验证」凭空制造出来的。

    verify=True：只用于「脚本本来就在仓库里、本轮没有真跑过它」的既有前置脚本
    （见 ensure_precondition_scripts 的 refactor 分支）。这时执行一次属于必要复核
    而不是重复执行；结构性失败会把文件回滚成原样并返回 None
    （此时 prompt 会退化成「让 agent 自己 read_script 后重构」，不会留下坏脚本）。

    Returns:
        入口函数名；无法安全重构 /（verify=True 时）验证失败已回滚则返回 None。
    """
    path = SCRIPTS_DIR / case.script_name
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"读取{label}失败，跳过入口函数补全：{path} -> {type(exc).__name__}: {exc}")
        return None

    # 已经是「入口函数 + test_ 转发」形态时直接返回，不走重构：
    # 否则会命中 _entry_refactor_plan 的「模块里已存在同名对象」拒绝分支，
    # 打印出「无法安全补入口函数 ... 改由代码生成 agent 处理」这种误导性结论
    # （实测 --all-cases 两条用例都打了这行，看起来像出了问题，其实无事可做）。
    existing = find_reusable_entry(path)
    if existing:
        print(f"{label} {case.script_name} 已有可复用入口 {existing}(driver)，无需重构")
        return existing

    try:
        entry_name, test_name, def_line_no, wrapper = _entry_refactor_plan(original)
    except ValueError as exc:
        print(f"{label} {case.script_name} 无法安全补入口函数（{exc}）；"
              f"改由代码生成 agent 按 prompt 提示处理")
        return None

    lines = original.splitlines(keepends=True)
    lines[def_line_no - 1] = lines[def_line_no - 1].replace(f"def {test_name}", f"def {entry_name}", 1)
    refactored = "".join(lines).rstrip("\n") + "\n" + wrapper
    try:
        ast.parse(refactored)
    except SyntaxError as exc:
        print(f"重构结果语法自检失败，保持原文件不动：{case.script_name} -> {exc}")
        return None

    path.write_text(refactored, encoding="utf-8")
    if not verify:
        # 不执行脚本：本轮该用例已在真实浏览器里跑过一遍（或 agent 刚 run_script 复核过），
        # 而这次改动只是「改名 + 加转发」，再跑一次就是重复执行同一条用例
        print(f"""{label} {case.script_name} 补入口函数：{test_name}(driver) -> {entry_name}(driver)\
        （原 test 函数保留为转发包装）。语法自检通过、已落盘，**不执行脚本**\
        （本轮该用例已真实跑过一遍，重构只是改名 + 加转发，再跑就是重复执行）""")
        return entry_name

    print(f"""{label} {case.script_name} 补入口函数：{test_name}(driver) -> {entry_name}(driver)\
    （原 test 函数保留为转发包装），run_script 验证中 ...""")
    output = run_script.invoke({"file_name": case.script_name})
    if _entry_refactor_broke(output):
        path.write_text(original, encoding="utf-8")
        print(f"重构后执行未通过，已回滚 {case.script_name}：{_entry_refactor_reason(output)}")
        return None
    print(f"重构验证通过：{case.script_name} 现在可被其它脚本 "
          f"`from {script_module_name(path)} import {entry_name}` 复用")
    return entry_name


# 本进程内已经**尝试**准备过的前置用例（用例名）：准备失败时不要在同一次运行里反复重试
# —— 每次重试都是一整轮 LLM + 浏览器，代价极高，且大概率还是同样的原因失败。
_PRECONDITION_ATTEMPTED: set[str] = set()


def ensure_precondition_scripts(
    case: TestCase = CASE,
    cases: Sequence[TestCase] = TEST_CASES,
    preparing: tuple[str, ...] = (),
    depth: int = 0,
) -> tuple[PreconditionRef, ...]:
    """开跑本用例前，先把它「前提条件」里引用的前置脚本准备成**可直接 import 复用**的状态。

    这就是本次迭代要解决的问题：`行政区域_区域名称` 的前提条件是「首页登录」，
        - `src/web/scripts/首页登录.py` 已存在（且有入口函数）-> 什么都不做，直接复用；
        - 已存在但只有 `test_login(driver)` -> add_reusable_entry 做确定性最小重构补入口；
        - 不存在 -> 把「首页登录」当一条独立用例跑一遍完整 chain（run_case_chain），
          重新生成出带入口函数的脚本，再复用。
    前置用例自己也可能有前提条件，所以这里是递归的；`preparing` 记录祖先用例名用于
    截断环形引用（A 依赖 B、B 又依赖 A），`depth` 再加一道 MAX_PRECONDITION_DEPTH 兜底。

    返回值是**重新解析过**的 refs：准备动作会改变文件系统状态，必须重解析才能拿到
    最新的 exists / entry（prompt 里的 import 语句要以它为准）。
    """
    refs = resolve_preconditions(case, cases)
    pending = [ref for ref in refs if ref.state in ("generate", "refactor")]
    if not pending:
        return refs

    for ref in pending:
        name = ref.case.name
        if name in preparing:
            # preparing 是祖先用例链，case 是当前用例，name 又指回了祖先 -> 成环
            print(f"检测到前置用例环形引用，跳过："
                  f"{' -> '.join((*preparing, case.name, name))}")
            continue
        if name in _PRECONDITION_ATTEMPTED:
            print(f"前置用例「{name}」本轮已尝试准备过，跳过（避免重复消耗 LLM / 浏览器）")
            continue
        if depth >= MAX_PRECONDITION_DEPTH:
            print(f"前置用例递归已达 {MAX_PRECONDITION_DEPTH} 层上限，跳过「{name}」")
            continue
        _PRECONDITION_ATTEMPTED.add(name)
        print(f"\n--- 准备前置用例：{case.name} 依赖「{ref.text}」"
              f"（{PRECONDITION_STATES[ref.state]}）---")
        if ref.state == "generate":
            # 脚本不存在 -> 重新生成：把前置用例当独立用例跑完整链路（必要时开浏览器采集步骤）
            run_case_chain(ref.case, cases=cases, preparing=preparing + (case.name,),
                           depth=depth + 1)
            # 模型未必按「代码规范」抽出入口函数，这时再用确定性重构补上。
            # verify 保持默认 False：这条前置用例刚刚在第一环真实跑过一遍，
            # 重构后再执行一次就是把同一条用例重复跑（多登录一次被测站点）
            if not find_reusable_entry(ref.script_path):
                add_reusable_entry(ref.case)
        else:
            # refactor：脚本本来就在仓库里、本轮没有真跑过它 -> 这次执行是必要复核，
            # 不是重复执行；它是后续 N 条用例都要 import 的共享前置，重构坏了必须
            # 当场回滚，而不是带病复用、等主用例失败时才暴露
            add_reusable_entry(ref.case, verify=True)

    return resolve_preconditions(case, cases)


def run_case_chain(
    case: TestCase = CASE,
    cases: Sequence[TestCase] = TEST_CASES,
    preparing: tuple[str, ...] = (),
    depth: int = 0,
    *,
    prepare_deps: Optional[bool] = None,
) -> str:
    """跑一条用例的完整链路（第一环采集步骤 -> 第二环生成 / 执行 / 修复），返回最终答案。

    与直接 `chain.invoke(...)` 的唯一区别：默认会先调 ensure_precondition_scripts，
    把「前提条件」引用的前置脚本准备好（存在即复用、缺入口即重构、不存在即生成）。
    `--no-deps` / `SKIP_PRECONDITION=1` 会关掉这一步（prepare_deps 可显式覆盖），
    此时前置脚本的缺失只会写进 prompt，交给代码生成 agent 自己兜底处理。

    抽成函数（而不是把逻辑塞在 main 里）是因为 ensure_precondition_scripts 需要
    用**同一套流程**去跑前置用例：递归复用同一条链路，前置用例的步骤缓存、
    「脚本已存在则跳过采集」等优化也就一并生效了。
    """
    skip_deps = skip_precondition() if prepare_deps is None else (not prepare_deps)
    if skip_deps:
        refs = resolve_preconditions(case, cases)
        if any(ref.state in ("generate", "refactor") for ref in refs):
            print(f"未自动准备前置脚本（--no-deps / {SKIP_PRECONDITION_ENV}=1）："
                  f"{summarize_preconditions(refs)}")
    else:
        ensure_precondition_scripts(case, cases, preparing=preparing, depth=depth)
    answer = chain.invoke(
        {
            "input": codegen_input(case),
            # case 随 inputs 透传（RunnablePassthrough.assign 不会丢弃未知键），
            # 两个 RunnableLambda 都用它推导脚本名 / prompt / 缓存路径 / 前提条件。
            "case": case,
        },
        # run_config 带上修复版 handler（可继承）：外层 chain 的 tracer 日志不会触发原版
        # ConsoleCallbackHandler 的 KeyError('input')，并顺着 LCEL 的环境上下文继承给
        # 两个 RunnableLambda 里的 executor.invoke（它们自己也显式传了同一份 config，
        # 单独 import persist_and_verify 调用时同样有日志）。
        # --quiet 时 callbacks 为 None，外层 chain 一行 tracer 日志都不打。
        config=run_config,
    )
    # 目标脚本自己也要能被**后续用例**复用：代码生成 agent 有时会把业务主流程直接写在
    # test_xxx(driver) 里（实测「行政区域 / 区域名称」就是这样），那样别的脚本没法
    # `from src.web.scripts.行政区域_区域名称 import region_names`。这里用与前置脚本
    # 完全相同的**确定性重构**补入口函数（ast 定位 + 按行改名 + 追加转发包装 +
    # ast.parse 语法自检），不交给模型重写整个文件；脚本已经是「入口函数 + test_ 转发」
    # 形态时是 no-op。
    # verify 必须保持默认 False（不执行脚本）：走到这里说明第二环刚刚结束 —— 要么本轮
    # 用第一环真实执行的步骤新生成了脚本（用例已在浏览器里跑过一遍），要么 agent 已经
    # run_script 复核过既有脚本。两种情况再执行一次都是重复执行（多登录一次被测站点）；
    # 更糟的是那次执行若因环境抖动失败，_entry_refactor_broke 会把这次有用的重构回滚掉。
    if target_script_exists(case):
        add_reusable_entry(case, label="目标脚本")
    return answer


def codegen_input(case: TestCase = CASE) -> str:
    """第二环（代码生成 agent）的任务描述，即 CODEGEN_TASK 里 {task} 的内容。

    脚本名取自 case.script_name —— 用例名即脚本名，这是本次改造的核心约定：
    md 里新增一条用例，不需要改这里的任何字符串就能生成对应的 <用例名>.py。
    """
    return f"""请根据以上的信息，给出对应的web自动化测试的代码: 
        首先在 `{SCRIPTS_DIR}` 文件夹下查找是否存在对应自动化脚本 `{case.script_name}`，
        如果存在则直接执行，执行时如果是脚本执行步骤失败(断言成功/失败不在判断范围内)，则修复脚本直到除断言之外的执行步骤全部成功
        如果不存在则按照测试步骤生成自动化测试脚本且保存在: {SCRIPTS_DIR} 文件夹下，名称为 {case.script_name}
        另外：本用例「前提条件」里引用的前置用例（如「首页登录」），只要 `{SCRIPTS_DIR}` 下已存在对应脚本，
        就必须 import 复用它的入口函数（具体要求见下方「前提条件」的处理方式），不要把前置流程在本脚本里重写一遍
        """


# `--help` 打印的用法说明。手写 argv 解析没有 argparse 的自动帮助，这份文本就是唯一的
# 对外说明书，故按「用例范围 / 采集与前置 / 日志 / 环境变量 / 示例」分组，并把每个开关的
# 同义写法都列全（少写一个别名，用户就会以为不支持）。
USAGE = """用法：python src/web/generate_autoweb.py [开关]
    
    不带任何用例开关时，默认顺序跑用例文档里的**全部**用例（文档里有几条就跑几条）。
    
    用例范围：
        --case <名称>              只跑指定用例（支持 `行政区域/区域名称`、末级标题如 `区域名称1`）
        --all-cases, --all         顺序跑全部用例（与默认行为一致，写出来只为显式表达）
        --first-case, --first      只跑文档里的第一条用例（旧默认行为，调试单条时用）
        --list-cases, --list       只打印用例清单（用例名/标题层级/脚本名/前提条件依赖），不调大模型
        --case-file <md>           换一份用例文档（相对路径按仓库根目录解析）
    
    采集与前置：
        --force-collect            忽略步骤缓存（src/web/.steps/*.steps.json），重新开浏览器采集真实步骤
        --no-deps                  不自动准备前置脚本，等价于 SKIP_PRECONDITION=1
                                   同义写法：--no-precondition / --skip-precondition
    
    日志：
        --quiet, --no-debug        只留业务 print 与最终答案，等价于 LANGCHAIN_DEBUG=0
        --debug                    打开调试日志（默认已打开，写出来只为显式表达）
        --debug-events=<事件>      指定打印哪几类 tracer 事件：all / llm / tool / chain 及其组合
                                   如 --debug-events=all、--debug-events=chain,tool
        --hide-debug-events=<事件> 在默认基础上再隐藏某几类，如 --hide-debug-events=llm/end
    
    环境变量：
        WEB_TESTCASE_FILE 用例文档    WEB_TESTCASE 用例名    FORCE_COLLECT 强制重采
        SKIP_PRECONDITION 跳过前置    LANGCHAIN_DEBUG / LANGCHAIN_DEBUG_EVENTS 日志开关
    
    示例：
        python src/web/generate_autoweb.py                     # 文档里的全部用例
        python src/web/generate_autoweb.py --case 区域名称1     # 只跑一条（末级标题即可匹配）
        python src/web/generate_autoweb.py --first-case        # 只跑第一条
        python src/web/generate_autoweb.py --list-cases        # 先看有哪些用例
        python src/web/generate_autoweb.py --force-collect --quiet
    """


def print_usage() -> None:
    """打印 USAGE（`--help` / `-h` 的唯一出口，不碰浏览器也不调大模型）。"""
    print(USAGE)


def main() -> None:
    """完整链路：准备前置脚本 ->（仅在脚本缺失时）跑浏览器 agent 收集真实步骤 -> 代码生成 agent 落盘/执行/修复。

    用例来自 markdown 文档（默认 src/web/testcase/home_page.md），用例名 / 脚本名 /
    测试步骤 / 预期结果都由文档派生，见文件顶部「用例参数化」那段注释。
    **不带任何用例开关时，默认顺序跑文档里的全部用例**（`# 1. 首页登录`、
    `# 2. 行政区域 / ## 2.1 区域名称1`……文档里有几条就跑几条，一条都不会漏）。
    可用开关：
        --case <名称>     只跑指定用例（支持 `行政区域/区域名称`、末级标题等写法）
        --all-cases       顺序跑文档里的全部用例（与默认行为一致，写出来只为显式表达）
        --first-case      只跑文档里的第一条用例（旧默认行为，调试单条时用）
        --list-cases      只打印用例清单（用例名 / 标题层级 / 脚本名 / 前提条件依赖），不调用大模型
        --case-file <md>  换一份用例文档（相对路径按仓库根目录解析）
        --no-deps         不自动准备前置脚本（见下），等价于 SKIP_PRECONDITION=1
        --help, -h        打印用法说明后退出（不开浏览器、不调大模型，见 USAGE）

    前提条件（前置用例）的处理是本轮迭代的重点：像 `行政区域_区域名称` 的前提条件写着
    「首页登录」，开跑前会先看 `src/web/scripts/首页登录.py`——
    **已存在就直接复用**（脚本里 `from src.web.scripts.首页登录 import login`，
    只有 `test_login(driver)` 的老脚本会先被自动补出 `login(driver)` 入口并跑一遍验证），
    **不存在就先把「首页登录」当一条独立用例重新生成出来**，再给本用例复用；
    详见 ensure_precondition_scripts 与文件里「前提条件（前置用例）」那段注释。

    脚本已存在时第一环会被跳过、脚本不存在但命中步骤缓存时也不会打开浏览器
    （见 resolve_steps），于是「一次运行只登录一次」：脚本不存在时登录只发生在第一环采集，
    脚本已存在时登录发生在 run_script 复核那一次。
    需要重新采集步骤（页面改版、想重建脚本）请加 --force-collect，
    或设置环境变量 FORCE_COLLECT=1。

    第二环的 write_script **只做「语法检查 -> 落盘」，不会执行脚本**：本轮步骤已在真实
    浏览器里跑通过一遍（含断言），落盘后再跑一次等于把同一条用例重复执行（多登录一次
    被测站点、多烧一轮 LLM）。run_script 只用于「脚本已存在时的复核」与「修复之后的确认」。

    控制台日志**默认打开**：会打印 agent 步骤行（「> Entering new AgentExecutor
    chain...」「Invoking: `write_script` with ...」「> Finished chain.」）与 llm / tool
    两类 tracer 日志（[llm/start] 发给模型的 prompt、[llm/end] 模型返回、
    [tool/start] 工具入参、[tool/end] 工具返回值）；chain 类日志默认隐藏，
    加 --debug-events=all 可以一并显示。想要干净输出（只有业务 print 与最终答案）
    就加 --quiet，或设环境变量 LANGCHAIN_DEBUG=0；这些开关与 --force-collect
    可叠加使用。

    包在 main() + __main__ 守卫里（与 generate_autoapp.py 的约定一致）：
    这样其他模块可以 `from src.web.generate_autoweb import persist_and_verify`
    单独验证第二环，而不会在 import 时就拉起浏览器。
    """
    if _cli_flag(*HELP_FLAGS):
        # 手写 argv 解析没有 argparse 的自动帮助：不在这里拦住，`--help` 会被静默忽略、
        # 紧接着就跑起完整流水线（开浏览器 + 调大模型）。放在最前面，先于 --list-cases。
        print_usage()
        return

    if _cli_flag(*LIST_CASES_FLAGS):
        # 纯本地能力：只解析 md（+ ast 解析已有脚本找入口函数）并打印清单，
        # 不构造任何 agent 调用（也就不产生 token 费用）
        print(f"\n用例文档：{TESTCASE_FILE}\n{describe_test_cases(TEST_CASES)}")
        for listed in TEST_CASES:
            print(f"\n[{listed.script_name}] 前提条件依赖：")
            print(describe_preconditions(listed, TEST_CASES))
        return

    for index, case in enumerate(SELECTED_CASES, start=1):
        if len(SELECTED_CASES) > 1:
            print(f"\n{'=' * 25} 用例 {index}/{len(SELECTED_CASES)}："
                  f"{case.name} -> {case.script_name} {'=' * 25}")
        # run_case_chain = 「先把前提条件引用的前置脚本准备好（存在即复用、缺失即生成）」
        # + 原来这段 chain.invoke（inputs / run_config 的说明见该函数）
        print(run_case_chain(case, cases=TEST_CASES))



if __name__ == "__main__":
    main()

