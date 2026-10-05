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
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .engine import Ctx
from .model import VALUE
from .syntax import literal_value, typ, unparen

MAX_VARIANTS = 6
TOKEN = "__sqldocgen_{}__"
UNDETERMINED = object()           # a value or condition that cannot be worked out from the code


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
    parameter: bool = False                   # a text parameter of the procedure: the caller decides it
    quoted: bool = False                      # passed through QUOTENAME / REPLACE(x, , '')


def _quoted(values):
    return [[Placeholder(p.label, p.parameter, True) if isinstance(p, Placeholder) else p for p in v] for v in values]


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
            return []                    # NULL + anything is NULL: no statement comes out of this branch
        if t == "ParenthesisExpression":
            return self.value(e.get("Expression"), step_id, ns, depth + 1)
        if t == "BinaryExpression" and e.get("BinaryExpressionType") == "Add":
            # a + b + c + ... is a left-deep tree, often 50+ levels in generated DDL: flatten it
            operands, stack = [], [e]
            while stack:
                x = stack.pop()
                if typ(x) == "BinaryExpression" and x.get("BinaryExpressionType") == "Add":
                    stack.append(x.get("SecondExpression"))
                    stack.append(x.get("FirstExpression"))
                else:
                    operands.append(x)
            acc: List[List[object]] = [[]]
            for op in operands:
                vals = self.value(op, step_id, ns, depth + 1)
                acc = [x + y for x, y in itertools.islice(itertools.product(acc, vals), MAX_VARIANTS)]
            return acc
        if t in ("SearchedCaseExpression", "SimpleCaseExpression", "IIfCall"):
            # every branch that can be taken is a possible value; a condition over variables whose
            # values are known here (SET @col = N'') decides its branch
            out: List[List[object]] = []
            for b in self._case_branches(e, step_id, ns, depth):
                if b is None:
                    continue             # no ELSE: NULL, which would make the whole string NULL
                out.extend(self.value(b, step_id, ns, depth + 1))
                if len(out) >= MAX_VARIANTS:
                    break
            return out[:MAX_VARIANTS] or [[Placeholder("CASE")]]
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
                inner = _quoted(self.value(params[0], step_id, ns, depth + 1))
                quote = params[1].get("Value", "[") if len(params) > 1 and typ(params[1]) == "StringLiteral" else "["
                close = {"[": "]", "]": "]", "'": "'", '"': '"', "(": ")", ")": ")", "<": ">", ">": ">",
                         "{": "}", "}": "}", "`": "`"}.get(quote[:1], "]")
                return [[quote[:1] or "["] + v + [close] for v in inner]
            if name in ("CONCAT", "CONCAT_WS"):
                acc = [[]]
                for p in (params[1:] if name == "CONCAT_WS" else params):
                    vals = self.value(p, step_id, ns, depth + 1) or [[""]]      # CONCAT treats NULL as ''
                    acc = [x + y for x, y in itertools.islice(itertools.product(acc, vals), MAX_VARIANTS)]
                return acc
            if name in ("ISNULL", "COALESCE") and params:
                for p in params:
                    vals = self.value(p, step_id, ns, depth + 1)
                    if vals:
                        return vals
                return []
            if name in ("CHAR", "NCHAR") and params:
                code = literal_value(params[0])
                if isinstance(code, int):
                    return [[chr(code)]]
            if name in ("LTRIM", "RTRIM", "TRIM", "UPPER", "LOWER") and params:
                return self.value(params[0], step_id, ns, depth + 1)
            if name == "SPACE" and params and isinstance(literal_value(params[0]), int):
                return [[" " * min(literal_value(params[0]), 200)]]
            if name == "REPLICATE" and len(params) == 2 and isinstance(literal_value(params[1]), int):
                n = min(literal_value(params[1]), 200)
                return [v * n for v in self.value(params[0], step_id, ns, depth + 1)]
            if name == "REPLACE" and len(params) == 3 and typ(params[1]) == "StringLiteral":
                # REPLACE(@template, N'@@@Table@@@', QUOTENAME(@t)): substitute inside the known text
                vals = self.value(params[0], step_id, ns, depth + 1)
                old = params[1].get("Value", "")
                news = self.value(params[2], step_id, ns, depth + 1)
                escapes = old == "'" and typ(params[2]) == "StringLiteral" and params[2].get("Value") == "''"
                if not old:
                    return vals
                rx = re.compile(re.escape(old), re.IGNORECASE)     # REPLACE follows the (usually CI) collation
                out = []
                for v, nv in itertools.islice(itertools.product(vals, news), MAX_VARIANTS):
                    pieces: List[object] = []
                    for p in v:
                        if isinstance(p, Placeholder):
                            pieces.append(Placeholder(p.label, p.parameter, True) if escapes else p)
                            continue
                        parts_ = rx.split(p)
                        for k, part in enumerate(parts_):
                            if k:
                                pieces.extend(nv)
                            pieces.append(part)
                    out.append(pieces)
                return out or [[Placeholder("REPLACE(…)")]]
            return [[Placeholder(f"{name}(…)")]]
        if t == "CoalesceExpression" and e.get("Expressions"):
            for p in e["Expressions"]:
                vals = self.value(p, step_id, ns, depth + 1)
                if vals:
                    return vals
            return []
        if t == "LeftFunctionCall" or t == "RightFunctionCall":
            return [[Placeholder("LEFT/RIGHT(…)")]]
        if t == "ColumnReferenceExpression":
            return [[Placeholder("a column value")]]
        return [[Placeholder(self._label(e))]]

    # ------------------------------------------------------------------ conditions with known values
    def _case_branches(self, e, step_id, ns, depth) -> list:
        t = typ(e)
        if t == "IIfCall":
            tv = self.truth(e.get("Predicate"), step_id, ns, depth)
            if tv is True:
                return [e.get("ThenExpression")]
            if tv is UNDETERMINED:
                return [e.get("ThenExpression"), e.get("ElseExpression")]
            return [e.get("ElseExpression")]
        out = []
        for w in e.get("WhenClauses", []) or []:
            if t == "SimpleCaseExpression":
                tv = self._compare("Equals", self.const(e.get("InputExpression"), step_id, ns, depth),
                                   self.const(w.get("WhenExpression"), step_id, ns, depth))
            else:
                tv = self.truth(w.get("WhenExpression"), step_id, ns, depth)
            if tv is True:
                out.append(w.get("ThenExpression"))
                return out               # later branches cannot be reached
            if tv is UNDETERMINED:
                out.append(w.get("ThenExpression"))
        out.append(e.get("ElseExpression"))
        return out

    def const(self, e, step_id, ns, depth):
        """The single value an expression has here (str, int or None for NULL), or UNDETERMINED."""
        if depth > 40:
            return UNDETERMINED
        e = unparen(e)
        t = typ(e)
        if t in ("StringLiteral", "IntegerLiteral", "NullLiteral"):
            return literal_value(e)
        if t == "VariableReference":
            key = self.ctx.var_key(e.get("Name") or "", ns)
            defs = self.defs_at(key, step_id)
            if len(defs) != 1:
                return UNDETERMINED
            n = self.ctx.nodes[defs[0]]
            if n.op == "declare" and n.expr == "NULL" and getattr(n, "ast", None) is None:
                return None
            ast = getattr(n, "ast", None)
            if ast is None or (key, n.step) in self.visiting:
                return UNDETERMINED
            self.visiting.add((key, n.step))
            try:
                return self.const(ast, n.step, ns, depth + 1)
            finally:
                self.visiting.discard((key, n.step))
        if t == "FunctionCall":
            name = ((e.get("FunctionName") or {}).get("Value") or "").upper()
            if name in ("COALESCE", "ISNULL"):
                for p in e.get("Parameters", []) or []:
                    v = self.const(p, step_id, ns, depth + 1)
                    if v is UNDETERMINED or v is not None:
                        return v
                return None
        if t == "CoalesceExpression":
            for p in e.get("Expressions", []) or []:
                v = self.const(p, step_id, ns, depth + 1)
                if v is UNDETERMINED or v is not None:
                    return v
            return None
        return UNDETERMINED

    @staticmethod
    def _compare(op, a, b):
        if a is UNDETERMINED or b is UNDETERMINED:
            return UNDETERMINED
        if a is None or b is None:
            return None                  # SQL UNKNOWN: the branch is not taken
        if isinstance(a, str) and isinstance(b, str):
            a, b = a.rstrip().lower(), b.rstrip().lower()    # case-insensitive, trailing spaces ignored
        elif isinstance(a, str) or isinstance(b, str):
            try:
                a, b = int(a), int(b)
            except (TypeError, ValueError):
                return UNDETERMINED
        ops = {"Equals": a == b, "NotEqualToBrackets": a != b, "NotEqualToExclamation": a != b}
        if op in ops:
            return ops[op]
        try:
            return {"GreaterThan": a > b, "LessThan": a < b, "GreaterThanOrEqualTo": a >= b,
                    "LessThanOrEqualTo": a <= b}.get(op, UNDETERMINED)
        except TypeError:
            return UNDETERMINED

    def truth(self, c, step_id, ns, depth):
        """True / False / None (SQL UNKNOWN) / UNDETERMINED for a condition over known values."""
        t = typ(c)
        if t == "BooleanParenthesisExpression":
            return self.truth(c.get("Expression"), step_id, ns, depth + 1)
        if t == "BooleanNotExpression":
            v = self.truth(c.get("Expression"), step_id, ns, depth + 1)
            return v if v is UNDETERMINED or v is None else not v
        if t == "BooleanBinaryExpression":
            a = self.truth(c.get("FirstExpression"), step_id, ns, depth + 1)
            b = self.truth(c.get("SecondExpression"), step_id, ns, depth + 1)
            if c.get("BinaryExpressionType") == "And":
                if a is False or b is False:
                    return False
                if a is UNDETERMINED or b is UNDETERMINED:
                    return UNDETERMINED
                return None if (a is None or b is None) else True
            if a is True or b is True:
                return True
            if a is UNDETERMINED or b is UNDETERMINED:
                return UNDETERMINED
            return None if (a is None or b is None) else False
        if t == "BooleanIsNullExpression":
            v = self.const(c.get("Expression"), step_id, ns, depth + 1)
            if v is UNDETERMINED:
                return UNDETERMINED
            return (v is None) != bool(c.get("IsNot"))
        if t == "BooleanComparisonExpression":
            return self._compare(c.get("ComparisonType"), self.const(c.get("FirstExpression"), step_id, ns, depth + 1),
                                 self.const(c.get("SecondExpression"), step_id, ns, depth + 1))
        return UNDETERMINED

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
            rel = self.ctx.relations.get(key)
            # a text parameter is decided by whoever calls the procedure; numbers and dates cannot carry SQL
            param = (rel is not None and rel.kind == "parameter" and "/" not in key and
                     bool(re.match(r"\s*(n?(var)?char|n?text|sysname|sql_variant)\b", rel.data_type or "", re.I)))
            if not defs:
                return [[Placeholder(name, param)]]
            out: List[List[object]] = []
            for nid in defs:
                n = self.ctx.nodes[nid]
                if n.op == "initial":
                    out.append([Placeholder(name, param)])
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


def rebuild(ctx: Ctx, flow: dict, step, registry: Optional[Dict[str, str]] = None
            ) -> Tuple[List[str], str, List[str], List[str]]:
    """Variants of the SQL run by a dynamic EXEC step: (texts, status, placeholder labels, unsafe labels).

    Unsafe labels are text parameters of the procedure pasted into the statement without
    QUOTENAME or quote doubling: whoever calls the procedure can change the SQL it runs.

    status: resolved (every piece is literal), partial (some pieces are run-time values) or
    unresolved (nothing could be rebuilt). Placeholder tokens are numbered through
    ``registry`` (token -> label), shared by every dynamic step of the procedure, so a token
    names the same run-time value wherever it appears."""
    if registry is None:
        registry = {}
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
    tokens: Dict[Placeholder, str] = {}

    def tok(p):
        if p not in tokens:
            tokens[p] = TOKEN.format(len(registry) + 1)
            registry[tokens[p]] = p.label
            labels.append(p.label)
        return tokens[p]

    for v in variants:
        txt = "".join(tok(p) if isinstance(p, Placeholder) else p for p in v)
        if any(isinstance(p, str) and p.strip() for p in v):
            any_literal = True
        texts.append(txt)
    texts = list(dict.fromkeys(t for t in texts if t.strip()))
    unsafe = list(dict.fromkeys(_pasted_parameters(variants)))
    if not texts or not any_literal:
        return [], "unresolved", labels, unsafe
    status = "partial" if labels else "resolved"
    return texts, status, list(dict.fromkeys(labels)), unsafe


def _pasted_parameters(variants) -> List[str]:
    """Text parameters pasted into the middle of a statement (a name or a value). A parameter that
    stands where a whole statement goes (at the start, or after ';') is caller-supplied SQL run by
    design, as in a command-runner procedure: that is not reported as injection."""
    out = []
    for v in variants:
        prefix = ""
        for p in v:
            if isinstance(p, Placeholder):
                if p.parameter and not p.quoted:
                    before = prefix.rstrip()
                    if before and not before.endswith(";"):
                        out.append(p.label)
                prefix += "x"
            else:
                prefix += p
    return out


_PARAM_ONE = re.compile(r"\s*(?P<name>@\w+)\s+(?:AS\s+)?(?P<type>.+?)(?:\s*=\s*(?P<default>.+?))?"
                        r"(?:\s+(?P<out>OUTPUT|OUT))?(?:\s+READONLY)?\s*$", re.IGNORECASE | re.DOTALL)


def parse_param_definitions(text: str) -> List[dict]:
    """'@from date, @n int OUTPUT, @amt decimal(18, 2) = 0' -> [{name, type, output, default}]"""
    parts, cur, depth = [], [], 0
    for ch in text or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    out = []
    for part in parts:
        m = _PARAM_ONE.match(part)
        if m:
            out.append({"name": m.group("name"), "type": m.group("type").strip(), "output": bool(m.group("out")),
                        "default": (m.group("default") or "").strip()})
    return out
