# 1. 正常情况下的请求
- 前提条件:
    1. 
- 测试步骤:
    1. 打开  app activity ".Settings" , app package "com.android.settings"
    2. 滚动到页面 直至找到 省电与电池
    3. 点击 省电与电池
    4. 获取 剩余电量
    5. 获取到剩余电量后，断言电量大于0 
    6. 返回上一级页面

# 2. 非法请求方法
- 前提条件:
    1. 
- 测试步骤:
    1. 打开  app activity ".Settings" , app package "com.android.settings"
    2. 滚动到页面 直至找到 省电与电池
    3. 点击 省电与电池
    4. 获取 剩余电量
    5. 获取到剩余电量后，断言电量大于0 
    6. 返回上一级页面