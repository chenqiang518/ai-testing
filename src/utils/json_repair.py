"""LLM 工具入参的 JSON 机械修复 —— web / app 两个生成域共用同一份实现。

为什么需要它（三个历史坑，最早都记在 src/web/generate_autoweb.py 第二环的注释里）：

坑 1：第二环曾是 `prompt | llm | StrOutputParser()` 的纯文本调用，llm 没绑定任何工具，
      「保存到 scripts 目录下」只是 prompt 里的一句空话 —— 模型只能把代码当字符串吐回来，
      print 到控制台就丢弃了，脚本一个字都没落盘。

坑 2：改成 structured chat agent 后**仍然**写不出文件：它要求模型把 action_input 以 JSON
      **文本**形式输出，而 write_script 的 code 参数是**多行代码**，模型会在 JSON 字符串里
      直接敲裸换行、或忘转义内部双引号（`assert "省电" in text` 必现），产生非法 JSON，
      实测连续 7 次 OUTPUT_PARSING_FAILURE；handle_parsing_errors 只能把它变成 Observation，
      模型下一轮仍犯同样的错。=> 第二环改用原生 function calling（bind_tools）。

坑 3：function calling 也**不是**万无一失 —— 模型仍会把 Python 习惯带进 JSON：
      单引号写成 `\\'`（JSON 没有这个转义）、代码里的裸双引号忘转义（把 JSON 字符串提前截断）。
      langchain 的 parse_ai_message_to_tool_action 直接抛
      OutputParserException("Could not parse tool input")，而 handle_parsing_errors 只会回一句
      「Invalid or incomplete response」，模型看不出自己错在哪，实测连撞 39 次、
      把 max_iterations 全烧光，脚本同样一个字都没落盘。

与其指望模型改习惯，不如在解析前把这类**可机械修复**的坏写法修掉：
    repair_json_string         逐字符扫描（按字符串边界判断），修坏转义 + 裸双引号；
    repair_json_arguments      逐级尝试三类修复，每级都用 json.loads 校验；
    repair_tool_call_arguments 挂在 `llm.bind(tools=...)` 与 OpenAIToolsAgentOutputParser
                               之间，原地改 AIMessage.additional_kwargs["tool_calls"]。

app 版早先自己写过一个 `_repair_illegal_tool_args`：从 `"code":` 后的引号只扫到**第一个换行**，
而真实 code 必然是多行，于是 `body.endswith('"')` 永远为 False、永远原样返回 ——
「已按规则修好的写法（可直接照抄）」那句提示从来没出现过，等于兜底形同虚设。现已统一到这里。

设计底线：修不好就**原样返回**，让上层按原来的路径报错（不做无根据的猜测，
宁可失败也不写坏文件）。
"""

import json
import re
from typing import Any

__all__ = ["repair_json_string", "repair_json_arguments", "repair_tool_call_arguments"]

_INVALID_JSON_ESCAPE = re.compile(r"\\(?![\"\\/bfnrt]|u[0-9a-fA-F]{4})(.)", re.S)
_VALID_JSON_ESCAPES = frozenset('"\\/bfnrt')
# 字符串值里的裸控制字符 -> 合法转义。模型把多行代码塞进 JSON 字符串时经常直接敲换行，
# 而不是写成 \n（下游 parse_tool_call / OpenAIToolsAgentOutputParser 都用 strict=False
# 容忍它，所以不算致命，但见 repair_json_string 第 4 条说明）。
_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _loads_ok(text: str) -> bool:
    """按**下游解析器的宽容度**判断这段 JSON 能不能被接受。

    必须用 strict=False，不能用默认的 strict=True：JSON 规范要求字符串里的换行写成
    `\\n`，而模型经常直接敲裸换行（write_script 的 code 参数是多行 Python，最容易出现）。
    下游三个消费方**都**容忍这种写法：
        langchain_core.output_parsers.openai_tools.parse_tool_call(..., strict=False)
        OpenAIToolsAgentOutputParser.strict 默认 False
        src/ai_model/qwen_model._repair_arguments 里的 json.loads(..., strict=False)
    若这里用 strict=True 校验，就会出现「其实已经修好了、但因为还留着裸换行被判失败而
    原样返回」-> 下游 json.loads 撞上未转义的裸双引号 -> OutputParserException，
    等于白修一轮（实测形态：code 里 `assert "系统打印服务" in page_text(driver)`）。
    """
    try:
        json.loads(text, strict=False)
    except ValueError:
        return False
    return True


def _is_json_string_end(rest: str) -> bool:
    """字符串里遇到一个未转义的 `"`，判断它是「字符串正常收尾」还是「代码里的裸双引号」。

    收尾的判据是它后面（跳过空白）接的东西必须是 JSON 结构符：
        `:`（键值分隔）、`}` / `]`（容器结束）、字符串结尾，
        或 `,` 且逗号后面确实跟着一个新值的开头（`"` / `{` / `[` / 数字 / true / false / null）。
    `,` 之所以要再看一眼后面：write_script 的 code 参数里全是 Python 代码，
    `foo("bar", baz)` 这种写法里 `"bar"` 后面同样跟着逗号，但它并不是 JSON 字符串的结尾；
    只有逗号后面是 `"`（下一个键，如 `"file_name"`）时才像真的收尾。
    """
    stripped = rest.lstrip()
    if not stripped:
        return True
    head = stripped[0]
    if head in ":}]":
        return True
    if head != ",":
        return False
    after = stripped[1:].lstrip()
    if not after:
        return True
    return (after[0] in '"{[-0123456789'
            or after.startswith(("true", "false", "null")))


def repair_json_string(raw: str) -> str:
    """逐字符扫描，把 JSON 字符串值里的坏转义与裸双引号修成合法 JSON（不保证一定成功）。

    在字符串内部按四类情况处理：
      1. 合法转义（`\\\"` `\\\\` `\\/` `\\b` `\\f` `\\n` `\\r` `\\t` `\\uXXXX`）原样保留；
      2. 非法转义：`\\'` 去掉反斜杠（JSON 里单引号不用转义），其它 `\\X` 补成 `\\\\X`
         （解析回来仍是 `\\X`，不改变代码语义）；
      3. 未转义的 `"`：按 _is_json_string_end 判断，是收尾就保留，否则补成 `\\\"`；
      4. 裸控制字符（换行 / 制表符）补成 `\\n` / `\\t`：下游虽然用 strict=False 容忍它，
         但转义之后这份修好的载荷在 strict=True 下同样合法 —— 既不再依赖「某个消费方
         恰好开了 strict=False」，也让 _codegen_parsing_error 摆给模型看的
         「已按规则修好的写法（可直接照抄）」真的是一个可以照抄的例子
         （早先那份示例里还留着裸换行，与同一句话里「换行写成 \\n」的要求自相矛盾）。
         转义前后 json.loads 得到的字符串完全一致，不改变代码语义。
    """
    out: list[str] = []
    in_string = False
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if not in_string:
            if char == '"':
                in_string = True
            out.append(char)
            index += 1
            continue

        if char == "\\":
            following = raw[index + 1:index + 2]
            if following in _VALID_JSON_ESCAPES:
                out.append(char + following)
                index += 2
                continue
            if following == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", raw[index + 2:index + 6] or ""):
                out.append(raw[index:index + 6])
                index += 6
                continue
            if following == "'":
                out.append("'")  # JSON 没有 \' 这个转义，单引号本来就不用转义
                index += 2
                continue
            out.append("\\\\" + following)
            index += 2
            continue

        if char in _CONTROL_ESCAPES:
            # 裸换行/制表符 -> 合法转义（见 docstring 第 4 条）
            out.append(_CONTROL_ESCAPES[char])
            index += 1
            continue

        if char == '"':
            if _is_json_string_end(raw[index + 1:]):
                in_string = False
                out.append(char)
            else:
                out.append('\\"')  # 代码里的裸双引号：补转义，别让它提前结束 JSON 字符串
            index += 1
            continue

        out.append(char)
        index += 1
    return "".join(out)


def repair_json_arguments(raw: str) -> str:
    """修复 function calling 入参里的非法 JSON；本来就是合法 JSON 时原样返回。

    三类可机械修复的坏写法（都是实测踩过、模型把 Python 习惯带进 JSON 导致的）：
      1. `\\'`：JSON 没有这个转义（单引号在 JSON 字符串里根本不用转义）-> 去掉反斜杠；
      2. 其它非法转义 `\\X`（如正则里的 `\\s` 被原样写进 JSON）-> 补成 `\\\\X`，
         解析回来仍是 `\\X`，不改变代码语义；
      3. **代码里未转义的裸双引号**（write_script 的 code 参数最常见）-> 补成 `\\"`。
         实测：模型写了 `assert ..., f'未找到含有"东城区"的文本'`，那两个 `"` 直接把
         JSON 字符串提前截断，langchain 报 `Could not parse tool input ... not valid JSON`；
         handle_parsing_errors 只会回一句「Invalid or incomplete response」，模型看不出
         自己错在哪，下一轮原样再犯，连撞 13 次把 15 轮 max_iterations 全烧光，
         脚本一个字都没落盘（第 1、2 类修复对它无效，必须按字符串边界扫描才修得动）。
    逐级尝试、每级都用 _loads_ok 校验（宽容度与下游解析器一致，见该函数说明）；
    全修不好就原样返回，让上层按原来的路径报错（不做无根据的猜测，宁可失败也不写坏文件）。
    """
    if not raw:
        return raw
    if _loads_ok(raw):
        return raw
    fixed = raw.replace("\\'", "'")
    fixed = _INVALID_JSON_ESCAPE.sub(lambda m: "\\\\" + m.group(1), fixed)
    if _loads_ok(fixed):
        return fixed
    scanned = repair_json_string(raw)
    if not _loads_ok(scanned):
        return raw
    return scanned


def repair_tool_call_arguments(message: Any) -> Any:
    """把 AIMessage 里 tool_calls 的 arguments 修成合法 JSON（原地改，返回同一条消息）。

    挂在 `llm_with_tools` 与 OpenAIToolsAgentOutputParser 之间：解析器优先读
    message.tool_calls（chat 集成已解析好的），为空时才回落到
    additional_kwargs["tool_calls"] 并对 arguments 做 json.loads —— 坏转义正是在那里炸的，
    所以这里只需要修 additional_kwargs 这一份原始字符串。
    """
    tool_calls = (getattr(message, "additional_kwargs", None) or {}).get("tool_calls") or []
    for call in tool_calls:
        function = (call or {}).get("function") or {}
        raw = function.get("arguments")
        if not isinstance(raw, str):
            continue
        fixed = repair_json_arguments(raw)
        if fixed != raw:
            function["arguments"] = fixed
            # 不写死「单引号被转义成 \\'」这一种成因：裸双引号（code 里的
            # assert "省电" in text）修好后走的也是这条分支，写死会把排查带偏。
            print(f"""已修复 {function.get('name')} 入参里的非法 JSON\
                （坏转义 / 代码里的裸双引号），本轮不再浪费在解析失败上""")
    return message

