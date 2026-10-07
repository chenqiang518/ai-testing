import pytest
from src.app.app_framework import create_driver, locate, scroll_to, page_text

@pytest.fixture
def driver():
    driver = create_driver(app_activity='.Settings', app_package='com.android.settings')
    yield driver
    driver.quit()

def check_system_personalization_theme(driver):
    # 点击 '系统个性化'
    system_personalization = scroll_to(driver, "//android.widget.TextView[contains(@text,'系统个性化')]", max_swipes=10)
    assert system_personalization is not None, '未能找到「系统个性化」设置项'
    system_personalization.click()

    # 点击 '主题'
    theme = scroll_to(driver, "//android.widget.TextView[contains(@text,'主题')]", max_swipes=10)
    assert theme is not None, '未能找到「主题」设置项'
    theme.click()

    # 断言页面包含 '我的主题'
    text = page_text(driver)
    assert '我的主题' in text, f'界面未包含「我的主题」，实际片段：{text[:300]}'

    # 返回上一级
    driver.back()

    # 再次返回上一级
    driver.back()

def test_system_personalization_theme(driver):
    check_system_personalization_theme(driver)
