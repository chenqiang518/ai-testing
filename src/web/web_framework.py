from time import sleep

from selenium import webdriver
from selenium.webdriver.common.by import By


class WebAutoFramework:

    def __init__(self):
        self.driver = None
        self.element = None

    def init(self):
        if not self.driver:
            self.driver = webdriver.Chrome()
            self.driver.implicitly_wait(5)

    def open(self, url):
        self.init()

        self.driver.get(url)
        return self.source()

    def source(self):
        return self.driver.execute_script(
            """
            var content="";
            document.querySelectorAll('button').forEach(x=> content+=x.outerHTML);
            document.querySelectorAll('input').forEach(x=> content+=x.outerHTML);
            //document.querySelectorAll('table').forEach(x=> content+=x.outerHTML);
            return content;
            """
        )

    def click(self):
        """
        点击当前的元素
        :return:
        """
        self.element.click()
        sleep(1)
        return self.source()

    def send_keys(self, text):
        self.element.clear()
        self.element.send_keys(text)
        return self.source()

    def find(self, locator):
        print(f"find css = {locator}")
        element = self.driver.find_element(by=By.CSS_SELECTOR, value=locator)
        self.element = element
        return self.source()

    def quit(self):
        self.driver.quit()

    def get_current_url(self):
        print(f"当前的url为{self.driver.current_url}")
        return self.driver.current_url
