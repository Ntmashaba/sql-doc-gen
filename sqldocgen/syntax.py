"""Helpers over the ScriptDom JSON tree emitted by the helper.

Each node is a dict: {"$": class name, "@": [offset, length, line, column], <properties>}.
Offsets are UTF-16 code units; ``Text`` converts them to Python string positions.
"""
from __future__ import annotations

from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple

from .textutil import LineIndex, Utf16Map

Node = Dict


def typ(n) -> Optional[str]:
    return n.get("$") if isinstance(n, dict) else None


def is_a(n, *names) -> bool:
    return isinstance(n, dict) and n.get("$") in names


def kids(n) -> Iterator[Node]:
    if not isinstance(n, dict):
        return
    for k, v in n.items():
        if k == "$" or k == "@":
            continue
        if isinstance(v, dict):
            yield v
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    yield x


def walk(n, stop=None) -> Iterator[Node]:
    """Pre-order walk without recursion. ``stop(node)`` true: yield the node but not its children."""
    stack = [n]
    while stack:
        cur = stack.pop()
        if not isinstance(cur, dict):
            continue
        yield cur
        if stop is not None and cur is not n and stop(cur):
            continue
        ch = list(kids(cur))
        ch.reverse()
        stack.extend(ch)


def find(n, *names, stop=None) -> List[Node]:
    return [x for x in walk(n, stop) if x.get("$") in names]


def ident(n) -> Optional[str]:
    if not isinstance(n, dict):
        return None
    if n.get("$") == "Identifier" or "Value" in n and n.get("$") in ("IdentifierOrValueExpression",):
        return n.get("Value")
    if "Value" in n:
        return n.get("Value")
    return None


def parts(n) -> List[str]:
    """Identifier values of a MultiPartIdentifier / SchemaObjectName ('' for an omitted part)."""
    if not isinstance(n, dict):
        return []
    return [(i.get("Value") or "") if isinstance(i, dict) else "" for i in n.get("Identifiers", [])]


class ObjName(NamedTuple):
    server: str
    database: str
    schema: str
    name: str

    @property
    def display(self) -> str:
        return ".".join(p for p in (self.server, self.database, self.schema, self.name) if p)


def obj_name(son) -> ObjName:
    p = parts(son)
    p = [""] * (4 - len(p)) + p[-4:]
    return ObjName(*p)


class Text:
    """A parsed text with position helpers."""

    def __init__(self, text: str):
        self.text = text
        self.umap = Utf16Map(text)
        self.lines = LineIndex(text)

    def span(self, n) -> Optional[Tuple[int, int]]:
        if not isinstance(n, dict) or "@" not in n:
            return None
        off, length = n["@"][0], n["@"][1]
        if off is None or off < 0:
            # some list containers carry no tokens: use their children
            ch = [self.span(c) for c in kids(n)]
            ch = [c for c in ch if c]
            if not ch:
                return None
            return min(c[0] for c in ch), max(c[1] for c in ch)
        s = self.umap.to_cp(off)
        e = self.umap.to_cp(off + length)
        return s, e

    def of(self, n, limit: Optional[int] = None) -> str:
        sp = self.span(n)
        if not sp:
            return ""
        t = self.text[sp[0]:sp[1]]
        if limit and len(t) > limit:
            t = t[:limit].rstrip() + " …"
        return t

    def squeeze(self, n, limit: int = 160) -> str:
        """Source text of a node on one line (runs of whitespace collapsed)."""
        t = " ".join(self.of(n).split())
        if limit and len(t) > limit:
            t = t[:limit].rstrip() + " …"
        return t

    def line_range(self, n) -> Optional[Tuple[int, int]]:
        return self.lines.lines(self.span(n))

    def to_u16_span(self, sp):
        if not sp:
            return None
        return [self.umap.to_u16(sp[0]), self.umap.to_u16(sp[1])]


def data_type_text(dt) -> str:
    """'varchar(50)', 'decimal(18,2)', 'dbo.MyType', 'xml' from a DataTypeReference."""
    if not isinstance(dt, dict):
        return ""
    t = dt.get("$")
    name = ".".join(p for p in parts(dt.get("Name")) if p)
    if t == "SqlDataTypeReference":
        base = (name or dt.get("SqlDataTypeOption", "")).lower()
        params = []
        for p in dt.get("Parameters", []) or []:
            if p.get("$") == "MaxLiteral":
                params.append("max")
            else:
                params.append(str(p.get("Value", "")))
        return f"{base}({','.join(params)})" if params else base
    if t == "XmlDataTypeReference":
        return "xml"
    if t == "UserDataTypeReference":
        params = [str(p.get("Value", "")) for p in dt.get("Parameters", []) or []]
        return f"{name}({','.join(params)})" if params else name
    return name


def literal_value(n):
    """Python value of a literal node, or None."""
    t = typ(n)
    if t in ("IntegerLiteral",):
        try:
            return int(n.get("Value", "0"))
        except ValueError:
            return n.get("Value")
    if t in ("NumericLiteral", "RealLiteral", "MoneyLiteral"):
        return n.get("Value")
    if t == "StringLiteral":
        return n.get("Value", "")
    if t == "NullLiteral":
        return None
    return None


def unparen(n):
    """Strip ParenthesisExpression / BooleanParenthesisExpression wrappers."""
    while typ(n) in ("ParenthesisExpression", "BooleanParenthesisExpression", "QueryParenthesisExpression"):
        n = n.get("Expression") or n.get("QueryExpression")
    return n
