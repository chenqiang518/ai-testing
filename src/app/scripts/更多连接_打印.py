import pytest
from src.app.app_framework import (
    create_driver, locate, locate_all, scroll_to, texts_of, page_text,
)


@pytest.fixture
def driver():
    driver = create_driver(app_activity=".Settings", app_package="com.android.settings")
    yield driver
    driver.quit()

def more_connections_print(driver):
    # 点击'更多连接'
    more_connections = locate(driver, "//*[contains(@text,'更多连接')]", timeout=10)
    assert more_connections is not None, '未能找到「更多连接」设置项'
    more_connections.click()
    
    # 点击'打印'
    print_option = locate(driver, "//*[contains(@text,'打印')]", timeout=10)
    assert print_option is not None, '未能找到「打印」设置项'
    print_option.click()
    
    # 显式等待「系统打印服务」控件出现（locate 内部即 WebDriverWait + presence_of_element_located）
    locate(driver, "//*[contains(@text,'系统打印服务')]", timeout=10)
    
    # 断言页面中包含'系统打印服务'
    text = page_text(driver)
    assert '系统打印服务' in text, f'界面未包含「系统打印服务」，实际片段：{text[:300]}'
    
    # 返回上一级
    driver.back()
    
    # 再次返回上一级
    driver.back()

def test_more_connections_print(driver):
    more_connections_print(driver)
