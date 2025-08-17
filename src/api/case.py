import requests
import pytest

# 测试数据
test_data = [
    {'ip': '182.92.156.22', 'url': 'https://httpbin.ceshiren.com/ip', 'method': 'GET'}
]


# 正常情况下的GET请求测试
def test_normal_get_request():
    url = "https://httpbin.ceshiren.com/ip"
    response = requests.get(url)

    assert response.status_code == 200, f"预期状态码200，实际为{response.status_code}"

    json_response = response.json()
    assert 'origin' in json_response, "响应中未包含'origin'字段"
    print("请求成功，IP为：", json_response['origin'])


# 非法请求方法测试（如POST）
def test_invalid_method_post():
    url = "https://httpbin.ceshiren.com/ip"
    response = requests.post(url)  # 发送POST请求

    assert response.status_code == 405, f"预期状态码405，实际为{response.status_code}"
    print("非法方法测试通过，状态码为：", response.status_code)


# 无效路径测试（404）
def test_invalid_path():
    url = "https://httpbin.ceshiren.com/nonexistent-path"
    response = requests.get(url)

    assert response.status_code == 404, f"预期状态码404，实际为{response.status_code}"
    print("无效路径测试通过，状态码为：", response.status_code)