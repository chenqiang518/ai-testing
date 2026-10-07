"""langchain 控制台日志的统一开关。

总开关默认值由**调用方**决定（`debug_enabled(default=...)`）：入口脚本
`src/web/generate_autoweb.py` 传 `default=True`，即**默认就打印** llm / tool 两类
tracer 日志与 agent 步骤行（`> Entering new AgentExecutor chain...` /
`Invoking: ...` / `> Finished chain.`）；不想看的时候再显式关掉：

    python src/web/generate_autoweb.py --quiet
    LANGCHAIN_DEBUG=0 python src/web/generate_autoweb.py

反过来，默认关闭的调用方也能用这两个开关临时打开（两种方式等价，都只影响当前进程）：

    python src/web/generate_autoweb.py --debug
    LANGCHAIN_DEBUG=1 python src/web/generate_autoweb.py

`--debug` / `--quiet`（别名 `--no-debug`）与 LANGCHAIN_DEBUG 的真值 / 假值
（1|true|yes|on 与 0|false|no|off）都能识别，命令行优先于环境变量；
两边都没写时才用调用方给的 default。

**再细一层**：tracer 事件可以单独隐藏（`chain/start`、`chain/end`、`llm/start`、
`llm/end`、`tool/start`、`tool/end`，以及三个 `*/error`）。
`--hide-debug-events` 是黑名单（隐藏列出的），`--debug-events` 是白名单（只显示列出的），
两者都有对应的环境变量，命令行优先；值可以写整个类别（`chain`）或单个事件
（`chain/start`），逗号或空格分隔，大小写不敏感：

    # 只看工具调用，不想被 chain 的层层嵌套刷屏
    python src/web/generate_autoweb.py --hide-debug-events=chain
    # 嫌大模型返回内容太长：隐藏 llm/end（chat model 的结束也走这个事件）
    python src/web/generate_autoweb.py --hide-debug-events=llm/end,chain/end
    # 反过来，只保留工具与大模型事件
    python src/web/generate_autoweb.py --debug-events=tool,llm
    LANGCHAIN_HIDE_DEBUG_EVENTS=chain,llm/end python src/web/generate_autoweb.py

事件名拼错会直接抛 ValueError 并列出全部合法值，不会静默失效（详见 debug_events.py）。
这套过滤只作用于 tracer 日志；verbose 带来的「Invoking: ...」「responded: ...」是
StdOutCallbackHandler 打的，只受 --debug / --quiet / LANGCHAIN_DEBUG 总开关控制。

为什么必须由本模块来注入 handler：`langchain_core.callbacks.manager._configure()`
在 debug 打开、且 callbacks 里**没有** ConsoleCallbackHandler 实例时，会自动塞一个
原版 handler；而原版 `_on_tool_start` 写死了 `run.inputs["input"]`，遇到「工具入参
是 dict」（structured chat agent 的 action_input、langgraph ToolNode 都是 dict）就抛
KeyError('input')，控制台只留一行 `Error in ConsoleCallbackHandler.on_tool_start
callback: KeyError('input')`，[tool/start] 日志全丢。所以开启时这里注入的是修复版
SafeConsoleCallbackHandler（子类同样满足 isinstance 判断，框架就不会再注入原版）。
它同时把 chat model 的 `[llm/start]` 变可读了：原版 tracer 的 `_schema_format="original"`
会让 chat model 的开始事件抛 NotImplementedError，被回退成
`on_llm_start(prompts=[get_buffer_string(messages)])`——整段对话（system prompt +
工具清单 + agent_scratchpad）压成 JSON 里一行转义字符串，长 prompt 根本没法读；
修复版改用 `_schema_format="original+chat"`，按 `[role] content` 逐条打印消息、
tool_calls 单独成行（详见 safe_console_handler.py 模块注释）。
"""

import os
import sys
from typing import Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import set_debug, set_verbose

from src.utils.debug_events import DebugEventFilter, build_event_filter
from src.utils.safe_console_handler import SafeConsoleCallbackHandler

# 环境变量取这些值时视为「开启」/「关闭」（与 generate_autoweb.force_collect 的约定一致）
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

# 命令行开关名：python xxx.py --debug / --quiet
DEBUG_FLAG = "--debug"
QUIET_FLAGS = ("--quiet", "--no-debug")

# 环境变量开关名
DEBUG_ENV = "LANGCHAIN_DEBUG"

# 事件级过滤：白名单（只显示列出的事件）与黑名单（隐藏列出的事件）
EVENTS_ONLY_CLI = "--debug-events"
EVENTS_HIDE_CLI = "--hide-debug-events"
EVENTS_ONLY_ENV = "LANGCHAIN_DEBUG_EVENTS"
EVENTS_HIDE_ENV = "LANGCHAIN_HIDE_DEBUG_EVENTS"


def _read_cli_option(argv: list, flag: str) -> Optional[str]:
    """从 argv 里读 `--flag=value`（也兼容 `--flag value`）；没配则返回 None。

    返回空串表示「配了但值为空」（例如 `--debug-events=`），与「没配」区分开，
    这样命令行才能覆盖掉环境变量里的值。
    """
    prefix = flag + "="
    for index, arg in enumerate(argv):
        if arg.startswith(prefix):
            return arg[len(prefix):]
        if arg == flag:
            return argv[index + 1] if index + 1 < len(argv) else ""
    return None


def _resolve_spec(cli_value: Optional[str], env_value: Optional[str]) -> Optional[str]:
    """命令行优先于环境变量；两边都没配时返回 None（表示不做限制）。"""
    if cli_value is not None:
        return cli_value
    env_value = (env_value or "").strip()
    return env_value or None


def resolve_event_filter(
    argv: Optional[list] = None,
    default_only: Optional[str] = None,
) -> DebugEventFilter:
    """按命令行 / 环境变量解析出 tracer 事件过滤器。

    优先级：`--debug-events` > `LANGCHAIN_DEBUG_EVENTS` > default_only（白名单），
    `--hide-debug-events` > `LANGCHAIN_HIDE_DEBUG_EVENTS`（黑名单）；
    两者可叠加——先取白名单，再从中剔除黑名单。

    Args:
        argv: 命令行参数列表，默认取 sys.argv；测试里可显式传入。
        default_only: 入口脚本给的**默认白名单**，仅在用户既没写命令行参数、也没设
            环境变量时才生效；None 表示不限制（9 类事件全打印）。例如
            generate_autoweb.py 传 "tool,llm"，让 chain 的几十条 LCEL 包装层噪音默认
            不刷屏，而使用者显式写 `--debug-events=all` 仍能拿回全部事件。

    Raises:
        ValueError: 事件名拼错（例如 `--hide-debug-events=chian`）。这里故意报错，
            否则用户会以为日志已经关掉了、实际还在刷屏。
    """
    args = sys.argv if argv is None else list(argv)
    only = _resolve_spec(_read_cli_option(args, EVENTS_ONLY_CLI), os.getenv(EVENTS_ONLY_ENV))
    if only is None:
        # 用户没显式指定白名单，才退回入口脚本给的默认值
        only = default_only
    hide = _resolve_spec(_read_cli_option(args, EVENTS_HIDE_CLI), os.getenv(EVENTS_HIDE_ENV))
    return build_event_filter(only=only, hide=hide)


def debug_enabled(argv: Optional[list] = None, default: bool = False) -> bool:
    """是否需要打印 langchain debug/verbose 日志。

    判定优先级（从高到低）：
        1. 命令行显式开启：`--debug`
        2. 命令行显式关闭：`--quiet`（别名 `--no-debug`）
        3. 环境变量 LANGCHAIN_DEBUG：1|true|yes|on 开启，0|false|no|off 关闭
        4. 都没写：用调用方给的 default

    Args:
        argv: 命令行参数列表，默认取 sys.argv；测试里可显式传入以便 monkeypatch。
        default: 调用方的默认值。入口脚本（generate_autoweb.py）传 True，
            即「默认打印 llm / tool tracer 日志与 agent 步骤行，想看安静输出再加 --quiet」。

    Returns:
        True 表示开启。

    注意 `--debug` 优先于 `--quiet`：两个都写时按「开启」处理，避免使用者在 IDE 的运行
    配置里留了一个开关、命令行又加了另一个开关时，得到与直觉相反的结果。
    """
    args = list(sys.argv if argv is None else argv)
    if DEBUG_FLAG in args:
        return True
    if any(flag in args for flag in QUIET_FLAGS):
        return False
    env_value = os.getenv(DEBUG_ENV, "").strip().lower()
    if env_value in _TRUTHY:
        return True
    if env_value in _FALSY:
        return False
    return default


def configure_langchain_logging(
    enabled: Optional[bool] = None,
    event_filter: Optional[DebugEventFilter] = None,
    only: Optional[str] = None,
    hide: Optional[str] = None,
    default: bool = False,
) -> list:
    """按开关设置 langchain 日志，并返回应注入的 callbacks 列表。

    Args:
        enabled: 是否开启总开关；None 表示按 debug_enabled(default=default) 自动判定。
        event_filter: 已构造好的事件过滤器；None 时按 only/hide 或命令行/环境变量解析。
        only: 白名单 spec（只显示这些事件），如 "tool,llm/start"；仅在 event_filter
            为 None 时生效。
        hide: 黑名单 spec（隐藏这些事件），如 "chain,llm/end"；仅在 event_filter
            为 None 时生效。
        default: 调用方的默认开关值，仅在 enabled 为 None 且命令行/环境变量都没写时生效
            （透传给 debug_enabled）。

    Returns:
        开启时返回 [SafeConsoleCallbackHandler(event_filter=...)]；下列情况返回空列表
        （空列表传给 AgentExecutor(callbacks=...) 或 invoke(config=...) 都无副作用）：
        总开关关闭，或 9 类 tracer 事件被全部隐藏（此时注入 handler 只会白白承担
        tracer 建 Run / 拷贝 inputs 的开销，一行日志都不打）。
        本函数只改全局开关与返回 handler，不做任何 print——是否提示由调用方决定，
        便于被 pytest 等安静环境复用。

    注意（开启时故意**不**调 set_debug(True)）：
        `langchain_core.callbacks.manager._configure()` 里是这么写的::

            if verbose and not any(isinstance(h, StdOutCallbackHandler) ...):
                if debug:
                    pass            # debug 打开时故意不注入 StdOutCallbackHandler
                else:
                    callback_manager.add_handler(StdOutCallbackHandler(), inherit=False)

        即 debug 与 verbose 同时为真时，「> Entering new AgentExecutor chain...」
        「Invoking: `tool` with ...」「responded: ...」「> Finished chain.」这些行
        会**消失**。而 tracer 日志（[chain/start] 等）本来就由我们显式注入的
        SafeConsoleCallbackHandler 提供，不依赖全局 debug 开关，所以这里只开 verbose，
        两套日志才能同时齐全。
        关闭时则把 debug 也一并置 False：万一同进程里别的模块（如
        src/api/generate_case.py 的 langchain_debug()）打开过全局 debug，框架会自动
        注入原版 ConsoleCallbackHandler，日志又会冒出来，这里做一次兜底静音。
    """
    on = debug_enabled(default=default) if enabled is None else bool(enabled)
    set_verbose(on)
    set_debug(False)
    if not on:
        return []
    if event_filter is None:
        event_filter = (
            build_event_filter(only=only, hide=hide)
            if (only is not None or hide is not None)
            else resolve_event_filter()
        )
    if event_filter.allows_none:
        # 9 类 tracer 事件全被隐藏：不注入 handler，省掉 tracer 建 Run / 拷贝 inputs 的开销
        return []
    handlers: list[BaseCallbackHandler] = [
        SafeConsoleCallbackHandler(event_filter=event_filter)
    ]
    return handlers


def describe_logging(
    enabled: bool,
    event_filter: Optional[DebugEventFilter] = None,
) -> str:
    """返回一行中文状态描述（只返回字符串、不 print），供入口脚本提示当前日志配置。

    提示里刻意不写 `[chain/start]` 这类带方括号的字面量，否则会污染对日志文件的
    grep 统计（例如 grep -c 会把提示行也算成一条 tracer 日志）。
    每种状态都同时给出「反方向」的开关，使用者不必回头翻代码就知道怎么改。
    """
    if not enabled:
        return """langchain 调试日志已关闭（chain / llm / tool 的 tracer 日志与 agent 步骤行\
        都不打印）；加 --debug 或设 LANGCHAIN_DEBUG=1 重新打开"""
    if event_filter is None or event_filter.allows_all:
        return """langchain 调试日志已开启（tracer 事件全开）；\
        可用 --hide-debug-events=chain,llm/end 精确隐藏其中某几个，\
        或加 --quiet / 设 LANGCHAIN_DEBUG=0 全部关闭"""
    if event_filter.allows_none:
        return """langchain 调试日志已开启，但 tracer 事件被全部隐藏（只保留 agent 步骤行）；\
        检查 --debug-events / --hide-debug-events 是否配置过头"""
    return f"""langchain 调试日志已开启；tracer 事件保留：{event_filter.describe()}；\
    已隐藏：{event_filter.describe_hidden()}\
    （加 --debug-events=all 可显示全部 9 类事件；加 --quiet 或设 LANGCHAIN_DEBUG=0 \
    可全部关闭）"""


def langchain_debug(enabled: bool = True) -> None:
    """打开（或关闭）langchain 全局 debug/verbose 日志。

    保留原有无参调用方式（src/api/generate_case.py 在 import 时调用），
    语义不变：默认就是把全局 debug 与 verbose 都打开。
    注意它只改全局开关、不注入修复版 handler；若还要 tracer 日志且工具入参可能是
    dict，请改用 configure_langchain_logging(enabled)。
    """
    set_debug(enabled)
    set_verbose(enabled)