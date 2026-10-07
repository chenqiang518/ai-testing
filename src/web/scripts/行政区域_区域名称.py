import pytest
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from src.web.scripts.首页登录 import login  # 复用首页登录.py的login函数
from src.web.web_framework import resolve_chromedriver, by_of


# 定义fixture创建WebDriver实例
@pytest.fixture
def driver():
    driver_path = resolve_chromedriver()
    driver = webdriver.Chrome(service=Service(driver_path)) if driver_path else webdriver.Chrome()
    driver.maximize_window()
    yield driver
    driver.quit()


# 行政区域_区域名称 测试函数
def administrative_area_region_name(driver):
    # 使用复用的login函数进行登录
    login(driver)

    # 展开（商场管理）
    WebDriverWait(driver, 20).until(
        EC.element_to_be_clickable((By.XPATH, "//li[.//span[contains(., '商场管理')]]"))).click()

    # 点击（行政区域）
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a[href='#/mall/region']"))).click()

    # 点击（北京市）左边的">展开表单
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable(
        (By.XPATH, "//tr[.//td[contains(., '北京市')]]//div[contains(@class, 'el-table__expand-icon')]"))).click()

    # 点击（市辖区）左边的">展开表单
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable(
        (By.XPATH, "//tr[.//td[contains(., '市辖区')]]//div[contains(@class, 'el-table__expand-icon')]"))).click()

    # 断言 区域名称包含 东城区
    assert '东城区' in driver.page_source, '页面未找到含有东城区的文本'

    # 断言当区域名称=北京市时, 区域类型=省, 区域编码=110000
    beijing_row_locator = "//tr[.//td[contains(., '北京市')]]"
    beijing_rows = driver.find_elements(by_of(beijing_row_locator), beijing_row_locator)
    aggregated_beijing = ''.join(row.text.replace("\n", "") for row in beijing_rows)
    assert '省' in aggregated_beijing, '缺少 省'
    assert '110000' in aggregated_beijing, '缺少 110000'

    # 断言当区域名称=市辖区时, 区域类型=市, 区域编码=110100
    shixiaqu_row_locator = "//tr[.//td[contains(., '市辖区')]]"
    shixiaqu_rows = driver.find_elements(by_of(shixiaqu_row_locator), shixiaqu_row_locator)
    aggregated_shixiaqu = ''.join(row.text.replace("\n", "") for row in shixiaqu_rows)
    assert '市' in aggregated_shixiaqu, '缺少 市'
    assert '110100' in aggregated_shixiaqu, '缺少 110100'

    # 断言当区域名称=东城区时, 区域类型=区, 区域编码=110101
    dongcheng_row_locator = "//tr[.//td[contains(., '东城区')]]"
    dongcheng_rows = driver.find_elements(by_of(dongcheng_row_locator), dongcheng_row_locator)
    aggregated_dongcheng = ''.join(row.text.replace("\n", "") for row in dongcheng_rows)
    assert '区' in aggregated_dongcheng, '缺少 区'
    assert '110101' in aggregated_dongcheng, '缺少 110101'


def test_administrative_area_region_name(driver):
    """pytest 入口：业务流程复用 administrative_area_region_name3(driver)，便于其它用例脚本 import 复用（勿删）。"""
    administrative_area_region_name(driver)
