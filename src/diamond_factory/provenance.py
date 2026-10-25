"""引文的保守排版匹配；返回原文位置，不改公式、前提或作者原始输出。"""
import re
from dataclasses import dataclass

VERSION = "literal-presentation-v1"
_TOKEN = re.compile(r"\\(?:\(|\)|\[|\])|\$|\\(?:mathcal|mathfrak|mathbb|mathrm)\s*(?:\{[A-Za-z]\}|[A-Za-z])|\\[A-Za-z]+|\s+|.", re.DOTALL)
_FONT = re.compile(r"(\\(?:mathcal|mathfrak|mathbb|mathrm))\s*(?:\{([A-Za-z])\}|([A-Za-z]))$")
_GREEK = dict(zip("ΓΔΘΛΞΠΣΥΦΨΩ", ("Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega"), strict=True))


@dataclass(frozen=True)
class QuoteMatch:
    start: int
    end: int
    literal: str
    method: str


def _presentation(text):
    units = []
    for token in _TOKEN.finditer(text):
        value = token.group()
        if value in ("$", r"\(", r"\)", r"\[", r"\]"):
            continue
        font = _FONT.fullmatch(value)
        if font:
            value = font[1] + "{" + (font[2] or font[3]) + "}"
        elif value in _GREEK:
            value = "\\" + _GREEK[value]
        # 命令边界保留：不能把 \alpha x 与 \alphax 当成相同公式。
        if value.startswith("\\") and not font:
            value = "⟬" + value[1:] + "⟭"
        units.append((value, token.start(), token.end()))
    output, spans = [], []
    for i, (value, start, end) in enumerate(units):
        if value.isspace():
            before = units[i-1][0][-1:] if i else ""
            after = units[i+1][0][:1] if i+1 < len(units) else ""
            # 保留词/数字边界；仅忽略公式标点周围的排版空白。
            if not (before.isalnum() and after.isalnum()):
                continue
            value = " "
        output.append(value)
        spans.extend([(start, end)] * len(value))
    return "".join(output), spans


def match_quote(quote: str, text: str, *, unique: bool = False) -> QuoteMatch | None:
    """只容忍数学分隔符、空白、单字字体参数和大写希腊字母的呈现差异。

    不做大小写折叠、近似字符串搜索、同义改写、符号/数字/括号删除。
    原文位置及literal始终指向原始文本，不能将匹配称为科学正确性证明。
    """
    if not quote.strip():
        return None
    pos = text.find(quote)
    if pos >= 0:
        if unique and text.find(quote, pos+1) >= 0:
            return None
        return QuoteMatch(pos, pos+len(quote), quote, "exact")
    needle, _ = _presentation(quote)
    body, spans = _presentation(text)
    if not needle:
        return None
    pos = body.find(needle)
    if pos < 0:
        return None
    if unique and body.find(needle, pos+1) >= 0:
        return None
    start, end = spans[pos][0], spans[pos+len(needle)-1][1]
    return QuoteMatch(start, end, text[start:end], VERSION)
