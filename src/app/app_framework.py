import time
from time import sleep
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common import NoSuchElementException


class AppAutoFramework:
    def __init__(self):
        self.driver = None
        self.element = None

    def init(self, app_activity, app_package):
        if not self.driver:
            # 设置 capability
            caps = {
                # 设置 app 安装的平台（Android、iOS）
                "platformName": "android",
                # 设置 appium 驱动
                "appium:automationName": "uiautomator2",
                # 设置设备名称
                "appium:deviceName": "192.168.3.6:5555",
                # 设置为超时时间为 60 秒
                "adbExecTimeout": 60000,

                "appium:noReset": True,
                # 设置以下两个参数来控制启动app和关闭掉app
                "appium:forceAppLaunch" : True,
                "appium:shouldTerminateApp" : True,
                # 设置 app 的包名
                "appium:appPackage": app_package,
                # 设置 app 启动页
                "appium:appActivity": app_activity
            }
            # 初始化 driver
            self.driver = webdriver.Remote(
                command_executor="http://127.0.0.1:4723",
                options=UiAutomator2Options().load_capabilities(caps)
            )
            self.driver.implicitly_wait(5)
        return self.source()

    def source(self):
        return self.driver.page_source

    def find(self, locator):
        print(f"find xpath = {locator}")
        element = self.driver.find_element(by=AppiumBy.XPATH, value=locator)
        self.element = element
        return self.source()

    def scroll_to_element(self, locator, max_swipes=10):
        window_size = self.driver.get_window_size()
        width = window_size["width"]
        height = window_size["height"]

        for _ in range(max_swipes):
            try:
                # 查找包含指定文本的元素
                print(f"查找包含指定文本的元素 xpath：{locator}")
                self.find(locator)

                break
            except NoSuchElementException:
                pass  # 没找到则继续滑动

            # 执行向下滑动操作
            self.driver.execute_script(
                "mobile: scrollGesture",
                {
                    "left": width // 2,
                    "top": height // 4,
                    "width": width // 2,
                    "height": height // 2,
                    "direction": "down",
                    "percent": 0.85
                }
            )
            time.sleep(1)  # 等待页面加载

        return self.source()

    def click(self):
        self.element.click()
        sleep(1)
        return self.source()

    def send_keys(self, text):
        self.element.clear()
        self.element.send_keys(text)
        return self.source()

    def back(self):
        self.driver.back()
        return self.source()


if __name__ == "__main__":
    app = AppAutoFramework()
    app.init(".Settings", "com.android.settings")
    xpath = "//*[contains(@text, '省电与电池')]"
    app.scroll_to_element(locator=xpath)