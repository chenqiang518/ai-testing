import pytest
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from src.web.web_framework import resolve_chromedriver, by_of


def login(driver):
    driver.get("https://litemall.hogwarts.ceshiren.com/#/login?redirect=%2Fdashboard")

    # 输入用户名
    username_input = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((by_of("input[name='username']"), "input[name='username']"))
    )
    username_input.clear()
    username_input.send_keys("hogwarts")

    # 输入密码
    password_input = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((by_of("input[name='password']"), "input[name='password']"))
    )
    password_input.clear()
    password_input.send_keys("test12345")

    # 点击登录按钮
    login_button = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((by_of("button.el-button--primary"), "button.el-button--primary"))
    )
    login_button.click()

    # 等待页面跳转到主页
    WebDriverWait(driver, 20).until(EC.url_contains("#/dashboard"))

    # 断言主页左侧导航栏包含"首页"、"商场管理"、"商品管理"
    menu_items = driver.find_elements(by_of("ul[role='menubar'].el-menu"), "ul[role='menubar'].el-menu")
    assert menu_items, '断言范围内一个元素都没匹配到：ul[role=menubar].el-menu'
    aggregated_menu_text = ''.join(item.text.replace("\n", "") for item in menu_items)
    assert '首页' in aggregated_menu_text, '缺少 首页'
    assert '商场管理' in aggregated_menu_text, '缺少 商场管理'
    assert '商品管理' in aggregated_menu_text, '缺少 商品管理'


@pytest.fixture
def driver():
    driver_path = resolve_chromedriver()
    driver = webdriver.Chrome(service=Service(driver_path)) if driver_path else webdriver.Chrome()
    driver.maximize_window()
    yield driver
    driver.quit()


def test_login(driver):
    login(driver)
