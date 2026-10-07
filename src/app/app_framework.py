"""Appium 移动端自动化基础框架（Android / UiAutomator2）。

与 src/web/web_framework.py 一一对应，只是把「Selenium + 浏览器」换成「Appium + 真机」：

    web_framework                          app_framework
    resolve_chromedriver()                 resolve_appium_server()
    by_of(css_or_xpath) -> By              locator_of(locator) -> (AppiumBy, 表达式)
    WebFramework.source()                  AppiumWeb.source()（**压缩后**的控件层级摘要）
    WebFramework.assert_contains()         AppiumWeb.assert_contains()
    WebFramework.quit()                    AppiumWeb.quit()
    WEB_FIXTURE_DOC 里的 webdriver.Chrome   create_driver() / build_options()

两处 App 领域特有的设计（都是被历史运行日志逼出来的）：

1. source() 必须压缩。Android 的 driver.page_source 是一整棵 UIAutomator 层级 XML，
   一屏「设置」页动辄 3~8 万字符（每个 node 带 20 多个属性、几十个纯布局容器）。
   原样返回会：
       a. 几步就把模型输入顶穿（DashScope: Range of input length should be [1, 30720]），
          invoke 一抛异常，本轮已采集到的真实步骤全丢；
       b. 让 agent 在噪音里找不到「哪个控件能点、它的 text / resource-id 是什么」，
          只能凭经验臆造定位表达式，然后反复定位失败烧光 max_iterations。
   所以这里只保留「有文本 / 有 id / 可交互」的控件，压成一行一个
       <TextView id="com.android.settings:id/title" text="省电与电池" clickable bounds="[0,336][1080,441]">
   与 web 版 `<tag attrs>text</tag>` 的摘要保持同构（见 summarize_hierarchy）。

2. 定位表达式统一走 locator_of() 分流。Appium 的定位方式比 Selenium 多
   （resource-id / content-desc / UiAutomator 表达式），且**完全不支持 css 选择器**；
   模型从 web 语料里带过来的 `input[name='x']` 这类写法在 Appium 下必然失败。
   locator_of 负责「识别 + 剥前缀 + 给可操作的报错」，报错文案直接告诉模型该改写成什么。
"""

import os
import re
import xml.etree.ElementTree as ET
from time import sleep
from typing import Any, Mapping, Optional, Sequence

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---- 连接与启动参数：全部支持环境变量覆盖，缺省值即「本机 Appium + 系统设置」----
# 换设备 / 换被测 app 时不必改代码：
#     APPIUM_SERVER=http://127.0.0.1:4723 APP_UDID=emulator-5554 \
#     APP_PACKAGE=com.android.settings APP_ACTIVITY=.Settings python src/app/generate_autoapp.py
DEFAULT_APPIUM_SERVER = "http://127.0.0.1:4723"
DEFAULT_PLATFORM_NAME = "Android"
DEFAULT_AUTOMATION_NAME = "UiAutomator2"
DEFAULT_DEVICE_NAME = "Android"
DEFAULT_APP_PACKAGE = "com.android.settings"
DEFAULT_APP_ACTIVITY = ".Settings"

# 显式等待的默认超时时间（秒）
DEFAULT_TIMEOUT = 10
# 返回给 LLM 的控件层级摘要最大长度，避免 prompt 过长（与 web_framework 同一约定）
MAX_SOURCE_LENGTH = 6000
# 返回给 LLM 的界面可见文本最大长度
MAX_TEXT_LENGTH = 2000
# 摘要中最多保留的控件个数
MAX_ELEMENT_COUNT = 150
# 滚动查找元素时最多滑动几次（超过说明该文本在当前界面体系里根本不存在）
MAX_SCROLL_SWIPES = 15
# 滑动后等界面重绘的时间 / activity 稳定轮询间隔（秒）
SWIPE_PAUSE = 0.8
ACTIVITY_POLL_INTERVAL = 0.2
# 滑动手势区域四周留出的死区比例：贴着屏幕边缘起手，Android 10+ 的手势导航会把它识别成
# 「下拉通知栏 / 侧滑返回」，页面纹丝不动 —— 症状和「滚动方向写反了」一模一样，很难查
SWIPE_DEAD_ZONE = 0.15

_ENV_KEYS = {
    "platform_name": "APP_PLATFORM_NAME",
    "automation_name": "APP_AUTOMATION_NAME",
    "device_name": "APP_DEVICE_NAME",
    "udid": "APP_UDID",
    "app_package": "APP_PACKAGE",
    "app_activity": "APP_ACTIVITY",
    "no_reset": "APP_NO_RESET",
    "new_command_timeout": "APP_NEW_COMMAND_TIMEOUT",
}


def _env_bool(key: str, default: str) -> bool:
    """把环境变量的常见真假写法统一成 bool。"""
    return os.getenv(key, default).strip().lower() in {"1", "true", "yes", "on"}


def resolve_appium_server() -> str:
    """Appium server 地址：默认本机 4723，可用 APPIUM_SERVER / APP_APPIUM_SERVER 覆盖。"""
    return (os.getenv("APPIUM_SERVER") or os.getenv("APP_APPIUM_SERVER")
            or DEFAULT_APPIUM_SERVER)


def resolve_capabilities() -> dict[str, Any]:
    """合并环境变量与默认值，得到 uiautomator2 能力集（capabilities）。

    只在这里集中解析一次：采集阶段用的 driver 与生成脚本里的 fixture 共用同一套口径，
    避免出现「采集时连得上、脚本跑起来连不上」这类两套配置不一致的问题。
    """
    caps: dict[str, Any] = {
        "platform_name": DEFAULT_PLATFORM_NAME,
        "automation_name": DEFAULT_AUTOMATION_NAME,
        "device_name": DEFAULT_DEVICE_NAME,
        "udid": "",
        "app_package": DEFAULT_APP_PACKAGE,
        "app_activity": DEFAULT_APP_ACTIVITY,
        "no_reset": True,
        "new_command_timeout": 300,
    }
    for key, env_name in _ENV_KEYS.items():
        value = os.getenv(env_name)
        if value is not None and str(value).strip():
            caps[key] = value
    caps["no_reset"] = _env_bool("APP_NO_RESET", "true")
    try:
        caps["new_command_timeout"] = int(os.getenv("APP_NEW_COMMAND_TIMEOUT", "300"))
    except ValueError:
        caps["new_command_timeout"] = 300
    return caps


def build_options(app_activity: Optional[str] = None,
                  app_package: Optional[str] = None,
                  **overrides: Any) -> UiAutomator2Options:
    """构造 UiAutomator2Options。

    优先级：显式入参（来自 md 用例的「前提条件」）> overrides > 环境变量 > 默认值。
    overrides 用于个别脚本追加能力（如 auto_grant_permissions=True）。
    """
    caps = resolve_capabilities()
    options = UiAutomator2Options()
    options.platform_name = overrides.pop("platform_name", None) or caps["platform_name"]
    options.automation_name = overrides.pop("automation_name", None) or caps["automation_name"]
    options.device_name = overrides.pop("device_name", None) or caps["device_name"]
    udid = overrides.pop("udid", None) or caps.get("udid")
    if udid:
        options.udid = str(udid)
    options.app_package = str(app_package or overrides.pop("app_package", None)
                              or caps["app_package"])
    options.app_activity = str(app_activity or overrides.pop("app_activity", None)
                               or caps["app_activity"])
    options.no_reset = bool(overrides.pop("no_reset", caps["no_reset"]))
    options.new_command_timeout = int(overrides.pop("new_command_timeout",
                                                    caps["new_command_timeout"]))
    # forceAppLaunch 默认打开。UiAutomator2 的默认值是 false：app 已在前台时建 session
    # 只把它 bring to front，**不会**回到 appActivity —— 于是上一条用例（或上一次失败
    # 中断）停在的子页面成了下一条用例的起点。实测设置 app 停在 .SubSettings 时，脚本
    # 第一步就找不到首页的「更多连接」，报错长得像「app 改版了 / 用例文案不对」，完全
    # 指不到「起点不是首页」这个真因，排查成本极高。测试必须从确定状态开始，所以每次建
    # session 都强制重启到 appActivity；no_reset 仍是 True，app 数据与登录态不受影响
    # （只是回到入口 activity）。调试时想保留 app 当前界面：APP_FORCE_APP_LAUNCH=0。
    force_launch = overrides.pop("force_app_launch", None)
    if force_launch is None:
        force_launch = _env_bool("APP_FORCE_APP_LAUNCH", "true")
    options.set_capability("appium:forceAppLaunch", bool(force_launch))
    for key, value in overrides.items():
        options.set_capability(key, value)
    return options


def _activity_state(driver: Any) -> tuple[str, str]:
    """当前 (package, activity)，取不到就用空串占位（不让状态判断本身抛异常）。"""
    try:
        return str(driver.current_package or ""), str(driver.current_activity or "")
    except WebDriverException:
        return "", ""


def _wait_activity_stable(driver: Any, max_wait: float = 4.0,
                          interval: float = ACTIVITY_POLL_INTERVAL) -> tuple[str, str]:
    """等界面切换落定：连续两次读到同一个 (package, activity) 就算稳定。

    Android 的界面切换是「新 activity 起来 -> 旧 activity 销毁」，
    在中间态取 page_source 会拿到上一个界面的控件，进而让 agent 用已经过期的
    定位表达式去点、点失败、再取一次…… 白烧好几轮。
    """
    deadline = max_wait
    previous = _activity_state(driver)
    while deadline > 0:
        sleep(interval)
        deadline -= interval
        current = _activity_state(driver)
        if current == previous and current[1]:
            return current
        previous = current
    return previous


def ensure_foreground(driver: Any, app_package: str, app_activity: str) -> str:
    """确认被测 app 真的在前台；不在就用 activate_app 兜底拉起来。返回空串表示已就绪。

    为什么必须校验：`appActivity` 写错时 **Appium 建 session 不会报错** ——
    典型例子是设置 app：AOSP 的入口是 `.Settings`，MIUI 上是 `.MainSettings`，
    用 `.Settings` 起 session 一切「正常」，界面却还停在上一个 app（如 com.miui.newhome）。
    此后每一步都会「合理地失败」：采集时 agent 拿着别家 app 的控件去 scroll/find，
    烧光 max_iterations；跑生成的脚本时则是一串定位超时 + 断言失败，看不出根因。
    兜底用 activate_app（走系统 launcher activity，不依赖 appActivity 写对），
    兜不住才报错，并把「怎么查真实入口 activity」写进消息里。

    两条建 session 的路径共用这一个实现：AppTools.init（第一环采集）与
    create_driver（第二环生成的 pytest 脚本），口径必须一致 —— 采集时能起来、
    脚本里起不来（或反之）都会让「第一环验证过的定位表达式」失去意义。
    """
    package, _ = _activity_state(driver)
    if package == app_package:
        return ""
    try:
        driver.activate_app(app_package)
        sleep(ACTIVITY_POLL_INTERVAL)
        package, _ = _wait_activity_stable(driver)
    except WebDriverException as exc:
        return (f"启动 {app_package}/{app_activity} 后前台是 {package or '未知'}，"
                f"尝试用 activate_app 拉起也失败：{exc}。"
                f"请确认该 app 已安装（adb shell pm list packages | grep {app_package}）。")
    if package == app_package:
        return ""
    return (f"启动 {app_package}/{app_activity} 后前台仍是 {package or '未知'}，"
            f"说明这个 app_activity 不是本机的入口。请用 "
            f"`adb shell cmd package resolve-activity --brief {app_package}` "
            f"查出真实入口（MIUI 的设置是 .MainSettings，AOSP 才是 .Settings），"
            f"改正 md 前提条件里的 app activity 后重跑。")


def create_driver(app_activity: Optional[str] = None,
                  app_package: Optional[str] = None,
                  server: Optional[str] = None,
                  verify_foreground: bool = True,
                  **overrides: Any) -> webdriver.Remote:
    """启动一个 Appium session 并把被测 app 拉到前台。

    生成的测试脚本**统一 import 这个函数**建 driver，而不是自己手拼 capabilities：
    一来 server 地址 / 设备 udid 由环境变量决定（脚本里写死 udid，换台机器就跑不了）；
    二来「采集时的 driver」与「脚本里的 driver」口径完全一致，
    采集阶段验证过的定位表达式才真的可复用。

    verify_foreground：建完 session 后校验前台包名（默认开）。校验不过会 quit 掉这个
    session 再抛 ValueError —— 宁可让 pytest 立刻红在「app 没起来」上，也不要让它
    红在十几步之后的定位超时里（那种失败信息完全指不到 appActivity 写错这个根因）。
    """
    options = build_options(app_activity, app_package, **overrides)
    driver = webdriver.Remote(server or resolve_appium_server(), options=options)
    # 超时统一交给显式等待（WebDriverWait / try_find）控制：隐式等待一旦设上，
    # 连 find_elements 判「空列表」都要先阻塞同样长的时间，滚动循环会被拖慢 N 倍。
    driver.implicitly_wait(0)
    if verify_foreground:
        problem = ensure_foreground(driver, str(options.app_package), str(options.app_activity))
        if problem:
            try:
                driver.quit()  # 别把连错 app 的 session 留在设备上占着
            except WebDriverException:
                pass
            raise ValueError(problem)
    return driver



# ---------------------------------------------------------------------------
# 定位表达式分流
# ---------------------------------------------------------------------------
# Appium 的定位方式比 Selenium 多，且**完全不支持 css 选择器**。统一用「前缀」显式声明，
# 避免歧义（`id` 在不同平台既可能指 resource-id，也可能指 accessibility id）：
#     //*[contains(@text,'省电与电池')]          xpath（默认：以 / . ( 开头即认）
#     xpath=//android.widget.TextView           同上，显式写法
#     id=com.android.settings:id/title          resource-id
#     com.android.settings:id/title             resource-id（不带前缀也能识别）
#     acc=更多 / desc=更多                       content-desc（accessibility id）
#     ui=new UiSelector().textContains("打印")   UiAutomator 表达式
#     class=android.widget.TextView             class name
_LOCATOR_PREFIXES: tuple[tuple[str, str], ...] = (
    ("xpath=", AppiumBy.XPATH),
    ("id=", AppiumBy.ID),
    ("resource-id=", AppiumBy.ID),
    ("resourceid=", AppiumBy.ID),
    ("acc=", AppiumBy.ACCESSIBILITY_ID),
    ("accessibility-id=", AppiumBy.ACCESSIBILITY_ID),
    ("accessibility_id=", AppiumBy.ACCESSIBILITY_ID),
    ("desc=", AppiumBy.ACCESSIBILITY_ID),
    ("content-desc=", AppiumBy.ACCESSIBILITY_ID),
    ("ui=", AppiumBy.ANDROID_UIAUTOMATOR),
    ("uiautomator=", AppiumBy.ANDROID_UIAUTOMATOR),
    ("class=", AppiumBy.CLASS_NAME),
    ("classname=", AppiumBy.CLASS_NAME),
    ("name=", AppiumBy.ID),
)
# 完整 resource-id：包名:id/控件名（Android 层级里最常见的稳定定位依据）
_RESOURCE_ID_RE = re.compile(r"^[\w.]+:id/[\w.$/]+$")
# UiAutomator 原生表达式
_UIAUTOMATOR_RE = re.compile(r"^new\s+Ui(Selector|Scrollable|Object|Collection)\b", re.I)
# Android 控件全限定类名
_CLASS_NAME_RE = re.compile(r"^(?:android|androidx|com|io|org|net)\w*(?:\.\w+)+$")

_LOCATOR_HINT = (
    "Appium 不支持 css 选择器，也不接受「只给一段可见文本」。"
    "按文本定位请写 xpath，如 //*[contains(@text,'省电与电池')]；"
    "按 resource-id 定位请写 id=com.android.settings:id/title；"
    "按 content-desc 定位请写 acc=更多；"
    "控件到底有哪些属性，先用 get_page_source 拿当前界面的控件层级摘要再决定。"
)


def locator_of(locator: str) -> tuple[str, str]:
    """把定位表达式解析成 (AppiumBy 常量, 真实表达式)，并剥掉 `xpath=` / `id=` 这类前缀。

    识别不了就抛 ValueError，**错误文案里直接给出正确写法**：这个异常最终会变成
    agent 的 Observation（见 appium_tools._execute），模型照着改一次就能过。
    反之若静默按 xpath 处理，Android 端只会回一句晦涩的
    `UiAutomator died while responding to command`，模型从中得不到任何可操作信息。
    """
    text = (locator or "").strip()
    if not text:
        raise ValueError(f"定位表达式不能为空。{_LOCATOR_HINT}")
    low = text.lower()
    for prefix, by in _LOCATOR_PREFIXES:
        if low.startswith(prefix):
            expression = text[len(prefix):].strip()
            if not expression:
                raise ValueError(
                    f"定位表达式 {locator!r} 的 {prefix.rstrip('=')} 后面是空的。{_LOCATOR_HINT}")
            return by, expression
    if text.startswith(("/", "./", "../", "(")):
        return AppiumBy.XPATH, text
    if _UIAUTOMATOR_RE.match(text):
        return AppiumBy.ANDROID_UIAUTOMATOR, text
    if _RESOURCE_ID_RE.match(text):
        return AppiumBy.ID, text
    if " " not in text and _CLASS_NAME_RE.match(text):
        return AppiumBy.CLASS_NAME, text
    raise ValueError(f"无法识别定位表达式 {locator!r}。{_LOCATOR_HINT}")


def by_of(locator: str) -> str:
    """定位表达式 -> AppiumBy 常量（不剥前缀；与 web_framework.by_of 同名同位，便于对照）。

    生成脚本里通常直接用 locate / find_element（内部会剥前缀）；这个函数只在
    需要自己组 (by, expr) 元组喂给 WebDriverWait.until(EC.xxx((by, expr))) 时才用。
    """
    return locator_of(locator)[0]


# ---------------------------------------------------------------------------
# 控件层级摘要（source 压缩）
# ---------------------------------------------------------------------------
# 摘要里保留的控件属性：(层级 XML 里的属性名, 摘要里的短名)。
# text / resource-id / content-desc 是 Android 上唯三稳定的定位依据，必须留；
# package / index / focusable / password 这些对「下一步怎么定位」毫无帮助，直接丢。
_KEEP_ATTRS: tuple[tuple[str, str], ...] = (
    ("resource-id", "id"),
    ("text", "text"),
    ("content-desc", "desc"),
)
# 值为 true 才输出的布尔属性：控件「能不能点 / 能不能滚 / 是不是已选中」直接决定
# agent 下一步该 click 还是该 scroll_to_element，false 时输出纯属噪音。
_FLAG_ATTRS: tuple[str, ...] = (
    "clickable", "scrollable", "checkable", "checked", "selected", "long-clickable",
)
# 单个属性值的最大长度（「说明段落」这类长文本控件会把摘要撑爆）
_ATTR_VALUE_MAX = 80

_NODE_TAG_RE = re.compile(r"<node\b[^>]*", re.S)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def _clean_attr(value: Optional[str]) -> str:
    """压掉换行与连续空白并截断。

    层级 XML 里的 text 常带换行，不压平会破坏摘要「一行一个控件」的格式。
    """
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= _ATTR_VALUE_MAX else text[:_ATTR_VALUE_MAX] + "…"


def _short_class(name: Optional[str]) -> str:
    """android.widget.TextView -> TextView（摘要里类名只需能区分控件种类）。"""
    return (name or "").rsplit(".", 1)[-1]


def _describe(attrs: Mapping[str, str]) -> str:
    """把一个控件的属性字典渲染成摘要里的一行。"""
    parts = [_short_class(attrs.get("class", ""))]
    for source_key, short_key in _KEEP_ATTRS:
        value = _clean_attr(attrs.get(source_key))
        if value:
            parts.append(f'{short_key}="{value}"')
    parts.extend(flag for flag in _FLAG_ATTRS
                 if (attrs.get(flag) or "").strip().lower() == "true")
    if (attrs.get("enabled") or "true").strip().lower() == "false":
        parts.append("disabled")
    bounds = (attrs.get("bounds") or "").strip()
    if bounds:
        parts.append(f"bounds={bounds}")
    return "<" + " ".join(part for part in parts if part) + ">"


def _is_worth_showing(attrs: Mapping[str, str]) -> bool:
    """只有「有文本 / 有 id / 可交互」的控件才值得进摘要。

    纯布局容器（FrameLayout / LinearLayout / ViewGroup：无 text、无 id、不可点）
    在 Android 层级里占了一半以上的行数，却对定位毫无价值 —— 全部滤掉。
    """
    if any(_clean_attr(attrs.get(key)) for key, _ in _KEEP_ATTRS):
        return True
    return any((attrs.get(flag) or "").strip().lower() == "true" for flag in _FLAG_ATTRS)



def summarize_hierarchy(xml_text: Optional[str], *,
                        max_count: int = MAX_ELEMENT_COUNT,
                        max_length: int = MAX_SOURCE_LENGTH) -> str:
    """把 Appium 的 UIAutomator 层级 XML 压成「一行一个控件」的摘要。

    输出形如::

        <TextView id="com.android.settings:id/title" text="省电与电池" clickable bounds="[0,336][1080,441]">
        <ScrollView scrollable bounds="[0,231][1080,2400]">

    模型据此就能拼出真实存在的定位表达式（//*[contains(@text,'省电与电池')] 或
    id=com.android.settings:id/title），并知道哪个容器可滚动 —— 这正是「agent 自己发现
    真实定位方式」的前提；给原始 XML 反而会因为过长被截断而丢掉关键控件。

    解析失败（少数 ROM 返回带非法字符的层级）时退回正则提取，至少保住文本线索，
    绝不抛异常：这个函数处在 Observation 的关键路径上，抛异常等于整轮采集作废。
    """
    if not xml_text or not xml_text.strip():
        return ""
    try:
        root = ET.fromstring(xml_text)
        rows = (_describe(node.attrib) for node in root.iter()
                if _is_worth_showing(node.attrib))
    except ET.ParseError:
        def _regex_rows():
            for tag in _NODE_TAG_RE.findall(xml_text):
                attrs = dict(_ATTR_RE.findall(tag))
                if _is_worth_showing(attrs):
                    yield _describe(attrs)
        rows = _regex_rows()
    lines: list[str] = []
    for row in rows:
        if len(lines) >= max_count:
            break
        lines.append(row)
    text = "\n".join(lines)
    if len(text) > max_length:
        text = text[:max_length].rstrip() + (
            f"\n…（控件层级过长已截断，当前只列出前 {len(lines)} 个控件；"
            "请用带 text / resource-id 的 xpath 精确到子树，或先滚动到目标区域再取摘要）"
        )
    return text


def extract_texts(xml_text: Optional[str], *, max_length: int = MAX_TEXT_LENGTH) -> str:
    """从层级 XML 里按文档顺序抽出所有可见文本（text + content-desc）。

    与 web_framework 的「页面全文」口径一致：只留非空文本、去掉重复项、超长截断。
    整页断言（assert_contains 不传 locator）就基于这段文本判断。
    """
    if not xml_text or not xml_text.strip():
        return ""
    try:
        root = ET.fromstring(xml_text)
        raw: list[str] = []
        for node in root.iter():
            for key in ("text", "content-desc"):
                value = _clean_attr(node.attrib.get(key))
                if value:
                    raw.append(value)
    except ET.ParseError:
        raw = [_clean_attr(value)
               for tag in _NODE_TAG_RE.findall(xml_text)
               for key, value in _ATTR_RE.findall(tag)
               if key in ("text", "content-desc") and _clean_attr(value)]
    seen: set[str] = set()
    texts: list[str] = []
    for item in raw:
        if item in seen:
            continue
        seen.add(item)
        texts.append(item)
    joined = "\n".join(texts)
    if len(joined) > max_length:
        joined = joined[:max_length].rstrip() + "\n…（界面文本过长已截断）"
    return joined



# ---------------------------------------------------------------------------
# 元素定位 helper：采集框架与生成的测试脚本共用同一套实现
# ---------------------------------------------------------------------------
def try_find(driver: Any, locator: str) -> Optional[Any]:
    """找一下元素，找不到就返回 None，**绝不抛异常、也绝不等待**。

    专门给「滚动-检查」循环用：每滑一次都要判断目标是否已经出现，
    若用 WebDriverWait，每次未命中都要白等满 timeout（滚 15 次能多花两分钟）。
    """
    by, expression = locator_of(locator)
    try:
        return driver.find_element(by, expression)
    except (NoSuchElementException, WebDriverException):
        return None


def locate(driver: Any, locator: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """显式等待并返回第一个匹配元素；等不到抛 TimeoutException。

    Appium 下**不要用 driver.implicitly_wait**：隐式等待一旦设上，find_elements
    判「空列表」也要阻塞同样长的时间，滚动循环会被拖慢 N 倍。统一用这里的显式等待。
    """
    by, expression = locator_of(locator)
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, expression)))


def locate_all(driver: Any, locator: str, timeout: float = DEFAULT_TIMEOUT) -> list[Any]:
    """显式等待并返回所有匹配元素（至少 1 个）；等不到抛 TimeoutException。

    超时抛异常而**不是**返回空列表：presence_of_all_elements_located 等不到时会抛
    TimeoutException，若在这里吞掉返回 []，调用方就拿不到「定位表达式写错了」这个信号，
    只会在后面的断言里看到一句莫名其妙的失败（第一环的 agent 会把它当成界面问题反复滚动）。
    """
    by, expression = locator_of(locator)
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_all_elements_located((by, expression)))


# swipe() 认的方向。语义与 Appium `mobile: scrollGesture` 的 direction **完全一致**，
# 说的是「内容往哪边滚 / 你会看到哪一侧的条目」，不是手指往哪边划：
#   down = 内容向上移，露出列表**更下面**的条目（旧的 swipe_up 就是这个方向）
#   up   = 内容向下移，回到列表**更上面**的条目（滚过头时用它退回去）
#   left / right = 横向翻页（Tab、ViewPager、横向列表）
SWIPE_DIRECTIONS = ("down", "up", "left", "right")
# 常见同义写法的容错映射。只收**没有歧义**的说法：
# 「up/down」这两个词本身就有两套读法（手指方向 vs 内容方向），所以不收
# 「swipe_up / 上滑」这类写法，宁可让调用方按 SWIPE_DIRECTIONS 的语义明确写 down/up。
_SWIPE_ALIASES = {
    "next": "down", "forward": "down", "bottom": "down", "lower": "down", "下": "down",
    "prev": "up", "previous": "up", "backward": "up", "top": "up", "upper": "up", "上": "up",
    "左": "left", "右": "right",
}


def _swipe_direction(direction: str) -> str:
    """把调用方给的方向写法归一成 SWIPE_DIRECTIONS 里的值；认不出来就报错并列出合法值。

    报错而不是「猜一个」或默认成 down：方向搞反时页面会朝反方向翻，症状是
    「滚了 N 屏还是找不到目标」，在真机日志里和「定位表达式写错了」长得一模一样，
    等跑完一轮才发现是方向拼错，白烧一次采集。
    """
    text = str(direction or "").strip().lower()
    if text in SWIPE_DIRECTIONS:
        return text
    alias = _SWIPE_ALIASES.get(text)
    if alias:
        return alias
    raise ValueError(
        f"swipe 的方向 {direction!r} 不认识。可选值：{' / '.join(SWIPE_DIRECTIONS)}"
        "（down = 露出更下面的条目，up = 回到更上面的条目，left / right = 横向翻页），"
        f"也接受同义写法：{' / '.join(sorted(_SWIPE_ALIASES))}")


def swipe(driver: Any, direction: str, percent: float = 0.6) -> None:
    """朝指定方向滚动一屏（旧名 swipe_up，只支持一个方向；现在四个方向都支持）。

    Args:
        driver: Appium WebDriver（create_driver 的返回值）。
        direction: **内容的滚动方向**，必填，见 SWIPE_DIRECTIONS：
            "down" = 往下翻页、露出更下面的条目（与旧 swipe_up(driver) 行为完全一致）；
            "up" = 往回翻，露出更上面的条目。
        percent: 滑动长度占手势区域的比例，(0, 1]，默认 0.6。太小则一屏翻不动多少、
            要更多轮才滚到目标；太大容易一次跨过目标条目 —— scroll_to 是「滑一屏
            就 try_find 一次」，跨过去就永远错过了。0.6 是实测在「设置」这类长列表
            上的折中值。

    direction **故意不给默认值**：函数名里已经不含方向了，再默认成 "down" 的话，
    光看 `swipe(driver)` 这一行根本不知道屏幕往哪翻，只能翻进实现里确认 ——
    旧 swipe_up 时代「方向藏在函数名里」至少是自解释的。强制写成
    `swipe(driver, "down")` / `swipe(driver, "up")`，调用点自己就说清了意图，
    也顺手堵掉「忘传方向结果一路往下翻」这类静默走反的 bug。

    为什么方向按「内容」而不是「手指」表述：旧函数叫 swipe_up，内部却发
    direction="down"（Appium 的 direction 指的是内容滚动方向），一个函数名里混着
    两套坐标系，读代码时怎么都对不上。改成 swipe(driver, direction) 之后，
    传进来的值就是 Appium 收到的值，中间不再有任何隐式翻转。

    用 UiAutomator2 的 `mobile: scrollGesture` 而不是 W3C Actions：后者在部分
    Android 13+ 设备上会被系统识别成「慢速拖拽」而触发长按/拖动手势，页面根本不动。
    """
    gesture = _swipe_direction(direction)
    if not 0 < percent <= 1:
        raise ValueError(f"swipe 的 percent 必须落在 (0, 1] 区间，收到 {percent!r}")
    size = driver.get_window_size()
    width, height = int(size["width"]), int(size["height"])
    if gesture in ("down", "up"):
        # 竖向：横向铺满、上下各留 SWIPE_DEAD_ZONE 死区。这份参数与旧 swipe_up 一模一样
        # （是在真机上验证过「设置」长列表能稳定翻页的），改名时不顺手改行为
        area = {"left": 0,
                "top": int(height * SWIPE_DEAD_ZONE),
                "width": width,
                "height": int(height * (1 - 2 * SWIPE_DEAD_ZONE))}
    else:
        # 横向：纵向铺满、左右各留死区 —— 从屏幕最左/最右边缘起手会被手势导航吃掉
        area = {"left": int(width * SWIPE_DEAD_ZONE),
                "top": 0,
                "width": int(width * (1 - 2 * SWIPE_DEAD_ZONE)),
                "height": height}
    driver.execute_script("mobile: scrollGesture", {
        **area,
        "direction": gesture,
        "percent": percent,
    })
    sleep(SWIPE_PAUSE)


def _uiautomator_scroll_into_view(by: str, expression: str) -> Optional[str]:
    """把常见的「按文本 / id / desc 定位」xpath 翻译成 UiScrollable.scrollIntoView 表达式。

    scrollIntoView 由设备端 UiAutomator 自己滚动查找，比「Python 端循环 swipe + 每屏取层级」
    快一个数量级，也不会因滑动步长不准而错过目标。翻译不出来就返回 None，
    由调用方退回通用 swipe 循环。
    """
    if by != AppiumBy.XPATH:
        return None
    patterns = (
        (r"contains\(\s*@text\s*,\s*'([^']*)'\s*\)", "textContains"),
        (r'contains\(\s*@text\s*,\s*"([^"]*)"\s*\)', "textContains"),
        (r"@text\s*=\s*'([^']*)'", "text"),
        (r'@text\s*=\s*"([^"]*)"', "text"),
        (r"contains\(\s*@content-desc\s*,\s*'([^']*)'\s*\)", "descriptionContains"),
        (r'contains\(\s*@content-desc\s*,\s*"([^"]*)"\s*\)', "descriptionContains"),
        (r"@content-desc\s*=\s*'([^']*)'", "description"),
        (r'@content-desc\s*=\s*"([^"]*)"', "description"),
        (r"@resource-id\s*=\s*'([^']*)'", "resourceId"),
        (r'@resource-id\s*=\s*"([^"]*)"', "resourceId"),
    )
    for pattern, method in patterns:
        matched = re.search(pattern, expression)
        if matched:
            value = matched.group(1).replace('"', '\\"')
            return ('new UiScrollable(new UiSelector().scrollable(true).instance(0))'
                    f'.scrollIntoView(new UiSelector().{method}("{value}").instance(0))')
    return None


def scroll_to(driver: Any, locator: str, *, max_swipes: int = MAX_SCROLL_SWIPES,
              timeout: float = 3) -> Optional[Any]:
    """滚动查找元素：先试 UiScrollable.scrollIntoView，失败再退回逐屏 swipe。

    返回找到的 WebElement；滚完 max_swipes 次仍找不到返回 None（**不抛异常**），
    让调用方（工具层 / 测试脚本）自己决定是断言失败还是换定位表达式。

    **返回值必须检查**：它返回 None 时页面已经被翻了 max_swipes 屏，后面的 click /
    locate 会拿到 TimeoutException，失败信息完全指不到「目标条目没找到」这个真因。
    脚本里的正确写法（也是 codegen prompt 要求的写法）：
        item = scroll_to(driver, "//*[contains(@text,'省电与电池')]", max_swipes=15)
        assert item is not None, '未能找到「省电与电池」设置项'
        item.click()                      # 用返回的元素，别再 locate 一次

    方向是**固定向下**的（UiScrollable.scrollIntoView 这条快路本身也只会往列表末尾找），
    滚过头要退回去请直接调 swipe(driver, "up")。
    """
    by, expression = locator_of(locator)
    found = try_find(driver, locator)
    if found is not None:
        return found
    uiautomator = _uiautomator_scroll_into_view(by, expression)
    if uiautomator:
        try:
            driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, uiautomator)
        except WebDriverException:
            pass  # 当前界面没有可滚动容器，或目标不在其中 -> 退回通用 swipe
        found = try_find(driver, locator)
        if found is not None:
            return found
    for _ in range(max_swipes):
        try:
            swipe(driver, "down")
        except WebDriverException:
            return try_find(driver, locator)
        found = try_find(driver, locator)
        if found is not None:
            return found
    return None


# Appium/UiAutomator2 在属性缺失时返回的是**字符串 "null"**（不是 Python 的 None）：
# 典型场景是给没有 text 的容器 / 父节点（`//*[contains(@text,'xx')]//..`）取 text。
# 这个值会被 `(getter() or "")` 当成有效文本收下，进而污染断言 ——
# 实测出现过 `re.search(r"(\d+)", texts_of(...))` 拿到 'null'，断言失败信息里只有
# 「实际：'null'」，看日志根本不知道是定位选错了节点还是控件真没文本。
# 统一在这里过滤掉，取不到就继续走下一个 getter / 退回 content-desc。
_NULLISH_TEXTS = {"null", "none"}


def _clean_text(value: Any) -> str:
    """把 Appium 返回的属性值规整成「真的能用于断言的文本」：nullish -> 空串。"""
    text = str(value or "").strip()
    return "" if text.lower() in _NULLISH_TEXTS else text


def _element_text(element: Any) -> str:
    """单个控件的文本：text -> content-desc -> value 依次退回，全取不到就是空串。

    Android 上图标类控件（返回键、更多按钮）往往没有 text，只有 content-desc；
    容器类控件两个都可能没有。任一 getter 抛 WebDriverException 都只当作「这个属性没有」，
    不让取值本身把用例弄红。
    """
    for getter in (lambda: element.text,
                   lambda: element.get_attribute("text"),
                   lambda: element.get_attribute("content-desc"),
                   lambda: element.get_attribute("value")):
        try:
            text = _clean_text(getter())
        except WebDriverException:
            continue
        if text:
            return text
    return ""


def page_text(driver: Any) -> str:
    """当前界面的全部可见文本（text + content-desc），用于整页断言。"""
    try:
        return extract_texts(driver.page_source)
    except WebDriverException as exc:
        raise AssertionError(f"取界面文本失败：{exc}") from exc


def texts_of(driver: Any, locator: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """定位表达式匹配到的所有元素的聚合文本（text 为空时退回 content-desc / value）。

    整页文本可能混入状态栏、底部导航这些无关区域，直接 `in page_text` 容易误判通过；
    断言时优先把范围限定到列表 / 容器，再用这个函数取聚合文本。
    一个都没匹配到就抛 AssertionError（带上定位表达式）：静默返回空串会让
    `assert '电量' in texts_of(...)` 变成「取不到文本 == 断言失败」，
    失败信息里看不出是定位写错了还是界面上真没有。
    """
    elements = locate_all(driver, locator, timeout=timeout)
    if not elements:  # 理论上到不了这里（locate_all 等不到会抛 TimeoutException），留一道保险
        raise AssertionError(f"定位表达式没匹配到任何控件：{locator}（请核对界面上的真实文案 / 控件 id）")
    texts = [text for text in (_element_text(element) for element in elements) if text]
    if not texts:
        # 命中的全是容器 / 父节点（如 `//*[contains(@text,'xx')]//..`）：text、content-desc、
        # value 都没有。静默返回空串会让 `assert '电量' in texts_of(...)` 变成一句
        # 「实际：''」的断言失败，看不出是定位选错了节点。这里直接把话说清楚。
        raise AssertionError(
            f"定位表达式命中了 {len(elements)} 个控件，但它们都没有可读文本：{locator}。"
            f"通常是选到了容器 / 父节点（例如 `//*[contains(@text,'剩余电量')]//..`），"
            f"请直接定位到带文本的控件本身，或用 page_text(driver) 取整页文本再断言")
    return "\n".join(texts)



class AppiumWeb:
    """Appium 移动端自动化框架封装（类名保留历史命名，别名 AppAutoFramework 兼容旧调用）。

    与 web_framework.WebFramework 的分工完全一致：这里只管「怎么驱动设备」，
    工具层（appium_tools）负责「把异常降级成 Observation」，
    编排层（generate_autoapp）负责「先真实执行采集、再落 pytest 脚本」的两环流程。
    """

    def __init__(self):
        self._driver: Optional[webdriver.Remote] = None
        self._element: Optional[Any] = None
        self.app_package: str = ""
        self.app_activity: str = ""

    # -- driver 生命周期 -----------------------------------------------------
    def init(self, app_activity: Optional[str] = None,
             app_package: Optional[str] = None) -> str:
        """启动 Appium session 并把被测 app 拉到前台（对应 web 的 open）。

        幂等：已经起来就直接返回当前界面摘要，不重复建 session ——
        agent 有时会连调两次 init，重复建 session 会留下僵尸进程占着设备。
        """
        if self._driver is not None:
            return self.source()
        self.app_package = str(app_package or resolve_capabilities()["app_package"])
        self.app_activity = str(app_activity or resolve_capabilities()["app_activity"])
        try:
            self._driver = create_driver(app_activity=self.app_activity,
                                         app_package=self.app_package)
        except WebDriverException as exc:
            raise ValueError(
                f"启动 Appium session 失败：{exc}。"
                f"请确认 Appium server（{resolve_appium_server()}）已启动、"
                f"设备已连接（adb devices 能看到）、且 app_package={self.app_package} "
                f"app_activity={self.app_activity} 确实存在。"
            ) from exc
        problem = self._ensure_foreground()
        if problem:
            self.quit()  # 别把连错 app 的 session 留在设备上占着
            raise ValueError(problem)
        package, activity = self._activity_state()
        return f"当前应用：{package}，当前 activity：{activity}\n{self.source()}"

    def _ensure_foreground(self) -> str:
        """确认被测 app 真的在前台（实现见模块级 ensure_foreground，与 create_driver 共用）。"""
        driver = self._driver
        if driver is None:
            return "Appium session 未启动"
        return ensure_foreground(driver, self.app_package, self.app_activity)

    @property
    def driver(self) -> Any:
        """当前 Appium driver（未启动返回 None）。"""
        return self._driver

    @property
    def element(self) -> Optional[Any]:
        """最近一次 find / click / send_keys 命中的元素。"""
        return self._element

    def _ensure_driver(self) -> Any:
        """没启动就给出可操作的报错，而不是抛 `NoneType has no attribute find_element`。

        agent 偶尔会跳过 init 直接调 click/find，那种 AttributeError 进 Observation 之后
        模型完全无法判断该做什么，只会反复重试同一个调用直到烧光 max_iterations。
        """
        if self._driver is None:
            raise ValueError(
                "Appium driver 尚未启动：请先调用 init(app_activity=..., app_package=...) "
                "启动被测 app，再执行其它操作。"
            )
        return self._driver

    def quit(self) -> str:
        """关闭 session、释放设备（对应 web 的 quit）。

        必须显式提供：不 quit 的话 session 会一直占着设备，
        下一轮采集 / 生成的 pytest 脚本再建 session 时会因设备被占用而超时失败。
        """
        if self._driver is None:
            return "Appium session 未启动，无需关闭"
        try:
            self._driver.quit()
        except WebDriverException as exc:
            return f"关闭 Appium session 时出现异常（已忽略）：{exc}"
        finally:
            self._driver = None
            self._element = None
        return "Appium session 已关闭，设备已释放"

    def _activity_state(self) -> tuple[str, str]:
        """当前 (package, activity)（模块级同名函数的实例包装）。"""
        return _activity_state(self._driver)

    def _wait_activity_stable(self, max_wait: float = 4.0,
                              interval: float = ACTIVITY_POLL_INTERVAL) -> tuple[str, str]:
        """等界面切换落定（模块级同名函数的实例包装，先确认 session 已起）。"""
        return _wait_activity_stable(self._ensure_driver(), max_wait, interval)

    def current_activity(self) -> str:
        """当前 package / activity，用于确认「点了之后是否真的跳到了目标界面」。"""
        driver = self._ensure_driver()
        package, activity = self._activity_state()
        if not activity:
            return "读取当前 activity 失败（session 可能已断开）"
        return f"当前应用：{package}，当前 activity：{activity}"


    # -- 界面观察 ------------------------------------------------------------
    def source(self, locator: Optional[str] = None) -> str:
        """当前界面的**控件层级摘要**（一行一个可定位控件），不是原始 XML。

        这是整个 App 采集流程里最关键的一个方法：agent 靠它拿到真实存在的
        text / resource-id / content-desc / bounds，才能拼出可用的定位表达式。
        返回原始 XML 会瞬间顶穿模型输入长度（见模块 docstring）。

        传 locator 时先报告该表达式命中几个元素，再给摘要 ——
        让 agent 一次调用就同时确认「定位对不对」和「界面上还有什么」。
        """
        driver = self._ensure_driver()
        try:
            xml_text = driver.page_source
        except WebDriverException as exc:
            return f"获取界面层级失败：{exc}"
        summary = summarize_hierarchy(xml_text)
        if not summary:
            return "当前界面没有可定位的控件（可能是启动动画 / 黑屏），请 sleep 后重试"
        if not locator:
            return summary
        try:
            count = len(driver.find_elements(*locator_of(locator)))
        except WebDriverException as exc:
            head = f"定位表达式 {locator!r} 求值失败：{exc}\n"
        else:
            head = f"定位表达式 {locator!r} 命中 {count} 个元素。\n"
        return head + summary

    def page_text(self) -> str:
        """当前界面的全部可见文本（text + content-desc）。"""
        driver = self._ensure_driver()
        try:
            return extract_texts(driver.page_source)
        except WebDriverException as exc:
            return f"获取界面文本失败：{exc}"

    def texts_of(self, locator: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """定位表达式匹配到的所有元素的聚合文本。"""
        return texts_of(self._ensure_driver(), locator, timeout=timeout)

    def text_of(self, locator: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """读取单个控件的文本，对应测试步骤里的「获取 XXX」（如「获取 剩余电量」）。

        text 为空时依次退回 content-desc / value：Android 上图标类控件（返回键、更多按钮）
        往往没有 text，只有 content-desc。
        """
        element = locate(self._ensure_driver(), locator, timeout=timeout)
        return _element_text(element)


    # -- 界面操作 ------------------------------------------------------------
    def find(self, locator: str, timeout: Optional[float] = None) -> str:
        """按定位表达式查找元素。

        返回的是「命中几个 + 命中控件长什么样」，而**不是**整屏摘要：
        web 版 find 返回整页 source，搬到 App 上每次都是几千字符，几步就把上下文填满；
        find 的语义本来也只是「这个定位表达式对不对」。要看整屏控件请显式调 get_page_source。
        """
        driver = self._ensure_driver()
        wait = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        by, expression = locator_of(locator)
        try:
            elements = WebDriverWait(driver, wait).until(
                EC.presence_of_all_elements_located((by, expression)))
        except TimeoutException:
            self._element = None
            raise NoSuchElementException(
                f"{wait:g}s 内没找到匹配 {locator!r} 的控件。"
                "请先用 get_page_source 看当前界面到底有哪些 text / resource-id / content-desc，"
                f"并确认目标是否需要先 scroll_to_element 滚动出来。{_LOCATOR_HINT}"
            ) from None
        self._element = elements[0]
        shown = "\n".join(_describe(_attrs_of(item)) for item in elements[:5])
        more = f"\n…（另有 {len(elements) - 5} 个未列出）" if len(elements) > 5 else ""
        return f"定位表达式 {locator!r} 命中 {len(elements)} 个控件：\n{shown}{more}"

    def click(self, locator: Optional[str] = None,
              timeout: Optional[float] = None) -> str:
        """点击元素；不传 locator 时点上一次 find 命中的元素（兼容旧调用方式）。

        用 element_to_be_clickable 而不是直接 .click()：Android 列表项常常
        「已经渲染出来但还没绑定点击事件」，直接点会被系统丢掉，表现为「点了没反应」。
        """
        driver = self._ensure_driver()
        wait = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        if locator:
            by, expression = locator_of(locator)
            try:
                self._element = WebDriverWait(driver, wait).until(
                    EC.element_to_be_clickable((by, expression)))
            except TimeoutException:
                raise NoSuchElementException(
                    f"{wait:g}s 内没等到可点击的控件 {locator!r}（可能不可点或还没渲染完）。"
                    f"请用 get_page_source 确认该控件是否带 clickable 标记。{_LOCATOR_HINT}"
                ) from None
        elif self._element is None:
            raise ValueError("没有可点击的元素：请先调用 find，或直接给 click 传 locator")
        try:
            self._element.click()
        except WebDriverException as exc:
            raise ValueError(
                f"点击失败：{exc}。若报 'element not interactable' 说明该控件本身不可点，"
                "请用 get_page_source 找它**外层带 clickable 的父容器**再点。"
            ) from exc
        self._wait_activity_stable()
        return f"已点击 {locator or '上一次 find 命中的元素'}，当前界面控件摘要：\n{self.source()}"


    def send_keys(self, locator: Optional[str] = None, text: Optional[str] = None,
                  clear: bool = True, timeout: Optional[float] = None) -> str:
        """往输入框写内容。

        兼容两种签名：send_keys(locator, text) 与旧式 send_keys(text)
        （作用于上一次 find 命中的元素）。
        """
        driver = self._ensure_driver()
        if text is None and locator is not None:
            locator, text = None, locator
        wait = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        if locator:
            by, expression = locator_of(locator)
            try:
                self._element = WebDriverWait(driver, wait).until(
                    EC.element_to_be_clickable((by, expression)))
            except TimeoutException:
                raise NoSuchElementException(
                    f"{wait:g}s 内没等到可输入的控件 {locator!r}。"
                    f"请用 get_page_source 确认它是否是 EditText。{_LOCATOR_HINT}"
                ) from None
        elif self._element is None:
            raise ValueError("没有可输入的元素：请先调用 find，或直接给 send_keys 传 locator")
        if text is None:
            raise ValueError("send_keys 缺少要输入的内容")
        try:
            if clear:
                self._element.clear()
            self._element.send_keys(str(text))
        except WebDriverException as exc:
            raise ValueError(f"输入失败：{exc}") from exc
        return f"已向 {locator or '上一次 find 命中的元素'} 输入 {text!r}"

    def scroll_to_element(self, locator: str,
                          max_swipes: int = MAX_SCROLL_SWIPES) -> str:
        """滚动查找元素，对应测试步骤里的「滚动到页面 直至找到 X」。

        返回文案里带上命中控件的属性，让 agent 不必再调一次 get_page_source
        就能确认「找到的确实是目标」，省一轮往返。
        """
        driver = self._ensure_driver()
        found = scroll_to(driver, locator, max_swipes=max_swipes)
        if found is None:
            raise NoSuchElementException(
                f"向下滚动 {max_swipes} 屏仍未找到 {locator!r}。"
                "说明该文本在当前界面体系里不存在：请检查是否走错了入口，"
                "或用 get_page_source 看看当前界面上的真实文本再改定位表达式。"
            )
        self._element = found
        return f"已滚动到目标，命中控件：{_describe(_attrs_of(found))}"

    def back(self) -> str:
        """按系统返回键，对应测试步骤里的「返回上一级页面」。"""
        driver = self._ensure_driver()
        try:
            driver.back()
        except WebDriverException as exc:
            raise ValueError(f"返回上一级失败：{exc}") from exc
        self._wait_activity_stable()
        return f"已返回上一级，{self.current_activity()}"

    # -- 断言 ----------------------------------------------------------------
    def assert_contains(self, text: str, locator: Optional[str] = None,
                        timeout: float = DEFAULT_TIMEOUT) -> bool:
        """断言文本出现在界面里：传 locator 就限定在该元素范围内，否则查整个界面。

        返回 bool 而不是直接 assert：工具层要把「断言失败」变成一条可读的
        Observation 让 agent 自己重试，而不是抛 AssertionError 打断整条 chain。
        """
        if not str(text or "").strip():
            raise ValueError("assert_contains 的 text 不能为空")
        self._ensure_driver()
        scope = self.texts_of(locator, timeout=timeout) if locator else self.page_text()
        return str(text) in scope


def _attrs_of(element: Any) -> Mapping[str, str]:
    """把一个 WebElement 反查成「摘要用的属性字典」。

    Appium 的元素对象没有 .attrib，只能逐个 get_attribute；每个属性都是一次
    HTTP 往返，所以 find() 里最多只渲染前 5 个元素。
    """
    attrs: dict[str, str] = {}
    for key in ("class", "resource-id", "text", "content-desc", "bounds",
                "clickable", "scrollable", "checkable", "checked", "selected",
                "long-clickable", "enabled"):
        try:
            value = element.get_attribute(key)
        except WebDriverException:
            value = None
        if value is not None:
            attrs[key] = str(value)
    return attrs


# 兼容历史命名：早期 appium_tools / appium.md 笔记里用的是 AppAutoFramework
AppAutoFramework = AppiumWeb



if __name__ == '__main__':
    # 手工冒烟：真机 + Appium server 就绪后直接 `python src/app/app_framework.py`，
    # 用来确认「层级摘要 / 滚动查找 / 点击 / 返回」这套底层能力是通的，
    # 再往上跑 generate_autoapp.py 才有意义（底层不通时 agent 只会反复重试烧轮次）。
    framework = AppiumWeb()
    print(framework.init(app_activity=DEFAULT_APP_ACTIVITY, app_package=DEFAULT_APP_PACKAGE)[:800])
    print(framework.scroll_to_element("//*[contains(@text,'省电与电池')]"))
    print(framework.click("//*[contains(@text,'省电与电池')]")[:800])
    print(framework.current_activity())
    print(framework.back())
    print(framework.quit())

