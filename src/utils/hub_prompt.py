#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LangChain Hub prompt 拉取工具。

背景（LangChain 1.x 迁移）：
    1. `from langchain import hub` 在 LangChain 1.x 中已被移除，
       经典实现搬到了 `langchain_classic.hub`，且自 1.0.6 起标记为废弃（2.0.0 移除）。
    2. 新版 langsmith 出于安全考虑，默认禁止按 `owner/name` 拉取公共 prompt，
       `langchain_classic.hub.pull()` 内部没有透传该确认参数，因此调用会直接抛
       ValueError: Pulling a public prompt by owner/name is disabled by default ...

    官方建议改用 LangSmith SDK，这里统一封装，避免各文件重复处理。
"""

from typing import Any, Optional

from langsmith import Client


def pull_prompt(prompt_name: str, include_model: Optional[bool] = False) -> Any:
    """从 LangChain Hub 拉取 prompt 并返回 LangChain 对象（如 ChatPromptTemplate）。

    Args:
        prompt_name: prompt 标识，形如 ``"hwchase17/structured-chat-agent"``，
            也可以带 commit：``"owner/name:commit_hash"``（推荐固定 commit 以保证可复现）。
        include_model: 是否同时反序列化 prompt 中声明的模型配置，默认 False。

    Returns:
        反序列化后的 LangChain 对象。

    Note:
        拉取公共 prompt 等同于加载「不受信任的可执行配置」——manifest 中可以声明
        自定义 base_url、headers、模型名等构造参数。``dangerously_pull_public_prompt=True``
        表示已确认信任该 prompt 的内容。
    """
    return Client().pull_prompt(
        prompt_name,
        include_model=include_model,
        dangerously_pull_public_prompt=True,
    )
