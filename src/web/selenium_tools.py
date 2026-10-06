import time
from typing import Any, Callable, Optional

from langchain_core.tools import tool

from src.web.web_framework import WebAutoFramework

web = WebAutoFramework()

# 定位失败时给 agent 的纠错提示：历史问题是 agent 看不到导航菜单元素，
# 只能凭经验臆测出 `.el-menu-vertical-demo [role='menu']` 这类不存在的选择器，
# 抛出的 NoSuchElementException 又没被兜住，直接终止了整个 chain。
_RETRY_HINT = (
    "请先调用 get_page_source 获取当前页面真实存在的元素列表，"
    "再从中挑选标签/属性构造 css 选择器，不要臆测类名或层级结构。"
    "css **不支持按文本定位**（`:contains()` / `:has-text()` 是 jQuery、Playwright 的语法，"
    "Selenium 会抛 InvalidSelectorException，原样重试永远失败）；"
    "需要按可见文本定位（如「『北京市』那一行左边的展开箭头」）时改用 xpath："
    "以 // 开头即按 xpath 处理，例如 "
    "//tr[.//td[contains(., '北京市')]]//div[contains(@class, 'el-table__expand-icon')]。"
    "同一个表达式已经失败过一次就不要再原样重试，换一种定位方式。"
)


def _error_message(exc: BaseException) -> str:
    """把异常压成一行可读信息（selenium 的异常字符串带一大段 Stacktrace）。"""
    raw = str(exc).split("Stacktrace:")[0].strip()
    return (raw.splitlines()[0] if raw else "") or exc.__class__.__name__


def _as_observation(value: Any) -> str:
    """工具返回值统一转成非空字符串，避免 Observation 出现 None / 空串这种噪音。"""
    if value is None:
        return "操作成功"
    if isinstance(value, str):
        return value.strip() or "操作成功"
    return str(value)


def _execute(
    action: str,
    func: Callable[[], Any],
    on_error_context: Optional[Callable[[], str]] = None,
) -> str:
    """统一执行工具动作，并把任何异常降级成字符串 Observation。

    为什么必须在工具内部兜异常：
        `AgentExecutor(handle_parsing_errors=True)` 只处理「LLM 输出解析失败」；
        `BaseTool.run` 对普通异常一律 raise（只有 ToolException / ValidationError
        才受 handle_tool_error / handle_validation_error 控制），
        所以 selenium 异常会一路冒泡到 chain.invoke，进程以 exit code 1 结束，
        浏览器也不会被关闭。降级成 Observation 后 agent 才有机会自我纠错。
    """
    try:
        return _as_observation(func())
    except Exception as exc:  # noqa: BLE001 - 工具层必须兜住所有异常
        message = (
            f"{action} 执行失败：{type(exc).__name__}: {_error_message(exc)}。{_RETRY_HINT}"
        )
        if on_error_context is not None:
            try:
                message += f"\n当前页面元素列表：\n{on_error_context()}"
            except Exception:  # noqa: BLE001 - 兜底信息本身失败时忽略
                pass
        print(message)
        return message


def _do_sleep(seconds: int) -> str:
    seconds = max(0, min(int(seconds), 30))
    time.sleep(seconds)
    return f"已等待 {seconds} 秒"


@tool
def open(url: str):
    """
    使用浏览器打开特定的url，并返回网页中可交互元素的html摘要
    """
    return _execute("open", lambda: web.open(url))


@tool
def find(css: str):
    """定位网页元素（会等待元素出现），返回当前页面可交互元素的html摘要；
    定位到的元素会被 click / send_keys 复用。
    css 参数支持两种写法：普通 css 选择器（如 "a[href='#/mall/region']"），
    或以 // 开头的 xpath（需要按可见文本定位时必须用 xpath，如
    "//tr[.//td[contains(., '北京市')]]//div[contains(@class, 'el-table__expand-icon')]"）"""
    return _execute("find", lambda: web.find(css), on_error_context=web.source)


@tool
def click(css: str = None):
    """定位网页元素后点击（css 参数可传 css 选择器或 // 开头的 xpath，不传则点击上一次
    find 定位到的元素），返回点击后页面可交互元素的html摘要"""

    def _click():
        if css:
            web.find(css)
        return web.click()

    return _execute("click", _click, on_error_context=web.source)


@tool
def send_keys(css: str, text: str):
    """定位到 css（css 选择器或 // 开头的 xpath）指定的元素，并输入 text，
    返回输入后页面可交互元素的html摘要"""

    def _send_keys():
        if css:
            web.find(css)
        return web.send_keys(text)

    return _execute("send_keys", _send_keys, on_error_context=web.source)

@tool
def get_page_source():
    """获取当前页面可交互元素的html摘要。页面跳转后、或定位失败时，
    必须先用它确认可用的标签与属性，再决定下一步的css选择器"""
    return _execute("get_page_source", web.source)


@tool
def get_page_text():
    """获取当前页面的可见文本，用于确认页面是否跳转成功"""
    return _execute("get_page_text", web.page_text)


@tool
def assert_contains(text: str, css: str = None):
    """断言：页面（css为空时）或css指定的元素中包含text。
    text支持用「、」或「,」分隔多个期望文本，全部包含才算通过；返回断言结论，不会抛异常。
    css 参数可传 css 选择器或 // 开头的 xpath（限定断言范围，如某一行的所有单元格：
    "//tr[.//td[contains(., '北京市')]]//td"）"""
    return _execute("assert_contains", lambda: web.assert_contains(text, css))


@tool
def sleep(seconds: int):
    """等待指定的秒数"""
    return _execute("sleep", lambda: _do_sleep(seconds))


@tool
def quit():
    """退出浏览器"""
    return _execute("quit", web.quit)


@tool
def get_current_url():
    """获取当前的url"""
    return _execute("get_current_url", web.get_current_url)


tools = [
    open, quit, get_current_url, find, click, send_keys, sleep,
    get_page_source, get_page_text, assert_contains,
]

# LLM 偶尔会把 action_input 写成字符串（而不是 dict），从而触发 pydantic ValidationError。
# 打开这两个开关，校验错误 / ToolException 也会变成 Observation，而不是终止 chain。
for _t in tools:
    _t.handle_validation_error = True
    _t.handle_tool_error = True


if __name__ == "__main__":
    # 不依赖 LLM 的最小自测：登录 litemall 后台 -> 断言导航栏 -> 验证异常不再冒泡
    print(open.invoke({"url": "https://litemall.hogwarts.ceshiren.com/#/login?redirect=%2Fdashboard"}))
    print(send_keys.invoke({"css": "input[name='username']", "text": "hogwarts"}))
    print(send_keys.invoke({"css": "input[name='password']", "text": "test12345"}))
    print(click.invoke({"css": "button.el-button--primary"}))
    print(get_current_url.invoke({}))
    print(assert_contains.invoke({"text": "首页、商场管理、商品管理"}))
    # 导航项选择器会匹配多个 li，验证「聚合所有匹配元素文本」的断言
    print(assert_contains.invoke({"text": "首页、商场管理、商品管理", "css": "li[role='menuitem']"}))
    print(assert_contains.invoke({"text": "不存在的菜单"}))
    print(find.invoke({"css": ".not-exist-element"}))
    print(quit.invoke({}))

