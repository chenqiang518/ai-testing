"""Appium 工具集：把 src/app/app_framework.py 的能力包成 langchain Tool，供第一环 agent 调用。

与 src/web/selenium_tools.py 一一对应，只是把浏览器动作换成移动端动作：

    selenium_tools                 appium_tools
    open                           init（启动 app）
    get_current_url                get_current_activity
    find / click / send_keys       find / click / send_keys
    （无）                          scroll_to_element / back / get_text（移动端特有）
    get_page_source                get_page_source（**控件层级摘要**，非原始 XML）
    get_page_text                  get_page_text
    assert_contains                assert_contains
    quit                           quit

本模块存在的理由（以及为什么每个工具都要包 _execute）：
    第一环是 structured chat agent，工具抛出的任何异常都会**打断整条 chain**，
    本轮已经跑通的真实步骤随之全丢（第二环拿不到 step，只能重新采集一遍）。
    而移动端失败极其常见：控件还没渲染完、软键盘挡住点击、列表需要滚动、
    activity 正在切换…… 这些都不该让 chain 崩掉，而应作为一条 Observation 回给模型，
    让它换定位表达式 / 先滚动 / 先 sleep 再试。所以这里统一用 _execute 把异常降级成字符串。

本模块还承担一项**执行期约束**：用例文案账本（见下方「用例文案账本」一节）。
    agent 用来定位 / 断言 / 输入的中文界面文案必须逐字来自测试用例原文，
    越权文案（原文「更多连接」→ 界面上的「更多设置」）在工具执行前就被拒绝。
    只读诊断类工具（get_page_source / get_page_text / get_current_activity）不校验：
    它们不改变界面状态，也不产出会被写进脚本的定位表达式，拦下来只会让 agent
    连「看看界面上到底有什么」都做不到，反而更难写出如实的失败报告。
"""

import ast
import json
import os
import re
import sys
from pathlib import Path
from time import sleep as _sleep
from typing import Any, Callable, Iterable, Optional, Sequence

# 允许直接以脚本方式运行（跑文件末尾那段不连设备的自测）：python src/app/appium_tools.py
# 此时 sys.path[0] 是 src/app，`from src.app...` 会 ModuleNotFoundError；
# 与 generate_autoapp.py 顶部的处理完全一致（IDE / -m 方式运行时 __package__ 非空，不会重复插入）。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.tools import tool
from selenium.common.exceptions import WebDriverException

from src.app.app_framework import AppiumWeb

app = AppiumWeb()

# 定位失败时追加的提示。与 generate_autoapp.step_failure_reason() 里的切分标记
# （"。请先调用 get_page_source"）配套：那一段会被剥掉再写进步骤缓存的 failed 原因，
# 改这里的文案必须同步改那边，否则缓存里的失败原因会带上这段冗长提示。
_RETRY_HINT = """。请先调用 get_page_source 获取当前界面的控件层级摘要，\
    再从摘要里挑真实存在的 text / resource-id / content-desc 拼定位表达式\
    （Appium 不支持 css 选择器，也不接受只给一段可见文本）；\
    若摘要里没有目标文本，说明它还没滚进可视区，先用 scroll_to_element 滚动查找。\
    禁止原样重试同一个定位表达式。"""

# Appium/Selenium 抛出的异常里，真正值得回给模型的部分通常就在前一两行；
# 完整消息动辄几十行（含 urllib3 / appium client 内部帧），只会挤占上下文。
_MAX_ERROR_LENGTH = 600


def _error_message(exc: BaseException) -> str:
    """把异常压成一行可读文案，并在末尾拼上 _RETRY_HINT。"""
    text = re.sub(r"\s+", " ", str(exc) or exc.__class__.__name__).strip()
    if len(text) > _MAX_ERROR_LENGTH:
        text = text[:_MAX_ERROR_LENGTH] + "…"
    return f"{text}{_RETRY_HINT}"


def _as_observation(exc: BaseException) -> str:
    """异常 -> Observation 文案。

    区分三类，因为模型对它们的正确反应完全不同：
      1. WebDriverException（定位/超时/session 断开）-> 换定位表达式或先滚动，附 _RETRY_HINT；
      2. ValueError -> 参数用法错了（app_framework 主动抛的，文案本身已含纠正建议）；
      3. 其它未预期异常 -> 原样给出类型名，避免模型误以为「换个 xpath 就能好」而死循环。
    """
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, WebDriverException):
        return _error_message(exc)
    return f"{exc.__class__.__name__}: {exc}"


def _execute(func: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """统一的工具执行包装：任何异常都降级成字符串 Observation，绝不打断 chain。"""
    try:
        result = func(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - 见模块 docstring，这里必须兜住一切
        return _as_observation(exc)
    return result if isinstance(result, str) else str(result)


# ---------------------------------------------------------------------------
# 用例文案账本：定位 / 断言 / 输入用的中文文案必须**逐字**来自测试用例原文
# ---------------------------------------------------------------------------
# 为什么要在工具层做这道硬校验（而不是只在 prompt 里写要求）：
#   实测「更多连接_打印」这条用例，被测机的设置首页上并不存在「更多连接」这个入口，
#   agent 在 scroll_to_element 找不到之后，自作主张把文案换成界面上真实存在的
#   「更多设置」，又试了「连接与共享」，一路点进不相干的页面，把 40 轮 max_iterations
#   烧光，最后交出一份「步骤都跑通了、但跑的不是这条用例」的轨迹。
#   （后续核实：那是**用例自身的笔误** —— 真机上这个入口叫「更多连接」，
#    setting.md 已按 adb shell uiautomator dump 的真实文案修正，用例名随之变成
#    「更多连接_打印」。这道校验保留：它拦住的正是「模型替人猜文案」这个动作，
#    猜对了也等于悄悄改了被测路径，猜错了就是一份跑通但测错对象的脚本。）
#   prompt 里写多少遍「不要猜文案」都拦不住：对模型来说，「界面上看得见的文字」
#   比「用例原文」更像权威。所以这里把它变成**执行期约束** —— 文案不在用例原文里，
#   工具直接拒绝执行，并把「唯一正确处置是判定失败 + quit」写进 Observation。
#
# 账本由编排层灌入（generate_autoapp.run_case_chain / collect_steps_on_device 调
# bind_case_wording），内容是「本用例 + matched 前置用例」的原文全文与前置步骤渲染结果。
# 未绑定（--no-testcase 模式、或单独 import 本模块调试底层能力）时一律放行，保持向后兼容。

# 中日韩统一表意文字。只有含 CJK 的字面量才参与校验 ——
# resource-id（com.android.settings:id/title）、控件类名、英文包名这些
# 本来就不可能出现在中文用例原文里，一并拦下来只会把正常步骤全堵死。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# xpath 里的文本类属性：@text='X' / contains(@text,'X') / @content-desc="X" / contains(@name,'X')
_TEXT_ATTR_RE = re.compile(
    r"@(?:text|content-desc|name|label)\s*(?:=|,)\s*(['\"])(.*?)\1", re.S)
# UiAutomator 表达式里的文本类条件：new UiSelector().textContains("打印")
_UIAUTOMATOR_TEXT_RE = re.compile(
    r"\.(?:text|textContains|textMatches|textStartsWith"
    r"|description|descriptionContains|descriptionMatches|descriptionStartsWith)"
    r"\(\s*(['\"])(.*?)\1", re.S)
# 前缀写法里「值本身就是文案」的那几种：acc=更多 / desc=更多 / content-desc=更多
_DESC_PREFIX_RE = re.compile(
    r"^(?:acc|desc|content-desc|accessibility[-_]id|name)\s*=\s*(.+)$", re.I | re.S)
# assert_contains / send_keys 的 text 支持一次传多个期望文案，分隔符口径与 web 版一致
_TEXT_SEPARATORS_RE = re.compile(r"[、,，;；/|]+")
# 步骤 input 里属于「整段就是文案」的参数名（其余参数按定位表达式抽取）
_TEXT_ARG_NAMES = frozenset({"text", "value", "content", "keyword", "assert_text"})

# 归一化后的用例原文（空串 = 未绑定 = 不校验）
_CASE_WORDING: str = ""


def _normalize_wording(text: Any) -> str:
    """文案归一化：去掉所有空白、转小写。

    只忽略**排版**差异（md 里的换行与缩进、全角空格），不忽略用字 ——
    「更多设置」不会因为归一化而变成「更多连接」的子串。
    """
    return re.sub(r"\s+", "", str(text or "")).lower()


def strict_wording() -> bool:
    """是否开启「文案必须来自用例原文」的硬校验（默认开）。

    留这个开关是给排障用的：怀疑某个合法文案被误拦时，
    `APP_STRICT_WORDING=0 python src/app/generate_autoapp.py ...` 即可临时放行，不必改代码。
    """
    return os.getenv("APP_STRICT_WORDING", "1").strip().lower() not in {"0", "false", "no", "off"}


def bind_case_wording(*texts: Any) -> None:
    """设置用例文案账本（全量替换）：texts 拼起来就是「允许出现的文案」全集。

    编排层传的是用例原文（TestCase.render()）与前置步骤渲染结果，所以这里只做
    「拼接 + 归一化」，不再尝试从自然语言里抠词 —— 抠词必然漏
    （「点击更多连接」这句里的文案没有任何引号标记），而**子串判定**天然覆盖了
    原文里出现过的任何片段，既不会误拦，也堵死了「换个近义词」这条路。
    """
    global _CASE_WORDING
    _CASE_WORDING = _normalize_wording("".join(str(text or "") for text in texts))


def extend_case_wording(text: Any) -> None:
    """往账本里追加一段原文（前置步骤渲染得比 bind 晚时用）。"""
    global _CASE_WORDING
    _CASE_WORDING += _normalize_wording(text)


def unbind_case_wording() -> None:
    """清空账本，回到「不校验」状态（采集收尾时调用，避免影响后续独立调试）。"""
    global _CASE_WORDING
    _CASE_WORDING = ""


def case_wording() -> str:
    """当前账本内容（归一化后）；空串表示未绑定。仅供日志与自测使用。"""
    return _CASE_WORDING


def _as_text(value: Any) -> str:
    """任意入参 -> 字符串（dict 走 json，保证中文不被转义成 \\uXXXX）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def locator_wording_literals(locator: Any) -> list[str]:
    """从定位表达式里抽出「按可见文本 / content-desc 定位」的字面量。

    只抽文本类条件：`//*[@resource-id='com.android.settings:id/title']` 抽不出东西，
    `//*[contains(@text,'更多连接')]` 抽出「更多连接」，
    `ui=new UiSelector().textContains("打印")` 抽出「打印」，`acc=更多` 抽出「更多」。
    """
    text = _as_text(locator)
    if not text:
        return []
    found = [match.group(2) for match in _TEXT_ATTR_RE.finditer(text)]
    found += [match.group(2) for match in _UIAUTOMATOR_TEXT_RE.finditer(text)]
    prefixed = _DESC_PREFIX_RE.match(text.strip())
    if prefixed:
        found.append(prefixed.group(1))
    return [item.strip().strip("'\"") for item in found if item and item.strip()]


def plain_wording_literals(text: Any) -> list[str]:
    """把 assert_contains / send_keys 的 text 入参拆成待校验的字面量列表。

    text 不是定位表达式，整段就是要比对的内容；支持「、」/「,」分隔多个期望文案
    （与 web 版 assert_contains 的口径一致），所以先按分隔符拆开再逐段校验。
    """
    raw = _as_text(text)
    if not raw:
        return []
    return [item.strip().strip("'\"「」") for item in _TEXT_SEPARATORS_RE.split(raw)
            if item and item.strip()]


def wording_offenders(literals: Iterable[str]) -> list[str]:
    """返回不在用例原文里的**中文**字面量（保序去重）；未绑定账本或关闭开关时返回空。"""
    if not strict_wording() or not _CASE_WORDING:
        return []
    offenders: list[str] = []
    for literal in literals:
        normalized = _normalize_wording(literal)
        # 无中文的字面量（resource-id / 英文文案 / 纯数字）不参与校验，理由见 _CJK_RE
        if not normalized or not _CJK_RE.search(normalized):
            continue
        if normalized in _CASE_WORDING:
            continue
        if literal not in offenders:
            offenders.append(literal)
    return offenders


def _parse_step_input(step_input: Any) -> Any:
    """把步骤 input 尽力还原成 dict；还原不了就返回等价的字符串。"""
    if isinstance(step_input, dict):
        return step_input
    text = _as_text(step_input)
    if not text:
        return text
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(text)
        except (ValueError, SyntaxError, TypeError):
            continue
    return text


def step_input_offenders(step_input: Any) -> list[str]:
    """从一条采集步骤的 input 里抽出越权文案（供编排层的采集门禁使用）。

    input 有两种形态：StepRecorder 记下的 dict / json 字符串，以及步骤缓存里的
    `{'locator': "//*[@text='更多设置']"}` 这种 Python 字面量字符串。
    能还原成 dict 就按 key 精确区分（text 类整段比对、locator 类只抽文本条件），
    还原不了就整段两种抽取都跑一遍 —— xpath 正则照样能命中里面的 @text='X'。
    """
    parsed = _parse_step_input(step_input)
    if isinstance(parsed, dict):
        literals: list[str] = []
        for key, value in parsed.items():
            if str(key).lower() in _TEXT_ARG_NAMES:
                literals += plain_wording_literals(value)
            else:
                literals += locator_wording_literals(value)
        return wording_offenders(literals)
    return wording_offenders(locator_wording_literals(parsed)
                             + plain_wording_literals(parsed))


# 拒绝标记前缀。这是**跨模块契约**：generate_autoapp.steps_wording_offenders 靠它区分
# 「被这道门禁拦下、从未真正操作过设备的步骤」与「真的执行过的步骤」——
# 前者不该让整轮采集作废（模型被拦后改回原文文案跑通了就是好轨迹），后者才会污染脚本。
# 改动时两边一起改。
WORDING_REJECTION_PREFIX = "已拒绝执行"

# 拒绝执行时回给模型的 Observation。措辞刻意写成「唯一正确处置」而不是「建议」：
# 实测模型面对开放式建议时会继续换文案试探，只有把「判定失败 + quit」写成明确的
# 收尾动作，它才会停下来。
_WORDING_REJECTION = WORDING_REJECTION_PREFIX + """ {label}：定位/断言文案 {offenders} 不在本条测试用例的原文里。\
    界面文案只能**逐字**取自测试步骤原文，禁止改写成界面上看起来相近的入口\
    （原文写「更多连接」就只能用「更多连接」，不得换成「更多设置」，\
    也不得改点「连接与共享」这类别的入口）—— 那不是同一条用例，跑通了也是错的。\
    唯一正确处置：1) get_page_source 核对当前界面；\
    2) 目标文案不在可视区就用 scroll_to_element 继续找**原文文案**（max_swipes 可给到 10）；\
    3) 滚完仍找不到，说明该机型/被测版本上没有这个入口 —— 判定本步骤失败，\
    停止后续步骤，直接调用 quit 释放设备，并在 Final Answer 里写明\
    「界面缺少用例原文文案『X』，当前界面上最接近的是『Y』，疑似 app 版本/机型不匹配，\
    需人工确认用例或换机型」。不要为了让流程跑下去而替换文案。"""


def _wording_rejection(label: str, *, locators: Sequence[Any] = (),
                       texts: Sequence[Any] = ()) -> Optional[str]:
    """工具入参的文案校验：全部合法返回 None，否则返回拒绝执行的 Observation 文案。"""
    literals: list[str] = []
    for locator in locators:
        literals += locator_wording_literals(locator)
    for text in texts:
        literals += plain_wording_literals(text)
    offenders = wording_offenders(literals)
    if not offenders:
        return None
    return _WORDING_REJECTION.format(
        label=label, offenders="、".join(f"「{item}」" for item in offenders))


@tool
def init(app_activity: str = "", app_package: str = "") -> str:
    """启动 Appium session 并把被测 app 拉到前台。**每条用例的第一步必须调用它**。

    app_activity / app_package 来自测试用例的「前提条件」（如 ".Settings" /
    "com.android.settings"）；留空则用环境变量 APP_ACTIVITY / APP_PACKAGE 或框架默认值。
    返回启动后的控件层级摘要，可据此直接拼下一步的定位表达式。
    """
    return _execute(app.init, app_activity or None, app_package or None)


@tool
def get_current_activity() -> str:
    """获取当前 package 与 activity。

    用于确认「点了之后是否真的跳到了目标界面」—— Android 上很多入口点了没反应
    （权限弹窗、控件不可点）却不会报错，只看 activity 才能发现。
    """
    return _execute(app.current_activity)


@tool
def get_page_source(locator: Optional[str] = None) -> str:
    """获取当前界面的**控件层级摘要**（一行一个可定位控件），而不是原始 XML。

    每行形如：
        <TextView id="com.android.settings:id/title" text="省电与电池" clickable bounds="[0,336][1080,441]">
    其中的 text / id(resource-id) / desc(content-desc) 就是当前界面真实存在、可直接用于
    定位的属性。**拼任何定位表达式之前都应该先调用本工具**，不要凭经验猜。

    可选传入 locator，此时会先报告该表达式命中几个元素，再给摘要。
    """
    return _execute(app.source, locator)


@tool
def find(locator: str, timeout: Optional[float] = None) -> str:
    """按定位表达式查找控件，返回命中个数与命中控件的属性。

    locator 支持的写法：
        //*[contains(@text,'省电与电池')]                   xpath，按可见文本（最常用）
        //*[@resource-id='com.android.settings:id/title']   xpath，按 resource-id
        id=com.android.settings:id/title                    resource-id 简写
        acc=更多                                            content-desc（accessibility id）
        ui=new UiSelector().textContains("打印")             UiAutomator 表达式
    **不支持 css 选择器**。找不到时会返回可读的失败原因，据此改用 get_page_source 重新定位。

    按文本定位时，文案必须**逐字**取自测试步骤原文（原文写「更多连接」就只能是「更多连接」）；
    用了原文之外的中文文案会被直接拒绝执行，不会真的去设备上找。
    """
    rejected = _wording_rejection("find", locators=(locator,))
    if rejected:
        return rejected
    return _execute(app.find, locator, timeout)


@tool
def click(locator: str, timeout: Optional[float] = None) -> str:
    """点击控件，返回点击后的界面控件摘要。

    locator 写法同 find。若报「element not interactable」，说明命中的是不可点的
    子控件（如 TextView），应改用 get_page_source 找它外层带 clickable 的父容器。

    点击的文案必须**逐字**取自测试步骤原文：原文是「更多连接」时，界面上的「更多设置」
    是另一个入口，点它等于跑了另一条用例 —— 这种调用会被直接拒绝执行。
    """
    rejected = _wording_rejection("click", locators=(locator,))
    if rejected:
        return rejected
    return _execute(app.click, locator, timeout)


@tool
def send_keys(locator: str, text: str, clear: bool = True) -> str:
    """往输入框（EditText）写内容；clear=True 时先清空再写。

    locator 写法同 find，例如 id=com.android.settings:id/search_src_text。

    locator 里的文本条件与要输入的 text 都必须来自测试步骤原文（如原文「输入 打印」），
    自己编一个搜索词会被直接拒绝执行。
    """
    rejected = _wording_rejection("send_keys", locators=(locator,), texts=(text,))
    if rejected:
        return rejected
    return _execute(app.send_keys, locator, text, clear)


@tool
def get_text(locator: str, timeout: Optional[float] = None) -> str:
    """读取单个控件的文本，对应测试步骤里的「获取 XXX」（如「获取 剩余电量」）。

    text 为空时自动退回 content-desc / value（图标类控件往往只有 content-desc）。
    locator 写法同 find；按文本定位时文案必须逐字取自测试步骤原文（如原文「获取 剩余电量」）。
    """
    rejected = _wording_rejection("get_text", locators=(locator,))
    if rejected:
        return rejected
    return _execute(app.text_of, locator, timeout if timeout is not None else 10)


@tool
def scroll_to_element(locator: str, max_swipes: int = 5) -> str:
    """滚动查找控件，对应测试步骤里的「滚动到页面 直至找到 X」。

    Android 列表（设置页、更多连接页）里的条目默认不在可视区，直接 find 必然失败，
    必须先用本工具滚出来。locator 建议用 //*[contains(@text,'省电与电池')] 这种按文本的写法，
    框架会自动翻译成设备端 UiScrollable.scrollIntoView（比逐屏 swipe 快得多）。
    滚完 max_swipes 屏仍找不到，说明**该机型/被测版本上没有这个入口**：此时判定该步骤失败、
    停止后续步骤并直接 quit，在 Final Answer 里报告缺失的文案；
    禁止改成界面上看起来相近的文案（如把「更多连接」换成「更多设置」）继续滚找或点击 ——
    那种调用会被直接拒绝执行。滚动用的文案必须逐字取自测试步骤原文。
    """
    rejected = _wording_rejection("scroll_to_element", locators=(locator,))
    if rejected:
        return rejected
    return _execute(app.scroll_to_element, locator, max_swipes)


@tool
def back() -> str:
    """按系统返回键回到上一级界面，对应测试步骤里的「返回上一级页面」。

    返回当前 package / activity，可据此确认是否真的退了一层。
    """
    return _execute(app.back)


@tool
def sleep(seconds: float = 1) -> str:
    """等待若干秒。仅在界面有动画/异步加载、且显式等待确实不够用时使用。

    优先用 find/click 自带的 timeout 或 scroll_to_element，不要拿 sleep 兜底所有等待。
    """
    try:
        _sleep(float(seconds))
    except (TypeError, ValueError):
        return f"sleep 参数非法：{seconds!r}，请传数字秒数"
    return f"已等待 {seconds} 秒"


@tool
def get_page_text() -> str:
    """获取当前界面的全部可见文本（所有控件的 text + content-desc）。

    适合「断言某段文字是否出现在界面上」这类整页判断；
    需要知道控件属性 / 怎么定位时请用 get_page_source。
    """
    return _execute(app.page_text)


@tool
def assert_contains(text: str, locator: Optional[str] = None) -> str:
    """断言界面（或 locator 指定的控件范围）内包含指定文本，返回通过/失败结论。

    对应测试步骤里的「断言 XXX」。传 locator 可把断言范围限定到某个列表/容器，
    避免整页文本里混入状态栏、导航栏内容导致误判通过。
    断言失败不会抛异常，只会返回失败说明，请据此检查是否漏了前置操作。

    断言的期望文案必须逐字取自测试步骤原文 / 预期结果（如原文「断言页面中包含『系统打印服务』」），
    自己换个相近说法会被直接拒绝执行。
    """
    rejected = _wording_rejection("assert_contains", locators=(locator,), texts=(text,))
    if rejected:
        return rejected

    def _run() -> str:
        if not str(text or "").strip():
            return "断言失败：text 不能为空"
        try:
            passed = app.assert_contains(text, locator)
        except WebDriverException as exc:
            return f"断言执行失败：{_error_message(exc)}"
        if passed:
            return f"断言通过：界面包含 {text!r}" + (f"（范围 {locator}）" if locator else "")
        try:
            scope = app.texts_of(locator) if locator else app.page_text()
        except WebDriverException:
            scope = ""
        snippet = re.sub(r"\s+", " ", scope)[:300]
        return (f"断言失败：界面未包含 {text!r}"
                + (f"（范围 {locator}）" if locator else "")
                + f"。当前界面文本片段：{snippet}")
    return _execute(_run)


@tool
def quit() -> str:
    """关闭 Appium session、释放设备。**每条用例的最后一步必须调用它**。

    不 quit 的话 session 会一直占着设备，下一轮采集或生成的 pytest 脚本
    再建 session 时会因设备被占用而超时失败。
    """
    return _execute(app.quit)


# 工具顺序即模型看到的顺序：把「观察类」放在「操作类」前面，
# 强化「先看控件层级摘要、再拼定位表达式」的习惯，减少臆造 xpath 的次数。
tools = [
    init,
    get_current_activity,
    get_page_source,
    find,
    click,
    send_keys,
    get_text,
    scroll_to_element,
    back,
    sleep,
    get_page_text,
    assert_contains,
    quit,
]

# 语义名 -> 注册名。当前两边一致，映射看似多余，但它把「编排层依赖了哪些工具名」
# 写成了一份显式清单：改名时只需要在这一处维护，下游 _REQUIRED_STEP_TOOLS / _ACTION_TOOLS
# 会在 import 期 KeyError，而不是等真机跑完才发现门禁形同虚设。
TOOL_NAMES = {
    "init": init.name,
    "quit": quit.name,
    "click": click.name,
    "send_keys": send_keys.name,
    "get_text": get_text.name,
    "get_page_text": get_page_text.name,
    "assert_contains": assert_contains.name,
    "scroll_to_element": scroll_to_element.name,
    "back": back.name,
    "find": find.name,
    "sleep": sleep.name,
    "get_current_activity": get_current_activity.name,
    "get_page_source": get_page_source.name,
}


def tool_name(semantic: str) -> str:
    """取工具的注册名；semantic 不在 TOOL_NAMES 里就 KeyError（fail fast，见上）。"""
    return TOOL_NAMES[semantic]


if __name__ == "__main__":
    # 不连设备的最小自测：只验证「用例文案账本」这道硬约束，
    #   python src/app/appium_tools.py
    # 复现的就是「更多连接_打印」那次失败：界面上没有「更多连接」，
    # agent 改点「更多设置」/「连接与共享」，跑完 40 轮交出一份不相干的轨迹。
    _CASE = """用例名：更多连接_打印
    - 前提条件:
        1. 打开  app activity ".Settings" ,
        2. app package "com.android.settings"
    - 测试步骤:
        1. 找寻「更多连接」，如果没找到，则向上滚动屏幕直至成功
        2. 点击「更多连接」
        3. 进入「更多连接」页面后，找寻「打印」，如果没找到，则向上滚动屏幕直至成功
        4. 点击「打印」
        5. 进入「打印」页面后，断言页面中包含「系统打印服务」
        6. 返回上一级页面
    """

    # 1) 未绑定账本：一切放行（--no-testcase 模式 / 单独调试底层能力时的向后兼容）
    unbind_case_wording()
    assert wording_offenders(["更多设置", "随便什么"]) == []
    print("未绑定账本 -> 不校验：", wording_offenders(["更多设置"]))

    # 2) 绑定用例原文后：原文里的文案放行，界面上「看起来相近」的文案一律算越权
    bind_case_wording(_CASE)
    assert wording_offenders(["更多连接", "打印", "系统打印服务", "更多"]) == []
    assert wording_offenders(["更多设置"]) == ["更多设置"]
    assert wording_offenders(["连接与共享", "更多设置"]) == ["连接与共享", "更多设置"]
    # 无中文的字面量（resource-id / 类名 / 英文）不参与校验
    assert wording_offenders(["com.android.settings:id/title", "Print", ""]) == []
    print("绑定账本 -> 越权文案：", wording_offenders(["更多设置", "连接与共享"]))

    # 3) 各种定位写法的文案抽取
    assert locator_wording_literals("//*[contains(@text,'更多连接')]") == ["更多连接"]
    assert locator_wording_literals('//*[@text="打印"]') == ["打印"]
    assert locator_wording_literals("//*[contains(@content-desc,'更多')]") == ["更多"]
    assert locator_wording_literals('ui=new UiSelector().textContains("打印")') == ["打印"]
    assert locator_wording_literals("acc=更多") == ["更多"]
    assert locator_wording_literals("id=com.android.settings:id/title") == []
    assert locator_wording_literals("//*[contains(@text,'更多设置')]/..") == ["更多设置"]
    print("定位表达式抽取：", locator_wording_literals("//*[contains(@text,'更多连接')]"))

    # 4) 步骤 input（StepRecorder 的 dict / 缓存里的字符串）里的越权文案，供采集门禁使用
    assert step_input_offenders({"locator": "//*[@text='更多连接']"}) == []
    assert step_input_offenders("{'locator': \"//*[@text='更多设置']\"}") == ["更多设置"]
    assert step_input_offenders('{"text": "系统打印服务"}') == []
    assert step_input_offenders({"locator": "id=com.android.settings:id/search_src_text",
                                 "text": "打印"}) == []
    assert step_input_offenders({"locator": "id=com.android.settings:id/search_src_text",
                                 "text": "无线打印"}) == ["无线打印"]
    assert step_input_offenders({}) == []
    print("步骤 input 抽取：", step_input_offenders("{'locator': \"//*[@text='更多设置']\"}"))

    # 5) 工具层：越权文案在执行前就被拦下（返回拒绝说明，而不是「driver 尚未启动」）
    _rejected = click.invoke({"locator": "//*[contains(@text,'更多设置')]"})
    assert _rejected.startswith("已拒绝执行 click") and "更多连接" in _rejected, _rejected
    for _tool, _args in ((scroll_to_element, {"locator": "//*[@text='连接与共享']"}),
                         (find, {"locator": "//*[contains(@text,'更多设置')]"}),
                         (get_text, {"locator": "//*[@text='更多设置']"}),
                         (send_keys, {"locator": "id=x:id/search_src_text", "text": "更多设置"}),
                         (assert_contains, {"text": "更多设置"})):
        _obs = _tool.invoke(_args)
        assert _obs.startswith("已拒绝执行"), (_tool.name, _obs)
    print("工具层拦截：", _rejected[:60], "…")
    # 合法文案会走到真正的执行路径（此时没连设备，应报「driver 尚未启动」而不是被拦）
    _allowed = click.invoke({"locator": "//*[contains(@text,'更多连接')]"})
    assert "driver 尚未启动" in _allowed, _allowed
    print("合法文案放行（走到执行层）：", _allowed[:40], "…")

    # 6) 开关：APP_STRICT_WORDING=0 时临时放行（排障用）
    os.environ["APP_STRICT_WORDING"] = "0"
    assert not strict_wording() and wording_offenders(["更多设置"]) == []
    os.environ.pop("APP_STRICT_WORDING")
    assert strict_wording()
    unbind_case_wording()
    print("\n自测完成")
