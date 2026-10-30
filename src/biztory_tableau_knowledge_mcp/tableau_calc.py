"""Translate Tableau calculation formulas into portable Ossie SQL (OSSIE_SQL_2026 subset).

Deliberately conservative: it translates the common, unambiguous subset (aggregates, arithmetic,
comparisons, IF/IIF/CASE, null handling, common date/string/math functions) and refuses the rest
(LOD expressions, table calculations, parameters, string concatenation with ``+``, unknown
functions) with a reason. A refused formula is still exported verbatim in the ``TABLEAU`` dialect,
so nothing is lost; it just isn't portable SQL.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Aggregations that make a calculation a metric rather than a row-level field.
AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "STDEV", "STDEVP", "VAR", "VARP"}
TABLE_CALCS = {
    "INDEX", "RANK", "RANK_DENSE", "RANK_MODIFIED", "RANK_PERCENTILE", "RANK_UNIQUE", "LOOKUP",
    "PREVIOUS_VALUE", "TOTAL", "SIZE", "FIRST", "LAST",
}  # fmt: skip
# Same name and argument order in Ossie SQL.
SAME = {
    "SUM", "AVG", "MIN", "MAX", "COUNT", "MEDIAN", "ABS", "ROUND", "FLOOR", "SQRT", "POWER", "EXP",
    "LN", "LOG", "SIGN", "LOWER", "UPPER", "TRIM", "LTRIM", "RTRIM", "LEFT", "RIGHT", "REPLACE",
    "CONTAINS", "STARTSWITH", "ENDSWITH", "YEAR", "MONTH", "DAY", "QUARTER", "WEEK", "COALESCE",
    "NULLIF", "GREATEST", "LEAST",
}  # fmt: skip
RENAMED = {
    "CEILING": "CEIL", "LEN": "LENGTH", "MID": "SUBSTRING", "STDEV": "STDDEV", "STDEVP": "STDDEV_POP",
    "VAR": "VARIANCE", "VARP": "VAR_POP", "IFNULL": "COALESCE",
}  # fmt: skip
CASTS = {
    "DATE": "DATE",
    "DATETIME": "TIMESTAMP",
    "INT": "INTEGER",
    "FLOAT": "DOUBLE PRECISION",
    "STR": "VARCHAR",
}
NILADIC = {"TODAY": "CURRENT_DATE", "NOW": "CURRENT_TIMESTAMP"}

_TOKEN = re.compile(
    r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<ref>\[(?:[^\]]|\]\])*\](?:\.\[(?:[^\]]|\]\])*\])?)
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<date>\#[^#]*\#)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|<>|<=|>=|[-+*/%<>=(),{}:])
  | (?P<ws>\s+)
  | (?P<bad>.)
    """,
    re.VERBOSE | re.DOTALL,
)


class Untranslatable(ValueError):
    """The formula uses something this translator refuses to guess at; the message says what."""


Token = tuple[str, str]  # (kind, text)


def tokenize(formula: str) -> list[Token]:
    out: list[Token] = []
    for m in _TOKEN.finditer(formula):
        kind = m.lastgroup or "bad"
        if kind in ("comment", "ws"):
            continue
        if kind == "bad":
            raise Untranslatable(f"unexpected character {m.group()!r}")
        out.append((kind, m.group()))
    return out


def ref_name(token_text: str) -> str:
    """``[Order Date]`` -> ``Order Date`` (``]]`` unescaped). Qualified refs keep both parts."""
    return token_text[1:-1].replace("]]", "]")


def is_aggregate(formula: str) -> bool:
    """True when an aggregation is applied outside any LOD ``{...}``: the calc is a metric."""
    try:
        toks = tokenize(formula)
    except Untranslatable:
        return False
    depth = 0
    for i, (kind, text) in enumerate(toks):
        if text == "{":
            depth += 1
        elif text == "}":
            depth = max(depth - 1, 0)
        elif (
            depth == 0
            and kind == "ident"
            and text.upper() in AGGREGATES
            and i + 1 < len(toks)
            and toks[i + 1][1] == "("
        ):
            return True
    return False


def referenced_names(formula: str) -> list[str]:
    """Field references in order of appearance, without brackets (deduplicated)."""
    try:
        toks = tokenize(formula)
    except Untranslatable:
        return []
    seen: dict[str, None] = {}
    for kind, text in toks:
        if kind == "ref":
            seen.setdefault(ref_name(text), None)
    return list(seen)


def translate(formula: str, resolve: Callable[[str], str], *, row_level: bool = False) -> str:
    """Translate a Tableau formula to Ossie SQL. ``resolve`` maps a field reference (without
    brackets) to its SQL text, or raises :class:`Untranslatable`.

    ``row_level``: the result is a field expression, so FIXED level-of-detail expressions may become
    window functions (``{FIXED [d]: SUM([x])}`` -> ``SUM(x) OVER (PARTITION BY d)``). Inside a
    metric that would nest a window in an aggregate, so it is refused there."""
    toks = tokenize(formula)
    sql, pos = _Translator(toks, resolve, row_level).expr(0, stop=set())
    if pos != len(toks):
        raise Untranslatable(f"unexpected {toks[pos][1]!r}")
    return sql


class _Translator:
    def __init__(self, toks: list[Token], resolve: Callable[[str], str], allow_lod: bool):
        self.t = toks
        self.resolve = resolve
        self.allow_lod = allow_lod

    def _peek(self, pos: int) -> str:
        return self.t[pos][1].upper() if pos < len(self.t) else ""

    def expr(self, pos: int, stop: set[str]) -> tuple[str, int]:
        """Translate tokens from ``pos`` until a top-level token in ``stop`` (not consumed)."""
        parts: list[str] = []
        while pos < len(self.t):
            kind, text = self.t[pos]
            up = text.upper()
            if up in stop or text in stop:
                break
            if text == "{":
                s, pos = self.lod(pos + 1)
                parts.append(s)
                continue
            if kind == "ref":
                name = ref_name(text)
                if name.lower().startswith("parameters]."):
                    raise Untranslatable(f"parameter reference [{name}]")
                parts.append(self.resolve(name))
                pos += 1
            elif kind == "str":
                body = text[1:-1].replace(text[0] * 2, text[0])
                parts.append("'" + body.replace("'", "''") + "'")
                pos += 1
            elif kind == "date":
                parts.append(f"DATE '{text[1:-1].strip()}'")
                pos += 1
            elif kind == "num":
                parts.append(text)
                pos += 1
            elif kind == "ident" and up == "IF":
                s, pos = self.if_block(pos + 1)
                parts.append(s)
            elif kind == "ident" and up == "CASE":
                s, pos = self.case_block(pos + 1)
                parts.append(s)
            elif kind == "ident" and self._peek(pos + 1) == "(":
                s, pos = self.call(up, pos + 2)
                parts.append(s)
            elif kind == "ident" and up in ("AND", "OR", "NOT", "TRUE", "FALSE", "NULL", "IN", "IS"):
                parts.append(up)
                pos += 1
            elif kind == "ident":
                raise Untranslatable(f"unknown identifier {text!r}")
            elif text == "(":
                inner, pos = self.expr(pos + 1, stop={")"})
                if self._peek(pos) != ")":
                    raise Untranslatable("unbalanced parentheses")
                parts.append(f"({inner})")
                pos += 1
            elif text in ("==", "="):
                parts.append("=")
                pos += 1
            elif text == "+" and self._string_neighbour(pos):
                raise Untranslatable("string concatenation with + (needs CONCAT)")
            elif text in ("!=", "<>", "<", ">", "<=", ">=", "+", "-", "*", "/", "%"):
                parts.append("<>" if text == "!=" else text)
                pos += 1
            elif text == ",":
                raise Untranslatable("unexpected ','")
            else:
                raise Untranslatable(f"unexpected {text!r}")
        return " ".join(parts), pos

    def _string_neighbour(self, pos: int) -> bool:
        return any(0 <= i < len(self.t) and self.t[i][0] == "str" for i in (pos - 1, pos + 1))

    def args(self, pos: int) -> tuple[list[str], list[list[Token]], int]:
        """Translate comma-separated arguments up to the closing ')' (consumed)."""
        out: list[str] = []
        raw: list[list[Token]] = []
        if self._peek(pos) == ")":
            return out, raw, pos + 1
        while True:
            start = pos
            a, pos = self.expr(pos, stop={",", ")"})
            out.append(a)
            raw.append(self.t[start:pos])
            if self._peek(pos) == ",":
                pos += 1
                continue
            if self._peek(pos) == ")":
                return out, raw, pos + 1
            raise Untranslatable("unbalanced function call")

    def call(self, name: str, pos: int) -> tuple[str, int]:
        if name in TABLE_CALCS or name.startswith(("WINDOW_", "RUNNING_")):
            raise Untranslatable(f"table calculation {name}()")
        if name == "ATTR":
            raise Untranslatable("ATTR() has no SQL equivalent")
        if name in NILADIC:
            if self._peek(pos) != ")":
                raise Untranslatable(f"{name}() takes no arguments")
            return NILADIC[name], pos + 1
        a, raw, pos = self.args(pos)
        if name == "COUNTD" and len(a) == 1:
            return f"COUNT(DISTINCT {a[0]})", pos
        if name == "ZN" and len(a) == 1:
            return f"COALESCE({a[0]}, 0)", pos
        if name == "ISNULL" and len(a) == 1:
            return f"({a[0]} IS NULL)", pos
        if name == "IIF" and len(a) == 3:  # the 4th "unknown" arg has no SQL form
            return f"CASE WHEN {a[0]} THEN {a[1]} ELSE {a[2]} END", pos
        if name in CASTS and len(a) == 1:
            return f"CAST({a[0]} AS {CASTS[name]})", pos
        if name in ("DATEADD", "DATEDIFF") and len(a) == 3 and _date_part(raw[0]):
            return f"{name}({_date_part(raw[0])}, {a[1]}, {a[2]})", pos
        if name == "DATETRUNC" and len(a) == 2 and _date_part(raw[0]):
            return f"DATE_TRUNC('{_date_part(raw[0])}', {a[1]})", pos
        if name == "DATEPART" and len(a) == 2 and _date_part(raw[0]):
            return f"DATE_PART('{_date_part(raw[0])}', {a[1]})", pos
        if name in RENAMED:
            return f"{RENAMED[name]}({', '.join(a)})", pos
        if name in SAME:
            return f"{name}({', '.join(a)})", pos
        raise Untranslatable(f"function {name}() is not in the portable subset")

    def lod(self, pos: int) -> tuple[str, int]:
        """{FIXED [d1], [d2] : AGG(x)}  ->  AGG(x) OVER (PARTITION BY d1, d2); {AGG(x)} -> OVER ()."""
        if not self.allow_lod:
            raise Untranslatable(
                "level-of-detail expression inside an aggregation (a window inside an aggregate)"
            )
        kw = self._peek(pos)
        if kw in ("INCLUDE", "EXCLUDE"):
            raise Untranslatable(
                f"{kw} level-of-detail expression (its result depends on the view's dimensions)"
            )
        dims: list[str] = []
        if kw == "FIXED":
            pos += 1
            while self._peek(pos) != ":":
                dim, pos = self.expr(pos, stop={",", ":"})
                dims.append(dim)
                if self._peek(pos) == ",":
                    pos += 1
                elif self._peek(pos) != ":":
                    raise Untranslatable("malformed FIXED expression")
            pos += 1
        name = self._peek(pos)
        if name not in AGGREGATES or self._peek(pos + 1) != "(":
            raise Untranslatable("level-of-detail body is not a single aggregation")
        self.allow_lod = False  # no windows nested inside the window's aggregate
        try:
            body, pos = self.call(name, pos + 2)
        finally:
            self.allow_lod = True
        if self._peek(pos) != "}":
            raise Untranslatable("level-of-detail body is not a single aggregation")
        over = "PARTITION BY " + ", ".join(dims) if dims else ""
        return f"{body} OVER ({over})", pos + 1

    def if_block(self, pos: int) -> tuple[str, int]:
        """IF c THEN v [ELSEIF c THEN v]* [ELSE v] END  ->  CASE WHEN ... END."""
        out = ["CASE"]
        while True:
            cond, pos = self.expr(pos, stop={"THEN"})
            if self._peek(pos) != "THEN":
                raise Untranslatable("IF without THEN")
            val, pos = self.expr(pos + 1, stop={"ELSEIF", "ELSE", "END"})
            out.append(f"WHEN {cond} THEN {val}")
            kw = self._peek(pos)
            if kw == "ELSEIF":
                pos += 1
                continue
            if kw == "ELSE":
                val, pos = self.expr(pos + 1, stop={"END"})
                out.append(f"ELSE {val}")
                kw = self._peek(pos)
            if kw != "END":
                raise Untranslatable("IF without END")
            out.append("END")
            return " ".join(out), pos + 1

    def case_block(self, pos: int) -> tuple[str, int]:
        """CASE [x] WHEN v THEN r ... [ELSE r] END: same shape in SQL."""
        out = ["CASE"]
        if self._peek(pos) != "WHEN":
            subject, pos = self.expr(pos, stop={"WHEN"})
            out.append(subject)
        while self._peek(pos) == "WHEN":
            when, pos = self.expr(pos + 1, stop={"THEN"})
            if self._peek(pos) != "THEN":
                raise Untranslatable("CASE WHEN without THEN")
            val, pos = self.expr(pos + 1, stop={"WHEN", "ELSE", "END"})
            out.append(f"WHEN {when} THEN {val}")
        if self._peek(pos) == "ELSE":
            val, pos = self.expr(pos + 1, stop={"END"})
            out.append(f"ELSE {val}")
        if self._peek(pos) != "END":
            raise Untranslatable("CASE without END")
        out.append("END")
        return " ".join(out), pos + 1


def _date_part(raw: list[Token]) -> str | None:
    """The date part of DATEADD('day', ...) etc., only when it is a plain string literal."""
    if len(raw) == 1 and raw[0][0] == "str":
        part = raw[0][1][1:-1].lower()
        if re.fullmatch(r"[a-z]+", part):
            return part
    return None
