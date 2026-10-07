"""App 自动化测试脚本自动生成工具（Appium 版）—— 与 src/web/generate_autoweb.py 同构。

整体流程、目录约定、两环架构与 web 版完全一致，只是把「Selenium + 浏览器」换成
「Appium + 真机」：

    「采集步骤」环（第一环）：LLM 通过 Appium 工具在真机上一步步真实执行 md 里的测试用例，
        边跑边记「哪一步用了什么工具、什么定位表达式、结果如何」（见 StepRecorder），
        结束后把这份真实执行轨迹交给第二环；
    「生成脚本」环（第二环）：LLM 用 script_tools（列目录 / 读 / 写 / 执行 + 语法与未定义名自检）
        把第一环的真实轨迹落成 src/app/scripts 下可重复运行的 pytest 脚本。
        本轮「要干什么」由 codegen_input(case) 渲染后经 chain.invoke({"input": ...})
        填进 CODEGEN_TASK 的 {task}（写法与 web 版 run_case_chain 一致）：
        查脚本是否已存在 -> **存在就直接执行复核**（执行步骤失败就修复到跑通，断言不计）
        / 不存在就按测试步骤生成（本轮新生成的脚本落盘即完成，不再重复执行，
        见下面第 7 条）-> 前提条件引用的前置脚本必须 import 复用，不许重写前置流程。

这样第二环不再靠「猜」写脚本：定位表达式都是第一环在真机上验证过的。

与 web 版的差异（都由 App 领域本身决定，不是随意改动）：

1. 入口/收尾工具：web 是 open(url) / quit()；App 是 init(app_activity, app_package) / quit()。
   steps_complete 门禁相应改成「init 开头、quit 结尾」。quit 在 App 上尤其不能省 ——
   session 不释放会一直占着设备，下一轮采集直接建不起 session。

2. 定位体系：Appium **不支持 css 选择器**，改用 xpath / resource-id / content-desc /
   UiAutomator 表达式；get_page_source 返回的是 app_framework.summarize_hierarchy 压缩后的
   「一行一个控件」摘要（原始 Android 层级 XML 一屏就 3~8 万字符，会顶穿模型输入）。

3. 「前提条件」语义不同：web 用例里通常是「已经登录」这类**可复用的前置用例**；
   App 用例（见 src/app/testcase/setting.md）里写的是**启动参数**
   （`打开 app activity ".Settings"` / `app package "com.android.settings"`）。
   本模块先用 parse_app_launch() 把它抽成 AppLaunch 并从 preconditions 里剔除，
   剩下的才交给 web 那套前置用例解析（resolve_preconditions）。
   否则「打开 app ...」会被当成一条名字匹配不上的前置用例，白白生成一段 inline 说明。

4. 采集前做 preflight：移动端环境比浏览器脆弱得多（server 没起、设备掉线、
   app 没装）。连不上时若不提前拦，agent 会在「init 失败」上空转 max_iterations 轮，
   白烧几十次模型调用才得到一句「环境问题」。

5. 滚动是常态：Android 列表里的条目默认不在可视区，scroll_to_element 因此被计入
   「有实质交互」的门禁工具集（web 版没有对应工具）。

6. 日志只有控制台一路（开关与 web 版同一套，见 src/utils/langchain_debug.py）：
   - LangChain 调试日志：默认打开，只打印 llm / tool 两类 tracer 日志（[llm/start] 发给
     模型的 prompt、[llm/end] 模型返回、[tool/start] 工具入参、[tool/end] 工具返回值）
     + agent 步骤行（「> Entering new AgentExecutor chain...」「Invoking: `xxx` with ...」）；
     chain 类默认隐藏（web 版同构 chain 实测一次运行 30 条 [chain/start] + 30 条
     [chain/end]，内容全是 RunnableSequence / RunnableLambda 这类 LCEL 包装层在层层
     转述同一份 input/output，只会把真正有用的 tool/llm 日志冲散）。
         --quiet / LANGCHAIN_DEBUG=0     全关（只留业务输出与最终答案）
         --debug-events=all              9 类事件全开（含 chain）
         --debug-events=chain,tool       自己指定组合
         --hide-debug-events=llm/end     在默认 tool,llm 基础上再去掉模型返回内容
   - 业务日志（app.* logger：采集到第几步、缓存命中/写入、入口重构结果、preflight 结论）
     同样打到控制台；它是进度信息而不是调试噪音，因此不受 --quiet 影响
     （见 configure_business_log）。
   早先这里还会往 src/app/logs/generate_autoapp/run_*.log **全量**落一份（LLM 请求/响应
   + 每次工具调用），并配 `--explain <run_id>` 从日志里抽时间线复盘；现已整体剔除 ——
   跑几轮就攒下一堆没人回看的机器产物，里面还混着界面文本、账号口令等敏感信息，
   而真要复盘时 `--debug-events=all` 的控制台输出并不更少（要留档自己接 `| tee`）。
   剔除后 app 版与「从来不落盘」的 web 版重新同构。

7. 第二环的执行口径**已与 web 版对齐**（早先的差异已撤除）：app 领域的 script_tools 同样
   暴露 run_script（ScriptTarget.expose_run_script=True），于是
     - 脚本**已存在** -> 直接执行复核：执行步骤失败（断言成功/失败不在判断范围内）就
       read_script + write_script 修复，直到除断言之外的执行步骤全部成功，修完再 run_script
       确认（最多 2 轮）；
     - 脚本是**本轮新生成**的 -> 落盘即完成，不再 run_script 重复执行：第一环已在真机上把
       整条用例连断言跑通了一遍，再执行等于把同一条用例重复跑（多起一次 Appium session、
       多占设备几十秒，--all-cases 顺序跑时成倍放大），而设备状态抖动带来的失败还会诱导
       模型去改一份本来正确的脚本。
   早先 app 版连「已存在脚本的复核」也不做（工具集里根本没有 run_script），代价是上一轮
   生成的坏脚本、被开发者改过的脚本、APP 改版后失效的定位表达式，全都只能等开发者手动
   `python -m pytest src/app/scripts/<脚本名>` 才暴露。
"""

import ast
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# 允许直接以脚本方式运行：python src/app/generate_autoapp.py
# 此时 sys.path[0] 是 src/app，`import src.*` 会失败（IDE 里运行则由 IDE 注入根目录），
# 这里把仓库根目录补进 sys.path，保证两种运行方式一致。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_classic.agents import AgentExecutor, create_structured_chat_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig, RunnableLambda, RunnablePassthrough

from src.ai_model.qwen_model import qwen_model
from src.app.app_framework import (
    DEFAULT_APP_ACTIVITY,
    DEFAULT_APP_PACKAGE,
    resolve_appium_server,
)
from src.app.appium_tools import TOOL_NAMES, app, tools
from src.utils.debug_events import DebugEventFilter
from src.utils.hub_prompt import pull_prompt
from src.utils.langchain_debug import (
    configure_langchain_logging,
    debug_enabled,
    describe_logging,
    resolve_event_filter,
)
from src.utils.script_tools import APP_TARGET, build_script_tools
from src.utils.testcase_md import (
    TestCase,
    describe_test_cases,
    find_test_case,
    load_test_cases,
    select_test_cases,
)

# 测试用例文件（markdown）与生成脚本目录：
#   src/app/testcase/*.md     用例定义（TestCase Name / 前提条件 / 测试步骤）
#   src/app/scripts/*.py      每个用例对应一个 pytest 脚本（文件名即用例名，见 script_name_of_case）
#   src/app/.steps/*.json     第一环采集到的真实步骤缓存（已被 .gitignore 忽略）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TESTCASE_DIR = PROJECT_ROOT / "src" / "app" / "testcase"
SCRIPTS_DIR = APP_TARGET.dir          # src/app/scripts
STEPS_CACHE_DIR = PROJECT_ROOT / "src" / "app" / ".steps"
# 默认用例文档：src/app/testcase/setting.md（系统设置 app 的用例）。
# 它是**唯一事实来源**：用例名 / 前提条件（含 app activity、app package 启动参数）/
# 测试步骤 / 脚本名全部从这份 md 派生，模块里不再出现任何写死的用例名或步骤
# （写死过一版 `DEFAULT_SCRIPT_NAME = "检查电源.py"`：md 里把「检查电源」改名或删掉，
#  --no-testcase 模式就会去生成一份文档里根本不存在的脚本，两份事实来源必然漂移）。
DEFAULT_TESTCASE_FILE = TESTCASE_DIR / "setting.md"

# 第二环的 script_tools 指向 src/app/scripts（"app" 是 build_script_tools 注册的 target 名）。
# 工具集是 list_scripts / read_script / write_script / run_script 四个（与 web 版一致）：
# 脚本**已存在**就直接 run_script 执行复核，执行步骤失败（断言成功/失败不在判断范围内）
# 就修复到跑通；只有**本轮新生成**的脚本落盘即完成、不再重复执行
# （理由见模块 docstring 第 7 条）。
script_tools = build_script_tools("app")


def configure_business_log() -> None:
    """把 app.* 业务日志接到 stdout —— 本模块不再往 src/app/logs/ 落任何文件。

    早先这里挂的是 root logger 的 FileHandler：每跑一次就新增一个
    src/app/logs/generate_autoapp/run_YYYYmmdd_HHMMSS.log，**全量**记录发给模型的 prompt、
    模型返回与每次工具调用，再配 `--explain <run_id>` 从文件里抽时间线复盘。现已整体剔除：
      * 跑几轮就攒下一堆没人回看的机器产物，且里面混着界面文本、账号口令等敏感信息；
      * 真要复盘，`--debug-events=all` 的控制台输出信息量并不更少，需要留档就自己接
        `... --debug-events=all 2>&1 | tee /tmp/run.log`；
      * web 版（src/web/generate_autoweb.py）从来就不落盘，剔掉两边才叫同构。

    剩下的业务日志（app.cli / app.steps / app.collect / app.run …：采集到第几步、缓存命中
    与写入、入口重构结果、preflight 结论）不是 LangChain 调试噪音，改用 StreamHandler
    打到 stdout，与各处 print 排在同一条时间线上，因此**不受 --quiet 影响**
    （--quiet 关的是 tracer 日志与 agent 步骤行）。logger 名统一以 "app." 开头，
    给父 logger "app" 挂一个 handler 就全覆盖；propagate=False 是为了避免宿主进程
    （例如 pytest 的 logging 插件）给 root 配过 handler 时，同一条日志被打两遍。
    """
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 多次调用（如 --list-cases 后再跑用例）不重复挂 handler，否则日志会翻倍
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
    # 这些库每次 HTTP 请求都打 INFO，会把真正要看的 LLM/工具轨迹冲散
    # （宿主进程给 root 配了 handler 时尤其明显，这里统一降噪）。
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "selenium", "appium",
                  "dashscope", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# import 时就配置好（而不是等到 main）：logging.getLogger("app.*") 遍布全文件，
# --list-cases 这类提前 return 的分支同样要能看到业务日志。
configure_business_log()

# ---- 控制台日志：默认打开，且默认只打印 llm / tool 两类 tracer 事件 ----
# 与 src/web/generate_autoweb.py 共用同一套实现（src/utils/langchain_debug.py），
# 开关名 / 环境变量 / 默认值完全一致，两个入口之间不必切换操作习惯：
#   --quiet（别名 --no-debug）/ LANGCHAIN_DEBUG=0     只留业务 print 与最终答案
#   --debug / LANGCHAIN_DEBUG=1                       显式打开（默认已打开）
#   --debug-events=all                                9 类事件全开（含 chain）
#   --hide-debug-events=llm/end                       在默认 tool,llm 上再去掉模型返回内容
DEFAULT_DEBUG_EVENTS: str = "tool,llm"
DEBUG_LOGGING: bool = debug_enabled(default=True)
EVENT_FILTER: DebugEventFilter = resolve_event_filter(default_only=DEFAULT_DEBUG_EVENTS)
# 打开时返回 [SafeConsoleCallbackHandler(event_filter=EVENT_FILTER)]，--quiet 时返回 []。
# （web 版里这个变量就叫 callbacks；app 版改名 DEBUG_CALLBACKS 是为了强调「这只是控制台
#   调试回调」—— 早先它还要经 _traced(tag) 追加一个落盘用的 RunTraceCallbackHandler，
#   落盘那一路已整体剔除，业务日志改走 configure_business_log 的 stdout handler。）
# 不再自己 new SafeConsoleCallbackHandler()：不带 event_filter 就是 all_events()，
# chain/start + chain/end 会一起打出来（web 版同构 chain 实测一次运行 30 + 30 条，
# 且绝大多数是 RunnableSequence / RunnableLambda / ChatPromptTemplate 这类 LCEL 包装层
# 在层层转述同一份 input/output），把真正有用的 tool / llm 日志冲散。
DEBUG_CALLBACKS: list[BaseCallbackHandler] = configure_langchain_logging(
    DEBUG_LOGGING, event_filter=EVENT_FILTER)
# 回调必须通过 invoke(config=...) 下发，**不能**只写 AgentExecutor(callbacks=...)：
# langchain_classic.chains.base.Chain.invoke 里是
#     CallbackManager.configure(callbacks_from_config, self.callbacks, self.verbose, ...)
# 即构造参数走 local_callbacks -> add_handler(handler, inherit=False)，handler 只挂在
# executor 自己这一层，**到不了嵌套的 chat model / tool run**，于是 [llm/start] /
# [tool/start] 一条都不打印。反过来「构造传 + config 也传」会让根节点事件重复打印
# （同一 handler 被 add 两次，web 版实测 --debug-events=all 时 [chain/start] 18 条
# vs 只走 config 13 条），所以两个 executor 都不写 callbacks= 参数，
# 统一只在这里定义一份 run_config。
# --quiet 时 DEBUG_CALLBACKS 是空列表，这里归一成 None（RunnableConfig.callbacks 允许 None），
# 语义即「不额外挂任何回调」，一行 tracer 日志都不打。
run_config: RunnableConfig = {"callbacks": DEBUG_CALLBACKS or None}


def _short(value: Any, limit: int = 200) -> str:
    """把任意值压成单行短文本，仅用于日志。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# 第一环上下文瘦身
# ---------------------------------------------------------------------------
_OBSERVATION_RE = re.compile(r"Observation:\s*(.*?)(?=\n\s*Thought:|\n\s*```|\Z)", re.S)
# click 这类工具的 Observation 里带整屏控件摘要，实测单条能到几千字符；
# 采集阶段每步都在变长，模型上下文被无用信息迅速填满（DashScope 输入上限 30720）。
_OBSERVATION_MAX_LENGTH = 300
# 工具入参（locator / text）超过这个长度基本是模型在瞎猜，截断以节省上下文
_MAX_TOOL_ARGS_LENGTH = 400


def trim_app_steps(text: str) -> str:
    """裁剪 agent scratchpad：只保留 Thought / Action / Action Input，Observation 截断到 300 字。

    第一环 agent 每多走一步，langchain 就把整段 scratchpad 重新塞回 prompt。
    App 场景下 Observation 特别长（控件层级摘要、界面全文），
    不裁剪的话模型上下文会被无用信息迅速填满，触发
    "Range of input length should be [1, 30720]" 直接让 invoke 抛异常 ——
    那样本轮已采集到的真实步骤会**全部丢失**，第二环只能重新采集。
    """
    if not text:
        return text

    def _cut(match: re.Match) -> str:
        body = match.group(1).strip()
        if len(body) > _OBSERVATION_MAX_LENGTH:
            body = body[:_OBSERVATION_MAX_LENGTH] + "...(已截断，详情见 get_page_source)"
        return f"Observation: {body}"

    trimmed = _OBSERVATION_RE.sub(_cut, text)
    return re.sub(r"\n{3,}", "\n\n", trimmed)


def _short_tool_args(value: Any) -> str:
    """把工具入参压成短字符串（步骤缓存只关心 locator / text，不需要整屏 source）。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= _MAX_TOOL_ARGS_LENGTH else text[:_MAX_TOOL_ARGS_LENGTH] + "…"


class StepRecorder(BaseCallbackHandler):
    """边跑边记：把 agent 每一步真实调用的 (tool, input, observation, failed) 攒成 list[dict]。

    为什么用 callback 而不是解析 agent_executor.invoke() 返回的 intermediate_steps：
    后者只在**整条 chain 正常结束**时才有值；一旦模型输出超长度限制或解析异常，
    invoke 抛错，前面几十次真实交互就全丢了。callback 是流式的，每一步发生即记录，
    所以「执行失败」也能把已跑过的步骤交给第二环去修，而不是白跑一轮。

    failed 这一位是关键：只给「跑通过的步骤」会让第二环以为脚本没问题，
    把注定失败的定位表达式原样写进 pytest；标出来它才知道该修哪一步。
    """

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.step_sources: list[str] = []   # 每步执行后的界面摘要（供第二环写断言范围）
        self._inputs: dict[str, Any] = {}
        self._log = logging.getLogger("app.steps")

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs: Any) -> None:
        self._inputs = {"tool": serialized.get("name", ""), "input": input_str}

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        if not self._inputs:
            return
        observation = output if isinstance(output, str) else str(output)
        self._record(observation, failed=False)
        self._snapshot(observation)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        if not self._inputs:
            return
        self._record(f"{error.__class__.__name__}: {error}", failed=True)

    def _record(self, observation: str, *, failed: bool) -> None:
        tool_name = self._inputs.get("tool", "")
        tool_input = self._inputs.get("input", "")
        try:
            parsed_input = json.loads(tool_input) if isinstance(tool_input, str) else tool_input
        except (json.JSONDecodeError, TypeError):
            parsed_input = tool_input
        self.steps.append({
            "tool": tool_name,
            "input": _short_tool_args(parsed_input),
            "observation": observation[:30000],
            "failed": failed,
        })
        self._log.info("记录第 %d 步 tool=%s input=%s failed=%s",
                       len(self.steps), tool_name, _short(parsed_input), failed)
        self._inputs = {}

    def _snapshot(self, observation: str) -> None:
        """从 Observation 里抓一段界面摘要存下来。

        App 的 click / scroll_to_element / init 返回值里都带控件层级摘要，
        第二环写断言时能据此把范围限定到具体列表，而不是整页 `in page_text`。
        """
        marker = "当前界面控件摘要："
        if marker in observation:
            self.step_sources.append(observation.split(marker, 1)[1][:2000])
        elif observation.startswith("<") and "\n" in observation:
            self.step_sources.append(observation[:2000])


# ---------------------------------------------------------------------------
# Agent 装配
# ---------------------------------------------------------------------------
prompt = pull_prompt("hwchase17/structured-chat-agent")
llm = qwen_model  # 换模型只改这一处；第一环与第二环共用，保证「采集」与「写码」口径一致


def _codegen_parsing_error(error: BaseException) -> str:
    """第二环 Action Input 解析失败时回给模型的 Observation。

    只做「把错误说清楚 + 给出修好的样子」，不尝试替模型重放工具调用 ——
    解析失败发生在 langchain 内部，此时工具还没被执行，重放需要自己实现一遍
    agent 循环，得不偿失。把修复后的 JSON 直接摆在模型面前，它下一轮照抄即可，
    实测比只回一句「请输出合法 JSON」有效得多（后者经常连着几轮犯同一个错）。
    """
    raw = str(getattr(error, "observation", "") or error)
    repaired = _repair_illegal_tool_args(raw)
    lines = [
        """上一次 Action Input 不是合法 JSON，工具没有被执行（脚本尚未写盘）。
        常见原因：字符串里嵌了未转义的双引号（如 assert \"省电\" in text）。
        请重新输出同一次工具调用，并遵守：字符串内部的双引号写成 \\\"，换行写成 \\n，
        不要出现裸控制字符。
        """
        # "上一次 Action Input 不是合法 JSON，工具没有被执行（脚本尚未写盘）。",
        # "常见原因：字符串里嵌了未转义的双引号（如 assert \"省电\" in text）。",
        # "请重新输出同一次工具调用，并遵守：字符串内部的双引号写成 \\\"，换行写成 \\n，"
        # "不要出现裸控制字符。",
    ]
    if repaired != raw:
        lines.append(f"已按规则修好的写法（可直接照抄）：{repaired[-1200:]}")
    return "\n".join(lines)


# 第一环：真机执行采集步骤。
#   max_iterations 给足 40：移动端每步往往是「get_page_source -> scroll_to_element -> click」
#   三连，比 web 更费轮次；给小了会在用例中途被 AgentExecutor 截断，
#   拿到一份「没有 quit、没有断言」的半截轨迹。
app_agent = create_structured_chat_agent(llm, tools, prompt)
app_agent_executor = AgentExecutor(
    agent=app_agent,
    tools=tools,
    # verbose 会额外注入 StdOutCallbackHandler，打印
    # 「> Entering new AgentExecutor chain...」「Invoking: `tool` with ...」等 agent 步骤行，
    # 与 tracer 日志共用同一个总开关（默认打开，--quiet / LANGCHAIN_DEBUG=0 关闭）。
    # 回调**不在这里传**：构造参数是不可继承的 local_callbacks，嵌套的 llm / tool run
    # 收不到，统一改由 invoke(config={"callbacks": ...}) 下发（见 run_config 处的说明）。
    verbose=DEBUG_LOGGING,
    return_intermediate_steps=True,
    handle_parsing_errors=True,  # 模型偶尔输出不合规的 Action Input，让它自己重试而不是崩掉
    max_iterations=40,
)

# 第二环：代码生成 agent（用 function-calling 调 script_tools，不再靠模型手抄代码）。
# 这一环**可以执行脚本**（工具集里有 run_script），轮次需求与 web 版对齐：
# 「已存在 -> read_script -> run_script -> 步骤失败则 write_script 修复 -> 再 run_script 确认」
# 最多修 2 轮就是 6~8 个 action；新生成脚本的路径只需 2~3 个 action
# （list_scripts -> write_script -> Final Answer，落盘即完成、不再执行）；
# 20 轮足够跑完并留出模型跑偏的余量。
codegen_prompt = pull_prompt("hwchase17/structured-chat-agent")
codegen_agent = create_structured_chat_agent(llm, script_tools, codegen_prompt)
codegen_agent_executor = AgentExecutor(
    agent=codegen_agent,
    tools=script_tools,
    verbose=DEBUG_LOGGING,       # 同上：控制台回调只走 invoke(config=...)
    return_intermediate_steps=True,
    max_iterations=20,
    # 关键：qwen 偶尔会产出非法 JSON 的 Action Input（工具入参里嵌未转义的双引号），
    # structured chat 默认直接抛 OutputParserException 终止整条 chain —— 此时脚本往往还没写盘，
    # 一整轮采集 + 生成全部作废。改成把解析错误作为 Observation 喂回去，让模型自己重试。
    handle_parsing_errors=_codegen_parsing_error,
)


# ---------------------------------------------------------------------------
# 测试用例文件解析 + App 启动参数
# ---------------------------------------------------------------------------
def resolve_testcase_file(given: Optional[str]) -> Path:
    """确定本次要读的测试用例 md 文件（用例名 / 前提条件 / 测试步骤的唯一事实来源）。

    优先级：命令行 --testcase-file（别名 --case-file，与 web 版同名同义）
          > 环境变量 APP_TESTCASE_FILE（兼容通用写法 TESTCASE_FILE）
          > src/app/testcase 下唯一的 .md（只有一个时直接用它，免得每次敲长路径）
          > DEFAULT_TESTCASE_FILE，即 src/app/testcase/setting.md。

    相对路径一律按仓库根目录解析：不管从哪个 cwd 启动，
    `--testcase-file src/app/testcase/setting.md` 都指向同一个文件。
    """
    if given:
        path = Path(given).expanduser()
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    # 注意：环境变量 TESTCASE_FILE 与本模块下面那个同名全局变量是两回事 ——
    # 前者是跨领域入口的通用约定（web 版认 WEB_TESTCASE_FILE），后者是本次真正要读的路径
    for env_key in ("APP_TESTCASE_FILE", "TESTCASE_FILE"):
        env_file = os.getenv(env_key)
        if env_file:
            path = Path(env_file).expanduser()
            return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    md_files = sorted(TESTCASE_DIR.glob("*.md")) if TESTCASE_DIR.is_dir() else []
    if len(md_files) == 1:
        return md_files[0]
    for candidate in md_files:
        if candidate.name == DEFAULT_TESTCASE_FILE.name:
            return candidate
    return DEFAULT_TESTCASE_FILE


# 本次要读的用例文档；--testcase-file / APP_TESTCASE_FILE 会在 _apply_cli_overrides 里改写它
TESTCASE_FILE: Path = resolve_testcase_file(None)


def parse_test_cases() -> list[TestCase]:
    """读取当前 TESTCASE_FILE 里的全部用例。

    与 web 版不同，这里不在 import 期就读文件：TESTCASE_FILE 会被 CLI 参数改写，
    import 期读到的可能是旧值；而且读不到用例时应当打印提示继续走「无用例」分支，
    而不是让 load_test_cases 的 ValueError 直接把模块 import 打挂。
    """
    log = logging.getLogger("app.cli")
    if not TESTCASE_FILE.is_file():
        log.warning("用例文档不存在：%s（用 --testcase-file 指定）", TESTCASE_FILE)
        return []
    try:
        return load_test_cases(TESTCASE_FILE)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("用例文档解析失败：%s", exc)
        return []


@dataclass(frozen=True)
class AppLaunch:
    """从用例里解析出的 App 启动参数（对应第一环的 init、第二环 driver fixture 的 capabilities）。

    text 保留「前提条件」原文，用于渲染进 prompt —— 让模型知道这些值是从哪儿来的，
    比凭空给两个字符串更容易被正确沿用。
    """

    activity: str
    package: str
    text: str = ""

    def describe(self) -> str:
        origin = f"（来自用例前提条件：{self.text}）" if self.text else "（来自环境变量/框架默认值）"
        return f'app_package="{self.package}"，app_activity="{self.activity}"{origin}'


# 启动参数的常见写法。md 里是自然语言（`打开 app activity ".Settings"`），
# 生成脚本里是关键字参数（app_activity=".Settings"），两种都要认出来。
_ACTIVITY_RE = re.compile(
    r"(?:app[\s_-]*activity|appActivity|启动\s*activity|入口\s*activity|activity)"
    r"\s*[:：=]?\s*[\"'“”‘’`]*([^\s\"'“”‘’`,，;；)）]+)", re.I)
_PACKAGE_RE = re.compile(
    r"(?:app[\s_-]*package|appPackage|应用包名|包名|package)"
    r"\s*[:：=]?\s*[\"'“”‘’`]*([^\s\"'“”‘’`,，;；)）]+)", re.I)
# 判定一行文本是不是「启动 app」类的前提条件（而不是「另一条前置用例」）
_LAUNCH_HINT_RE = re.compile(
    r"(打开|启动|拉起|运行)\s*(app|应用|客户端|被测)"
    r"|app[\s_-]*(activity|package)|包名|activity", re.I)


def parse_app_launch(case: Optional[TestCase]) -> AppLaunch:
    """从用例的「前提条件」和「测试步骤」里抽出 app_activity / app_package。

    抽不到就退回环境变量 / app_framework 默认值，保证第一环总能给出可用的 init 参数 ——
    让 agent 自己去猜 package 名，十次有九次是猜错的。
    """
    haystack: list[str] = []
    if case is not None:
        haystack.extend(case.preconditions)
        haystack.extend(case.steps)
    joined = " ".join(haystack)
    activity_match = _ACTIVITY_RE.search(joined)
    package_match = _PACKAGE_RE.search(joined)
    caps = {"activity": "", "package": "", "text": ""}
    if activity_match:
        caps["activity"] = activity_match.group(1)
    if package_match:
        caps["package"] = package_match.group(1)
    if caps["activity"] or caps["package"]:
        caps["text"] = next((line for line in haystack if _LAUNCH_HINT_RE.search(line)), "")
    defaults = {"activity": DEFAULT_APP_ACTIVITY, "package": DEFAULT_APP_PACKAGE}
    return AppLaunch(
        activity=caps["activity"] or defaults["activity"],
        package=caps["package"] or defaults["package"],
        text=caps["text"],
    )


def is_app_launch_precondition(text: str) -> bool:
    """判断一行「前提条件」是不是启动参数（而非可复用的前置用例）。

    必须在 resolve_preconditions 之前把这些行剔除：否则 `打开 app activity ".Settings"`
    会被当成一条名字匹配不上任何 TestCase 的前置用例，生成一段毫无用处的 inline 说明，
    还会在第一环的 prompt 里制造噪音。
    """
    return bool(_LAUNCH_HINT_RE.search(text or ""))


# 「无用例」模式（--no-testcase）下的兜底脚本名：只有 md 里一条用例都解析不出来时才用。
FALLBACK_SCRIPT_NAME = "app_未命名用例.py"
# 按用例文档路径缓存派生结果：script_name_of_case 会被 steps_cache_path / list_cases /
# 各处日志反复调用，没必要每次都重新解析一遍 md。
_default_script_names: dict[Path, str] = {}


def default_script_name() -> str:
    """没有具体用例时的脚本名：取用例文档里的**第一条用例名**（依然由 md 派生，不写死）。

    为什么不能写死：写死过一版 `DEFAULT_SCRIPT_NAME = "检查电源.py"`，它与 setting.md
    第一条用例同名纯属巧合 —— md 里把这条用例改名 / 删掉之后，--no-testcase 模式生成的
    脚本就成了「文档里查无此用例」的孤儿，缓存指纹也跟着失真。
    """
    cached = _default_script_names.get(TESTCASE_FILE)
    if cached:
        return cached
    cases = parse_test_cases()
    name = cases[0].script_name if cases else FALLBACK_SCRIPT_NAME
    _default_script_names[TESTCASE_FILE] = name
    return name


def script_name_of_case(case: Optional[TestCase]) -> str:
    """脚本文件名 = 用例名 + .py；没有用例（--no-testcase / 旧入口）时退化成文档第一条用例名。"""
    name = (case.name if case else "") or default_script_name()
    return name if name.endswith(".py") else f"{name}.py"


# ---------------------------------------------------------------------------
# 前提条件解析（与 web 版同构，差别只在于「先剔除 App 启动参数」）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreconditionRef:
    """前提条件解析结果：原话 + 解析状态。

    state 三态（对应三种不同的「怎么满足它」）：
      matched     -> md 里找到了同名/近名的前置用例，会先生成那个前置脚本，本用例 import 复用
      inline      -> 泛指写法（「打开 X 页」），在本用例脚本内自行导航实现
      unresolved  -> 提到了具体用例名，但 md 里找不到对应用例；只能按字面意思实现，并告警
    case 仅 matched 时非空；script_name 为该前置用例对应的脚本文件名。
    """

    text: str
    state: str
    case: Optional[TestCase] = None
    script_name: str = ""
    entry_name: str = ""

    @property
    def is_matched(self) -> bool:
        return self.state == "matched"

    @property
    def is_inline(self) -> bool:
        return self.state == "inline"

    @property
    def is_unresolved(self) -> bool:
        return self.state == "unresolved"


def _is_fixture_decorator(dec: ast.expr) -> bool:
    """判断装饰器是否为 @pytest.fixture（含 @pytest.fixture() / @pytest.fixture(scope=...)）。"""
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        return target.attr == "fixture"
    return isinstance(target, ast.Name) and target.id == "fixture"


# 入口函数（以及可安全重构的 test 函数）的首参名：app_framework 的 fixture 就叫 driver
ENTRY_DRIVER_ARGS: tuple[str, ...] = ("driver", "d")


def _business_entry_of_tests(tree: ast.Module) -> Optional[str]:
    """找出顶层 `test_*(driver)` 用例实际调用的那个业务函数名（即真正的复用入口）。

    为什么要单独找：一个脚本里可能有**多个**「首参是 driver」的顶层函数，例如
        def check_battery_assert(driver): ...   # 只有断言，不含前面的导航
        def check_battery(driver):             # 导航 + 断言，才是完整入口
            check_battery_assert(driver)
        def test_check_battery(driver):
            check_battery(driver)
    按「文件里第一个首参为 driver 的函数」挑会选中 check_battery_assert，
    后续用例把它当前提条件 import 复用时就**少了导航**，脚本必然定位超时。
    而 pytest 入口调用的那个函数，语义上就是「跑完整条用例」的入口，最可靠。

    只在能唯一确定时返回：test 函数体里调用了 0 个或 2 个以上同模块函数时返回 None，
    交给调用方回落到启发式（宁可不猜，也不要给出错误的复用入口）。
    """
    top_level = {node.name for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    called: set[str] = set()
    for node in tree.body:
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")):
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
         语义上就是「跑完整条用例」的入口，含前置导航，最可靠；
      2. 回落启发式：模块顶层函数、不以 `test_` / `_` 开头、不带 pytest fixture 装饰器、
         至少有一个位置参数；首参名是 driver 这类约定名的**优先**返回。

    只做静态解析、不 import 目标脚本：app 脚本 import 进来会连带解析 appium client，
    纯属浪费；而且解析失败（语法错误 / 编码问题）时返回 None 就好，绝不能抛异常打断链路。
    """
    if not script_path or not script_path.is_file():
        return None
    try:
        tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
    except (OSError, SyntaxError, ValueError) as exc:
        logging.getLogger("app.entry").warning(
            "解析脚本失败，按「没有入口函数」处理：%s -> %s: %s",
            script_path.name, type(exc).__name__, exc)
        return None

    reusable: set[str] = set()
    fallback: Optional[str] = None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        if name.startswith(("test_", "__", "_")):
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
    return fallback


def _precondition_keyword(precondition: str) -> str:
    """把一条前提条件的文字裁成 find_test_case 能用的关键词。

    去掉 `.py` 后缀、去掉包裹的引号 / 书名号 / 括号、去掉首尾标点与空白：
    md 是手写的，「首页登录」「首页登录.py」「`首页登录`」「首页登录（账号 hogwarts）」
    都应该指向同一条前置用例。再去掉「已经 / 请先 / 需要」这类语气前缀
    （「请先完成 打开设置页」-> 「完成 打开设置页」，交由子串兜底匹配）。
    """
    keyword = re.sub(r"\.py$", "", (precondition or "").strip(), flags=re.IGNORECASE).strip()
    keyword = keyword.strip("「」『』\"'`【】[]（）() ")
    keyword = re.sub(r"[\s。．.，,；;：:、！!？?]+$", "", keyword).strip()
    return re.sub(r"^(已经|已|请先|先|需要|要求|完成)+\s*", "", keyword).strip()


# 前提条件里的占位写法：md 手写时常留一行空的 `1.`，或写「无」，都不指向真实前置用例
PLACEHOLDER_PRECONDITIONS: frozenset[str] = frozenset({
    "", "无", "none", "n/a", "na", "null", "nil", "-", "--", "暂无", "不涉及", "见测试步骤",
})


def is_placeholder_precondition(precondition: str) -> bool:
    """前提条件是不是空占位（`1. ` 这种空列表项、或写「无」）：这类行不指向任何前置用例。

    testcase_md 已经过滤了**完全空**的项，但「无」「暂无」这类文字还在，
    这里统一判掉，免得下游把「无」当成一个叫「无」的用例去找、报一条无从修起的 unresolved。
    """
    keyword = _precondition_keyword(precondition)
    normalized = re.sub(r"[\s_\-*.、，,。.;；:：]+", "", keyword).lower()
    return normalized in PLACEHOLDER_PRECONDITIONS


# 「打开 X 页/界面」「进入 X」这种泛指写法（不指向某条具体用例名）无法在 md 里找到对应
# TestCase，只能在本用例脚本里自行实现（导航），归为 inline 而非 unresolved，
# 否则会被当成「md 里缺失的前置用例」而报出根本无从修起的问题。
# 只在**句首**认导航动词（允许「请先 / 已经」这类语气前缀）：中文没有词边界，
# 用 \b 或「后面不能跟汉字」的写法会把「打开设置首页」漏掉。
_NAVIGATION_PRECONDITION_RE = re.compile(
    r"^\s*(?:请先|请|先|已经|已|需要|要求)?\s*(?:完成\s*)?"
    r"(打开|进入|跳到|跳转到|来到|访问|启动|拉起)"
)


def is_navigation_precondition(precondition: str) -> bool:
    """前提条件是否为「打开 X 页」这类导航写法（在本用例脚本内自行实现）。

    只要以导航/打开类动词开头就算，无需再叠加关键词：
    md 里的写法五花八门（"打开首页"、"进入 X 管理页"、"访问后台"…），
    靠关键词白名单永远漏，用「打开类动词」这一个特征即可稳定识别。
    """
    return bool(_NAVIGATION_PRECONDITION_RE.search(precondition or ""))


def find_precondition_case(precondition: str, all_cases: Sequence[TestCase],
                           current: Optional[TestCase] = None) -> Optional[TestCase]:
    """把一条前提条件的文字（如「首页登录」）解析成用例文档里的 TestCase；解析不出返回 None。

    两级匹配（与 web 版一致）：
      1. 借用 find_test_case 的宽松匹配（用例名 / 各级标题 / 末级标题 / 带 .py 都认）；
         它命中多条（歧义）或一条都没命中时会抛 ValueError，这里捕获后走第 2 级；
      2. 兜底子串匹配：前提条件常写成一句话（「首页登录（账号 hogwarts）」「先完成 首页登录」），
         这时用例名 / 各级标题是这句话的子串，取**命中标题最长**的那条用例（最具体优先）。
    都匹配不上 -> None，说明这只是环境 / 数据 / 导航描述，不是可复用的前置脚本。
    """
    keyword = _precondition_keyword(precondition)
    if not keyword or is_placeholder_precondition(keyword):
        return None
    pool = [case for case in all_cases if case is not current]
    try:
        return find_test_case(pool, keyword)
    except ValueError:
        pass  # 歧义 / 未命中 -> 走子串兜底

    best: Optional[tuple[int, TestCase]] = None
    source = precondition or keyword
    for candidate in pool:
        hit = max((len(title) for title in {candidate.name, *candidate.title_path}
                   if title and title in source), default=0)
        if hit and (best is None or hit > best[0]):
            best = (hit, candidate)
    return best[1] if best else None


def resolve_preconditions(case: Optional[TestCase], all_cases: Sequence[TestCase],
                          scripts_dir: Path) -> list[PreconditionRef]:
    """解析前提条件，按「能否落到具体前置脚本」分成 matched / inline / unresolved 三态。

    App 版比 web 版多一步：**先把「打开 app activity/package」这类启动参数剔除**
    （它们由 parse_app_launch 接管），启动参数不是前置用例。剩下的再走同一套逻辑：
      - 能在 all_cases 里按名字找到 -> matched，返回其脚本名与可复用入口，供第二环 import；
      - 泛指写法（打开 X 页）-> inline，第二环自行实现，不告警；
      - 提到具体用例名却找不到 -> unresolved，第二环按字面实现并告警（提示补 md 或改泛指写法）。
    """
    if case is None:
        return []
    # dict.fromkeys 去重并保持原顺序；启动参数（打开 app activity / app package）由
    # parse_app_launch 接管，空占位（「无」「暂无」）不指向任何用例，两者都不算前置用例
    preconditions = [p for p in dict.fromkeys(case.preconditions)
                     if p and not is_app_launch_precondition(p)
                     and not is_placeholder_precondition(p)]
    resolved: list[PreconditionRef] = []
    for raw in preconditions:
        matched = find_precondition_case(raw, all_cases, case)
        if matched is not None:
            script_name = script_name_of_case(matched)
            candidate = scripts_dir / script_name
            entry = find_reusable_entry(candidate) if candidate.is_file() else None
            resolved.append(PreconditionRef(
                text=raw, state="matched", case=matched,
                script_name=script_name, entry_name=entry or ""))
        elif is_navigation_precondition(raw):
            resolved.append(PreconditionRef(text=raw, state="inline"))
        else:
            resolved.append(PreconditionRef(text=raw, state="unresolved"))
    return resolved


def summarize_preconditions(refs: Sequence[PreconditionRef]) -> dict[str, list[PreconditionRef]]:
    """把解析结果按 state 分组，便于上层分别处理（生成前置脚本 / 告警）。"""
    grouped: dict[str, list[PreconditionRef]] = {"matched": [], "inline": [], "unresolved": []}
    for ref in refs:
        grouped.setdefault(ref.state, []).append(ref)
    return grouped


def describe_preconditions(refs: Sequence[PreconditionRef]) -> str:
    """把前提条件解析结果渲染成给第二环 prompt 用的中文说明。"""
    if not refs:
        return "（无前提条件）"
    lines: list[str] = []
    for ref in refs:
        if ref.is_matched:
            if ref.entry_name:
                lines.append(f"- [前置用例] {ref.text} -> 复用 {ref.script_name} 的 {ref.entry_name}()")
            else:
                lines.append(f"- [前置用例] {ref.text} -> 复用 {ref.script_name}"
                             f"（暂未找到可复用入口，将按模板重构出入口后再 import）")
        elif ref.is_inline:
            lines.append(f"- [自行实现] {ref.text} -> 在本用例脚本内导航/操作实现")
        else:
            lines.append(f"- [未匹配] {ref.text} -> md 里找不到对应用例，"
                         f"按字面意思实现（建议补 md 或改成泛指写法）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令行开关（与 web 版同名同义，两套脚本共用一套操作习惯）
# ---------------------------------------------------------------------------
def _flag_on(*env_keys: str, cli_flag: str) -> bool:
    """环境变量（任一为真）或命令行开关命中即返回 True。"""
    for key in env_keys:
        if os.getenv(key, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return cli_flag in sys.argv


def force_collect() -> bool:
    """是否强制重新采集步骤（忽略 src/app/.steps 缓存）。

    额外认 APP_ 前缀的环境变量：web 与 app 在同一 shell 里交替跑时互不干扰。
    """
    return _flag_on("FORCE_COLLECT", "APP_FORCE_COLLECT", cli_flag="--force-collect")


def skip_precondition() -> bool:
    """是否跳过前提条件解析（不生成/复用前置脚本，也不注入前置约束）。"""
    return _flag_on("SKIP_PRECONDITION", "APP_SKIP_PRECONDITION", cli_flag="--skip-precondition")


# ---------------------------------------------------------------------------
# 第一环 query 组装
# ---------------------------------------------------------------------------
# 第一环必须调的工具（用于 steps_complete 门禁）：
#   init   对应 web 的 open   —— 不启动 app 就没有 session，后面每一步都会失败
#   quit   对应 web 的 quit   —— 不释放设备，下一轮采集/生成的脚本会因设备被占用而起不来
#
# 这里用 appium_tools.TOOL_NAMES 而不是硬编码字符串：工具改名（appium_tools 里那个
# _TOOL_RENAMES 就是在做这件事）时 import 期就会 KeyError，而不是等真机跑完才发现门禁形同虚设。
_REQUIRED_STEP_TOOLS = (TOOL_NAMES["init"], TOOL_NAMES["quit"])
# 算作「测试动作」的工具：click / send_keys 改变界面状态，get_text / assert_contains 取校验值。
# **scroll_to_element / back 不算** —— 它们只是导航辅助。真机上最常见的一种废轮次是：
# 滚动没找到目标（MIUI 上「省电与电池」这类 AOSP 文案根本不存在）-> agent 直接 quit 收尾，
# 轨迹里只有 init + scroll + get_page_source + quit。这种轨迹若放进第二环，
# 模型会照实写出「scroll 一下 + assert 元素不是 None」的占位脚本（还带一句
# 「此处可添加更多业务逻辑」），而脚本一旦存在，后续运行就只会被「小修」而永远留着。
_ACTION_TOOLS = frozenset(TOOL_NAMES[name] for name in
                          ("click", "send_keys", "get_text", "assert_contains"))

# ---------------------------------------------------------------------------
# 「当前正在跑哪条用例」的运行时上下文
# ---------------------------------------------------------------------------
# 用模块级变量而不是层层传参：第一环的采集结果要经过
# RunnablePassthrough.assign -> RunnableLambda 两跳才拼进第二环 prompt，
# 中间还要给 script_tools 定位目标脚本；把这些都塞进 chain 的 input dict
# 会让 prompt 模板里多出一堆与模型无关的占位符，反而容易被模型复述进 Final Answer。
CURRENT_CASE: Optional[TestCase] = None
CURRENT_LAUNCH: AppLaunch = AppLaunch(activity=DEFAULT_APP_ACTIVITY,
                                      package=DEFAULT_APP_PACKAGE)
CURRENT_REFS: list[PreconditionRef] = []
# 本次采集的 StepRecorder（第二环要从 step_sources 里取界面摘要来限定断言范围）
CURRENT_RECORDER: Optional[StepRecorder] = None
# md 里解析出的全部用例（main 写入；用于前提条件的跨用例查找）
ALL_CASES: list[TestCase] = []


def build_launch_steps(case: Optional[TestCase], launch: AppLaunch) -> list[dict[str, Any]]:
    """把「启动 app」渲染成第一环的第 1 步（固定动作，不让模型去猜 package/activity）。"""
    return [{
        "tool": "init",
        "input": {"app_activity": launch.activity, "app_package": launch.package},
        "note": f"启动被测 app（{launch.describe()}），返回启动后的控件层级摘要",
    }]


def build_precondition_steps(refs: Sequence[PreconditionRef]) -> list[dict[str, Any]]:
    """把 matched 前置用例的「已验证步骤」渲染成第一环的前置动作。

    与 web 版同理：前置用例（如「首页登录」）的真实步骤已经采集并验证过，
    直接喂给第一环照着做，比让它自己摸索登录流程可靠得多。
    """
    steps: list[dict[str, Any]] = []
    for ref in refs:
        if not ref.is_matched or ref.case is None:
            continue
        # 带上前置用例自己的启动参数做指纹校验：前置用例的 md 改过（步骤/前提变了）
        # 就不该再拿它的旧步骤当权威；不传 launch 等于永远认为缓存有效。
        cached = load_cached_steps(ref.case, parse_app_launch(ref.case))
        if not cached:
            continue
        steps.append({"tool": "# precondition", "input": ref.text, "case": ref.case.name,
                      "note": f"以下为前置用例「{ref.case.name}」已验证的步骤，请照做"})
        # 前置用例缓存里的 init / quit 必须剔掉：本用例自己会 init 一次、最后 quit 一次，
        # 中间再插一个 quit 会直接关掉 session，后面的步骤全部报 "driver 尚未启动"。
        steps.extend(step for step in cached
                     if str(step.get("tool", "")) not in _REQUIRED_STEP_TOOLS)
    return steps


def build_query(case: Optional[TestCase], launch: AppLaunch,
                precondition_steps: Optional[list[dict[str, Any]]] = None) -> str:
    """组装第一环的执行指令：启动参数 + 前置步骤 + 本用例步骤 + App 定位规范。

    「定位规范」这一段是 App 版特有的重点：模型脑子里的自动化语料绝大多数是 web 的，
    不显式禁止就会写出 `input[name='x']` 这种 css 选择器、或者只丢一段中文文本当 locator，
    在 Appium 下必然失败，然后反复重试烧光 max_iterations。
    """
    if case is None:
        steps_text = "（未指定测试用例：请先用 --testcase 选择，或直接在 md 里补充用例）"
    else:
        steps_text = "\n".join(f"{index}. {step}" for index, step in enumerate(case.steps, 1))
    lines: list[str] = [
        "你是一个 app 自动化测试工程师，技术栈为 pytest + Appium（Android / UiAutomator2）。",
        "接下来请在**真机/模拟器上真实执行**下面这条测试用例，每一步都以上一步执行后的界面为准：",
        "",
        f"测试用例 -> {case.name if case else default_script_name()[:-3]}",
        f"被测 app：{launch.describe()}",
        "",
        "【必须严格按顺序执行的动作】",
    ]
    ordered: list[dict[str, Any]] = [*build_launch_steps(case, launch)]
    if precondition_steps:
        ordered.extend(precondition_steps)
    if case is not None:
        ordered.extend({"tool": "# step", "input": step} for step in case.steps)
    ordered.append({"tool": "quit", "input": {}, "note": "释放设备，必须执行"})
    for index, item in enumerate(ordered, 1):
        note = f"  # {item['note']}" if item.get("note") else ""
        tool = str(item.get("tool", ""))
        if tool == "# step":
            # 测试步骤原文不是工具调用，渲染成「工具调用样式」会让模型去找名为 step 的工具
            lines.append(f"{index}. 【测试步骤】{item.get('input', '')}")
        elif tool == "# precondition":
            # 标签里放**匹配到的用例名**（模型要照它做），原话跟在后面（里面常带账号等额外约束）
            lines.append(f"{index}. 【前置用例：{item.get('case', '')}】"
                         f"原话「{item.get('input', '')}」；{item.get('note', '')}")
        else:
            lines.append(f"{index}. {tool}({json.dumps(item.get('input', {}), ensure_ascii=False)}){note}")
    lines.extend([
        "",
        "【测试步骤原文】",
        steps_text,
        "",
        "【Appium 定位规范（务必遵守，否则一定失败）】",
        "1. 拼任何定位表达式之前，先调 get_page_source 拿当前界面的控件层级摘要；",
        "   摘要每行形如 <TextView id=\"com.android.settings:id/title\" text=\"省电与电池\" clickable bounds=\"...\">，",
        "   其中的 text / id(resource-id) / desc(content-desc) 才是真实可用的定位依据。",
        "2. 定位表达式只支持这几种写法，**不支持 css 选择器，也不接受只给一段可见文本**：",
        "     //*[contains(@text,'省电与电池')]                    按可见文本（最常用）",
        "     //*[@resource-id='com.android.settings:id/title']     按 resource-id",
        "     id=com.android.settings:id/title                      resource-id 简写",
        "     acc=更多                                              content-desc（accessibility id）",
        "     ui=new UiSelector().textContains(\"打印\")               UiAutomator 表达式",
        "3. Android 列表里的条目默认不在可视区，直接 find 会失败：先 scroll_to_element 滚出来。",
        "4. 点击报 element not interactable，说明命中的是不可点的子控件（如 TextView），",
        "   改点它外层带 clickable 的父容器。",
        "5. 「断言 XXX」用 assert_contains；「获取 XXX」用 get_text，再用 assert_contains 校验取值。",
        "   **定位表达式和断言文本里只能用稳定文案，禁止写采集那一刻的具体数值** ——",
        "   电量百分比 / 时间 / 未读数 / 版本号 下次运行就变了，写进去等于给脚本埋定时炸弹：",
        "     ✗ get_text(//*[@text='剩余电量59%']) + assert_contains('剩余电量59%')",
        "     ✓ get_text(//*[contains(@text,'剩余电量')]) + assert_contains('剩余电量')",
        "   数值大小比较（如「断言电量大于0」）留给第二环写进 pytest 断言，本环只负责取到值并确认存在。",
        "6. 「返回上一级页面」用 back，不要用 xpath 去点返回箭头。",
        "7. 最后必须调 quit 释放设备。",
        "",
        "【结束条件】",
        "全部步骤执行完（含 quit）后，输出 Final Answer，简要说明每步是否成功、"
        "遇到的定位问题是怎么解决的。不要输出脚本代码 —— 写脚本是下一环的事。",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 步骤缓存：src/app/.steps/<用例名>.json
# ---------------------------------------------------------------------------
# 为什么需要缓存：第一环在真机上跑一条用例要几分钟（滚动 + 等待 + 模型往返），
# 而「生成脚本 -> 回执报语法/未定义名 -> 修 -> 再写」这个循环完全不需要重新采集。
# 缓存命中就直接把已验证的步骤喂给第二环，省掉整轮真机执行。
#
# 失效条件（steps_cache_fingerprint）：用例名 / 前提条件 / 测试步骤 / 启动参数任一变化。
# 把启动参数纳入指纹是 App 版特有的：同一条用例换个 app_package 跑，
# 采集到的 resource-id 前缀会变（com.android.settings:id/xxx），旧步骤必然失效。
def steps_cache_path(case: Optional[TestCase]) -> Path:
    """缓存文件路径：src/app/.steps/<脚本名去 .py>.json。"""
    return STEPS_CACHE_DIR / f"{Path(script_name_of_case(case)).stem}.json"


def steps_cache_fingerprint(case: Optional[TestCase], launch: AppLaunch) -> str:
    """用例内容 + 启动参数的指纹；与缓存里存的不一致就说明步骤已过期。"""
    payload = {
        "name": (case.name if case else ""),
        "preconditions": list(case.preconditions) if case else [],
        "steps": list(case.steps) if case else [],
        "app_package": launch.package,
        "app_activity": launch.activity,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def step_failure_reason(step: Mapping[str, Any]) -> str:
    """从失败步骤里抽一句人能看懂的原因（写进缓存，供下次排障与第二环参考）。

    appium_tools._RETRY_HINT 那一大段「请先调用 get_page_source …」是给模型看的行动指令，
    写进缓存纯属噪音，所以按标记切掉，只留前半段的真实错误。
    """
    observation = str(step.get("observation", "") or "")
    head = observation.split("。请先调用 get_page_source", 1)[0]
    head = re.sub(r"\s+", " ", head).strip()
    return head[:300] or f"{step.get('tool', '?')} 执行失败"


def steps_blocking_reason(steps: Sequence[Mapping[str, Any]]) -> str:
    """判断采集到的步骤能否交给第二环；能则返回空串，否则返回原因。

    这道门禁是整个流程的质量闸门。没有它会出现三类废轮次：
      1. agent 没启动 app 就开始 find/click（满屏 "driver 尚未启动" 的 Observation），
         第二环照着写出一份根本跑不起来的脚本 —— 而**本轮新生成**的脚本按约定不再
         run_script 复核（它逐条来自第一环轨迹），这份坏脚本要等下一轮运行被执行时才暴露，
         等于白烧一轮真机 + 一轮修复；
      2. agent 只调了 init + get_page_source 就 Final Answer（模型以为「已经看过了」），
         第二环拿不到任何操作步骤，只能凭空编。
      3. agent 滚动没找到目标就 quit 收尾（轨迹里只有导航类工具），第二环照着写出
         「scroll + assert 元素存在」的占位脚本 —— 见 _ACTION_TOOLS 的说明。
    三类都表现为「白跑一轮真机 + 十几次模型调用」，所以在进第二环之前直接拦掉。
    """
    if not steps:
        return "本轮没有采集到任何步骤（agent 可能一次工具都没调成功）"
    real = [step for step in steps if not str(step.get("tool", "")).startswith("#")]
    if not real:
        return "本轮只有占位步骤，没有任何真实工具调用"
    tools_used = [str(step.get("tool", "")) for step in real]
    missing = [name for name in _REQUIRED_STEP_TOOLS if name not in tools_used]
    if missing:
        return (f"缺少必需工具调用：{'、'.join(missing)}"
                "（每条用例必须以 init 启动 app 开始、以 quit 释放设备结束）")
    if not any(name in _ACTION_TOOLS for name in tools_used):
        return ("没有任何测试动作（click / send_keys / get_text / assert_contains 一个都没调用，"
                f"本轮只调了 {'、'.join(dict.fromkeys(tools_used))}）—— 通常是滚动/查找没命中目标，"
                "agent 就提前 quit 收尾了。请先用 get_page_source 或 "
                "adb shell uiautomator dump 核对界面上的真实文案/控件 id，再重跑采集")
    if tools_used[0] != "init":
        return f"第一步是 {tools_used[0]} 而不是 init，说明 app 还没启动就开始操作了"
    return ""


def load_cached_steps(case: Optional[TestCase],
                      launch: Optional[AppLaunch] = None) -> list[dict[str, Any]]:
    """读缓存步骤；指纹不匹配 / 文件损坏 / 内容为空都返回 []（等于没缓存）。

    缓存里存的是「精简步骤」（tool / input / failed / reason），不含 observation ——
    完整 observation 只在采集当轮喂给第二环，落盘会让缓存膨胀到几 MB 且毫无用处。
    """
    path = steps_cache_path(case)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.getLogger("app.steps").warning("步骤缓存 %s 读取失败，按未缓存处理：%s", path, exc)
        return []
    if launch is not None and payload.get("fingerprint") != steps_cache_fingerprint(case, launch):
        logging.getLogger("app.steps").info(
            "步骤缓存 %s 已过期（用例内容或启动参数变了），将重新采集", path)
        return []
    steps = payload.get("steps")
    return steps if isinstance(steps, list) and steps else []


def save_steps_cache(case: Optional[TestCase], launch: AppLaunch,
                     steps: Sequence[dict[str, Any]]) -> Optional[Path]:
    """把本轮采集到的步骤落盘；只在「步骤完整」时缓存（见 steps_blocking_reason）。

    半截轨迹（没有 quit、没有断言）缓存下来，只会让后续每次都拿到一份注定要返工的步骤，
    还不如下次重新采集。
    """
    blocking = steps_blocking_reason(steps)
    if blocking:
        logging.getLogger("app.steps").info("步骤不完整，跳过缓存：%s", blocking)
        return None
    path = steps_cache_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": steps_cache_fingerprint(case, launch),
        "script_name": script_name_of_case(case),
        "app_package": launch.package,
        "app_activity": launch.activity,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "steps": [
            {
                "tool": step.get("tool", ""),
                "input": step.get("input", ""),
                "failed": bool(step.get("failed", False)),
                "reason": step_failure_reason(step) if step.get("failed") else "",
            }
            for step in steps
        ],
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logging.getLogger("app.steps").warning("步骤缓存写入失败 %s：%s", path, exc)
        return None
    return path


# ---------------------------------------------------------------------------
# 采集前环境自检
# ---------------------------------------------------------------------------
def _adb_devices() -> Optional[list[str]]:
    """列出 adb 已连接且处于 device 状态的设备序列号；adb 不可用返回 None（不作为失败依据）。"""
    import shutil
    import subprocess
    adb = shutil.which("adb")
    if not adb:
        return None
    try:
        finished = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return [line.split("\t", 1)[0].strip() for line in finished.stdout.splitlines()[1:]
            if line.strip().endswith("\tdevice")]


def preflight_appium() -> str:
    """采集前确认 Appium server 与设备就绪；返回空串表示就绪，否则返回一段可读说明。

    移动端环境比浏览器脆弱得多（server 没起、设备掉线、adb 未授权、app 没装）。
    不做这道自检的话，agent 会在「init 失败 -> 换个参数再试 -> 还是失败」上空转满
    max_iterations 轮，白烧几十次模型调用，最后只得到一句「环境问题」。
    """
    import urllib.error
    import urllib.request
    server = resolve_appium_server().rstrip("/")
    try:
        with urllib.request.urlopen(f"{server}/status", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return (f"Appium server（{server}）连不上：{exc}。"
                "请先启动 server（appium -p 4723 --allow-cors），"
                "或用 APPIUM_SERVER 指定正确地址后重跑。")
    if not payload.get("value", {}).get("ready", False):
        return (f"Appium server（{server}）回报未就绪："
                f"{json.dumps(payload, ensure_ascii=False)[:300]}")
    devices = _adb_devices()
    if devices is not None and not devices:
        return ("adb devices 没有列出任何处于 device 状态的设备。"
                "请确认设备已连接并授权（unauthorized 需要在设备上点「允许 USB 调试」），"
                "或用 APP_UDID 指定模拟器/设备序列号。")
    return ""


# ---------------------------------------------------------------------------
# 第一环执行
# ---------------------------------------------------------------------------
def collect_steps_on_device(case: Optional[TestCase], launch: AppLaunch,
                            refs: Sequence[PreconditionRef]) -> tuple[list[dict[str, Any]], str]:
    """在真机上真实执行这条用例，返回 (步骤列表, 说明文本)。

    步骤列表来自 StepRecorder（流式记录，invoke 抛异常也不会丢），
    说明文本用于「采集失败时告诉第二环发生了什么」，让它别拿空步骤硬写脚本。
    """
    log = logging.getLogger("app.collect")
    query = build_query(case, launch, build_precondition_steps(refs))
    recorder = StepRecorder()
    global CURRENT_RECORDER
    CURRENT_RECORDER = recorder
    # 回调统一从这里下发：控制台调试回调（DEBUG_CALLBACKS，--quiet 时为空列表）+ recorder。
    run_callbacks = [*DEBUG_CALLBACKS, recorder]
    log.info("开始真机采集：%s（%d 个步骤）", script_name_of_case(case),
             len(case.steps) if case else 0)
    note = ""
    try:
        result = app_agent_executor.invoke({"input": query},
                                           config={"callbacks": run_callbacks})
        finish = result.get("output", "")
        log.info("采集结束，Final Answer：%s", _short(finish, 1000))
        note = str(finish)
    except BaseException as exc:  # noqa: BLE001 - 采集失败也要把已跑步骤交出去
        log.error("采集过程异常（已保留 %d 步）：%s: %s",
                  len(recorder.steps), exc.__class__.__name__, exc)
        note = f"采集过程抛出异常：{exc.__class__.__name__}: {exc}"
    finally:
        # 无论成功失败都要释放设备：session 泄漏会让后续所有运行都建不起 driver
        try:
            app.quit()
        except BaseException as exc:  # noqa: BLE001
            log.warning("采集收尾 quit 失败（已忽略）：%s", exc)
    return recorder.steps, note


def resolve_steps(case: Optional[TestCase], launch: AppLaunch,
                  refs: Sequence[PreconditionRef]) -> tuple[list[dict[str, Any]], str]:
    """决定本轮的步骤来源：缓存 / 重新采集，并做完整性门禁。

    返回 (steps, note)：steps 为空表示本轮没有可用步骤，note 说明原因；
    steps 不完整（见 steps_blocking_reason）时调用方会短路第二环，不落占位脚本。
    """
    log = logging.getLogger("app.steps")
    if not force_collect():
        cached = load_cached_steps(case, launch)
        if cached:
            blocking = steps_blocking_reason(cached)
            if not blocking:
                log.info("命中步骤缓存 %s（%d 步），跳过真机采集",
                         steps_cache_path(case), len(cached))
                return cached, "本轮未采集步骤：命中 src/app/.steps 缓存，直接复用已验证的步骤。"
            log.info("缓存步骤不完整（%s），转为重新采集", blocking)
    problem = preflight_appium()
    if problem:
        log.warning("环境自检未通过：%s", problem)
        return [], f"本轮未采集步骤：{problem}"
    steps, note = collect_steps_on_device(case, launch, refs)
    blocking = steps_blocking_reason(steps)
    if blocking:
        log.warning("采集结果不完整：%s", blocking)
        return steps, (f"本轮采集到的步骤不完整：{blocking}。"
                       f"采集过程的 Final Answer：{note[:500]}")
    saved = save_steps_cache(case, launch, steps)
    if saved:
        log.info("步骤已缓存到 %s", saved)
    return steps, note


def app_execute_result(_inputs: dict) -> str:
    """第一环入口：RunnablePassthrough.assign(steps=...) 调用的就是它。

    返回采集到的步骤 json；若本轮没有可用步骤，则把原因写进 CODEGEN_SKIP_REASON
    并返回空串 —— 第二环据此短路（见 _invoke_codegen_agent），不去写占位脚本。

    用例 / 启动参数 / 前置解析结果一律读模块级 CURRENT_*（run_case_chain 在 invoke 前设好），
    不在这里重新解析：一是省掉一次重复的前置匹配，二是递归生成前置脚本时
    （allow_precondition=False）CURRENT_REFS 是空的，这里再解析就会把递归层数放穿。
    """
    global CODEGEN_SKIP_REASON
    CODEGEN_SKIP_REASON = ""
    case, launch, refs = CURRENT_CASE, CURRENT_LAUNCH, CURRENT_REFS
    steps, note = resolve_steps(case, launch, refs)
    blocking = steps_blocking_reason(steps) if steps else (note or "本轮没有采集到步骤")
    if blocking:
        logging.getLogger("app.steps").warning("本轮步骤不可用，跳过代码生成：%s", blocking)
        CODEGEN_SKIP_REASON = build_skip_message(blocking, case)
        return ""
    payload = [
        {
            "tool": step.get("tool", ""),
            "input": step.get("input", ""),
            "failed": bool(step.get("failed", False)),
            "reason": step_failure_reason(step) if step.get("failed") else "",
        }
        for step in steps
        if not str(step.get("tool", "")).startswith("#")
    ]
    log = logging.getLogger("app.steps")
    log.info("交给第二环的步骤共 %d 条（其中失败 %d 条）%s",
             len(payload), sum(1 for item in payload if item["failed"]),
             f"；采集说明：{_short(note, 300)}" if note else "")
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 第二环：把真实轨迹落成 pytest 脚本
# ---------------------------------------------------------------------------
def _repair_illegal_tool_args(text: str) -> str:
    """修复 qwen 在 Action Input 里产出的非法 JSON（字符串内嵌裸双引号）。

    实测高频形态：write_script 的 code 入参里，Python 代码含 `assert "x" in text`，
    模型忘了把内层双引号转义，于是整段 Action Input 变成非法 JSON。
    handle_parsing_errors 只能让模型重试，而它往往连着几轮犯同一个错，白白耗尽
    max_iterations（脚本一个字都没写盘）。这里先做一次确定性修复：
    找到 `"code": "` 之后到该行末尾 `"` 之前的内容，把内部裸双引号补上反斜杠。
    修不动就原样返回，交给 handle_parsing_errors 兜底。
    """
    marker = '"code":'
    start = text.find(marker)
    if start < 0:
        return text
    quote = text.find('"', start + len(marker))
    while quote >= 0 and text[quote + 1:quote + 2] in (" ", "\n", "\t"):
        quote = text.find('"', quote + 1)
    if quote < 0:
        return text
    end = text.find('\n', quote)
    if end < 0:
        end = len(text)
    body = text[quote + 1:end].rstrip()
    if not body.endswith('"'):
        return text
    inner = body[:-1]
    fixed = re.sub(r'(?<!\\)"', r'\\"', inner)
    if fixed == inner:
        return text
    return text[:quote + 1] + fixed + '"' + text[end:]


APP_FIXTURE_DOC = '''\
# driver fixture 必须这样写（Appium / Android）：
#   - 统一 import src.app.app_framework 里的 create_driver，**不要自己手拼 capabilities**：
#     server 地址 / 设备 udid 由环境变量决定，写死在脚本里换台机器就跑不了。
#   - 必须 yield 之后 quit：session 不释放会一直占着设备，后续用例全部起不来。
import pytest
from src.app.app_framework import (
    create_driver, locate, locate_all, scroll_to, texts_of, page_text,
)


@pytest.fixture
def driver():
    driver = create_driver(app_activity="{app_activity}", app_package="{app_package}")
    yield driver
    driver.quit()
'''

APP_CODE_RULES = '''\
# App 脚本代码规范（务必遵守）：
# 1. 结构：业务步骤写在一个独立函数里（函数名用测试步骤的语义，如 check_battery），
#    test_* 函数只负责调它 + 断言；driver 由上面的 fixture 注入。
# 2. 定位：一律用 app_framework 的 locate / locate_all / scroll_to（内部已做
#    「显式等待 + 定位方式分流」），**不要**直接写 driver.find_element(by, expr)，
#    更不要自己算 AppiumBy —— 定位表达式原样照抄第一环验证过的那一份。
#    例外：第一环若用了 `@text='...'` **全等**匹配去取带动态值的文本（如「剩余电量」），
#    脚本里要改成 contains 前缀匹配（`//*[contains(@text,'剩余电量')]`）——
#    真机上的完整文本是「剩余电量59%」，全等匹配下次就取不到了。
# 3. 脚本必须**自包含**：用到 re / time / random 就要在文件头 import 它们。
#    write_script 会做未定义名检查并拒收（Observation 里给出行号），别指望 conftest 代劳。
# 4. 列表/长页面里的条目：先 scroll_to 拿到元素，**判空之后直接用返回的元素**点击，
#    不要滚完再 locate 一次（重复定位；滚动失败时只会得到一句 TimeoutException）：
#       item = scroll_to(driver, "//*[contains(@text,'省电与电池')]", max_swipes=15)
#       assert item is not None, '未能找到「省电与电池」设置项'
#       item.click()
#    不要用 sleep 兜底。
# 5. 取文本：定位表达式要指到**带文本的控件本身**，不要加 `//..` 去取父节点 ——
#    容器/父节点没有 text，texts_of 只会抛「都没有可读文本」。
#    texts_of / locate 取不到时是抛异常（不是返回空串），所以不需要再包一层判空。
# 6. 等待：统一用 locate/locate_all 的 timeout 参数（默认 10s）；
#    禁止 driver.implicitly_wait（会让 find_elements 判空也阻塞同样久，滚动循环慢 N 倍）。
# 7. 返回上一级：driver.back()，不要 xpath 去点返回箭头。
# 8. 每一步都要有中文注释说明它对应测试用例里的哪一步。
'''

APP_ASSERT_RULES = '''\
# 断言规范：
# 1. 文本断言优先限定范围：texts_of(driver, "id=com.android.settings:id/dashboard_container")
#    比整页 page_text(driver) 可靠 —— 整页会混入状态栏/导航栏文本，容易误判通过。
#    确实需要整页判断时才用 page_text(driver)。
# 2. 断言失败信息里必须带上「实际取到的文本片段」，否则失败时根本不知道界面上有什么：
#       text = page_text(driver)
#       assert "系统打印服务帮助" in text, f"界面未包含「系统打印服务帮助」，实际片段：{text[:300]}"
# 3. 数值类断言（如「断言电量大于0」）：先用 locate 取控件文本，再 re 抽数字比较；
#    re.search 可能返回 None，**必须先判空再取值**，否则文本里没有数字时抛的是
#    AttributeError，而不是一条能看懂的断言失败：
#       raw = texts_of(driver, "//*[contains(@text,'剩余电量')]")
#       match = re.search(r"(\\d+)", raw)
#       assert match, f"没能从剩余电量文本里抽出数字，实际：{raw!r}"
#       level = int(match.group(1))
#       assert level > 0, f"剩余电量应大于 0，实际 {raw!r}"
#    抽不到数字要让断言失败并给出原文，不要静默跳过。
# 4. **动态数值不能照抄**：第一环采集时界面上是「剩余电量59%」，步骤里就可能留下
#    `get_text(//*[@text='剩余电量59%'])` / `assert_contains('剩余电量59%')` 这种快照值。
#    写进脚本等于定时炸弹（电量一变就红）。一律改写成稳定前缀 + 上面的抽数比较，
#    并按测试步骤原文的语义断言（原文说「断言电量大于0」，就不要断言「包含 59%」）。
#    同理适用于时间、未读数、版本号、剩余空间等一切会变的值。
# 5. 不要断言第一环没有验证过的内容（凭想象加的断言必然失败）。
'''

APP_ENTRY_DOC = '''\
# 可复用入口（前置用例被别的用例 import 时必须满足）：
#   - 业务步骤函数签名统一为 def <name>(driver): ...，不自己建/关 driver；
#   - test_* 函数形如 def test_xxx(driver): <name>(driver)；
#   - 这样其它脚本可以 `from src.app.scripts.<模块名> import <name>` 直接复用。
'''


# 第二环的任务模板。占位符除 {task} 外都由 codegen_prompt_inputs 组装；
# {task} 是 chain.invoke 传进来的 `input`（run_case_chain 传 codegen_input(case)），
# 即「本轮要干什么」的业务口径 —— 与 web 版 CODEGEN_TASK 的 {task} 同构，
# 两边读起来是同一套约定（差别见 codegen_input 的 docstring）。
# 模板用 PromptTemplate.from_template（str.format）渲染：除下面这些占位符外，
# 模板里不要再出现裸花括号，需要字面量花括号时写成 {{}}。
CODEGEN_TASK = """你是一个 app 自动化测试工程师，技术栈为 pytest + Appium（Android / UiAutomator2）。
你的任务：把下面这次在真机上真实执行过的测试步骤，落成一个可重复运行的自动化测试脚本。
脚本**已存在**时：read_script 读出来 + run_script **直接执行**复核，执行步骤失败
（断言成功/失败不在判断范围内）就修复脚本，直到除断言之外的执行步骤全部成功；
脚本是**本轮新生成**的：write_script 落盘即完成，不要再执行它 —— 上面的步骤已经在真机上
把这条用例连断言完整跑通过一遍，落盘后再跑一次纯属重复执行（理由与要求见下面第 3 条）。

{task}

目标脚本：{scripts_dir} 目录下的 {script_name}
被测 app：{app_launch}

{testcase_desc}

前提条件解析结果：
{precondition_desc}

{precondition_note}本次真实执行过的测试步骤（json 数组：tool 是工具名，input 是工具入参，
failed=true 表示这一步当时失败了、reason 是失败原因）；
其中的定位表达式都是当时在真机上真实存在、且已经验证可用的：

{steps}

{fixture_doc}
{code_rules}
{assert_rules}
{entry_doc}
必须严格按以下流程使用工具，不要臆测文件是否存在：
1. 先调用 list_scripts，确认 {script_name} 是否已经存在；
2. 已存在（上一轮生成过，本轮命中步骤缓存）：调用 read_script 读取内容，与上面的步骤 json
   逐条对照，再调用 run_script **直接执行**复核，然后按执行结论分支处理：
   a) 执行通过（exit code 0）-> 不必重写，直接给出 Final Answer；
   b) **脚本执行步骤失败**（定位不到控件 / 等待超时 / 语法或导入错误 / 漏了测试步骤 /
      定位表达式与本轮已验证的不一致 / 违反上面的代码规范 / 前置用例没有 import 复用）
      -> 用 write_script 写入修复后的**完整**代码，然后再 run_script 确认，最多修复 2 轮，
      直到除断言之外的执行步骤全部成功；
   c) **断言成功/失败不在判断范围内**：纯断言失败（AssertionError，且输出里没有 appium /
      定位 / 超时类异常）说明脚本本身跑得通，不要为了让断言变绿去改执行步骤，
      在 Final Answer 里如实报告哪条断言失败、失败时实际取到的文本是什么即可；
      唯一该改的断言缺陷是「照抄了采集那一刻的动态数值」（见上面的断言规范第 4 条）；
   d) 同一个原因连续失败两次，说明不是改一行就能好的问题（多为设备/被测 app 状态），
      立即停止修复并在 Final Answer 中说明，不要重复写入内容相同的代码；
3. 不存在：按上面的「代码规范」生成完整脚本，调用 write_script 保存，**保存完就结束** ——
   write_script 只做语法检查 + 未定义名检查 + 落盘，落盘即完成；保存之后也**不要**再调用
   run_script 去跑它。原因：本轮的步骤 json 就是第一环在真机上把这条用例（含断言）完整跑通
   后记录下来的，脚本里每个定位表达式都刚刚验证过；落盘后再执行一遍等于把同一条用例重复跑
   一次（多起一次 Appium session、多占设备几十秒），信息量几乎为零，还可能因为设备状态抖动
   把本来正确的脚本误判成有问题、进而改坏它。需要确认脚本可用性时，在 Final Answer 里提示
   开发者自己执行：`python -m pytest {scripts_dir}/{script_name}`；
4. 失败回执要按类型处置，不要一律当成脚本问题去改代码：
   a) run_script 报环境类失败（Connection refused / Could not start a new session /
      设备不在线 / InvalidSessionIdException）-> **不是脚本的问题**：不要为此改定位表达式，
      直接在 Final Answer 里提示开发者启动 Appium server（默认 http://127.0.0.1:4723）、
      用 `adb devices` 确认设备在线后重跑；
   b) write_script 回执报「语法检查未通过」或「用到了从未导入 / 从未定义的名字」
      -> 按回执给出的行号修正（通常是漏了 import re / from time import sleep）后重新写入；
   c) 若发现步骤 json 里的定位表达式与测试步骤原文对不上（很可能是 app 改版、采集步骤已过期），
      不要自己臆测新的 xpath / resource-id，直接在 Final Answer 中提示：用
      `python src/app/generate_autoapp.py --testcase "{case_name}" --force-collect`
      重新在真机上采集步骤；
5. failed=true 的步骤：不要原样照抄进脚本。先判断 reason ——
   若是「滚动不够/控件还没渲染」这类时序问题，脚本里用 scroll_to + locate(timeout=...) 解决；
   若是「定位表达式本身错了」而后续步骤又用了新的表达式成功，就采用成功的那一个；
   若这一步最终没跑通，在脚本对应位置写注释说明「此步在采集时失败：<reason>」，
   并在 Final Answer 里明确报告，不要假装它成功。
6. 结束后给出 Final Answer，说明：脚本绝对路径、执行结论（本轮新生成的写「已按第一环真实
   执行过的步骤落盘，未重复执行」；复核已有脚本的写 执行通过 / 断言失败 / 修复了几轮、
   修完是否已通过）、关键改动、以及有哪些步骤是采集时就失败因而需要人工确认的。
"""


def build_skip_message(reason: str, case: Optional[TestCase]) -> str:
    """第一环产出不合格时，直接返回给调用方的结论（本轮不调用第二环）。

    为什么不让第二环「凑合着写」：脚本一旦落盘，下一轮 run 会看到「脚本已存在」，
    模型倾向于只做小修小补，那份凭想象编出来的 xpath / resource-id 就再也换不掉了 ——
    这正是 web 版踩过的坑（占位脚本永远不被真实步骤替换）。所以宁可本轮什么都不写，
    把原因和重跑命令交代清楚。
    """
    retry = (f'python src/app/generate_autoapp.py --testcase "{case.name}" --force-collect'
             if case else "python src/app/generate_autoapp.py --force-collect")
    return (
        f"脚本未生成：{reason}\n"
        "为避免落下只有 pass / 「待补充」的占位脚本（脚本一旦存在，后续运行容易被模型\n"
        "当成「只需小修」而永远保留错误的定位表达式），本轮不调用代码生成 agent，也不写任何文件。\n"
        f"排查上面的原因后重跑：{retry}"
    )


# 第一环判定「本轮没有可用步骤」时写入；_invoke_codegen_agent 据此短路，不调用 agent。
CODEGEN_SKIP_REASON: str = ""


def build_precondition_note(refs: Sequence[PreconditionRef]) -> str:
    """把前提条件的处理要求渲染成第二环 prompt 的一段约束。

    matched 的前置用例已经有独立脚本（且带可复用入口），这里要求 import 复用而不是复制粘贴 ——
    复制粘贴会让同一段登录/导航逻辑散落在多个脚本里，前置流程一变就要改 N 处。
    """
    grouped = summarize_preconditions(refs)
    matched = grouped.get("matched", [])
    if not matched:
        return ""
    lines = ["前提条件处理方式（必须遵守）："]
    for ref in matched:
        module = Path(ref.script_name).stem
        if ref.entry_name:
            lines.append(
                f"- 「{ref.text}」由前置脚本 {ref.script_name} 提供，"
                f"直接 `from src.app.scripts.{module} import {ref.entry_name}` 复用，"
                f"在本用例的业务函数开头调用 {ref.entry_name}(driver)，**不要把它的步骤复制进来**；")
        else:
            lines.append(
                f"- 「{ref.text}」对应前置脚本 {ref.script_name}，但它还没有可复用入口："
                f"先 read_script 看清结构，按 APP_ENTRY_DOC 把业务步骤抽成 "
                f"`def <语义名>(driver)` 并 write_script 回写（改的是**既有**前置脚本，"
                f"回写后可以 run_script 复核一次；本轮新生成的目标脚本不要重复执行），"
                f"再 import 复用；")
    lines.append("- 前置脚本与本用例共用同一个 driver fixture（同一个 Appium session），"
                 "不要在前置函数里再建 driver。")
    return "\n".join(lines) + "\n\n"


def codegen_input(case: Optional[TestCase] = None) -> str:
    """第二环（代码生成 agent）的任务描述，即 CODEGEN_TASK 里 {task} 的内容。

    与 web 版 src/web/generate_autoweb.codegen_input 同构：以前 app 这边 chain.invoke 传的是
    `{"input": ""}`，而模板里压根没有对应占位符，「本轮要干什么」全靠 CODEGEN_TASK 里那段
    写死的工具流程兜着。现在改成显式传任务描述，「查脚本是否存在 -> 存在就**直接执行**、
    执行步骤失败就修复到跑通（断言不计）/ 不存在就按测试步骤生成 -> 前提条件必须 import 复用」
    这几条业务要求出现在 prompt 最前面，措辞与 web 版逐句一致。

    脚本名取自用例名（script_name_of_case）：md 里新增一条用例，不需要改这里的任何字符串；
    case 为空（--no-testcase / 旧入口）时退化成用例文档第一条用例的脚本名。

    与 web 原文的唯一差别是技术栈措辞（「web自动化测试」->「app自动化测试」）：
    app 领域的 script_tools 现在同样提供 run_script（ScriptTarget.expose_run_script=True，
    见模块 docstring 第 7 条），所以「存在则直接执行、执行步骤失败则修复到全部成功」
    在 App 上完全成立，不需要再改写成「只与真机步骤对照」的弱化版本。
    执行口径的细节（最多修 2 轮、纯断言失败不改脚本、环境类失败不要改定位）
    写在 CODEGEN_TASK 的工具流程里。
    """
    script_name = script_name_of_case(case)
    return f"""请根据以上的信息，给出对应的 app 自动化测试的代码:
        首先在 `{SCRIPTS_DIR}` 文件夹下查找是否存在对应自动化脚本 `{script_name}`，
        如果存在则直接执行，执行时如果是脚本执行步骤失败(断言成功/失败不在判断范围内)，则修复脚本直到除断言之外的执行步骤全部成功
        如果不存在则按照测试步骤生成自动化测试脚本且保存在: {SCRIPTS_DIR} 文件夹下，名称为 {script_name}
        另外：本用例「前提条件」里引用的前置用例，只要 `{SCRIPTS_DIR}` 下已存在对应脚本，
        就必须 import 复用它的入口函数（具体要求见下方「前提条件」的处理方式），不要把前置流程在本脚本里重写一遍
        """


def codegen_prompt_inputs(case: Optional[TestCase], launch: AppLaunch,
                          refs: Sequence[PreconditionRef]) -> dict[str, str]:
    """组装 CODEGEN_TASK 的静态字段（真机步骤 {steps} 由 chain 传入）。"""
    testcase_desc = describe_test_cases([case] if case else [])
    # 采集过程中抓到的界面摘要：第二环写断言时据此把范围限定到具体控件/列表，
    # 而不是整页 `assert "xxx" in page_text(driver)`（后者会混入状态栏文本，容易假通过）。
    snapshots = list(getattr(CURRENT_RECORDER, "step_sources", []) or [])
    if snapshots:
        testcase_desc += "\n\n采集时抓到的界面控件摘要（用于确定断言范围，最多 3 段）：\n"
        testcase_desc += "\n---\n".join(snapshots[-3:])
    return {
        "scripts_dir": str(SCRIPTS_DIR),
        "script_name": script_name_of_case(case),
        "case_name": (case.name if case else "") or Path(script_name_of_case(case)).stem,
        "app_launch": launch.describe(),
        "testcase_desc": testcase_desc,
        "precondition_desc": describe_preconditions(refs),
        "precondition_note": build_precondition_note(refs),
        "fixture_doc": APP_FIXTURE_DOC.format(app_activity=launch.activity,
                                              app_package=launch.package),
        "code_rules": APP_CODE_RULES,
        "assert_rules": APP_ASSERT_RULES,
        "entry_doc": APP_ENTRY_DOC,
    }


def _invoke_codegen_agent(prompt_value: Any) -> str:
    """调用第二环 agent 并返回 Final Answer；第一环产出不合格时直接短路。

    短路判断放在这里而不是 chain 外面：chain 是模块级常量，无法按运行时结果改结构；
    放在这个 RunnableLambda 里既保留了「一条 chain 跑完两环」的可读性，
    又能确保半截轨迹永远不会变成一份占位脚本。

    这里显式传 callbacks（控制台调试回调）：AgentExecutor.invoke 不会自动继承外层 chain
    的 config，不传的话第二环的工具调用一行 tracer 日志都不打，
    而「脚本为什么写成这样」恰恰只能从那段轨迹里看出来。
    """
    if CODEGEN_SKIP_REASON:
        print(CODEGEN_SKIP_REASON)
        return CODEGEN_SKIP_REASON
    text = prompt_value.to_string() if hasattr(prompt_value, "to_string") else str(prompt_value)
    try:
        result = codegen_agent_executor.invoke(
            {"input": text}, config={"callbacks": list(DEBUG_CALLBACKS)})
    except Exception as exc:  # noqa: BLE001 - 第二环异常不能让整条 chain 以 traceback 收尾
        message = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
        logging.getLogger("app.codegen").exception("代码生成 agent 执行异常：%s", message)
        return f"脚本生成失败：{message}（加 --debug-events=all 重跑可看到完整工具轨迹）"
    return str(result.get("output", ""))


# ---------------------------------------------------------------------------
# 第二环的 chain
# ---------------------------------------------------------------------------
codegen_prompt_template = PromptTemplate.from_template(CODEGEN_TASK)


def _codegen_prompt_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把 chain 的透传字段（input / steps）与静态 prompt 字段合成 CODEGEN_TASK 的渲染入参。

    {task} 取 chain.invoke 传进来的 `input`（run_case_chain 传的是 codegen_input(case)）；
    为空时兜底再算一次 codegen_input(CURRENT_CASE)：单独
    `from src.app.generate_autoapp import chain` 调试、或调用方忘了传 input 时，
    prompt 里不会留下一段空白的任务描述（web 版在 persist_and_verify 里用
    `inputs.get("input", "")` 取同一段文本，只是那边不兜底）。

    payload 里多出来的键（input / steps 之外的）不会有问题：PromptTemplate 最终走
    str.format，多余的 kwargs 会被忽略。
    """
    task = str(payload.get("input") or "").strip() or codegen_input(CURRENT_CASE)
    return {**payload,
            "task": task,
            **codegen_prompt_inputs(CURRENT_CASE, CURRENT_LAUNCH, CURRENT_REFS)}


# 把第一环产出的 {steps}（真机步骤 json）与 run_case_chain 传入的 {task}（codegen_input(case)）
# 一起填进 CODEGEN_TASK，交给 codegen agent（structured chat + script_tools）
# 读脚本 / 写脚本 —— 注意它**不跑脚本**。
#
# 为什么第二环是 agent 而不是「llm | StrOutputParser」：
# 早先这里让模型把整份脚本打印出来，再由外层代码写文件。实测两个硬伤：
#   1. 长代码里模型经常漏转义，输出的 python 根本没法 ast.parse；
#   2. 语法 / 未定义名这类问题要等外层写盘之后才发现，失败只能靠外层再开一轮。
# 换成 agent + script_tools 之后，模型直接调 write_script：工具内部先做 compile 语法自检
# 与未定义名自检，问题连同行号作为 Observation 回给模型，它当轮改好再写一次即可，
# 闭环在一个 agent 里完成（这一环**可以执行脚本**：已存在的脚本要 run_script 直接复核、
# 修复之后再确认；本轮新生成的脚本落盘即完成，不重复执行）。
chain = (
    RunnablePassthrough.assign(steps=RunnableLambda(app_execute_result))
    | RunnableLambda(_codegen_prompt_payload)
    | codegen_prompt_template
    | RunnableLambda(_invoke_codegen_agent)
)


# ---------------------------------------------------------------------------
# 前置脚本复用：把「步骤写在 test_ 里」的旧脚本确定性地重构成可 import 的结构
# ---------------------------------------------------------------------------
# 为什么要用 ast 做确定性重构，而不是让模型改：
# 让模型 read_script -> 改写 -> write_script 有三个反复出现的问题：
#   1. 长脚本里模型会顺手「优化」掉断言或改定位表达式，把已经跑通的逻辑改坏；
#   2. 中文字符串偶尔被转义成 \uXXXX，diff 噪音极大；
#   3. 一来一回至少两次模型调用，而且不保证成功。
# 这里只做一件纯机械的事：把唯一的 `test_xxx(driver)` **原地改名**成 `xxx(driver)`，
# 再在文件末尾追加一个转发包装，保证 pytest 仍然收集得到这条用例。
# 业务代码 / fixture / import / 定位表达式一个字符都不动，结果完全可预期。
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
        raise ValueError(
            f"{node.name} 的参数不是「单个 driver」（实际：{', '.join(positional) or '无参'}）")
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


def refactor_script_into_entry(candidate: Path) -> Optional[str]:
    """给「只有 test_xxx(driver)、没有可复用入口」的脚本补一个入口函数；成功返回入口名。

    改动只有两处（业务逻辑、fixture、import、定位表达式全部原样不动）：
        1. 唯一的 `test_检查电源(driver)` 原地改名为 `检查电源(driver)`；
        2. 文件末尾追加转发包装：`def test_检查电源(driver): 检查电源(driver)`。

    重构结果一律先过 `ast.parse` 语法自检，不通过就保持原文件不动（宁可没有入口，
    也不能把一份已经跑通的脚本改成坏的）。

    **本函数自身不执行脚本验证**：两个调用点（第二环刚结束的目标脚本、刚由真机步骤生成的
    前置脚本）本轮都已经在设备上把这条用例完整跑过一遍了，而重构只是「改名 + 加转发」的
    纯机械变换，再跑一遍等于把同一条用例重复执行（多启动一次 app、多等几十秒）；更糟的是
    设备状态抖动会让「验证」误判成结构性失败，把一次有用的重构回滚掉，问题反而是这次验证
    凭空制造的。
    注意这与第二环的执行口径不冲突：**既有**脚本（含被本函数重构过的前置脚本）在下一轮
    命中「脚本已存在」分支时，仍会被 run_script 直接执行复核（见模块 docstring 第 7 条）。
    """
    log = logging.getLogger("app.entry")
    try:
        source = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("读取 %s 失败，跳过入口重构：%s", candidate, exc)
        return None
    try:
        entry_name, test_name, lineno, wrapper = _entry_refactor_plan(source)
    except ValueError as exc:
        log.info("%s 不满足安全重构条件，保持原样：%s", candidate.name, exc)
        return None

    lines = source.splitlines(keepends=True)
    def_index = lineno - 1
    if def_index >= len(lines) or not lines[def_index].lstrip().startswith("def "):
        log.warning("%s 第 %d 行不是 def 语句，放弃重构（行号定位失败）", candidate.name, lineno)
        return None
    lines[def_index] = lines[def_index].replace(f"def {test_name}(", f"def {entry_name}(", 1)
    rebuilt = "".join(lines).rstrip("\n") + "\n" + wrapper
    try:
        ast.parse(rebuilt)
    except SyntaxError as exc:
        log.warning("%s 重构后语法检查未通过，放弃改动：%s", candidate.name, exc)
        return None
    try:
        candidate.write_text(rebuilt, encoding="utf-8")
    except OSError as exc:
        log.warning("写入 %s 失败，入口重构未落盘：%s", candidate, exc)
        return None
    log.info("已把 %s 的 %s 重构出可复用入口 %s(driver)", candidate.name, test_name, entry_name)
    return entry_name


def ensure_precondition_scripts(refs: Sequence[PreconditionRef],
                                all_cases: Sequence[TestCase]) -> list[PreconditionRef]:
    """确保 matched 的前置脚本存在且带可复用入口，返回补全 entry_name 后的 refs。

    顺序很重要：先保证前置脚本本身是好的（必要时递归生成），再让本用例去 import 它。
    反过来做的话，本用例 import 一个还不存在的模块，脚本一执行必然 ImportError；
    而本轮新生成的脚本按约定不再 run_script 复核（见模块 docstring 第 7 条），
    这个错误要等下一轮运行才暴露，届时的「修法」往往是把 import 删掉改成复制粘贴 ——
    复用就白设计了。
    """
    log = logging.getLogger("app.precondition")
    updated: list[PreconditionRef] = []
    for ref in refs:
        if not ref.is_matched or ref.case is None:
            updated.append(ref)
            continue
        candidate = SCRIPTS_DIR / ref.script_name
        if not candidate.is_file():
            log.info("前置脚本 %s 不存在，先生成它", ref.script_name)
            run_case_chain(ref.case, all_cases, allow_precondition=False)
        entry = find_reusable_entry(candidate) or refactor_script_into_entry(candidate)
        if entry:
            updated.append(PreconditionRef(
                text=ref.text, state=ref.state, case=ref.case,
                script_name=ref.script_name, entry_name=entry))
        else:
            log.warning("前置脚本 %s 没能抽出可复用入口，本用例将按字面自行实现", ref.script_name)
            updated.append(PreconditionRef(text=ref.text, state="inline"))
    return updated


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_case_chain(case: Optional[TestCase], all_cases: Sequence[TestCase],
                   allow_precondition: bool = True) -> str:
    """跑完一条用例的完整链路：解析前提 -> 采集步骤 -> 生成/修复脚本，返回第二环结论。

    allow_precondition=False 用于「生成前置脚本本身」的递归调用：
    前置脚本再去找它自己的前置，很容易在 md 写法不规范时形成环（A 依赖 B、B 又提到 A），
    所以递归时只展开一层。
    """
    global CURRENT_CASE, CURRENT_LAUNCH, CURRENT_REFS
    log = logging.getLogger("app.run")

    launch = parse_app_launch(case)
    refs: list[PreconditionRef] = []
    if allow_precondition and not skip_precondition():
        refs = resolve_preconditions(case, all_cases, SCRIPTS_DIR)
        refs = ensure_precondition_scripts(refs, all_cases)
        unresolved = [ref.text for ref in refs if ref.is_unresolved]
        if unresolved:
            log.warning("以下前提条件在 md 里找不到对应用例，将按字面实现：%s",
                        "；".join(unresolved))
    CURRENT_CASE, CURRENT_LAUNCH, CURRENT_REFS = case, launch, refs
    log.info("=== 用例 %s | 脚本 %s | %s | 前提 %d 条 ===",
             case.name if case else "(未指定)", script_name_of_case(case),
             launch.describe(), len(refs))
    # 第二环的任务描述显式传进 chain（与 web 版 run_case_chain 的
    # `chain.invoke({"input": codegen_input(case), ...})` 同构）：它就是 CODEGEN_TASK 里
    # {task} 的内容 ——「查脚本是否存在 -> 存在就**直接执行**复核、执行步骤失败就修复到
    # 跑通（断言不计）/ 不存在就按测试步骤生成 -> 前提条件必须 import 复用」，
    # 见 codegen_input。
    # 其余一切（用例 / 启动参数 / 前置解析结果）仍走模块级 CURRENT_*，真机步骤 json 走
    # RunnablePassthrough.assign(steps=...)，都不塞进这个 input dict：多塞键会让 prompt
    # 模板长出一堆与模型无关的占位符（理由见 CURRENT_* 处的说明）。
    # config=run_config 把控制台 handler 作为**可继承**回调传下去（构造参数是不可继承的
    # local_callbacks，见 run_config 处的说明）：外层 chain 的 tracer 日志不会触发原版
    # ConsoleCallbackHandler 的 KeyError('input')；--quiet 时 callbacks 为 None，
    # 外层 chain 一行 tracer 日志都不打。
    result = chain.invoke({"input": codegen_input(case)}, config=run_config)
    return str(result) if result else ""


def list_cases(all_cases: Sequence[TestCase]) -> None:
    """打印 md 里的全部用例（用例名 / 启动参数 / 前置依赖 / 测试步骤 / 脚本与缓存状态）。

    这是「用例究竟从 md 里读出了什么」的自检入口：不连设备、不调大模型，
    解析错一步就能在这里看出来（层级名拼错、启动参数被当成前置用例、步骤漏读……）。
    """
    print(f"用例来源：{TESTCASE_FILE}")
    print(f"脚本目录：{SCRIPTS_DIR}")
    if not all_cases:
        print("（没有解析到用例：检查 md 是否有「# 标题 + - 测试步骤:」结构）")
        return
    print(f"共 {len(all_cases)} 条用例（不带 --testcase 时默认全部顺序执行）：\n")
    for index, case in enumerate(all_cases, 1):
        script = SCRIPTS_DIR / script_name_of_case(case)
        cached = steps_cache_path(case)
        marks = [f"脚本{'✓' if script.is_file() else '✗'}",
                 f"缓存{'✓' if cached.is_file() else '✗'}"]
        print(f"{index:>3}. {case.name}  ->  {case.script_name}  [{', '.join(marks)}]")
        print(f"       标题层级：{case.hierarchy}（md 第 {case.line_no} 行）")
        print(f"       启动参数：{parse_app_launch(case).describe()}")
        # 前提条件要经 resolve_preconditions 才看得出「是前置用例 / 泛指写法 / 根本没匹配上」；
        # 直接打印 case.preconditions 会把「打开 app activity ...」这类**启动参数**也列成前提条件，
        # 与上一行重复，还会让人以为要去找一条名叫「打开 app activity」的前置用例。
        refs = resolve_preconditions(case, all_cases, SCRIPTS_DIR)
        print(f"       前置依赖：{describe_preconditions(refs)}")
        for step in case.steps:
            print(f"       - {step}")
        if case.expected:
            print(f"       预期：{'；'.join(case.expected)}")
    print('\n用法：python src/app/generate_autoapp.py --testcase "<用例名>"'
          "   # 不带参数则跑文档里的全部用例")


def _select_targets(all_cases: Sequence[TestCase]) -> tuple[list[Optional[TestCase]], int]:
    """把 CLI / 环境变量的用例选择解析成要跑的用例列表，返回 (targets, 退出码)。

    匹配规则复用 testcase_md.select_test_cases（与 web 版同一套：全名 / 层级后缀 /
    末级标题 / 子串都能匹配），默认行为也与 web 版对齐：**不带任何用例开关时，
    跑用例文档里的全部用例**（setting.md 里有几条就跑几条）。

    曾经默认只跑第 1 条，后果很实在：setting.md 里第二条及以后新增的用例
    （如「更多链接_打印」）永远不会被执行，看起来像「用例场景丢了」，实际只是没被选中
    —— --list-cases 仍能完整列出，很容易误判成 md 解析问题。
    调试单条时显式加 --first-case，或用 --testcase 指定名字。
    """
    if _cli_flag("--no-testcase"):
        print("已指定 --no-testcase：不选用例，按「无测试用例」模式跑一遍流程。")
        return [None], 0
    keyword = (_arg_value("--testcase") or _arg_value("--case")
               or os.getenv("APP_TESTCASE", "").strip())
    run_all = not (keyword or _cli_flag("--first-case", "--first"))
    if not all_cases:
        return [None], 0
    try:
        selected = select_test_cases(all_cases, keyword, select_all=run_all)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return [], 2
    if not selected:
        print("没有选中任何用例。", file=sys.stderr)
        return [], 2
    return list(selected), 0


def _cli_flag(*flags: str) -> bool:
    """这些开关里任意一个出现在命令行就算命中（别名写法与 web 版一致）。"""
    return any(flag in sys.argv for flag in flags)


def _arg_value(flag: str) -> str:
    """取 `--flag value` 或 `--flag=value` 形式的命令行参数值（没有则空串）。"""
    if flag in sys.argv:
        index = sys.argv.index(flag)
        if index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    for item in sys.argv:
        if item.startswith(f"{flag}="):
            return item.split("=", 1)[1]
    return ""


def _apply_cli_overrides() -> None:
    """把命令行参数翻译成环境变量，供 app_framework 读取。

    走环境变量而不是改函数签名：app_framework 里 create_driver / resolve_appium_server
    已经是「环境变量优先」的实现，CLI 只要写进 os.environ 就能同时影响本进程里的
    preflight 自检与「第一环真机采集」（以及它们派生的 adb / appium 子进程），
    不必在两层之间传参。第二环只对**既有**脚本执行 run_script 复核（本轮新生成的脚本
    落盘即完成，见模块 docstring 第 7 条），它跑在 pytest 子进程里、自己读环境变量，
    与这里无关。
    """
    global TESTCASE_FILE
    mapping = {
        "--udid": "APP_UDID",
        "--package": "APP_PACKAGE",
        "--activity": "APP_ACTIVITY",
        "--appium-server": "APPIUM_SERVER",
        "--device-name": "APP_DEVICE_NAME",
        "--platform-version": "APP_PLATFORM_VERSION",
    }
    for flag, env_key in mapping.items():
        value = _arg_value(flag)
        if value:
            os.environ[env_key] = value
            logging.getLogger("app.cli").info("命令行覆盖 %s=%s", env_key, value)
    testcase_file = _arg_value("--testcase-file") or _arg_value("--case-file")
    if testcase_file:
        os.environ["APP_TESTCASE_FILE"] = testcase_file
        TESTCASE_FILE = resolve_testcase_file(testcase_file)
        # 兜底脚本名是按「用例文档路径」缓存的（见 default_script_name）：换了文档就要重算，
        # 否则 --no-testcase 模式会拿着上一份 md 的用例名去生成脚本
        _default_script_names.clear()
        logging.getLogger("app.cli").info("命令行覆盖用例文档 APP_TESTCASE_FILE=%s", TESTCASE_FILE)


_USAGE = '''\
用法：python src/app/generate_autoapp.py [选项]

  不带任何用例开关时，默认顺序跑用例文档里的**全部**用例（文档里有几条就跑几条）。

  用例范围（用例名 / 前提条件 / 测试步骤全部取自 md，默认 src/app/testcase/setting.md）：
  --list-cases           列出 md 里的全部用例（含启动参数 / 前置依赖 / 脚本 / 缓存状态）
  --testcase NAME        只跑指定用例（支持写用例名的一部分，如「打印」「更多链接_打印」）
                         别名 --case；也可用环境变量 APP_TESTCASE
  --all-cases / --all    跑 md 里的全部用例（与默认行为一致，写出来只为显式表达）
  --first-case / --first 只跑 md 里的第一条用例（调试单条时用）
  --no-testcase          不选用例，按「无测试用例」模式跑一遍流程
  --testcase-file PATH   换一份 md（别名 --case-file；相对路径按仓库根目录解析）
                         也可用环境变量 APP_TESTCASE_FILE

  采集与前置：
  --force-collect        忽略 .steps 缓存，强制重新在真机上采集步骤（= FORCE_COLLECT=1）
  --skip-precondition    跳过前提条件解析（不生成 / 复用前置脚本，= SKIP_PRECONDITION=1）

  日志（与 web 版同一套开关，见 src/utils/langchain_debug.py；**只打控制台，不落盘**）：
  --quiet / --no-debug   关掉 LangChain 调试日志，只留业务输出与最终答案（= LANGCHAIN_DEBUG=0）
  --debug                打开控制台调试日志（默认已打开）
  --debug-events=EVENTS  指定打印哪几类 tracer 事件：默认 tool,llm；
                         all = 9 类全开（含 chain），也可写 --debug-events=chain,tool
  --hide-debug-events=E  在默认基础上再隐藏某几类，如 --hide-debug-events=llm/end
                         要把整轮轨迹留档复盘就自己重定向（本工具不再写 run 日志文件）：
                         ... --debug-events=all 2>&1 | tee /tmp/run.log

  环境：
  --udid / --package / --activity / --appium-server / --device-name /
  --platform-version     覆盖设备与 app 启动参数（也可用同名环境变量）
                         注意：md 前提条件里写的 app activity / app package 优先级最高，
                         这里的 --package / --activity 只对「md 没写启动参数」的用例生效

  第二环的执行口径（与 web 版一致）：
  脚本**已存在** -> 直接执行复核（run_script）：执行步骤失败（断言成功/失败不在判断范围内）
                         就修复脚本，直到除断言之外的执行步骤全部成功，修完再执行确认；
  脚本是**本轮新生成**的 -> 落盘即完成、不再重复执行：本轮用例第一环已经在真机上连断言
                         完整跑过一遍，落盘后再执行等于把同一条用例重复跑一次
                         （多起一个 Appium session、多占设备几十秒）。
                         要单独验证脚本，自己执行：
                         python -m pytest src/app/scripts/<脚本名>

返回码：0 全部成功；1 有用例失败或脚本没落盘；2 参数 / 用例选择错误。
'''

# 认识的全部命令行开关。校验它的理由很实在：这个入口一旦跑起来就会**连真机、起 Appium
# session、调模型几十次**，而参数是手工解析的（sys.argv 里逐个找 flag）——
# 实测敲 `--help` 会被当成「没指定用例」直接开跑一整轮真机流程。
# 所以未知 flag 一律拒绝并打印用法，宁可多一行报错，也不要白烧一轮设备 + token。
# 日志开关（--debug / --quiet / --debug-events / --hide-debug-events）由
# src/utils/langchain_debug.py 在 import 期直接读 sys.argv，这里只需登记，避免被误判成未知参数。
_KNOWN_FLAGS = frozenset({
    "--list-cases", "--testcase", "--case", "--all-cases", "--all", "--first-case",
    "--first", "--no-testcase", "--testcase-file", "--case-file", "--force-collect",
    "--skip-precondition", "--udid", "--package",
    "--activity", "--appium-server", "--device-name", "--platform-version", "--help", "-h",
    "--debug", "--quiet", "--no-debug", "--debug-events", "--hide-debug-events",
})

# 需要跟一个值的开关（校验时不要把它们的值当成未知 flag）
_FLAGS_WITH_VALUE = frozenset({
    "--testcase", "--case", "--testcase-file", "--case-file", "--udid",
    "--package", "--activity", "--appium-server", "--device-name", "--platform-version",
    "--debug-events", "--hide-debug-events",
})


def _reject_unknown_flags(argv: Sequence[str]) -> Optional[int]:
    """发现不认识的 flag 就打印用法并返回退出码 2；一切正常返回 None。"""
    unknown: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.startswith("--") or (item.startswith("-") and len(item) > 1 and not item[1].isdigit()):
            name = item.split("=", 1)[0]
            if name not in _KNOWN_FLAGS:
                unknown.append(item)
            elif name in _FLAGS_WITH_VALUE and "=" not in item:
                index += 1  # 跳过它的值，避免把「用例名 / 路径」误判成 flag
        index += 1
    if not unknown:
        return None
    print(f"不认识的命令行参数：{'、'.join(unknown)}\n\n{_USAGE}", file=sys.stderr)
    return 2


def main() -> int:
    """命令行入口（选项见 _USAGE）。"""
    if any(item in ("--help", "-h") for item in sys.argv):
        print(_USAGE)
        return 0
    bad = _reject_unknown_flags(sys.argv[1:])
    if bad is not None:
        return bad

    _apply_cli_overrides()
    global ALL_CASES
    ALL_CASES = parse_test_cases()
    if "--list-cases" in sys.argv:
        list_cases(ALL_CASES)
        return 0
    print(f"用例文档：{TESTCASE_FILE}（解析到 {len(ALL_CASES)} 条用例）")
    print(f"脚本目录：{SCRIPTS_DIR}")
    # 日志开关在 import 期就按 sys.argv 定下来了（见 src/utils/langchain_debug.py），这里只做播报：
    # 真机一轮要跑几分钟，跑完才发现「日志被 --quiet 关掉了」或「chain 事件把屏淹了」代价太大。
    # 与 web 版同一句提示（describe_logging），但不在 import 期打印 —— 那样会污染
    # --list-cases 这个「只想看用例」的入口输出。
    print(describe_logging(DEBUG_LOGGING, EVENT_FILTER))

    targets, code = _select_targets(ALL_CASES)
    if code:
        return code
    if not ALL_CASES:
        print(f"{TESTCASE_FILE} 里没有解析到用例，按「无测试用例」模式运行。")
    print(f"本次执行 {len(targets)} 条用例："
          f"{'、'.join(c.name if c else '(未指定)' for c in targets)}")

    failures = 0
    for position, case in enumerate(targets, 1):
        # 逐条播报启动参数：md 前提条件里写的 app activity / package 优先级高于
        # APP_ACTIVITY / APP_PACKAGE 环境变量，跑错 app 时这一行是第一个能看出来的地方
        print(f"\n>>> [{position}/{len(targets)}] {case.name if case else '(未指定)'}"
              f"  |  {parse_app_launch(case).describe()}")
        try:
            answer = run_case_chain(case, ALL_CASES)
        except Exception as exc:  # noqa: BLE001 - 单条用例失败不影响其余用例
            failures += 1
            logging.getLogger("app.run").exception(
                "用例 %s 运行失败：%s", case.name if case else "(未指定)", exc)
            continue
        print("\n" + "=" * 70)
        print(f"用例：{case.name if case else '(未指定)'}")
        print(f"脚本：{SCRIPTS_DIR / script_name_of_case(case)}")
        print("=" * 70)
        print(answer or "(第二环没有返回内容)")
        if not (SCRIPTS_DIR / script_name_of_case(case)).is_file():
            failures += 1
            print(f"\n[警告] 脚本没有落盘：{SCRIPTS_DIR / script_name_of_case(case)}",
                  file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

