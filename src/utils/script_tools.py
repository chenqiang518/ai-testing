#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用「脚本落盘 / 执行」工具集：给代码生成 agent 补上文件系统与运行能力。

支持的领域（注册即可扩展，不限于此）：
    web -> src/web/scripts   pytest + selenium（Chrome，本地 chromedriver 经 Service 指定）
    api -> src/api/scripts   pytest + requests（HTTP 接口）
    app -> src/app/scripts   pytest + appium（Android UiAutomator2 / iOS XCUITest）
    其它 -> register_target() 注册（playwright、locust、unittest、纯 python 脚本 ...）

背景（为什么需要这个模块）：
    `generate_autoweb.py` / `generate_autoapp.py` 的「代码生成环」过去是
    `prompt | llm | StrOutputParser()` 的**纯文本**调用：
        1. 这一环的 llm 没有 bind 任何工具（tools 只绑给了操作浏览器/App 的那一环），
           模型收到「把脚本保存到 scripts 文件夹下」时只能把代码当字符串吐回来；
        2. 返回值被 print 到控制台就丢弃了，代码里没有任何落盘逻辑，
           于是 scripts/ 目录始终只有一个空的 __init__.py；
        3. 「查找脚本是否存在 -> 存在则执行 -> 失败则修复」同样无从谈起，
           模型既看不到文件系统，也跑不了 pytest。

    这里把「列目录 / 读 / 写 / 跑」做成工具，交给代码生成 agent 自主调用，
    形成「生成 -> 落盘 -> （按需）执行 -> 失败则修复」的闭环。
    注意 write_script **只落盘、不执行**：本轮要落成脚本的那条用例，第一环已经在真实
    浏览器 / 设备上完整跑通过一遍（步骤 json 就是那次执行的产物），落盘后再自动跑一遍
    等于把同一条用例重复执行（web 多登录一次站点、app 多连一次设备）。执行验证只在
    「本轮没有真跑过该用例」时由 agent 显式调用 run_script（详见文件顶部那段注释）。
    （注意：承载这些工具的 agent 必须用原生 function calling，即
    `create_openai_tools_agent`；structured chat agent 要求模型把 action_input 以
    JSON 文本输出，write_script 的多行 code 参数会把它打崩，详见 generate_autoweb.py。）

两种装配方式：
    1. 绑定单一领域（推荐；各领域的 generate_auto*.py 用这种）：
           tools = build_script_tools("app")
       工具不带 target 参数，落盘目录 / 执行方式 / 失败特征 / 修复提示全部按该领域处理；
    2. 不绑定领域（一个 agent 同时管多个领域）：
           tools = build_script_tools()
       每个工具多一个 target 参数（web/api/app...），并额外提供 list_targets 工具。

设计约定（与 src/web/selenium_tools.py 保持一致）：
    1. 所有异常都在工具内部降级成字符串 Observation，绝不冒泡终止 chain；
    2. 返回值统一为非空字符串，避免 Observation 出现 None / 空串噪音；
    3. 路径以 REPO_ROOT + 各领域 scripts_dir 锚定，与进程 CWD 无关（prompt 里写
       相对路径 `./scripts` 时，实际落点取决于运行方式，极易错位）；
    4. 本模块只依赖 stdlib + langchain_core，不 import selenium/appium/requests，
       所以放在 src/utils 下，各领域都能复用；产物目录仍按领域落在各自的
       src/<domain>/scripts（见 ScriptTarget.scripts_dir）。

没有直接用 `langchain_community.agent_toolkits.FileManagementToolkit` 的原因：
    1. langchain-community 已声明 sunset（导入即 DeprecationWarning）；
    2. 它的 `write_file` 只做 `f.write(text)`，不剥 markdown 围栏、不做语法自检，
       模型输出带 ```python 围栏时会直接写出无法运行的 .py；
    3. 它没有执行 pytest 的能力，`run_script` 无论如何都得自建；
    4. 它没有「领域」概念，无法按 web/api/app 区分落点与失败特征。
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union

from langchain_core.tools import BaseTool, StructuredTool

# 仓库根目录，同时作为执行子进程的 cwd：
#   1. 本文件位于 src/utils/script_tools.py，parents[0]=src/utils、[1]=src、[2]=仓库根；
#   2. cwd=REPO_ROOT 才能让脚本里的 `from src.web... import ...` 被解析。
REPO_ROOT = Path(__file__).resolve().parents[2]

# 读脚本时返回给 LLM 的最大字符数，避免把上下文挤爆
MAX_READ_LENGTH = 8000
# 执行输出保留的尾部字符数（失败摘要在末尾）
MAX_RUN_OUTPUT = 4000
# 默认单次执行超时（秒）。各领域可覆盖：接口测试更快、App 起 session 更慢。
DEFAULT_RUN_TIMEOUT = 180
# 向后兼容：旧代码 import 过这个名字
RUN_TIMEOUT = DEFAULT_RUN_TIMEOUT

# write_script **无条件只做「语法自检 -> 落盘」**：没有任何开关、环境变量、参数或领域
# 例外能让它顺带执行脚本（历史反复：曾一度改成「保存即验证」并留了 AUTO_VERIFY_ON_WRITE
# / SCRIPT_AUTO_VERIFY 开关，实测更糟，已全部撤除；这里把结论写死，避免以后又被加回去）。
# 这条约定由 __main__ 自测里的 canary 脚本**机械守住**：写一个「一旦被执行必然留下输出」
# 的脚本，回执里若出现执行痕迹，自测当场 AssertionError。
#
# 为什么不在保存后自动跑一遍 pytest：这条用例在本轮**已经被真实执行过一次**了 ——
# 第一环的浏览器 agent 正是照着测试步骤把 open / send_keys / click / assert_contains
# 全部跑通（含断言）之后，才把步骤 json 交给第二环生成脚本的；脚本里的选择器逐条来自
# 那次真实执行。落盘后再自动执行一遍，等于把同一条用例重复跑一次：
#     - web 领域要多登录一次被测站点（多 3~5 秒 + 一次真实会话，还可能触发风控）；
#     - app 领域要重连一次设备 / 重启 App；
#     - 换来的信息量几乎为零（失败也只可能是环境抖动，反而诱导 agent 去改正确的脚本）。
# 所以「执行验证」只在**本轮没有真跑过该用例**时才需要，由 agent 显式调用 run_script：
#     1. 目标脚本已存在（第一环跳过采集）时的复核；
#     2. 复核发现脚本步骤失败、write_script 修复写回之后的确认。

# 跨领域通用的「脚本步骤失败」特征：出现这些就说明是脚本/环境问题，必须修脚本，
# 不能当成「纯断言失败」（两者的处置指令完全相反）。
_COMMON_STEP_MARKERS = (
    "ModuleNotFoundError",
    "ImportError",
    "SyntaxError",
    "IndentationError",
    "NameError",
    "AttributeError",
    "TypeError",
    "FileNotFoundError",
    "No such file or directory",
)


@dataclass(frozen=True)
class ScriptTarget:
    """一个「脚本领域」的完整定义：落在哪、怎么跑、失败长什么样、怎么提示修复。

    新增领域不需要改本模块的任何函数，注册一个 ScriptTarget 即可，例如：
        register_target(ScriptTarget(
            key="perf", title="性能测试", scripts_dir="src/perf/scripts",
            stack="pytest + locust", runner="pytest", timeout=600))
    """

    key: str                              # 领域标识：web / api / app ...
    title: str                            # 中文名，用于给 agent 的提示文案
    scripts_dir: str                      # 相对 REPO_ROOT 的脚本目录
    stack: str                            # 技术栈说明，会写进工具描述
    step_markers: tuple[str, ...] = ()    # 领域特有的「步骤失败」异常特征
    fix_hint: str = ""                    # 领域特有的修复建议（追加在通用提示后）
    assert_hint: str = ""                 # 领域特有的「假断言失败」自查项
    aliases: tuple[str, ...] = ()         # 别名：允许 agent 用技术栈名指定领域
    runner: str = "pytest"                # pytest | python
    runner_args: tuple[str, ...] = ()     # 追加给 runner 的参数
    timeout: int = DEFAULT_RUN_TIMEOUT    # 执行超时（秒）

    @property
    def dir(self) -> Path:
        """脚本目录的绝对路径（始终以 REPO_ROOT 锚定，与 CWD 无关）。"""
        return REPO_ROOT / self.scripts_dir

    @property
    def markers(self) -> tuple[str, ...]:
        """该领域的全部「步骤失败」特征 = 通用特征 + 领域特征。"""
        return _COMMON_STEP_MARKERS + tuple(self.step_markers)

    @property
    def names(self) -> tuple[str, ...]:
        """key + 别名，用于宽松匹配 agent 传入的领域名。"""
        return (self.key,) + tuple(self.aliases)


WEB_TARGET = ScriptTarget(
    key="web",
    title="Web 端到端测试",
    scripts_dir="src/web/scripts",
    stack="pytest + selenium（Chrome，本地 chromedriver 经 Service 指定）",
    aliases=("selenium", "chrome", "browser", "web端"),
    step_markers=(
        "TimeoutException",
        "NoSuchElementException",
        "WebDriverException",
        "ElementClickInterceptedException",
        "ElementNotInteractableException",
        "StaleElementReferenceException",
        "InvalidSelectorException",
        "chromedriver",
        "session not created",
    ),
    fix_hint=(
        "web 领域常见脚本问题：\n"
        "- 启动浏览器必须用 webdriver.Chrome(service=Service(resolve_chromedriver()))，"
        "禁止 executable_path=（Selenium 4 已移除该参数，会直接 TypeError）；\n"
        "- 定位不到元素 / 等待超时：先检查是否漏了显式等待（WebDriverWait + "
        "expected_conditions），以及 css 选择器是否被改写（必须原样使用采集到的选择器）；\n"
        "- 点击没反应：点击前要用 EC.element_to_be_clickable，仅用 "
        "presence_of_element_located 会在 Vue 绑定事件前点到，表单不会真正提交。"
    ),
    assert_hint=(
        "- 断言「多个元素里包含某些文本」时必须用 find_elements（复数）取全部匹配元素后聚合文本，"
        "用 find_element（单数）只会拿到第一个，典型症状是 assert '商场管理' in '首页'；\n"
        "- SPA 异步渲染：断言前要先等目标元素/文本真正渲染出来，取太早会拿到空串。"
    ),
    timeout=180,
)

API_TARGET = ScriptTarget(
    key="api",
    title="接口测试",
    scripts_dir="src/api/scripts",
    stack="pytest + requests（HTTP 接口）",
    aliases=("requests", "http", "interface", "接口", "rest"),
    step_markers=(
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "HTTPError",
        "TooManyRedirects",
        "MissingSchema",
        "InvalidURL",
        "InvalidSchema",
        "JSONDecodeError",
        "SSLError",
        "ProxyError",
        "Max retries exceeded",
        "Failed to establish a new connection",
    ),
    fix_hint=(
        "api 领域常见脚本问题：\n"
        "- 连接类错误先确认 url 带协议头（https://）、被测服务可达、requests 传了 timeout=；\n"
        "- response.json() 之前要先确认状态码与 content-type，否则会把「服务返回 HTML 错误页」"
        "误判成断言失败（JSONDecodeError 属于脚本步骤问题，需要修脚本）；\n"
        "- 需要鉴权的接口先取 token 再放进 header/cookie，不要硬编码过期凭证；\n"
        "- 用例之间共享数据请用 fixture / parametrize，不要依赖执行顺序。"
    ),
    assert_hint=(
        "- 断言响应体字段时确认取值路径正确（如 resp.json()['data']['list'] 而不是只取第一个元素）；\n"
        "- 断言列表/集合类结果时要聚合全部条目再判断，只看 [0] 会漏判。"
    ),
    timeout=120,
)

APP_TARGET = ScriptTarget(
    key="app",
    title="移动端 App 测试",
    scripts_dir="src/app/scripts",
    stack="pytest + appium（Android UiAutomator2 / iOS XCUITest）",
    aliases=("appium", "android", "ios", "mobile", "app端"),
    step_markers=(
        "TimeoutException",
        "NoSuchElementException",
        "WebDriverException",
        "InvalidSessionIdException",
        "NoSuchContextException",
        "StaleElementReferenceException",
        "Could not start a new session",
        "Connection refused",
        "uiautomator2",
        "xcodebuild",
    ),
    fix_hint=(
        "app 领域常见脚本问题：\n"
        "- 「Could not start a new session / Connection refused」属于环境问题：appium server "
        "未启动（默认 http://127.0.0.1:4723）或设备/模拟器不在线（adb devices 检查），"
        "这类原因连续失败两次应停止修复并在 Final Answer 中说明；\n"
        "- capabilities 要与真机一致（platformName / automationName / appPackage / "
        "appActivity / udid）；\n"
        "- 定位要用 AppiumBy（accessibility_id、android.widget.* 等），不要照搬 web 的 css "
        "选择器；driver.quit() 必须放在 fixture 的 yield 之后，否则 session 泄漏。"
    ),
    assert_hint=(
        "- 断言控件文本前先等控件真正出现（WebDriverWait + presence_of_element_located）；\n"
        "- 列表类断言要 find_elements 取全部控件后聚合 text，只取第一个会漏判。"
    ),
    timeout=300,
)

# 领域注册表：key（小写）-> ScriptTarget
TARGETS: dict[str, ScriptTarget] = {}
# 别名索引：selenium/appium/requests ... -> key
_ALIASES: dict[str, str] = {}


def register_target(target: ScriptTarget, *, replace: bool = False) -> ScriptTarget:
    """注册（或覆盖）一个脚本领域，返回该领域对象。

    新领域只需给出「落盘目录 + 技术栈 + 失败特征 + 提示」，读写执行逻辑全部复用。
    默认不允许静默覆盖已注册的 key，避免手滑把内置领域改掉。
    """
    key = target.key.strip().lower()
    if not key:
        raise ValueError("ScriptTarget.key 不能为空")
    if key in TARGETS and not replace:
        raise ValueError(f"领域 {key} 已注册，如需覆盖请传 replace=True")
    target = ScriptTarget(**{**target.__dict__, "key": key})
    TARGETS[key] = target
    for alias in target.names:
        _ALIASES[alias.strip().lower()] = key
    return target


def _target_choices() -> str:
    """一行紧凑的领域清单，专用于错误信息。

    必须单行：`_error_message` 只保留异常文案的首行，多行清单会被截断，
    agent 就拿不到可选值、也就无法自我纠正（这正是多领域模式最需要的信息）。
    """
    if not TARGETS:
        return "（尚未注册任何脚本领域）"
    return "、".join(f"{t.key}（{t.title}）" for t in TARGETS.values())


def resolve_target(target: Union[str, ScriptTarget, None]) -> ScriptTarget:
    """把 agent 传入的领域名解析成 ScriptTarget；未知名报错并列出可选值。

    宽松匹配：大小写不敏感、支持技术栈别名（selenium->web、appium->app、
    requests->api）；传入 ScriptTarget 实例时原样返回。
    报错文案里带上可选领域，agent 下一轮就能自己纠正。
    """
    if isinstance(target, ScriptTarget):
        return target
    name = (target or "").strip().lower()
    if not name:
        raise ValueError(f"target 不能为空，可选领域：{_target_choices()}")
    key = _ALIASES.get(name)
    if key is None:
        raise ValueError(f"未知的脚本领域：{target}，可选领域：{_target_choices()}，"
                         f"也可用技术栈别名（selenium/requests/appium），"
                         f"或调用 list_targets 查看目录与技术栈")
    return TARGETS[key]


def available_targets() -> str:
    """列出全部已注册领域（含目录与技术栈），既给 agent 看，也用于报错提示。"""
    if not TARGETS:
        return "尚未注册任何脚本领域"
    lines = [f"- {t.key}：{t.title}，{t.stack}，脚本目录 {t.dir}" for t in TARGETS.values()]
    return "可用的脚本领域（target）：\n" + "\n".join(lines)


for _t in (WEB_TARGET, API_TARGET, APP_TARGET):
    register_target(_t)

# 向后兼容：旧代码从本模块 import 过 SCRIPTS_DIR（web 领域的脚本目录）
SCRIPTS_DIR = WEB_TARGET.dir

# 执行失败时给 agent 的通用纠错提示：区分「脚本步骤失败」与「断言失败」，
# 只有前者才需要修脚本（断言失败说明脚本本身跑得通，属于被测系统/用例预期问题）。
_FIX_HINT_BASE = (
    "请判断失败类型后决定下一步：\n"
    "- 脚本步骤失败（定位不到元素 / 会话或请求建立失败 / 等待超时 / 选择器或字段写错 / "
    "导入错误 / 语法错误）：先调用 read_script 读取当前脚本，再用 write_script 写入"
    "修复后的**完整**代码，然后重新 run_script 确认（最多修复 2 轮）；\n"
    "- 断言失败（assert 不成立）：脚本本身能跑通，**不要**修改脚本，"
    "直接在 Final Answer 中说明断言结果。"
)

# 断言失败的通用提示：实测最常见的「假断言失败」其实是脚本取值方式写错
# （只取第一个匹配项 / 等待不足导致取到空值），必须让 agent 先自查再下结论，
# 否则会把脚本缺陷误报成被测系统的问题。领域专属自查项由 target.assert_hint 补充。
_ASSERT_HINT_BASE = (
    "断言失败。先自查是不是脚本自身的取值方式问题（这类必须修脚本，最多修复 2 轮）：\n"
    "{extra}\n"
    "确认脚本取值方式无误后，才把它当作被测系统的真实断言结果，此时**不要**改脚本，"
    "直接在 Final Answer 中说明。"
)
_DEFAULT_ASSERT_EXTRA = "- 取值路径 / 等待时机是否正确，是否取到了空值或只取了第一个匹配项；"


def _fix_hint(target: ScriptTarget) -> str:
    """通用修复提示 + 领域专属修复提示。"""
    return f"{_FIX_HINT_BASE}\n{target.fix_hint}" if target.fix_hint else _FIX_HINT_BASE


def _assert_hint(target: ScriptTarget) -> str:
    """通用断言自查提示 + 领域专属自查项。"""
    return _ASSERT_HINT_BASE.format(extra=target.assert_hint or _DEFAULT_ASSERT_EXTRA)


# 注意：_error_message / _as_observation / _execute 与 src/web/selenium_tools.py 里的
# 同名实现重复（IDE 会报 Duplicated code fragment）。这是刻意的：本模块要保持
# 「不 import selenium/appium/requests」的通用性，从 selenium_tools 复用会把 web 依赖
# 带进 api/app 领域的执行路径。三处逻辑都很短，重复成本低于耦合成本。
def _error_message(exc: BaseException) -> str:
    """把异常压成一行可读信息（subprocess / selenium 的异常串常带大段 Stacktrace）。"""
    raw = str(exc).split("Stacktrace:")[0].strip()
    return (raw.splitlines()[0] if raw else "") or exc.__class__.__name__


def _as_observation(value: Any) -> str:
    """工具返回值统一转成非空字符串。"""
    if value is None:
        return "操作成功"
    if isinstance(value, str):
        return value.strip() or "操作成功"
    return str(value)


def _execute(action: str, func: Callable[[], Any]) -> str:
    """统一执行工具动作，并把任何异常降级成字符串 Observation。

    与 selenium_tools._execute 同理：`BaseTool.run` 对普通异常一律 raise，
    不兜住就会一路冒泡到 chain.invoke，进程以 exit code 1 结束。
    降级成 Observation 后 agent 才有机会自我纠错（例如换一个合法领域名、
    或重写一份语法正确的脚本）。
    """
    try:
        return _as_observation(func())
    except Exception as exc:  # noqa: BLE001 - 工具层必须兜住所有异常
        message = f"{action} 执行失败：{type(exc).__name__}: {_error_message(exc)}"
        print(message)
        return message


# ```python ... ``` / ```py ... ``` / ``` ... ``` 围栏
_FENCE_RE = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# 判断某一行是否「看起来像代码」，用于剥掉模型输出的解释性前言
_CODE_PREFIXES = (
    "import ", "from ", "#!", "# -*-", "# ", '"""', "'''", "def ", "class ",
    "@", "with ", "if __name__", "try:", "async ",
)

# 中日韩统一表意文字区间：用于识别「解释性文字」行
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
# 代码行里几乎必然出现的字符；解释性文字（如「以上代码实现了登录断言」）通常一个都没有
_CODE_MARKS = ("'", '"', "(", ")", "=", ":", "[", "]", ",", ".")


def _is_prose_line(line: str) -> bool:
    """判断一行是否是模型附带的解释性文字（而非代码）。

    为什么不能简单「从尾部逐行裁到能 compile 为止」：中文在 Python 3 里是**合法
    标识符**，`希望对你有帮助` 单独一行就是合法的表达式语句，compile 会通过，
    但 pytest 导入时会 NameError。所以改用字符特征判断：
    顶格 + 含中文 + 不含任何代码特征字符 + 不是注释 → 认定为解释性文字。
    """
    stripped = line.strip()
    if not stripped or line[:1].isspace() or stripped.startswith("#"):
        return False
    if not _CJK_RE.search(stripped):
        return False
    return not any(mark in stripped for mark in _CODE_MARKS)


def _trim_trailing_prose(text: str) -> str:
    """裁掉代码尾部的解释性文字（如「以上脚本实现了……」）。"""
    lines = text.rstrip("\n").splitlines()
    while lines and _is_prose_line(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _strip_code_fence(code: str) -> str:
    """剥掉 markdown 代码围栏与解释性前言/后语，只留纯 Python 代码。

    为什么必须在写盘前清洗：模型即便被要求「只输出代码」，也常带上
    ```python 围栏，或先来一句「以下是生成的脚本：」。直接写盘会得到
    无法运行的 .py，等到执行阶段才暴露，白白浪费一整轮运行。
    """
    text = (code or "").strip()
    if not text:
        return ""

    blocks = [block.strip() for block in _FENCE_RE.findall(text) if block.strip()]
    if blocks:
        # 多个代码块时全部保留（模型可能把 conftest 与用例分开写）
        text = "\n\n".join(blocks)
    else:
        # 没有围栏：丢掉第一行代码之前的解释性文字
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.lstrip().startswith(_CODE_PREFIXES):
                text = "\n".join(lines[index:])
                break

    text = _trim_trailing_prose(text).strip()
    # 统一以换行结尾，符合 PEP8 且避免 diff 噪音
    return f"{text}\n" if text else ""


def _fstring_hint(code: str) -> str:
    """检测 f-string 里被写成 {{var}} 的占位符，给出非阻断式提醒。

    实测 qwen 生成断言消息时会把 f"期望 {text} 未出现" 写成双花括号形式
    （f"期望 {{text}} 未出现"），运行时原样输出 "{text}" 而不是变量值。
    这种写法语法完全合法、pytest 也不会报错，属于「静默 bug」，所以在写盘回执里
    提醒 agent 自查；不做自动改写（{{ 也可能是刻意输出字面花括号）。
    """
    if "{{" not in code:
        return ""
    if 'f"' not in code and "f'" not in code:
        return ""
    return ("注意：f-string 中出现了 {{...}}，运行时会原样输出花括号而不是变量值；"
            "若不是刻意输出字面花括号，请改成单花括号后重新写入。")


def _ensure_package(target: ScriptTarget) -> Path:
    """确保领域的脚本目录存在，并且是一个 Python 包（含 __init__.py）。

    __init__.py 不是可有可无的：pytest 默认 importmode=prepend 会沿目录向上找
    第一个没有 __init__.py 的目录并把它加进 sys.path。有了 src/<domain>/scripts/
    __init__.py（且 src/、src/<domain>/ 也都是包），仓库根才会被加进 sys.path，
    脚本里的 `from src.web.web_framework import ...` 才能导入成功。
    目录按需创建，因此新增一个领域不必先手工建目录。
    """
    scripts_dir = target.dir
    scripts_dir.mkdir(parents=True, exist_ok=True)
    init_file = scripts_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    return scripts_dir


def _effective_target(target: ScriptTarget, file_name: str) -> tuple[ScriptTarget, str]:
    """模型给的路径若明显属于另一个领域，纠正落点并回告。

    典型情况：agent 传 target="web" 但 file_name 写成 "src/api/scripts/登录接口.py"。
    只取 basename 会把接口脚本静默写进 web 目录，因此这里先按路径里的领域特征纠偏。
    仅在入参确实带路径分隔符时才判断，避免误伤纯文件名。
    """
    name = (file_name or "").strip().replace("\\", "/")
    if "/" not in name:
        return target, ""
    for candidate in TARGETS.values():
        if candidate.key == target.key:
            continue
        if candidate.scripts_dir in name or f"/{candidate.key}/" in name:
            return candidate, (f"（入参路径指向 {candidate.key} 领域，"
                               f"落点已改到 {candidate.scripts_dir}）")
    return target, ""


def _resolve_script_path(target: ScriptTarget, file_name: str) -> Path:
    """把模型给出的文件名解析成该领域脚本目录内的绝对路径。

    策略是「归一化」而不是「报错」：模型经常写 `./scripts/***.py`、
    `src/web/scripts/***.py` 甚至 `../../evil.py`，一律只取 basename，
    保证读写范围永远落在该领域目录内（报错只会让 agent 白白空转一轮）。
    末尾的 is_relative_to 校验是纵深防御：basename 已保证同级，此处再挡
    符号链接等把路径指回目录外的极端情况。
    """
    name = (file_name or "").strip().replace("\\", "/")
    if not name:
        raise ValueError("file_name 不能为空，请给出脚本文件名，例如 ***.py")

    base = Path(name).name
    if not base or base in (".", ".."):
        raise ValueError(f"非法的文件名：{file_name}")
    if not base.endswith(".py"):
        base = f"{base}.py"

    scripts_dir = _ensure_package(target)
    path = (scripts_dir / base).resolve()
    if not path.is_relative_to(scripts_dir.resolve()):
        raise ValueError(f"路径 {file_name} 越出允许目录 {scripts_dir}")
    return path


def _list_scripts(target: ScriptTarget) -> str:
    """列出该领域脚本目录下已有的自动化脚本（忽略 __init__.py）。"""
    scripts_dir = _ensure_package(target)
    names = sorted(p.name for p in scripts_dir.glob("*.py") if p.name != "__init__.py")
    if not names:
        return (f"{target.title}（{target.stack}）脚本目录 {scripts_dir} 下暂无自动化脚本，"
                f"需要用 write_script 生成")
    return f"{scripts_dir} 下已有的脚本：{', '.join(names)}"


def _read_script(target: ScriptTarget, file_name: str) -> str:
    """读取脚本内容（超长截断），供 agent 在修复前查看现状。"""
    path = _resolve_script_path(target, file_name)
    if not path.is_file():
        return (f"脚本不存在：{path}。"
                f"可先用 list_scripts 查看已有脚本，或用 write_script 生成新脚本")
    text = path.read_text(encoding="utf-8")
    total = len(text)
    if total > MAX_READ_LENGTH:
        text = text[:MAX_READ_LENGTH] + f"\n...（内容已截断，全文共 {total} 字符）"
    return f"脚本 {path.name}（{total} 字符）内容如下：\n{text}"


def _write_script(target: ScriptTarget, file_name: str, code: str) -> str:
    """把生成的代码写入该领域的脚本目录：**无条件**只做「语法自检 -> 落盘」，不执行脚本。

    这里没有任何开关 / 环境变量 / 参数能让本函数顺带跑一遍脚本 —— 唯一的执行入口是
    agent 显式调用 run_script（见 _run_script），且本函数体内也不该出现 subprocess。

    语法自检（compile）是关键兜底：模型输出的代码若被解释性文字污染，
    这里立刻以 Observation 形式反馈行号与原因，agent 可当轮重写，
    而不是等到执行时起完浏览器 / 连完设备才失败。

    为什么落盘后不顺手跑一遍验证（完整理由见文件顶部「write_script 只做语法自检 -> 落盘」
    那段注释）：本轮的步骤 json 来自第一环在真实浏览器 / 设备上的完整执行（含断言），
    脚本里的选择器逐条都是刚刚验证过的；落盘后再执行一遍就是把同一条用例重复跑一次 ——
    web 领域要多登录一次被测站点、app 领域要重连一次设备，信息量几乎为零，
    还会因为环境抖动诱导 agent 去改本来正确的脚本。
    所以回执里明确告诉 agent：新生成的脚本不要再调 run_script 重复执行；
    run_script 只用于「本轮没有真跑过该用例」的场景（脚本已存在时的复核、修复后的确认）。
    """
    target, redirected = _effective_target(target, file_name)
    path = _resolve_script_path(target, file_name)
    cleaned = _strip_code_fence(code)
    if not cleaned:
        raise ValueError("code 为空，没有可写入的内容")

    try:
        compile(cleaned, str(path), "exec")
    except SyntaxError as exc:
        raise ValueError(
            f"代码语法检查未通过（第 {exc.lineno} 行）：{exc.msg}。"
            f"请只输出完整可运行的 Python 代码（不要 markdown 围栏、不要解释文字）后重试"
        ) from exc

    path.write_text(cleaned, encoding="utf-8")
    # 模型给的路径常被归一化（如 ./scripts/x.py -> x.py），必须把真实落点回告，
    # 否则 agent 会在 Final Answer 里报错一个不存在的路径
    normalized = "" if path.name == (file_name or "").strip() else f"（入参已归一化为 {path.name}）"
    return (f"脚本已保存：{path}{normalized}{redirected}（{len(cleaned.splitlines())} 行，"
            f"{len(cleaned)} 字符）。{_fstring_hint(cleaned)}"
            f"语法检查已通过。本工具**只落盘、不执行**，新生成的脚本也**不需要**再调用 "
            f"run_script 重复验证 —— 本轮步骤已在真实环境里完整跑通过一遍，"
            f"再跑一次等于把同一条用例重复执行（多登录一次被测站点 / 多连一次设备）。"
            f"只有「脚本原本就存在、本轮没有重新采集步骤」或「你刚用 write_script 修复过脚本」"
            f"这两种情况，才需要调用 run_script 验证。")


def _is_assertion_failure(target: ScriptTarget, output: str) -> bool:
    """判断执行失败是否纯由断言引起（而非定位/连接/超时/导入等脚本步骤问题）。

    两者给 agent 的指令完全相反：步骤失败必须修脚本，纯断言失败默认不改脚本。
    但只要输出里还夹着该领域的步骤类异常（selenium / appium / requests / 导入），
    就说明脚本本身有问题，仍按步骤失败处理，避免把脚本缺陷误判成被测系统的问题。
    """
    if "AssertionError" not in output:
        return False
    return not any(marker in output for marker in target.markers)


def _build_command(target: ScriptTarget, path: Path) -> list[str]:
    """按领域拼装执行命令。

    runner="pytest"（默认）：`python -m pytest <脚本> -q --no-header -p no:cacheprovider`
        - `sys.executable` 保证用当前 venv 解释器（IDE 与命令行均成立）；
        - `-p no:cacheprovider` 不往仓库里写 .pytest_cache。
    runner="python"：直接跑脚本，适用于非 pytest 的领域（如 locust 压测、演示脚本）。
    """
    if target.runner == "python":
        return [sys.executable, str(path), *target.runner_args]
    return [sys.executable, "-m", "pytest", str(path), "-q", "--no-header",
            "-p", "no:cacheprovider", *target.runner_args]


def _run_script(target: ScriptTarget, file_name: str) -> str:
    """执行指定脚本，返回可读的通过/失败结论 + 领域相关的纠错提示。

    失败分类是这里的关键：exit code 5（未收集到用例）、超时、断言失败、步骤失败，
    给 agent 的下一步指令完全不同，笼统返回一句「失败」只会让它瞎改。

    什么时候才该调用（write_script 只落盘不执行，理由见文件顶部那段注释）：
        1. 目标脚本**原本就存在**、本轮没有重新采集步骤 -> 复核它是否仍然可用；
        2. 复核发现脚本步骤失败、用 write_script 写回修复代码之后 -> 确认修复生效；
        3. 仓库代码重构**既有**脚本后的复核（generate_autoweb.add_reusable_entry(verify=True)，
           只用于本轮没有真跑过的既有前置脚本；刚生成 / 刚跑过的脚本一律不执行）。
    本轮刚由步骤 json 生成的新脚本**不要**再跑一遍：那些步骤第一环已在真实浏览器 /
    设备上完整执行过（含断言），重复执行只会多登录一次被测站点、多连一次设备。
    """
    path = _resolve_script_path(target, file_name)
    if not path.is_file():
        return f"脚本不存在：{path}，无法执行。请先用 write_script 生成"

    try:
        proc = subprocess.run(
            _build_command(target, path), cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=target.timeout,
        )
    except subprocess.TimeoutExpired:
        return (f"执行超时（超过 {target.timeout} 秒）：{path}\n"
                f"多为等待/重试一直挂着、或 driver/session 未正常释放导致，请检查显式等待的"
                f"超时时间与资源释放（如 driver.quit() 放在 fixture 的 yield 之后）。\n"
                f"{_fix_hint(target)}")

    output = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    tail = output[-MAX_RUN_OUTPUT:] if len(output) > MAX_RUN_OUTPUT else output

    if proc.returncode == 0:
        return f"执行通过（exit code 0）：{path}\n{tail}"
    if proc.returncode == 5 and target.runner == "pytest":
        # pytest 约定：exit code 5 = 没有收集到任何用例
        return (f"执行未收集到任何测试用例（exit code 5）：{path}\n{tail}\n"
                f"请确认脚本里的测试函数以 `test_` 开头（pytest 只收集 test_* 函数），"
                f"修正后用 write_script 重写再执行。\n{_fix_hint(target)}")
    hint = _assert_hint(target) if _is_assertion_failure(target, output) else _fix_hint(target)
    return f"执行失败（exit code {proc.returncode}）：{path}\n{tail}\n{hint}"


def _as_tool(func: Callable, name: str, description: str) -> BaseTool:
    """把普通函数包成 StructuredTool，并打开异常降级开关。

    用 StructuredTool.from_function 而不是 @tool 装饰器：这里的函数是工厂内动态
    生成的闭包（领域已绑定），装饰器写法拿不到稳定的 docstring/签名。
    """
    tool_obj = StructuredTool.from_function(func=func, name=name, description=description)
    # LLM 偶尔会把 action_input 写成字符串（而不是 dict），从而触发 pydantic ValidationError。
    # 打开这两个开关，校验错误 / ToolException 也会变成 Observation，而不是终止 chain。
    tool_obj.handle_validation_error = True
    tool_obj.handle_tool_error = True
    return tool_obj


def build_bound_tools(target: Union[str, ScriptTarget]) -> list[BaseTool]:
    """构造**绑定单一领域**的工具集：不带 target 参数，落点/执行方式/提示都已确定。

    各领域的 generate_auto*.py 用这种——prompt 里不必再解释「领域」概念，
    模型也不可能把脚本写错目录。
    """
    bound = resolve_target(target)
    where = f"{bound.title}（{bound.stack}）脚本目录 {bound.dir}"

    # 闭包名加下划线前缀：工具名由 _as_tool(name=...) 显式指定，函数名不参与，
    # 这样也不会与模块级导出的 list_scripts / read_script ... 同名而产生遮蔽歧义。
    def _list() -> str:
        return _execute("list_scripts", lambda: _list_scripts(bound))

    def _read(file_name: str) -> str:
        return _execute("read_script", lambda: _read_script(bound, file_name))

    def _write(file_name: str, code: str) -> str:
        return _execute("write_script", lambda: _write_script(bound, file_name, code))

    def _run(file_name: str) -> str:
        return _execute("run_script", lambda: _run_script(bound, file_name))

    return [
        _as_tool(_list, "list_scripts",
                 f"列出{where}下已存在的自动化脚本文件名，用于判断目标脚本是否已经生成过"),
        _as_tool(_read, "read_script",
                 f"读取{where}下指定脚本（如 ***.py）的完整内容；修复脚本前必须先调用它"),
        _as_tool(_write, "write_script",
                 f"把{bound.stack}的自动化测试代码保存到{where}下的 file_name 文件中"
                 f"（只做语法检查 + 落盘，**不会执行脚本**）。"
                 f"code 必须是完整可运行的 Python 代码（新建与修复都用它整体覆盖写入）；"
                 f"写入前会做语法检查，语法错误以 Observation 返回，需修正后重新调用。"
                 f"刚由本轮真实步骤生成的脚本，保存后即完成，不要再调用 run_script 重复执行"),
        _as_tool(_run, "run_script",
                 f"用 {bound.runner} 执行{where}下的指定脚本，返回执行是否通过与失败摘要。"
                 f"只用于「本轮没有真实执行过该用例」的场景：目标脚本原本就存在时复核、"
                 f"或用 write_script 修复脚本之后确认；刚生成的新脚本不要重复执行一遍"),
    ]


def build_generic_tools() -> list[BaseTool]:
    """构造**多领域**工具集：每个工具多一个 target 参数，另加 list_targets。

    一个 agent 同时管 web/api/app 时用这种；target 支持技术栈别名
    （selenium->web、requests->api、appium->app），传错会以 Observation 回列可选值。
    """
    domains = "、".join(TARGETS)

    def _targets() -> str:
        return _execute("list_targets", available_targets)

    # 形参名必须是 target：StructuredTool.from_function 按签名推断 args_schema，
    # 改名会让 LLM 看到的参数名与工具描述/调用示例不一致。
    def _list(target: str) -> str:
        return _execute("list_scripts", lambda: _list_scripts(resolve_target(target)))

    def _read(target: str, file_name: str) -> str:
        return _execute("read_script", lambda: _read_script(resolve_target(target), file_name))

    def _write(target: str, file_name: str, code: str) -> str:
        return _execute("write_script",
                        lambda: _write_script(resolve_target(target), file_name, code))

    def _run(target: str, file_name: str) -> str:
        return _execute("run_script", lambda: _run_script(resolve_target(target), file_name))

    target_desc = f"脚本领域，可选值：{domains}（也可用技术栈别名，如 selenium/requests/appium）"
    return [
        _as_tool(_targets, "list_targets",
                 "列出全部可用的脚本领域（target）及其技术栈与脚本目录，不确定领域时先调用它"),
        _as_tool(_list, "list_scripts",
                 f"列出指定领域（target：{domains}）脚本目录下已存在的自动化脚本文件名，"
                 f"用于判断目标脚本是否已经生成过"),
        _as_tool(_read, "read_script",
                 f"读取指定领域脚本的完整内容；修复脚本前必须先调用它。{target_desc}"),
        _as_tool(_write, "write_script",
                 f"把自动化测试代码保存到指定领域的脚本目录下（只做语法检查 + 落盘，"
                 f"**不会执行脚本**）。{target_desc}。"
                 f"code 必须是完整可运行的 Python 代码（新建与修复都用它整体覆盖写入）；"
                 f"写入前会做语法检查，语法错误以 Observation 返回，需修正后重新调用。"
                 f"刚由本轮真实步骤生成的脚本，保存后即完成，不要再调用 run_script 重复执行"),
        _as_tool(_run, "run_script",
                 f"执行指定领域脚本目录下的脚本（pytest 领域用 pytest 跑），"
                 f"返回执行是否通过与失败摘要。只用于「本轮没有真实执行过该用例」的场景："
                 f"目标脚本原本就存在时复核、或用 write_script 修复脚本之后确认；"
                 f"刚生成的新脚本不要重复执行一遍。{target_desc}"),
    ]


def build_script_tools(target: Union[str, ScriptTarget, None] = None) -> list[BaseTool]:
    """工具集入口：给了 target 就绑定单一领域，不给就返回多领域工具集。"""
    return build_generic_tools() if target is None else build_bound_tools(target)


# 各领域预置工具集（导入即构造，无副作用：目录在工具真正被调用时才创建）
web_script_tools = build_bound_tools(WEB_TARGET)
api_script_tools = build_bound_tools(API_TARGET)
app_script_tools = build_bound_tools(APP_TARGET)
# 一个 agent 管所有领域时用这套（工具带 target 参数）
multi_script_tools = build_generic_tools()

# 向后兼容：generate_autoweb.py 里 `from src.utils.script_tools import script_tools`
# 拿到的就是 web 领域工具集；单独 import 四个工具名同样可用。
script_tools = web_script_tools
list_scripts, read_script, write_script, run_script = web_script_tools

if __name__ == "__main__":
    # 不依赖 LLM 的最小自测：覆盖多领域「写 -> 列 -> 读 -> 跑」、路径归一化、
    # 跨领域纠偏、未知领域、语法错误的 Observation 降级。
    # 全程只用 trivial 用例（assert 1 == 1），不起浏览器、不联网、不需要 Appium 设备。
    print(available_targets())

    SMOKE = "```python\nimport pytest\n\n\ndef test_smoke():\n    assert 1 == 1\n```"

    # 1) 三个内置领域各自绑定一套工具，落点互不干扰（分别落在 web/api/app 的 scripts 下）
    # 自测块里的变量统一用 _xxx / xxx_tools 命名：`if __name__` 块是模块级作用域，
    # 用 target/key 这类名字会变成全局变量，与工具函数的同名形参产生遮蔽告警。
    # write_script 的回执只有「脚本已保存 + 语法检查已通过」，**不含任何执行结论** ——
    # 这就是「只落盘不执行」：落盘不会触发 pytest（本轮用例已在真实环境跑过一遍，
    # 重复执行只会多登录一次站点 / 多连一次设备）；执行验证由随后的 run_script 单独完成。
    for domain_key in ("web", "api", "app"):
        bound_tools = {t.name: t for t in build_script_tools(domain_key)}
        smoke_name = f"smoke_{domain_key}.py"
        saved_receipt = bound_tools["write_script"].invoke(
            {"file_name": smoke_name, "code": SMOKE})
        print(saved_receipt)
        # 守住「write_script 不执行脚本」这条约定：回执里一旦出现执行结论，
        # 说明有人把自动验证又加回了 _write_script（那会让每条用例落盘后被重复跑一遍）
        assert "exit code" not in saved_receipt, \
            "write_script 不该执行脚本：回执里出现了执行结论"
        print(bound_tools["list_scripts"].invoke({}))
        print(bound_tools["run_script"].invoke({"file_name": smoke_name}))

    # 1b) canary：机械守住「write_script 无条件不执行脚本」这条约定。
    # 这个脚本一旦被执行必然在 stderr 打出 CANARY_EXECUTED 并抛 boom_canary，
    # 因此回执里出现任何一个执行痕迹，都说明有人在 _write_script 里加回了自动执行验证
    # （那会让每条用例在第一环真跑过一遍之后、落盘时又被重复跑一遍：多登录一次站点 /
    #  多连一次设备）。文件名用 smoke_ 前缀，第 5 步的清理规则会顺带删掉它。
    CANARY = ("import sys\n"
              "\n"
              "\n"
              "def test_canary():\n"
              "    print('CANARY_EXECUTED', file=sys.stderr)\n"
              "    raise RuntimeError('boom_canary')\n")
    canary_tools = {t.name: t for t in build_script_tools("web")}
    canary_receipt = canary_tools["write_script"].invoke(
        {"file_name": "smoke_canary.py", "code": CANARY})
    for executed_marker in ("CANARY_EXECUTED", "boom_canary", "exit code", "执行通过", "执行失败"):
        assert executed_marker not in canary_receipt, \
            f"write_script 不该执行脚本：回执里出现了执行痕迹 {executed_marker!r}"
    print(canary_receipt.splitlines()[0])
    # 对照组：同一个脚本交给 run_script 就必须真的被执行，
    # 否则 canary 本身失效（比如脚本没落盘），上面那组断言就成了假绿
    canary_run_receipt = canary_tools["run_script"].invoke({"file_name": "smoke_canary.py"})
    assert "boom_canary" in canary_run_receipt, \
        "run_script 应当真的执行脚本：没看到 canary 抛出的 boom_canary"
    print("canary 对照通过：run_script 真的执行了它（看到 boom_canary），"
          "而 write_script 落盘时没有")

    # 2) 多领域模式：工具带 target 参数，且支持技术栈别名（requests->api、appium->app）
    generic_tools = {t.name: t for t in build_script_tools()}
    print(generic_tools["list_targets"].invoke({}))
    print(generic_tools["read_script"].invoke({"target": "requests", "file_name": "smoke_api.py"}))
    print(generic_tools["run_script"].invoke({"target": "appium", "file_name": "smoke_app.py"}))

    # 3) 跨领域纠偏：target 写 web，但路径是 src/api/scripts -> 应落到 api 目录并回告
    print(generic_tools["write_script"].invoke({
        "target": "web",
        "file_name": "src/api/scripts/smoke_cross.py",
        "code": "def test_smoke():\n    assert True",
    }))

    # 4) 越权路径 / 未知领域 / 语法错误 / 脚本不存在：都只降级成 Observation，不抛异常
    print(generic_tools["write_script"].invoke(
        {"target": "web", "file_name": "../../evil.py", "code": "print(1)"}))
    print(f"evil.py 是否被写到仓库外: {(REPO_ROOT / 'evil.py').exists()}（应为 False）")
    print(generic_tools["run_script"].invoke({"target": "不存在的领域", "file_name": "x.py"}))
    print(generic_tools["write_script"].invoke(
        {"target": "web", "file_name": "bad.py", "code": "def f(:\n    pass"}))
    print(generic_tools["run_script"].invoke({"target": "api", "file_name": "not_exist.py"}))

    # 5) 清理自测残留（含 __pycache__ 里对应的 .pyc），保持仓库干净
    for cleanup_target in TARGETS.values():
        leftovers = list(cleanup_target.dir.glob("smoke_*.py")) + [
            cleanup_target.dir / "evil.py", cleanup_target.dir / "bad.py"]
        for leftover in leftovers:
            leftover.unlink(missing_ok=True)
        pycache = cleanup_target.dir / "__pycache__"
        if pycache.is_dir():
            for pyc in (list(pycache.glob("smoke_*")) + list(pycache.glob("evil.*"))
                        + list(pycache.glob("bad.*"))):
                pyc.unlink(missing_ok=True)

    for cleanup_target in TARGETS.values():
        print(_list_scripts(cleanup_target))





