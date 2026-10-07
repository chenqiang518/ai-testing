# 1. 检查电源
- 前提条件:
    1. 打开  app activity ".Settings" , 
    2. app package "com.android.settings"
- 测试步骤:
    1. 滚动到页面 直至找到 省电与电池
    2. 点击 省电与电池
    3. 获取 剩余电量
    4. 获取到剩余电量后，断言电量大于0 
    5. 返回上一级页面

# 2. 更多链接
## 2.1 打印
- 前提条件:
    1. 打开  app activity ".Settings" , 
    2. app package "com.android.settings"
- 测试步骤: 
    1. 滚动到页面 直至找到 更多链接
    2. 点击 更多链接 
    3. 滚动到页面 直至找到 打印
    4. 点击 打印
    5. 断言 页面包含系统打印服务帮助
    6. 返回上一级页面
    7. 返回上一级页面