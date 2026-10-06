import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from src.web.web_framework import resolve_chromedriver

@pytest.fixture
def driver():
    driver_path = resolve_chromedriver()
    if driver_path:
        driver = webdriver.Chrome(service=Service(driver_path))
    else:
        driver = webdriver.Chrome()
    driver.maximize_window()
    yield driver
    driver.quit()

def login(driver):
    # 打开登录页面
    driver.get("https://litemall.hogwarts.ceshiren.com/#/login?redirect=%2Fdashboard")

    # 输入用户名
    username_input = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='username']"))
    )
    username_input.clear()
    username_input.send_keys("hogwarts")

    # 输入密码
    password_input = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='password']"))
    )
    password_input.clear()
    password_input.send_keys("test12345")

    # 点击登录按钮
    login_button = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button.el-button--primary"))
    )
    login_button.click()

    # 等待页面跳转完成
    WebDriverWait(driver, 20).until(EC.url_contains("#/dashboard"))

    # 断言页面包含指定文本
    expected_texts = ["首页", "商场管理", "商品管理"]
    for text in expected_texts:
        assert text in driver.page_source


def test_login(driver):
    """pytest 入口：业务流程复用 login(driver)，便于其它用例脚本 import 复用（勿删）。"""
    login(driver)
