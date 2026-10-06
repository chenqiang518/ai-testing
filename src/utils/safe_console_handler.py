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

按事件过滤（本文件新增能力）：
    `[chain/start]` / `[chain/end]` / `[llm/start]` / `[llm/end]` / `[tool/start]` /
    `[tool/end]`（以及三个 `*/error`）这 9 类 tracer 日志可以单独关掉任意一个或几个::

        from src.utils.debug_events import build_event_filter
        handler = SafeConsoleCallbackHandler(event_filter=build_event_filter(hide="chain,llm/end"))

    事件名规则与解析见 src/utils/debug_events.py；命令行 / 环境变量的接线见
    src/utils/langchain_debug.py。

补齐 chat model 的 `[llm/start]`（本文件第二个修复）：
    langchain-core 的 tracer 默认 `_schema_format="original"`，chat model 的开始事件会先在
    `_TracerCore._create_chat_model_run` 里抛 NotImplementedError，再由
    `callbacks.manager.handle_event` 回退成 `on_llm_start(prompts=[get_buffer_string(messages)])`。
    所以原版 `[llm/start]` 并非「不打印」，而是把整段对话压成 JSON 里的**一行转义字符串**::

        {"prompts": ["System: 你是一个自动化测试工程师...\nHuman: ...\nAI: [{'name': ...}]"}

    prompt 一长（agent 的 system prompt + 工具清单 + agent_scratchpad 轻松上千字），
    `\n` 全变成字面量、引号还要转义，基本没法读。本文件把 `_schema_format` 设为
    `"original+chat"`，让 chat model 走 `_on_chat_model_start`，按 `[role] content`
    逐条打印消息、tool_calls 单独成行；事件名仍归到 `llm/start`，因此
    `--debug-events=llm` / `--hide-debug-events=llm/start` 这些开关对它同样生效。
    （text LLM 仍走 `_on_llm_start`，行为与原版一致。）

`[llm/end]` 改成可读输出：
    原版 `_on_llm_end` 直接 `try_json_stringify(run.outputs)`，而 chat model 的
    `run.outputs` 是 `LLMResult.model_dump()`（generations 里每条消息又被 `dumpd` 成
    `{"lc": 1, "type": "constructor", "id": [...], "kwargs": {...}}`），几十行嵌套 JSON
    里翻一句模型回复、或者翻它到底调了哪个工具，几乎不可读。这里改成优先打印
    消息正文与 tool_calls（`add(a=1, b=2)` 这种一行式），拿不到才回退原始 JSON。
"""

from typing import Any, Optional

from langchain_core.tracers.schemas import Run
from langchain_core.tracers.stdout import (
    ConsoleCallbackHandler,
    elapsed,
    try_json_stringify,
)
from langchain_core.utils.input import get_bolded_text, get_colored_text

from src.utils.debug_events import DebugEventFilter


def _stringify_tool_io(value: Any, fallback: str) -> str:
    """把工具入参/出参安全地转成字符串（str 直接 strip，dict 转 JSON）。"""
    if isinstance(value, str):
        return f'"{value.strip()}"'
    return try_json_stringify(value, fallback)


# ---- 下面这组小函数把「被 langchain 序列化过的消息」还原成人类可读文本 ----
# 背景：tracer 拿到的不是 BaseMessage 对象本身，而是 dumpd() 之后的 dict::
#
#     {"lc": 1, "type": "constructor",
#      "id": ["langchain", "schema", "messages", "HumanMessage"],
#      "kwargs": {"content": "...", "tool_calls": [...], "additional_kwargs": {...}}}
#
# 直接 json.dumps 出来几十行，看不出「发给模型的 prompt 是什么」「模型要调哪个工具」。
# 因此统一走 kwargs 取值，取不到再退化成 try_json_stringify，绝不因为结构变化而抛异常
# （tracer 里的异常会被 CallbackManager 吞成一行 Error in ... callback，日志就丢了）。


def _message_kwargs(payload: Any) -> dict:
    """取出消息的 kwargs（dumpd 之后的 dict / 原始 BaseMessage 都兼容）。"""
    if isinstance(payload, dict):
        kwargs = payload.get("kwargs")
        return kwargs if isinstance(kwargs, dict) else payload
    return getattr(payload, "__dict__", {}) if hasattr(payload, "content") else {}


def _message_role(payload: Any) -> str:
    """消息角色，如 system / human / ai / tool。"""
    if isinstance(payload, dict):
        ids = payload.get("id")
        if isinstance(ids, list) and ids:
            # "HumanMessage" -> "human"；非标准 id 至少给个可读的名字
            return str(ids[-1]).removesuffix("Message").lower()
        if isinstance(payload.get("type"), str):
            return payload["type"].lower()
    message_type = getattr(payload, "type", None)
    if isinstance(message_type, str) and message_type:
        return message_type.lower()
    return type(payload).__name__.removesuffix("Message").lower()


def _stringify_content(content: Any) -> str:
    """消息正文：str 原样，多模态的 content blocks 取其中的 text 拼起来。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"].strip())
            else:
                parts.append(try_json_stringify(block, "[content]").strip())
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content).strip()


def _message_text(payload: Any) -> str:
    """一条消息的可读正文（不含 tool_calls）。"""
    kwargs = _message_kwargs(payload)
    if "content" in kwargs:
        return _stringify_content(kwargs["content"])
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"].strip()
    return try_json_stringify(payload, "[message]")


def _message_tool_calls(payload: Any) -> list:
    """消息上的 tool_calls 列表（可能来自 kwargs，也可能是 BaseMessage 属性）。"""
    kwargs = _message_kwargs(payload)
    calls = kwargs.get("tool_calls")
    if not isinstance(calls, list):
        calls = getattr(payload, "tool_calls", None)
    return calls if isinstance(calls, list) else []


def _format_tool_call(call: Any) -> str:
    """把一个 tool_call 打成 `add(a=1, b=2)` 这种一行式，方便一眼看出模型要干什么。"""
    if not isinstance(call, dict):
        return str(call)
    name = call.get("name") or call.get("id") or "tool"
    args = call.get("args")
    if not isinstance(args, dict):
        return f"{name}({try_json_stringify(args, '')})" if args is not None else f"{name}()"
    rendered = ", ".join(f"{key}={_render_arg_value(value)}" for key, value in args.items())
    return f"{name}({rendered})"


def _render_arg_value(value: Any) -> Any:
    """入参值：字符串加引号，其余（dict/list/数字）转 JSON，避免打印出 Python repr 的歧义。"""
    if isinstance(value, str):
        return f'"{value}"'
    return try_json_stringify(value, repr(value))


def _format_messages(messages: Any) -> str:
    """把 chat model 的入参 messages（list[list[dict]]）格式化成多行可读文本。"""
    batches = messages if isinstance(messages, list) else [messages]
    lines: list[str] = []
    for batch in batches:
        items = batch if isinstance(batch, list) else [batch]
        for payload in items:
            role = _message_role(payload)
            text = _message_text(payload)
            lines.append(f"[{role}] {text}" if text else f"[{role}] (空)")
            for call in _message_tool_calls(payload):
                lines.append(f"    ↳ tool_call: {_format_tool_call(call)}")
    return "\n".join(lines) if lines else try_json_stringify(messages, "[messages]")


def _format_generation(generation: Any) -> str:
    """格式化 LLMResult.generations 里的一条（chat model 是 dumpd 后的消息 dict）。"""
    if isinstance(generation, dict):
        message = generation.get("message")
        if message is not None:
            text = _message_text(message)
            calls = [_format_tool_call(call) for call in _message_tool_calls(message)]
            parts = [text] if text else []
            if calls:
                parts.append("tool_calls: " + "; ".join(calls))
            if parts:
                return "\n".join(parts)
        if isinstance(generation.get("text"), str):
            return generation["text"]
    return try_json_stringify(generation, "[generation]")


def _format_llm_outputs(outputs: Any) -> str:
    """格式化 `[llm/end]` 的输出：优先取 generations 里的消息正文与 tool_calls。

    拿不到熟悉的结构时（例如 text LLM 的 {"generations": [[{"text": ...}]]} 之外的形态）
    退回原版的整体 JSON，保证信息不丢。
    """
    if not isinstance(outputs, dict):
        return try_json_stringify(outputs, "[response]")
    generations = outputs.get("generations")
    if not isinstance(generations, list) or not generations:
        return try_json_stringify(outputs, "[response]")
    rendered: list[str] = []
    for batch in generations:
        items = batch if isinstance(batch, list) else [batch]
        for generation in items:
            rendered.append(_format_generation(generation))
    return "\n".join(part for part in rendered if part) or try_json_stringify(outputs, "[response]")


class SafeConsoleCallbackHandler(ConsoleCallbackHandler):
    """工具入参为 dict 时不会抛 KeyError、且支持**按事件过滤**的 ConsoleCallbackHandler。

    过滤能力：构造时传 `event_filter=DebugEventFilter(...)`，被排除的事件在对应的
    `_on_xxx` 里直接 return，连日志字符串都不拼。

    为什么拦在 `_on_*` 而不是公有的 `on_*`：`BaseTracer.on_chain_start` 的顺序是
    `_create_chain_run` -> `_start_trace`（写 run_map）-> `_on_chain_start`，
    `_end_trace`（弹出 run_map、触发 `_persist_run`）-> `_on_chain_end`。
    拦在 `_on_*` 只跳过「打印」，run_map / get_breadcrumbs 的父子面包屑 /
    `_persist_run` 全部照常，不会出现「隐藏了 chain/start 之后，子节点日志里的
    `[chain:RunnableSequence > chain:RunnableLambda]` 前缀断链」这种副作用。
    """

    name: str = "safe_console_callback_handler"

    def __init__(
        self,
        event_filter: Optional[DebugEventFilter] = None,
        **kwargs: Any,
    ) -> None:
        """Args:
            event_filter: 事件白名单，None 表示不过滤（打印全部 9 类 tracer 事件）。
            **kwargs: 透传给 FunctionCallbackHandler（如自定义 function）。
        """
        # 打开 chat model 的开始事件（默认 "original" 会让它抛 NotImplementedError，
        # 回退成一行转义过的 prompt 字符串）；显式传参仍可覆盖
        kwargs.setdefault("_schema_format", "original+chat")
        super().__init__(**kwargs)
        # 用普通实例属性即可：BaseCallbackHandler 不是 pydantic model
        # （langchain 自己的 FunctionCallbackHandler 也是这么存 function_callback 的）
        self.event_filter = DebugEventFilter.all_events() if event_filter is None else event_filter

    def _should_log(self, event: str) -> bool:
        """该事件是否要打印，event 形如 "chain/start"。"""
        return self.event_filter.allows(event)

    # ---- chain：[chain/start] / [chain/end] / [chain/error] ----
    def _on_chain_start(self, run: Run) -> None:
        if not self._should_log("chain/start"):
            return
        super()._on_chain_start(run)

    def _on_chain_end(self, run: Run) -> None:
        if not self._should_log("chain/end"):
            return
        super()._on_chain_end(run)

    def _on_chain_error(self, run: Run) -> None:
        if not self._should_log("chain/error"):
            return
        super()._on_chain_error(run)

    # ---- llm：[llm/start] / [llm/end] / [llm/error] ----
    # text LLM 走 _on_llm_start；chat model（ChatTongyi / ChatOpenAI 这些）走
    # _on_chat_model_start（本类已把 _schema_format 打开，见 __init__），
    # 两者的**结束**都复用 _on_llm_end —— 所以 [llm/end] 才是「大模型返回内容」那一坨
    def _on_llm_start(self, run: Run) -> None:
        if not self._should_log("llm/start"):
            return
        super()._on_llm_start(run)

    def _on_chat_model_start(self, run: Run) -> None:
        """chat model 版的 [llm/start]：按条打印真正发给模型的消息。

        归到 `llm/start` 事件下，与 text LLM 的 [llm/start] 共用同一个开关。
        原版这里会抛 NotImplementedError，被 CallbackManager 回退成
        `on_llm_start(prompts=[get_buffer_string(messages)])`：整段对话压成 JSON 里
        一行转义字符串，长 prompt 完全没法读；本实现直接按 `[role] content` 逐条打印，
        并把 tool_calls 单独成行。
        """
        if not self._should_log("llm/start"):
            return
        crumbs = self.get_breadcrumbs(run)
        inputs = run.inputs if isinstance(run.inputs, dict) else {}
        messages = inputs.get("messages")
        self.function_callback(
            f"{get_colored_text('[llm/start]', color='green')} "
            + get_bolded_text(f"[{crumbs}] Entering Chat Model run with input:\n")
            + (_format_messages(messages) if messages is not None
               else try_json_stringify(inputs, "[inputs]"))
        )

    def _on_llm_end(self, run: Run) -> None:
        if not self._should_log("llm/end"):
            return
        crumbs = self.get_breadcrumbs(run)
        # 原版打印整个 LLMResult.model_dump()，嵌套 JSON 里翻不出一句回复；
        # 这里优先打印消息正文 + tool_calls，结构不认识时仍回退原始 JSON
        self.function_callback(
            f"{get_colored_text('[llm/end]', color='blue')} "
            + get_bolded_text(f"[{crumbs}] [{elapsed(run)}] Exiting LLM run with output:\n")
            + _format_llm_outputs(run.outputs)
        )

    def _on_llm_error(self, run: Run) -> None:
        if not self._should_log("llm/error"):
            return
        super()._on_llm_error(run)

    # ---- tool：[tool/start] / [tool/end] / [tool/error]（前两个是 KeyError 修复版）----
    def _on_tool_start(self, run: Run) -> None:
        if not self._should_log("tool/start"):
            return
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
        if not self._should_log("tool/end"):
            return
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

    def _on_tool_error(self, run: Run) -> None:
        # 原版实现没有 inputs["input"] 的问题，这里只加过滤、其余照旧交给父类
        if not self._should_log("tool/error"):
            return
        super()._on_tool_error(run)
