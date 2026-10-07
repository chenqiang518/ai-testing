#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""langchain tracer 日志的**事件级**过滤规则（纯解析逻辑，不依赖 langchain）。

`[chain/start]` / `[llm/end]` / `[tool/start]` 这些彩色标签全部出自
`langchain_core.tracers.stdout.ConsoleCallbackHandler`，而它实际会打印的事件只有
9 个（见 ALL_EVENTS）：chain / llm / tool 三类 × start / end / error 三阶段。
所以细粒度开关只需要覆盖这 9 个即可：

* chat model（ChatTongyi / ChatOpenAI 这类）的**开始**在 langchain 原版里会先抛
  NotImplementedError、再被回退成 `on_llm_start(prompts=[...])`（整段对话压成一行
  转义字符串）；本项目的 `SafeConsoleCallbackHandler` 改用 `_schema_format=
  "original+chat"`，直接实现 `_on_chat_model_start` 按条打印消息，并把日志同样归到
  `llm/start` 事件下，因此配 `llm/start` 对 text LLM 与 chat model 一样生效。
  chat model 的**结束**复用 `[llm/end]`，想静音大模型输出就配 `llm/end`。
* retriever 的三类事件同理不打印，无需配置。

配置值支持三种写法，可混用、大小写不敏感、`-` 与 `_` 等价、逗号或空格分隔：

    all            全部事件（默认）
    none           一个都不要
    tool           整个类别（= tool/start + tool/end + tool/error）
    chain/start    单个事件

拼错的事件名会直接抛 ValueError（而不是静默忽略），避免「以为关掉了其实没关」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Union

# 事件类别 -> 该类别下会被 ConsoleCallbackHandler 打印的阶段
EVENT_PHASES: dict[str, tuple[str, ...]] = {
    "chain": ("start", "end", "error"),
    "llm": ("start", "end", "error"),
    "tool": ("start", "end", "error"),
}

# 全部可过滤事件
ALL_EVENTS: frozenset[str] = frozenset(
    f"{category}/{phase}"
    for category, phases in EVENT_PHASES.items()
    for phase in phases
)

# 稳定的展示顺序：隐藏提示、报错信息都用它，避免 set 的随机顺序让输出每次不一样
EVENT_ORDER: tuple[str, ...] = tuple(
    f"{category}/{phase}" for category in EVENT_PHASES for phase in EVENT_PHASES[category]
)

ALL_TOKEN = "all"
NONE_TOKEN = "none"

# spec 可以是字符串（"chain,tool/start"）、字符串列表，或 None
EventSpec = Union[str, Iterable[str], None]


def split_spec(spec: EventSpec) -> list[str]:
    """把配置值切成 token 列表，兼容字符串 / 列表 / None，逗号与空格都可作分隔符。

    Examples:
        >>> split_spec("chain, tool/start  llm")
        ['chain', 'tool/start', 'llm']
        >>> split_spec(None)
        []
    """
    if spec is None:
        return []
    items = [str(s) for s in spec] if isinstance(spec, (list, tuple, set, frozenset)) else [str(spec)]
    tokens: list[str] = []
    for item in items:
        for part in item.replace(";", ",").split(","):
            tokens.extend(part.split())
    return [token for token in (t.strip() for t in tokens) if token]


def normalize_token(token: str) -> str:
    """归一化：小写 + `-` 视作 `_`（chat-model / chat_model 写法都能认）。"""
    return token.strip().lower().replace("-", "_")


def _invalid_token_error(token: str, field_name: str) -> ValueError:
    valid = ", ".join([ALL_TOKEN, NONE_TOKEN, *EVENT_PHASES, *EVENT_ORDER])
    return ValueError(
        f"""{field_name} 中的 {token!r} 不是合法的 tracer 事件名。
        可用值：{valid}
        （也可只写类别名，如 `tool` 表示 tool/start + tool/end + tool/error）"""
    )


def expand_tokens(tokens: Iterable[str], *, field_name: str = "events") -> frozenset[str]:
    """把 token 列表展开成事件键集合；遇到非法 token 抛 ValueError。"""
    expanded: set[str] = set()
    for raw in tokens:
        token = normalize_token(raw)
        if token in (ALL_TOKEN, "*"):
            expanded |= ALL_EVENTS
        elif token == NONE_TOKEN:
            continue  # 显式表示「不要任何事件」，展开为空
        elif "/" in token:
            if token not in ALL_EVENTS:
                raise _invalid_token_error(raw, field_name)
            expanded.add(token)
        elif token in EVENT_PHASES:
            expanded |= {f"{token}/{phase}" for phase in EVENT_PHASES[token]}
        else:
            raise _invalid_token_error(raw, field_name)
    return frozenset(expanded)


@dataclass(frozen=True)
class DebugEventFilter:
    """允许打印的 tracer 事件白名单；不在白名单里的事件一律静默跳过。"""

    events: frozenset[str] = ALL_EVENTS

    @classmethod
    def all_events(cls) -> "DebugEventFilter":
        """不过滤（默认行为）。"""
        return cls(ALL_EVENTS)

    @classmethod
    def no_events(cls) -> "DebugEventFilter":
        """全部过滤。"""
        return cls(frozenset())

    def allows(self, event: str) -> bool:
        """该事件是否应该打印，例如 allows("chain/start")。"""
        return event in self.events

    @property
    def allows_all(self) -> bool:
        """是否一个都没过滤（用于决定是否要打印「已隐藏 xxx」提示）。"""
        return ALL_EVENTS <= self.events

    @property
    def allows_none(self) -> bool:
        """是否全部被过滤（此时连 handler 都不必注入，省掉 tracer 的开销）。"""
        return not self.events

    def shown(self) -> tuple[str, ...]:
        """仍会打印的事件，按固定顺序。"""
        return tuple(event for event in EVENT_ORDER if event in self.events)

    def hidden(self) -> tuple[str, ...]:
        """被隐藏的事件，按固定顺序。"""
        return tuple(event for event in EVENT_ORDER if event not in self.events)

    def describe(self) -> str:
        """人类可读的「会打印哪些」描述，用于启动提示。"""
        if self.allows_none:
            return "无（tracer 日志全部隐藏）"
        if self.allows_all:
            return "全部"
        return ", ".join(self.shown())

    def describe_hidden(self) -> str:
        """人类可读的「隐藏了哪些」描述；没有隐藏时返回空串。"""
        return ", ".join(self.hidden())


def build_event_filter(only: EventSpec = None, hide: EventSpec = None) -> DebugEventFilter:
    """按白名单 / 黑名单构造过滤器。

    Args:
        only: 只保留这些事件（None 表示不限制，等价 "all"；"none" 表示全隐藏）。
        hide: 从结果里剔除这些事件。

    Returns:
        DebugEventFilter（先应用 only，再减去 hide）。

    Examples:
        >>> sorted(build_event_filter(hide="chain").shown())
        ['llm/end', 'llm/error', 'llm/start', 'tool/end', 'tool/error', 'tool/start']
        >>> sorted(build_event_filter(only="tool", hide="tool/error").shown())
        ['tool/end', 'tool/start']
    """
    allowed: set[str] = (
        set(expand_tokens(split_spec(only), field_name="only"))
        if only is not None
        else set(ALL_EVENTS)
    )
    blocked = expand_tokens(split_spec(hide), field_name="hide") if hide is not None else frozenset()
    return DebugEventFilter(frozenset(allowed - blocked))
