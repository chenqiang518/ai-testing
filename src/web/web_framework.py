import os
import re
import shutil
from pathlib import Path
from time import sleep

from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# 显式等待的默认超时时间（秒）
DEFAULT_TIMEOUT = 10
# 返回给 LLM 的页面摘要最大长度，避免 prompt 过长
MAX_SOURCE_LENGTH = 6000
# 返回给 LLM 的页面可见文本最大长度
MAX_TEXT_LENGTH = 2000
# 摘要中最多保留的元素个数
MAX_ELEMENT_COUNT = 150

# 抽取页面上「可交互 / 可用于断言」的元素，压缩成 `<tag 关键属性>可见文本</tag>`。
#
# 为什么不直接返回整页 HTML：
#   1. 整页 HTML 动辄几万 token，会挤爆上下文；
#   2. agent 需要的是「有哪些元素、类名/文本是什么」，据此拼 css 选择器；
#   3. 只抓 button/input（历史实现）会让 agent 看不到 SPA 的导航菜单，
#      只能凭经验臆测选择器，进而抛 NoSuchElementException。
_SOURCE_JS = r"""
var KEEP_ATTRS = ['id', 'class', 'name', 'type', 'href', 'placeholder',
                  'role', 'title', 'value', 'index', 'aria-label'];
var SELECTORS = ['button', 'input', 'textarea', 'select', 'a', 'li', 'label',
                 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', '[role]'];
var MAX_COUNT = arguments[0];
var seen = new Set();
var out = [];

function describe(el) {
    var tag = el.tagName.toLowerCase();
    var attrs = '';
    for (var i = 0; i < el.attributes.length; i++) {
        var attr = el.attributes[i];
        if (KEEP_ATTRS.indexOf(attr.name) < 0) { continue; }
        var value = (attr.value || '').replace(/\s+/g, ' ').trim();
        if (value.length > 120) { value = value.slice(0, 120); }
        attrs += ' ' + attr.name + '="' + value + '"';
    }
    var text = (el.innerText || el.textContent || el.value || '');
    text = text.replace(/\s+/g, ' ').trim();
    if (text.length > 100) { text = text.slice(0, 100); }
    return '<' + tag + attrs + '>' + text + '</' + tag + '>';
}

SELECTORS.forEach(function (selector) {
    if (out.length >= MAX_COUNT) { return; }
    document.querySelectorAll(selector).forEach(function (el) {
        if (out.length >= MAX_COUNT || seen.has(el)) { return; }
        seen.add(el);
        out.push(describe(el));
    });
});
return out.join('\n');
"""

_PAGE_TEXT_JS = "return document.body ? document.body.innerText : '';"

# DOM 稳定快照：url + 元素总数 + 可见文本长度，连续两次一致即认为渲染结束。
# 用于解决「点击登录后立刻取内容，拿到的还是登录页（按钮 is-loading）」的时序问题。
_SNAPSHOT_JS = (
    "return (location.href || '') + '|' + document.querySelectorAll('*').length + '|'"
    " + (document.body ? document.body.innerText.length : 0);"
)


def resolve_chromedriver() -> str | None:
    """定位本机可用的 chromedriver：优先 PATH，其次常见安装位置。

    找不到时返回 None，交给 Selenium Manager 联网解析。
    """
    driver_path = shutil.which("chromedriver")
    if driver_path:
        return driver_path
    candidates = (
        Path.home() / "webdriver" / "chromedriver",
        Path("/usr/local/bin/chromedriver"),
        Path("/opt/homebrew/bin/chromedriver"),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class WebAutoFramework:
    """Selenium Web 自动化基础框架。

    设计要点：
        1. **页面摘要**而不是整页 HTML：LLM 需要依据「上一步返回的内容」决定下一步
           定位器，摘要只保留可交互/可断言元素的关键属性与可见文本；
        2. **显式等待**：SPA（Vue + element-ui）登录后路由与菜单是异步渲染的，
           click 之后立即取 HTML 很容易拿到空串（历史问题：click 返回 ""，
           agent 只能靠猜选择器），这里统一 WebDriverWait + 稳定重试；
        3. 本层**不吞异常**，由上层 selenium_tools 统一降级成字符串 Observation，
           避免工具异常直接终止 AgentExecutor。
    """

    def __init__(self, headless: bool | None = None):
        self.driver = None
        self.element = None
        # 默认有头模式（与历史行为一致）；CI/无桌面环境可用 WEB_HEADLESS=1 打开无头模式
        if headless is None:
            headless = os.getenv("WEB_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}
        self.headless = headless

    def init(self):
        """惰性初始化 driver（已初始化则直接复用）。"""
        if not self.driver:
            # 默认的 webdriver.Chrome() 会交给 Selenium Manager 联网解析 driver，
            # 当其下载源不可达时会长时间阻塞（表现为 open 工具一直不返回）。
            # 这里优先复用本机已安装的 chromedriver，找不到再回退给 Selenium Manager。
            driver_path = resolve_chromedriver()
            service = Service(executable_path=driver_path) if driver_path else None
            options = webdriver.ChromeOptions()
            options.add_argument("--window-size=1440,900")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            if self.headless:
                options.add_argument("--headless=new")
            self.driver = webdriver.Chrome(service=service, options=options)
            # 隐式等待与显式等待混用会让 WebDriverWait 的超时行为不可预期，
            # 统一只用显式等待。
            self.driver.implicitly_wait(0)

    def _ensure_driver(self):
        if not self.driver:
            raise RuntimeError("浏览器尚未启动，请先调用 open 工具打开页面")

    def _ensure_element(self):
        self._ensure_driver()
        if not self.element:
            raise RuntimeError("尚未定位到任何元素，请先调用 find/click/send_keys 并传入 css 选择器")

    def open(self, url):
        """打开 url，等待首屏渲染完成后返回页面元素摘要。"""
        self.init()
        self.driver.get(url)
        # stable_source 内部会等待 readyState + DOM 稳定，SPA 首屏（Vue 挂载）同样适用
        return self.stable_source()

    def source(self):
        """返回当前页面「可交互 / 可断言」元素的摘要（供 LLM 构造 css 选择器）。"""
        self._ensure_driver()
        content = self.driver.execute_script(_SOURCE_JS, MAX_ELEMENT_COUNT) or ""
        content = re.sub(r"\n{3,}", "\n\n", content).strip()
        if len(content) > MAX_SOURCE_LENGTH:
            content = content[:MAX_SOURCE_LENGTH] + "\n...(内容过长已截断)"
        return content

    def _wait_page_stable(self, checks: int = 6, interval: float = 0.4):
        """等待页面渲染稳定：document.readyState == complete 且 DOM 快照连续一致。

        这是修复「agent 重复点击登录 / 断言到旧页面文本」的关键：Vue + element-ui 的
        路由跳转与菜单渲染是异步的，click 返回的瞬间页面往往还是旧的。
        """
        self._ensure_driver()
        try:
            WebDriverWait(self.driver, DEFAULT_TIMEOUT).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )
        except WebDriverException:
            # readyState 判定超时不代表页面不可用，继续走 DOM 快照判定
            pass
        last = None
        for _ in range(max(1, checks)):
            try:
                snapshot = self.driver.execute_script(_SNAPSHOT_JS)
            except WebDriverException:
                return
            if snapshot == last:
                return
            last = snapshot
            sleep(interval)

    def stable_source(self, retries: int = 5, interval: float = 0.6) -> str:
        """页面跳转/异步渲染后获取元素摘要：先等 DOM 稳定，拿到空内容时再重试。

        兜底：确实没有可交互元素时返回可见文本，至少让 agent 知道当前在哪一页。
        """
        self._wait_page_stable()
        for _ in range(max(1, retries)):
            content = self.source()
            if content:
                return content
            sleep(interval)
        return self.page_text() or "(当前页面没有可交互元素)"

    def page_text(self) -> str:
        """返回当前页面的可见文本（截断），用于确认跳转结果或做文本断言。"""
        self._ensure_driver()
        text = self.driver.execute_script(_PAGE_TEXT_JS) or ""
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH] + "\n...(内容过长已截断)"
        return text

    def click(self):
        """点击当前元素，等待页面稳定后返回新的元素摘要。"""
        self._ensure_element()
        try:
            self.element.click()
        except ElementClickInterceptedException:
            # 被遮罩/固定头挡住时，回退到 JS 点击
            self.driver.execute_script("arguments[0].click();", self.element)
        except WebDriverException:
            self.driver.execute_script("arguments[0].click();", self.element)
        return self.stable_source()

    def send_keys(self, text):
        """向当前元素输入文本（输入前先清空），返回输入后的元素摘要。"""
        self._ensure_element()
        self._clear_element()
        self.element.send_keys(text)
        return self.stable_source(retries=2)

    def _clear_element(self):
        """清空当前输入框，clear() 失败时回退到 JS 清值。

        输入前必须清空：残留的默认值/上次输入会与新文本拼成「旧值+新值」。
        clear() 在元素不可交互（被遮挡、readonly 瞬时态、动画未结束）时会抛
        WebDriverException，此时用 JS 兜底；element-plus / Vue 的输入框由 v-model
        接管，直接改 value 后必须派发 input 事件，否则视图与数据模型不同步
        （看起来清空了，提交时仍是旧值）。
        """
        try:
            self.element.clear()
        except WebDriverException:
            self.driver.execute_script(
                "arguments[0].value = '';"
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                self.element)

    def find(self, locator, timeout: int = DEFAULT_TIMEOUT):
        """以 css 选择器定位元素（显式等待），返回当前页面元素摘要。

        定位不到时抛出 TimeoutException（携带可读信息），由上层转成 Observation，
        让 agent 依据摘要重新选择选择器，而不是直接终止整个 chain。
        """
        self._ensure_driver()
        print(f"find css = {locator}")
        element = WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, locator)),
            message=f"页面上找不到 css 选择器: {locator}",
        )
        self.element = element
        return self.source()

    def _texts_of(self, locator: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[str, int]:
        """返回 locator 匹配到的**所有**元素的文本聚合结果与元素个数。

        为什么不用 find_element（单个）：像 `li[role='menuitem']` 这种导航项选择器会
        匹配多个元素，只取第一个就只能断言到「首页」，导致 agent 反复试错。
        """
        self._ensure_driver()
        elements = WebDriverWait(self.driver, timeout).until(
            lambda driver: driver.find_elements(By.CSS_SELECTOR, locator) or None,
            message=f"页面上找不到 css 选择器: {locator}",
        )
        self.element = elements[0]
        return "\n".join((element.text or "") for element in elements), len(elements)

    def assert_contains(self, text: str, locator: str | None = None,
                        timeout: int = DEFAULT_TIMEOUT) -> str:
        """断言页面（或 locator 指定元素）的可见文本包含期望文本。

        Args:
            text: 期望文本，支持用「、」/「,」/「|」分隔多个，全部包含才算通过。
            locator: 可选，限定断言范围的 css 选择器（如左侧导航栏容器）；
                     匹配到多个元素时会聚合它们的文本一起断言。
            timeout: 指定 locator 时的等待超时时间。

        Returns:
            可读的断言结论字符串（不抛异常，便于 agent 继续后续步骤）。
        """
        if locator:
            actual, count = self._texts_of(locator, timeout=timeout)
            scope = f"元素[{locator}]（匹配 {count} 个）"
        else:
            # 断言前先等渲染稳定，否则会读到跳转前的旧页面文本（历史坑）
            self._wait_page_stable()
            actual = self.page_text()
            scope = "当前页面"
        expected = [item.strip() for item in re.split(r"[,，、|;；\n]+", text or "") if item.strip()]
        if not expected:
            return "断言失败：未提供期望文本"
        missing = [item for item in expected if item not in actual]
        if missing:
            snippet = re.sub(r"\s+", " ", actual).strip()[:500]
            return f"断言失败：{scope} 未包含 {missing}，实际文本片段：{snippet}"
        return f"断言通过：{scope} 包含 {expected}"

    def quit(self) -> str:
        """关闭浏览器（幂等：未启动或已关闭时不报错）。"""
        if self.driver:
            try:
                self.driver.quit()
            finally:
                self.driver = None
                self.element = None
            return "浏览器已退出"
        return "浏览器已退出（此前未启动或已关闭）"

    def get_current_url(self) -> str:
        self._ensure_driver()
        current_url = self.driver.current_url
        print(f"当前的url为{current_url}")
        return current_url

