from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium import webdriver  # 添加缺少的导入
from src.web.web_framework import resolve_chromedriver, by_of
from src.web.scripts.首页登录 import login
import pytest


def region_name_verification(driver):
    # 展开「商场管理」
    mall_management_locator = "//li[contains(., '商场管理')]"
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((by_of(mall_management_locator), mall_management_locator))).click()

    # 点击「行政区域」
    administrative_regions_locator = "a[href='#/mall/region']"
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((by_of(administrative_regions_locator), administrative_regions_locator))).click()

    # 点击「北京市」左边的">"展开表单
    beijing_expand_locator = "//tr[.//td[contains(., '北京市')]]//div[contains(@class, 'el-table__expand-icon')]"
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((by_of(beijing_expand_locator), beijing_expand_locator))).click()

    # 点击「市辖区」左边的">"展开表单
    shixiaqu_expand_locator = "//tr[.//td[contains(., '市辖区')]]//div[contains(@class, 'el-table__expand-icon')]"
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((by_of(shixiaqu_expand_locator), shixiaqu_expand_locator))).click()

    # 断言 区域名称包含 东城区
    dongcheng_locator = "//tr[.//td[contains(., '东城区')]]"
    assert WebDriverWait(driver, 10).until(EC.presence_of_element_located((by_of(dongcheng_locator), dongcheng_locator))), f'未能找到东城区'

    # 断言当区域名称=北京市时, 区域类型=省，区域编码=110000
    beijing_row_locator = "//tr[.//td[contains(., '北京市')]]"
    beijing_row_element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((by_of(beijing_row_locator), beijing_row_locator)))
    beijing_row_text = beijing_row_element.text.replace("\n", "")
    assert '省' in beijing_row_text, f'缺少 省'
    assert '110000' in beijing_row_text, f'缺少 110000'

    # 断言当区域名称=市辖区时, 区域类型=市，区域编码=110100
    shixiaqu_row_locator = "//tr[.//td[contains(., '市辖区')]]"
    shixiaqu_row_element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((by_of(shixiaqu_row_locator), shixiaqu_row_locator)))
    shixiaqu_row_text = shixiaqu_row_element.text.replace("\n", "")
    assert '市' in shixiaqu_row_text, f'缺少 市'
    assert '110100' in shixiaqu_row_text, f'缺少 110100'

    # 断言当区域名称=东城区时, 区域类型=区，区域编码=110101
    dongcheng_row_locator = "//tr[.//td[contains(., '东城区')]]"
    dongcheng_row_element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((by_of(dongcheng_row_locator), dongcheng_row_locator)))
    dongcheng_row_text = dongcheng_row_element.text.replace("\n", "")
    assert '区' in dongcheng_row_text, f'缺少 区'
    assert '110101' in dongcheng_row_text, f'缺少 110101'


@pytest.fixture(scope="function")
def driver():
    driver_path = resolve_chromedriver()
    driver = webdriver.Chrome(service=Service(driver_path)) if driver_path else webdriver.Chrome()
    driver.maximize_window()
    yield driver
    driver.quit()


def region_name(driver):
    login(driver)
    region_name_verification(driver)


def test_region_name(driver):
    """pytest 入口：业务流程复用 region_name(driver)，便于其它用例脚本 import 复用（勿删）。"""
    region_name(driver)
