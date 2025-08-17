import time

from langchain_core.tools import tool

from src.app.app_framework import AppAutoFramework

app = AppAutoFramework()


@tool
def init(app_activity, app_package):
    """
    打开app的安装包，并返回app的resource
    """
    return app.init(app_activity, app_package)


@tool
def find(xpath: str):
    """通过xpath定位元素"""
    return app.find(xpath)


@tool
def scroll_to_element(locator: str):
    """滚动到 xpath 指定的元素"""
    return app.scroll_to_element(locator=locator)


@tool
def click(xpath: str = None):
    """以xpath的方式定位网页元素后点击"""
    app.find(xpath)
    return app.click()


@tool
def send_keys(xpath, text):
    """定位到xpath指定的元素，并输入text"""
    app.find(xpath)
    return app.send_keys(text)


@tool
def sleep(seconds: int):
    """等待指定的秒数"""
    time.sleep(seconds)


@tool
def back():
    """
    返回上一级界面
    :return:
    """
    app.back()


tools = [init, scroll_to_element, find, click, send_keys, sleep, back]


if __name__ == "__main__":
    init.invoke({"app_activity": ".Settings", "app_package": "com.android.settings"})
    scroll_to_element.invoke({"locator": "//*[contains(@text, '省电与电池')]"})