import json
import os
from typing import Any, Optional

from langchain_community.chat_models.tongyi import ChatTongyi
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import ToolCall
from langchain_core.outputs import ChatResult
from langchain_core.output_parsers.openai_tools import parse_tool_call

from src.utils.json_repair import repair_json_arguments


model_name = "qwen-max"

# 设置环境变量（替换为你的 DashScope API Key）
# os.environ["DASHSCOPE_API_KEY"] =
# os.environ["DASHSCOPE_BASE_URL"] =


def _repair_arguments(arguments: Any) -> Optional[str]:
    """把模型输出的「非法 JSON」入参修成合法的；修不了返回 None。

    实测 DashScope function calling 的三个高频问题（都会在 write_script 这类
    「入参是大段多行代码」的工具上必现）：
        1. 代码里的单引号被转义成 ``\\'``——JSON 规范里没有这个转义，
           json.loads 直接报 Invalid \\escape；
        2. 字符串里夹裸换行/制表符——JSON 规范要求写成 \\n / \\t，
           但 ``json.loads(..., strict=False)`` 可以容忍；
        3. **代码里的裸双引号没转义**（如 ``assert "省电" in text``）——它会把 JSON
           字符串提前截断，前两类修法都无效，必须按字符串边界逐字符扫描才修得动。
           这一类交给 src/utils/json_repair.repair_json_arguments（web / app 两个生成域
           共用的同一份实现，早先只有 web 侧的解析器链路上挂了它，模型出口这一层没有，
           于是同一个坏载荷在两层各修一半、最终还是抛 OutputParserException）。
    """
    if not isinstance(arguments, str) or not arguments:
        return None

    unescaped = arguments.replace("\\'", "'")
    for candidate in (arguments, unescaped):
        for strict in (True, False):
            try:
                json.loads(candidate, strict=strict)
            except (json.JSONDecodeError, TypeError):
                continue
            return candidate

    repaired = repair_json_arguments(arguments)
    if repaired == arguments:
        return None  # 共享修复也修不动：保持 None，由框架按原路径报错
    try:
        json.loads(repaired, strict=False)
    except (json.JSONDecodeError, TypeError):
        return None
    return repaired


def _repair_message_tool_calls(message: AIMessage) -> bool:
    """就地修复 AIMessage 上解析失败的 tool_calls，成功返回 True。

    为什么必须修在模型出口：ChatTongyi 内部用 parse_tool_call 解析失败后，会把该
    tool_call 放进 ``invalid_tool_calls`` 并让 ``tool_calls`` 保持为空，
    而 ``langchain_classic`` 的输出解析器（agents/output_parsers/tools.py）随后走
    best-effort 分支，对 additional_kwargs 里的原始 arguments 再 json.loads 一次，
    仍旧失败 -> 抛 OutputParserException。开了 handle_parsing_errors 也只是把它变成
    Observation，模型下一轮还会犯同样的错（实测连续 7 次），文件永远写不出来。
    这里在消息交给解析器之前就把 arguments 修好、并回填 tool_calls，
    解析器会直接走 ``if message.tool_calls`` 的正常分支。
    """
    raw_tool_calls = message.additional_kwargs.get("tool_calls") or []
    if not isinstance(raw_tool_calls, list):
        return False

    repaired: list[ToolCall] = []
    for raw in raw_tool_calls:
        function = (raw or {}).get("function") or {}
        fixed = _repair_arguments(function.get("arguments"))
        if fixed is None:
            continue
        if fixed != function.get("arguments"):
            # 同步写回 additional_kwargs：后续轮次会把它原样发回 DashScope，
            # 保持消息自洽（也避免把非法 JSON 再传一次）
            function["arguments"] = fixed
        try:
            parsed = parse_tool_call(raw, return_id=True)
        except Exception:  # noqa: BLE001 - 修不动就保持原样，交给框架报错
            continue
        if parsed:
            repaired.append(parsed)

    if not repaired:
        return False

    message.tool_calls = repaired
    # 全部修好才清空 invalid_tool_calls；只修好一部分时保留，便于排查
    if len(repaired) == len(raw_tool_calls):
        message.invalid_tool_calls = []
    return True


class QwenChatTongyi(ChatTongyi):
    """ChatTongyi + tool_calls 非法 JSON 自动修复。

    只在「模型返回了 tool_calls 且解析失败」时才介入，其余行为与父类完全一致，
    因此对不使用工具调用的场景（普通问答、structured output）零影响。
    """

    def _generate(
        self,
        messages: Any,
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        for generation in result.generations:
            message = getattr(generation, "message", None)
            if not isinstance(message, AIMessage):
                continue
            # 只有出现解析失败的 tool_call 时才需要修
            if message.invalid_tool_calls or (
                not message.tool_calls and message.additional_kwargs.get("tool_calls")
            ):
                if _repair_message_tool_calls(message):
                    print("已自动修复 DashScope 返回的非法 tool_calls JSON 转义")
        return result


# 初始化大模型（支持 function calling）
qwen_model = QwenChatTongyi(model=model_name, api_key=os.environ.get("DASHSCOPE_API_KEY"))


