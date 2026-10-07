#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Markdown 测试用例文档解析器（纯 stdlib，各领域通用）。

背景（为什么需要这个模块）：
    `src/web/generate_autoweb.py` 过去把「用例名 + 测试步骤 + 目标脚本名」全部硬编码
    在模块里：

        SCRIPT_NAME = "首页登录.py"
        query = "...执行测试用例 -> 首页登录，测试步骤如下: 1. 打开 https://..."

    而用例本身早已写在 `src/web/testcase/home_page.md`（以及 `src/api/testcase/setting.md`、
    `src/app/testcase/ip.md`）里。两份事实来源必然漂移：md 改了步骤，脚本生成用的还是
    旧步骤；md 里新增的用例（如「行政区域 / 区域名称」）永远没人跑。
    本模块把 md 变成**唯一事实来源**，各领域入口只需 `load_test_cases(path)`。

约定的文档格式（与仓库现有 md 一致，宽容解析）::

    # 1. 首页登录                <- 一级标题
    - 前提条件:
        1.
    - 测试步骤:
        1. 打开 https://...
        2. 输入用户名 hogwarts
    - 预期结果:                   <- 可选章节，缺省即「未提供」
        1. 左侧导航栏包含"首页"

    # 2. 行政区域                <- 只有标题、没有章节的节点是「分组」，不算用例
    ## 2.1 区域名称              <- 二级标题，用例名取各级标题拼接
    - 前提条件:
        1. 首页登录
    - 测试步骤:
        1. ...

命名规则（用户约定）：
    用例名 = 各级标题去掉编号前缀后用 `_` 连接，即
    「一级标题_二级标题_三级标题_四级标题_...」；有几层写几层：
        `# 1. 首页登录`            -> 首页登录          -> 首页登录.py
        `## 2.1 区域名称`          -> 行政区域_区域名称  -> 行政区域_区域名称.py
        `### 3.1.2 新增区域`       -> 行政区域_区域名称_新增区域 -> 同名 .py

设计约定（与 src/utils/script_tools.py 保持一致）：
    1. 只依赖 stdlib，不 import langchain / selenium，所以放在 src/utils 下各领域都能复用；
    2. 解析失败、找不到用例都抛带**可选值清单**的异常（ValueError / FileNotFoundError），
       让使用者一眼看到该怎么改，而不是拿到一个空列表后在下游莫名其妙地失败；
    3. `TestCase` 是 frozen dataclass（字段全为 tuple），可安全地跨模块传递、
       也能直接塞进 LCEL chain 的 inputs 里。

已知取舍：
    标题里的编号前缀是按「数字 + 点」识别的（`1.` / `2.1` / `3.1.2`），因此形如
    `# 3 个用例的批量检查` 这种「数字开头但并非编号」的标题会被误剥成
    `个用例的批量检查`。原始标题保存在 `TestCase.raw_title` 里，需要时可回溯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

# ---- 章节别名：md 是手写的，同一个意思常见好几种写法，全部归一到三类 ----
# 匹配前会做归一化（去空白与 `*`/`_` 强调符、转小写），所以英文别名写不写空格都行。
PRECONDITION_KEYS: tuple[str, ...] = (
    "前提条件", "前置条件", "预置条件", "前提", "precondition", "preconditions",
)
STEP_KEYS: tuple[str, ...] = (
    "测试步骤", "操作步骤", "执行步骤", "步骤", "test steps", "steps",
)
EXPECTED_KEYS: tuple[str, ...] = (
    "预期结果", "期望结果", "预计结果", "预期", "expected results", "expected result",
)


def _normalize_key(text: str) -> str:
    """章节名归一化：去空白、去 markdown 强调符号、转小写（中英混写都能命中）。"""
    return re.sub(r"[\s_*·]+", "", text or "").lower()


# 归一化后的章节名 -> TestCase 的字段名
SECTION_FIELDS: dict[str, str] = {
    _normalize_key(key): field
    for keys, field in ((PRECONDITION_KEYS, "preconditions"),
                        (STEP_KEYS, "steps"),
                        (EXPECTED_KEYS, "expected"))
    for key in keys
}
# 别名按长度倒序匹配：「测试步骤」要先于「步骤」命中，否则会被短别名截胡
_SECTION_ALIASES: tuple[str, ...] = tuple(sorted(SECTION_FIELDS, key=len, reverse=True))

# 标题行：# ~ ######，允许结尾再带一串 #（ATX 闭合写法）
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.*?)\s*#*\s*$")
# 章节行：`- 前提条件:` / `**测试步骤**：` / `3. 预期结果: 首条内容`
_SECTION_RE = re.compile(
    r"^\s*(?:[-*+]\s*|\d+[.、)）]\s*)?(?:\*\*|__)?\s*(?P<key>[^\s:：][^:：]{1,24}?)\s*"
    r"(?:\*\*|__)?\s*[:：]\s*(?P<rest>.*)$"
)
# 列表项行：`1. xxx` / `- xxx` / `* xxx`（空项如 `1. ` 也会被匹配，text 为空串）
_ITEM_RE = re.compile(r"^\s*(?:(?P<num>\d+)[.、)）]|(?P<bullet>[-*+]))\s*(?P<text>.*)$")
# 标题编号前缀：`1.` / `2.1` / `3.1.2` / `1、` / `2)`，后面可跟空白
_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+(?:\.\d+)*[.、)）]?\s*")
# 文件名非法字符（含全角空格）统一换成下划线
_UNSAFE_NAME_RE = re.compile(r"[\\/:*?\"<>|\s\u3000]+")
# 匹配用例名时忽略的分隔符：让「行政区域/区域名称」「行政区域-区域名称」都能命中
_SEPARATOR_RE = re.compile(r"[\s_\-/\\.、,，:：>》]+")

DEFAULT_CASE_NAME = "未命名用例"


def strip_number_prefix(title: str) -> str:
    """去掉标题里的编号前缀：`1. 首页登录` -> `首页登录`，`2.1 区域名称` -> `区域名称`。"""
    return _NUMBER_PREFIX_RE.sub("", (title or "").strip()).strip()


def sanitize_name(text: str) -> str:
    """把标题清洗成可作文件名 / 脚本名的一部分（保留中文，非法字符换成 `_`）。"""
    cleaned = _UNSAFE_NAME_RE.sub("_", (text or "").strip()).strip("_.")
    return cleaned or DEFAULT_CASE_NAME


def match_key(text: str) -> str:
    """用例名匹配用的归一化键：去掉分隔符与空白、转小写。"""
    return _SEPARATOR_RE.sub("", (text or "")).lower()


@dataclass(frozen=True)
class TestCase:
    """一条从 markdown 解析出来的测试用例。

    Attributes:
        title_path: 各级标题（已去编号前缀并清洗），如 ("行政区域", "区域名称")；
        level: 用例自身标题的层级（1 表示 `#`）；
        raw_title: 标题原文（含编号），排查解析问题时用；
        preconditions / steps / expected: 前提条件 / 测试步骤 / 预期结果，
            空条目（如 md 里占位的 `1. `）已被过滤；
        source: 来源文件路径字符串；
        line_no: 用例标题在文件中的行号（1-based），报错提示里用来定位；
        seq: 重名序号（默认 1）；同名用例第 2 条起为 2、3 ...，只影响 name/script_name，
            不污染 title_path（否则标题层级会显示成「首页登录 > 2」这种假层级）。
    """

    title_path: tuple[str, ...]
    level: int = 1
    raw_title: str = ""
    preconditions: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    expected: tuple[str, ...] = ()
    source: str = ""
    line_no: int = 0
    seq: int = 1

    @property
    def name(self) -> str:
        """用例名 = 「一级标题_二级标题_三级标题_...」（有几层拼几层），重名再加 `_序号`。"""
        base = "_".join(self.title_path) if self.title_path else DEFAULT_CASE_NAME
        return base if self.seq <= 1 else f"{base}_{self.seq}"

    @property
    def title(self) -> str:
        """最末一级标题（即用例本身的短名）。"""
        return self.title_path[-1] if self.title_path else (self.raw_title or DEFAULT_CASE_NAME)

    @property
    def script_name(self) -> str:
        """目标脚本文件名：`行政区域_区域名称.py`。"""
        return f"{self.name}.py"

    @property
    def hierarchy(self) -> str:
        """标题层级的可读形式：`行政区域 > 区域名称`。"""
        return " > ".join(self.title_path)

    @property
    def is_runnable(self) -> bool:
        """是否具备「可执行」的最小信息：有测试步骤，或至少有预期结果。"""
        return bool(self.steps or self.expected)

    def sections(self) -> dict[str, tuple[str, ...]]:
        """三个章节的内容（字段名与 dataclass 一致，便于调用方按需渲染）。"""
        return {
            "preconditions": self.preconditions,
            "steps": self.steps,
            "expected": self.expected,
        }

    def render(self, *, indent: str = "") -> str:
        """渲染成给 LLM 看的用例描述（前提条件 / 测试步骤 / 预期结果，编号列表）。

        空章节也会输出一行「无 / 用例文档未提供」：明确告知「没有」比留白更好，
        否则模型会自行脑补前提条件（例如凭空加一段登录流程）。
        """
        lines = [f"用例名称：{self.name}", f"标题层级：{self.hierarchy}"]
        for label, items, empty_hint in (
            ("前提条件", self.preconditions, "无"),
            ("测试步骤", self.steps, "用例文档未提供"),
            # 很多 md 把断言写在测试步骤里（「...断言主页左侧导航栏包含...」）而不单列
            # 「预期结果」章节；这行提示把模型的注意力引回步骤里的断言，避免它以为
            # 「没有预期结果 = 不用断言」。
            ("预期结果", self.expected,
             "未单独列出，以「测试步骤」中写明「断言 ...」的内容为准"),
        ):
            lines.append(f"{label}：" + ("" if items else empty_hint))
            lines.extend(f"{index}. {item}" for index, item in enumerate(items, 1))
        return ("\n" + indent).join(lines) if indent else "\n".join(lines)

    def summary(self) -> str:
        """一行摘要，用于 `--list-cases` 与报错提示里的可选值清单。"""
        return (f"{self.name}  <-  {self.hierarchy}"
                f"（前提条件 {len(self.preconditions)} 条、测试步骤 {len(self.steps)} 条、"
                f"预期结果 {len(self.expected)} 条）->  {self.script_name}")

    def candidate_keys(self) -> tuple[str, ...]:
        """本用例可被匹配到的所有写法（归一化后）：全名、各级后缀、末级标题、原始标题。"""
        keys = [match_key(self.name), match_key(self.script_name)]
        for start in range(len(self.title_path)):
            keys.append(match_key("_".join(self.title_path[start:])))
        keys.append(match_key(self.title))
        keys.append(match_key(self.raw_title))
        return tuple(key for key in dict.fromkeys(keys) if key)


@dataclass
class _Node:
    """解析过程中的中间节点：一个标题 + 它名下已识别的三个章节。

    `current_section` 记录「当前正在收集哪个章节」：md 里的列表项本身不带章节信息，
    必须靠它把 `1. 打开 ...` 归到测试步骤而不是前提条件。
    """

    path: list[str]
    level: int
    raw_title: str
    line_no: int
    sections: dict[str, list[str]]
    current_section: Optional[str] = None

    def add_item(self, text: str) -> None:
        """往当前章节追加一条内容（还没有章节时忽略，见 parse_test_cases 的说明）。"""
        if self.current_section:
            self.sections[self.current_section].append(text)

    def append_to_last_item(self, text: str) -> None:
        """把普通文本行拼到上一条内容后面（md 里长步骤常被手工折行）。"""
        if not self.current_section:
            return
        items = self.sections[self.current_section]
        if items:
            items[-1] = f"{items[-1]} {text}".strip()


def _match_section(line: str) -> Optional[tuple[str, str]]:
    """识别章节标题行，返回 (字段名, 同行首条内容)；不是章节则返回 None。

    必须做别名白名单校验：`1. 打开 https://xxx` 这种步骤里也带冒号，
    只看「冒号前有一段文字」会把 URL 前的内容误判成章节名。
    """
    matched = _SECTION_RE.match(line)
    if not matched:
        return None
    key = _normalize_key(matched.group("key"))
    for alias in _SECTION_ALIASES:
        # endswith 兜住 `**测试步骤**`、`用例测试步骤` 这类前后带修饰的写法
        if key == alias or key.endswith(alias):
            return SECTION_FIELDS[alias], (matched.group("rest") or "").strip()
    return None


def _flush_node(node: _Node, seen_names: dict[str, int], source: str) -> list[TestCase]:
    """把一个解析节点转成 TestCase（不是用例则返回空列表），并消除重名。"""
    case = TestCase(
        title_path=tuple(node.path),
        level=node.level,
        raw_title=node.raw_title,
        preconditions=tuple(node.sections.get("preconditions") or ()),
        steps=tuple(node.sections.get("steps") or ()),
        expected=tuple(node.sections.get("expected") or ()),
        source=source,
        line_no=node.line_no,
    )
    if not case.is_runnable:
        return []
    count = seen_names.get(case.name, 0) + 1
    seen_names[case.name] = count
    if count > 1:
        # 重名 -> 加序号：用例名直接决定脚本文件名，重名会让两条用例互相覆盖同一个 .py
        case = replace(case, seq=count)
    return [case]


def parse_test_cases(text: str, source: Union[str, Path] = "") -> list[TestCase]:
    """解析 markdown 文本，返回全部**用例**节点（文档顺序）。

    判定规则：
        1. 只有带「测试步骤」或「预期结果」的标题节点才算用例；
           `# 2. 行政区域` 这种纯分组标题（以及 `# 2. ***` 占位）会被跳过，
           但它会作为子用例名的前缀参与拼接（行政区域_区域名称）；
        2. 章节归属于**最近的一个标题**，遇到新标题即切换；
        3. 章节内的列表项按顺序收集，空项（`1. ` 后面没内容）过滤掉；
           非列表的普通文本行视为上一条目的续行（用单个空格拼接）；
        4. 标题之前出现的内容无处归属，直接忽略（宽容解析，不抛异常）；
        5. 同名用例自动加后缀 `_2` / `_3`，保证「用例名 == 脚本名」仍是一一对应。
    """
    stack: list[_Node] = []
    cases: list[TestCase] = []
    seen_names: dict[str, int] = {}
    source_text = str(source or "")

    for line_no, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.rstrip()
        if not line.strip():
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group("hashes"))
            # 弹出层级 >= 当前的节点：剩下的就是新标题的祖先链（它们的名字要做前缀）
            while stack and stack[-1].level >= level:
                cases.extend(_flush_node(stack.pop(), seen_names, source_text))
            path = [node.path[-1] for node in stack]
            path.append(sanitize_name(strip_number_prefix(heading.group("title"))))
            stack.append(_Node(path=path, level=level,
                               raw_title=heading.group("title").strip(), line_no=line_no,
                               sections={"preconditions": [], "steps": [], "expected": []}))
            continue

        if not stack:
            continue

        node = stack[-1]
        section = _match_section(line)
        if section is not None:
            field, first_item = section
            node.current_section = field
            if first_item:
                node.add_item(first_item)
            continue

        item = _ITEM_RE.match(line)
        if item:
            content = (item.group("text") or "").strip()
            if content:
                node.add_item(content)
            continue

        node.append_to_last_item(line.strip())

    while stack:
        cases.extend(_flush_node(stack.pop(), seen_names, source_text))
    # 出栈顺序是「子标题先于父标题」，按行号重排一次，保证返回值就是文档顺序
    cases.sort(key=lambda case: case.line_no)
    return cases


def load_test_cases(path: Union[str, Path]) -> list[TestCase]:
    """读取并解析 markdown 用例文档；文件缺失 / 解析不出用例都抛带提示的异常。

    这里选择「快速失败」而不是返回空列表：调用方（generate_autoweb.py）要用第一条
    用例推导 SCRIPT_NAME，空列表只会在下游变成 IndexError，排查成本高得多。
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(
            f"测试用例文档不存在：{file_path}。请确认路径，"
            f"或用 --case-file=<md 路径> 指定其它用例文档（如 src/app/testcase/ip.md）"
        )
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"测试用例文档不是 utf-8 编码，无法解析：{file_path}（{exc}）") from exc
    cases = parse_test_cases(text, source=file_path)
    if not cases:
        raise ValueError(
            f"测试用例文档里没有解析到任何用例：{file_path}。"
            f"一条用例需要「标题 + 测试步骤（或预期结果）」，形如："
            f"`# 1. 首页登录` 换行 `- 测试步骤:` 换行 `    1. 打开 ...`"
        )
    return cases


def describe_test_cases(cases: Sequence[TestCase]) -> str:
    """用例清单（多行），用于 `--list-cases` 输出与「找不到用例」的报错提示。"""
    if not cases:
        return "（没有可用用例）"
    lines = [f"共 {len(cases)} 条用例："]
    lines.extend(f"  {index}. {case.summary()}" for index, case in enumerate(cases, 1))
    return "\n".join(lines)


def find_test_case(cases: Sequence[TestCase], keyword: str) -> TestCase:
    """按名字（宽松匹配）从用例列表里挑一条。

    支持的写法（大小写不敏感，分隔符 `_ - / . 空格 >` 等价）：
        行政区域_区域名称 / 行政区域/区域名称 / 区域名称（末级标题）/ 行政区域_区域名称.py

    分三级匹配，命中即停（级别越靠前越精确）：
        1. 用例全名完全相等 —— 「首页登录」优先选中它本身，而不是被重名后缀的
           「首页登录_2」的标题后缀一起命中（否则去重后反而选不中第一条）；
        2. 全名 / 脚本名 / 各级标题后缀 / 原始标题 完全相等；
        3. 全名子串匹配（`--case 区域` 这种简写）。
    某一级匹配到多条时报「歧义」并列出候选，三级都没命中时报错并列出全部用例——
    两种情况都把可选值给全，使用者不必回头翻 md。
    """
    if not cases:
        raise ValueError("用例列表为空，无法选择用例")

    key = match_key(keyword)
    if not key:
        raise ValueError(f"未指定用例名，可选用例：\n{describe_test_cases(cases)}")

    for keyfunc in (
        lambda case: (match_key(case.name) == key),
        lambda case: (key in case.candidate_keys()),
        lambda case: (key in match_key(case.name)),
    ):
        hits = [case for case in cases if keyfunc(case)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = "、".join(case.name for case in hits)
            raise ValueError(
                f"用例名「{keyword}」有歧义，同时匹配到：{names}；请写完整的层级名"
                f"（如 {hits[0].name}），可选用例：\n{describe_test_cases(cases)}"
            )
    raise ValueError(
        f"在用例文档里找不到用例「{keyword}」，可选用例：\n{describe_test_cases(cases)}"
    )



def select_test_cases(
    cases: Sequence[TestCase],
    keyword: Optional[str] = None,
    *,
    select_all: bool = False,
) -> list[TestCase]:
    """把「命令行 / 环境变量给的用例选择」解析成本次要跑的用例列表。

    Args:
        cases: 文档里解析出的全部用例；
        keyword: 用例名（None / 空串表示没指定）；`all` / `*` / `全部` 等价于 select_all；
        select_all: True 时返回全部用例（`--all-cases`）。

    Returns:
        选中的用例列表；未指定 keyword 且 select_all=False 时取文档里的第一条。

    注意：`select_all=False` 只是**工具函数层面的保守默认**，不等于各领域入口该有的默认
    行为。入口如果希望「不带参数就把文档里的用例全跑一遍」，必须显式传 select_all=True
    （src/web/generate_autoweb.py 就是这么做的：不带 --case 时 RUN_ALL_CASES 为真）——
    否则 md 里第二条及以后新增的用例永远不会被执行，看起来像「用例场景丢了」，
    实际只是没被选中（--list-cases 仍能完整列出，很容易误判成解析问题）。
    """
    all_cases = list(cases)
    if select_all:
        return all_cases
    name = (keyword or "").strip()
    if name.lower() in {"all", "*", "全部"}:
        return all_cases
    if not name:
        return all_cases[:1]
    return [find_test_case(all_cases, name)]


def iter_test_cases(path: Union[str, Path]) -> Iterable[TestCase]:
    """便捷迭代器：`for case in iter_test_cases(md): ...`。"""
    return iter(load_test_cases(path))


if __name__ == "__main__":
    # 不依赖 LLM / 网络的最小自测：
    #   1) 仓库里三份真实用例文档都能解析，命名符合「一级_二级_...」约定；
    #   2) 内嵌一份三/四级标题的文档，验证更深层级、「分组标题不算用例」、重名去重；
    #   3) find_test_case 的宽松匹配、歧义与未命中报错。
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    # 自测块里的临时变量统一加下划线前缀：这里是模块级作用域，用 cases/path 这类
    # 名字会与函数形参同名，产生「遮蔽」告警（与 script_tools.py 的自测块约定一致）。
    for _md in ("src/web/testcase/home_page.md",
                "src/api/testcase/setting.md",
                "src/app/testcase/ip.md"):
        _path = _REPO_ROOT / _md
        if not _path.is_file():
            print(f"跳过（文件不存在）：{_path}")
            continue
        _cases = load_test_cases(_path)
        print(f"\n=== {_path} ===")
        print(describe_test_cases(_cases))
        for _case in _cases:
            print(f"--- {_case.script_name}（第 {_case.line_no} 行，level={_case.level}）---")
            print(_case.render())

    _DEEP_MD = "\n".join([
        "# 1. 行政区域",
        "## 1.1 区域名称",
        "### 1.1.1 新增",
        "#### 1.1.1.1 名称重复",
        "- 前提条件:",
        "    1. 首页登录",
        "    2. ",
        "- 测试步骤:",
        "    1. 点击「行政区域」菜单",
        "    2. 输入已存在的区域名称",
        "- 预期结果:",
        "    1. 提示「名称已存在」",
        "# 2. 只有标题没有步骤的分组",
        "# 3. 首页登录",
        "- 测试步骤:",
        "    1. 打开登录页",
        "# 4. 首页登录",
        "- 测试步骤:",
        "    1. 再打开一次登录页（与上一条重名，验证自动加后缀）",
    ])
    print("\n=== 内嵌多层标题用例 ===")
    _deep_cases = parse_test_cases(_DEEP_MD, source="<inline>")
    print(describe_test_cases(_deep_cases))
    assert [case.name for case in _deep_cases] == [
        "行政区域_区域名称_新增_名称重复", "首页登录", "首页登录_2",
    ], _deep_cases
    assert _deep_cases[0].script_name == "行政区域_区域名称_新增_名称重复.py"
    # 空占位项（`2. `）被过滤，前提条件只剩「首页登录」
    assert _deep_cases[0].preconditions == ("首页登录",), _deep_cases[0].preconditions
    assert _deep_cases[0].expected == ("提示「名称已存在」",), _deep_cases[0].expected
    assert _deep_cases[0].level == 4 and _deep_cases[0].line_no == 4
    # 纯分组标题（没有测试步骤 / 预期结果）不产生用例，但会作为子用例名的前缀
    assert "只有标题没有步骤的分组" not in [case.name for case in _deep_cases]
    assert _deep_cases[0].hierarchy == "行政区域 > 区域名称 > 新增 > 名称重复"

    print("\n=== 宽松匹配 ===")
    for _keyword in ("行政区域_区域名称_新增_名称重复", "行政区域/区域名称/新增/名称重复",
                     "名称重复", "行政区域_区域名称_新增_名称重复.py", "区域",
                     "首页登录", "首页登录_2"):
        print(f"{_keyword!r} -> {find_test_case(_deep_cases, _keyword).script_name}")
    # 「首页」同时命中 首页登录 / 首页登录_2 -> 歧义；「不存在」-> 未命中；"" -> 未指定
    for _bad in ("首页", "不存在的用例", ""):
        try:
            find_test_case(_deep_cases, _bad)
        except ValueError as _exc:
            print(f"{_bad!r} -> ValueError: {str(_exc).splitlines()[0]}")
    # 重名去重只改 name / script_name，不改标题层级
    assert _deep_cases[2].hierarchy == "首页登录", _deep_cases[2].hierarchy
    assert _deep_cases[2].seq == 2 and _deep_cases[2].script_name == "首页登录_2.py"

    print("\n=== select_test_cases ===")
    print(f"不指定 -> {[case.name for case in select_test_cases(_deep_cases)]}")
    print(f"--all-cases -> {[case.name for case in select_test_cases(_deep_cases, select_all=True)]}")
    print(f"case=首页登录 -> {[case.name for case in select_test_cases(_deep_cases, '首页登录')]}")

    print("\n自测完成")





