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
"""

import re
from time import sleep as _sleep
from typing import Any, Callable, Optional

from langchain_core.tools import tool
from selenium.common.exceptions import WebDriverException

from src.app.app_framework import AppiumWeb

app = AppiumWeb()

# 定位失败时追加的提示。与 generate_autoapp.step_failure_reason() 里的切分标记
# （"。请先调用 get_page_source"）配套：那一段会被剥掉再写进步骤缓存的 failed 原因，
# 改这里的文案必须同步改那边，否则缓存里的失败原因会带上这段冗长提示。
_RETRY_HINT = (
    "。请先调用 get_page_source 获取当前界面的控件层级摘要，"
    "再从摘要里挑真实存在的 text / resource-id / content-desc 拼定位表达式"
    "（Appium 不支持 css 选择器，也不接受只给一段可见文本）；"
    "若摘要里没有目标文本，说明它还没滚进可视区，先用 scroll_to_element 滚动查找。"
    "禁止原样重试同一个定位表达式。"
)

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
    """
    return _execute(app.find, locator, timeout)


@tool
def click(locator: str, timeout: Optional[float] = None) -> str:
    """点击控件，返回点击后的界面控件摘要。

    locator 写法同 find。若报「element not interactable」，说明命中的是不可点的
    子控件（如 TextView），应改用 get_page_source 找它外层带 clickable 的父容器。
    """
    return _execute(app.click, locator, timeout)


@tool
def send_keys(locator: str, text: str, clear: bool = True) -> str:
    """往输入框（EditText）写内容；clear=True 时先清空再写。

    locator 写法同 find，例如 id=com.android.settings:id/search_src_text。
    """
    return _execute(app.send_keys, locator, text, clear)


@tool
def get_text(locator: str, timeout: Optional[float] = None) -> str:
    """读取单个控件的文本，对应测试步骤里的「获取 XXX」（如「获取 剩余电量」）。

    text 为空时自动退回 content-desc / value（图标类控件往往只有 content-desc）。
    locator 写法同 find。
    """
    return _execute(app.text_of, locator, timeout if timeout is not None else 10)


@tool
def scroll_to_element(locator: str, max_swipes: int = 5) -> str:
    """滚动查找控件，对应测试步骤里的「滚动到页面 直至找到 X」。

    Android 列表（设置页、更多链接页）里的条目默认不在可视区，直接 find 必然失败，
    必须先用本工具滚出来。locator 建议用 //*[contains(@text,'省电与电池')] 这种按文本的写法，
    框架会自动翻译成设备端 UiScrollable.scrollIntoView（比逐屏 swipe 快得多）。
    滚完 max_swipes 屏仍找不到会返回失败原因，说明入口走错了。
    """
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
    """
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

