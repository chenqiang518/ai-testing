#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复版的 ConsoleCallbackHandler。

背景（langchain-core 1.6.x 的已知缺陷）：
    `langchain_core.tracers.stdout.FunctionCallbackHandler._on_tool_start` 里写死了::

        f'"{run.inputs["input"].strip()}"'

    而 `langchain_core.tools.base.BaseTool.run` 上报的是
    `inputs=filtered_tool_input`：当工具入参是 **dict**（structured chat agent 的
    `action_input` 就是 dict，例如 `{"url": "https://..."}`）时，`run.inputs`
    就是这个 dict 本身，压根没有 `"input"` 键，于是抛 `KeyError('input')`。

    该异常会被 CallbackManager 吞掉，只在控制台打印一行::

        Error in ConsoleCallbackHandler.on_tool_start callback: KeyError('input')

    日志不会中断，但 `[tool/start]` 这一步的输入信息全部丢失，debug 体验很差。

修复方式：
    继承 `ConsoleCallbackHandler` 并重写 `_on_tool_start` / `_on_tool_end`，
    对入参/出参做兼容处理（str 与 dict 都能打印）。

    注意：`langchain_core.callbacks.manager._configure` 中的判断是::

        if debug and not any(
            isinstance(handler, ConsoleCallbackHandler)
            for handler in callback_manager.handlers
        ):
            callback_manager.add_handler(ConsoleCallbackHandler())

    所以只要 **提前把本子类的实例放进 callbacks**，框架就不会再自动注入那个
    有缺陷的原版 handler，从而彻底消除上面的报错::

        chain.invoke({"input": "..."}, config={"callbacks": [SafeConsoleCallbackHandler()]})
"""

from typing import Any

from langchain_core.tracers.schemas import Run
from langchain_core.tracers.stdout import (
    ConsoleCallbackHandler,
    elapsed,
    try_json_stringify,
)
from langchain_core.utils.input import get_bolded_text, get_colored_text


def _stringify_tool_io(value: Any, fallback: str) -> str:
    """把工具入参/出参安全地转成字符串（str 直接 strip，dict 转 JSON）。"""
    if isinstance(value, str):
        return f'"{value.strip()}"'
    return try_json_stringify(value, fallback)


class SafeConsoleCallbackHandler(ConsoleCallbackHandler):
    """工具入参为 dict 时也不会抛 KeyError 的 ConsoleCallbackHandler。"""

    name: str = "safe_console_callback_handler"

    def _on_tool_start(self, run: Run) -> None:
        crumbs = self.get_breadcrumbs(run)
        inputs = run.inputs if isinstance(run.inputs, dict) else {"input": run.inputs}
        # 原版只认 inputs["input"]，这里兼容 dict 形式入参（如 {"url": "..."}）
        payload = inputs["input"] if "input" in inputs else inputs
        self.function_callback(
            f"{get_colored_text('[tool/start]', color='green')} "
            + get_bolded_text(f"[{crumbs}] Entering Tool run with input:\n")
            + _stringify_tool_io(payload, "[inputs]")
        )

    def _on_tool_end(self, run: Run) -> None:
        crumbs = self.get_breadcrumbs(run)
        if not run.outputs:
            return
        outputs = run.outputs if isinstance(run.outputs, dict) else {"output": run.outputs}
        # 原版只认 outputs["output"]，工具无返回值（如 quit）时可能没有该键
        payload = outputs["output"] if "output" in outputs else outputs
        self.function_callback(
            f"{get_colored_text('[tool/end]', color='blue')} "
            + get_bolded_text(
                f"[{crumbs}] [{elapsed(run)}] Exiting Tool run with output:\n"
            )
            + _stringify_tool_io(payload, "[response]")
        )
