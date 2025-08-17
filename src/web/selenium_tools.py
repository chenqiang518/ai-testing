from datetime import time

from langchain_core.tools import tool

from src.web.web_framework import WebAutoFramework

web = WebAutoFramework()
@tool
def open(url: str):
    """
    使用浏览器打开特定的url，并返回网页内容
    """
    r = web.open(url)
    return r

@tool
def find(css: str):
    """定位网页元素"""
    return web.find(css)

@tool
def click(css: str = None):
    """以css的方式定位网页元素后点击"""
    web.find(css)
    return web.click()

@tool
def send_keys(css, text):
    """定位到css指定的元素，并输入text"""
    web.find(css)
    return web.send_keys(text)

@tool
def sleep(seconds: int):
    """等待指定的秒数"""
    time.sleep(seconds)


@tool
def quit():
    """退出浏览器"""
    web.quit()

@tool
def get_current_url():
    """获取当前的url"""
    return web.get_current_url()

tools = [open, quit, get_current_url, find, click, send_keys]
