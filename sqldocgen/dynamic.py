"""Rebuild the text of dynamic SQL from the code that assembles it.

For ``EXEC (@sql)`` and ``sp_executesql @sql`` the string is followed back through the
assignments that can reach the EXEC (``SET @sql = 'SELECT …' + @where``). Literal pieces
are kept; a piece that is only known at run time (a parameter, a value read from a table)
becomes a placeholder token, so the statement can still be parsed and its shape documented.
When the placeholder ends up as an object name, the target is reported as built at run time.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .engine import Ctx, norm
from .model import VALUE
from .syntax import literal_value, typ

MAX_VARIANTS = 6
TOKEN = "__sqldocgen_{}__"


@dataclass
class Variant:
    pieces: List[object]                      # str or Placeholder
    def text(self, tokens) -> str:
        out = []
        for p in self.pieces:
            if isinstance(p, Placeholder):
                out.append(tokens(p))
            else:
                out.append(p)
        return "".join(out)


@dataclass(frozen=True)
class Placeholder:
    label: str                                # what the run-time value comes from


class Rebuilder:
    def __init__(self, ctx: Ctx, flow: dict):
        self.ctx = ctx
        self.flow = flow
        self.visiting = set()

    def defs_at(self, var_key: str, step_id: str) -> List[int]:
        mask = self.flow["IN"].get(step_id, 0) & self.flow["key_mask"].get((var_key, VALUE), 0)
        return self.flow["ids_of"](mask)

    def value(self, e, step_id: str, ns: str, depth: int = 0) -> List[List[object]]:
        """Possible values of an expression as lists of pieces."""
        if depth > 40:
            return [[Placeholder("expression")]]
        t = typ(e)
        if t == "StringLiteral":
            return [[e.get("Value", "")]]
        if t in ("IntegerLiteral", "NumericLiteral", "RealLiteral", "MoneyLiteral"):
            return [[str(e.get("Value", ""))]]
        if t == "NullLiteral":
            return [[""]]
        if t == "ParenthesisExpression":
            return self.value(e.get("Expression"), step_id, ns, depth + 1)
        if t == "BinaryExpression" and e.get("BinaryExpressionType") == "Add":
            a = self.value(e.get("FirstExpression"), step_id, ns, depth + 1)
            b = self.value(e.get("SecondExpression"), step_id, ns, depth + 1)
            return [x + y for x, y in itertools.islice(itertools.product(a, b), MAX_VARIANTS)]
        if t == "VariableReference":
            return self.variable(e.get("Name") or "", step_id, ns, depth)
        if t in ("CastCall", "ConvertCall", "TryCastCall", "TryConvertCall"):
            inner = e.get("Parameter")
            if typ(inner) in ("StringLiteral", "VariableReference", "BinaryExpression"):
                return self.value(inner, step_id, ns, depth + 1)
            return [[Placeholder(f"CONVERT({self._label(inner)})")]]
        if t == "FunctionCall":
            name = ((e.get("FunctionName") or {}).get("Value") or "").upper()
            params = e.get("Parameters", []) or []
            if name == "QUOTENAME" and params:
                inner = self.value(params[0], step_id, ns, depth + 1)
                return [["["] + v + ["]"] for v in inner]
            if name in ("CONCAT",):
                acc = [[]]
                for p in params:
                    vals = self.value(p, step_id, ns, depth + 1)
                    acc = [x + y for x, y in itertools.islice(itertools.product(acc, vals), MAX_VARIANTS)]
                return acc
            if name in ("ISNULL", "COALESCE") and params:
                return self.value(params[0], step_id, ns, depth + 1)
            if name in ("CHAR", "NCHAR") and params:
                code = literal_value(params[0])
                if isinstance(code, int):
                    return [[chr(code)]]
            if name in ("LTRIM", "RTRIM", "TRIM", "UPPER", "LOWER") and params:
                return self.value(params[0], step_id, ns, depth + 1)
            if name == "REPLACE" and len(params) == 3 and all(typ(p) == "StringLiteral" for p in params[1:]):
                vals = self.value(params[0], step_id, ns, depth + 1)
                old, new = params[1].get("Value", ""), params[2].get("Value", "")
                out = []
                for v in vals:
                    if all(isinstance(p, str) for p in v):
                        out.append(["".join(v).replace(old, new)])
                    else:
                        out.append([Placeholder("REPLACE(…)")])
                return out
            return [[Placeholder(f"{name}(…)")]]
        if t == "LeftFunctionCall" or t == "RightFunctionCall":
            return [[Placeholder("LEFT/RIGHT(…)")]]
        if t == "ColumnReferenceExpression":
            return [[Placeholder("a column value")]]
        return [[Placeholder(self._label(e))]]

    def _label(self, e) -> str:
        if typ(e) == "VariableReference":
            return e.get("Name") or "a variable"
        return "an expression"

    def variable(self, name: str, step_id: str, ns: str, depth: int) -> List[List[object]]:
        key = self.ctx.var_key(name, ns)
        if (key, step_id) in self.visiting:
            return [[Placeholder(f"{name} (built in a loop)")]]
        self.visiting.add((key, step_id))
        try:
            defs = self.defs_at(key, step_id)
            if not defs:
                return [[Placeholder(name)]]
            out: List[List[object]] = []
            for nid in defs:
                n = self.ctx.nodes[nid]
                if n.op == "initial":
                    out.append([Placeholder(name)])
                    continue
                ast = getattr(n, "ast", None)
                if ast is None:
                    if n.expr == "NULL" and n.op == "declare":
                        continue
                    out.append([Placeholder(name)])
                    continue
                out.extend(self.value(ast, n.step, ns, depth + 1))
                if len(out) >= MAX_VARIANTS:
                    break
            return out[:MAX_VARIANTS] or [[Placeholder(name)]]
        finally:
            self.visiting.discard((key, step_id))


def rebuild(ctx: Ctx, flow: dict, step) -> Tuple[List[str], str, List[str]]:
    """Variants of the SQL run by a dynamic EXEC step: (texts, status, placeholder labels).

    status: resolved (every piece is literal), partial (some pieces are run-time values) or
    unresolved (nothing could be rebuilt)."""
    from .program import exec_spec_of
    ent = exec_spec_of(step.node).get("ExecutableEntity") or {}
    rb = Rebuilder(ctx, flow)
    if typ(ent) == "ExecutableStringList":
        exprs = ent.get("Strings", []) or []
    else:
        params = ent.get("Parameters", []) or []
        exprs = [params[0].get("ParameterValue")] if params and params[0].get("ParameterValue") is not None else []
    if not exprs:
        return [], "unresolved", []
    variants: List[List[object]] = [[]]
    for e in exprs:
        vals = rb.value(e, step.id, step.namespace)
        variants = [x + y for x, y in itertools.islice(itertools.product(variants, vals), MAX_VARIANTS)]
    texts, labels = [], []
    any_literal = False
    for v in variants:
        tokens = {}

        def tok(p):
            if p not in tokens:
                tokens[p] = TOKEN.format(len(tokens) + 1)
                labels.append(p.label)
            return tokens[p]

        txt = "".join(tok(p) if isinstance(p, Placeholder) else p for p in v)
        if any(isinstance(p, str) and p.strip() for p in v):
            any_literal = True
        texts.append(txt)
    texts = list(dict.fromkeys(t for t in texts if t.strip()))
    if not texts or not any_literal:
        return [], "unresolved", labels
    status = "partial" if labels else "resolved"
    return texts, status, list(dict.fromkeys(labels))


_PARAM_DEF = re.compile(r"(@\w+)\s+([^,]+?)(?:\s+(OUTPUT|OUT))?\s*(?:,|$)", re.I)


def parse_param_definitions(text: str) -> List[dict]:
    """'@from date, @n int OUTPUT' -> [{name, type, output}]"""
    out = []
    for m in _PARAM_DEF.finditer(text or ""):
        out.append({"name": m.group(1), "type": m.group(2).strip(), "output": bool(m.group(3)), "default": ""})
    return out
