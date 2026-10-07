import pytest
from src.app.app_framework import create_driver, locate, texts_of, scroll_to

@pytest.fixture
def driver():
    driver = create_driver(app_activity='.Settings', app_package='com.android.settings')
    yield driver
    driver.quit()

# 检查电源功能
def check_power(driver):
    # 滚动到省电与电池设置项
    item = scroll_to(driver, "//*[contains(@text,'省电与电池')]", max_swipes=10)
    assert item is not None, '未能找到「省电与电池」设置项'
    item.click()

    # 获取剩余电量文本
    battery_text = texts_of(driver, "//*[contains(@text,'剩余电量')]")
    assert '剩余电量' in battery_text, f'界面未包含「剩余电量」，实际片段：{battery_text[:300]}'

    # 返回上一级
    driver.back()

def test_check_power(driver):
    check_power(driver)
