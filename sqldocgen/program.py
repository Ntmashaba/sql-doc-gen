"""Turn a procedure body into numbered steps, nested scopes and a control-flow graph.

Every executable statement becomes a step, numbered in source order ("14"). Statements that
run inside another one (resolved dynamic SQL, an expanded procedure call) are numbered under
it ("14.2"). IF and WHILE are steps too, because evaluating their predicate reads data.

The control-flow graph links steps the way T-SQL can run them: both arms of an IF, loop back
edges, BREAK/CONTINUE, RETURN, GOTO, and an edge from every statement in a TRY block to its
CATCH block (any of them may fail).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .model import Condition, Scope, Step
from .syntax import Text, ident, parts, typ

ENTRY, EXIT = "ENTRY", "EXIT"

KINDS = {
    "SelectStatement": "select", "SelectStatementSnippet": "select",
    "InsertStatement": "insert", "UpdateStatement": "update", "DeleteStatement": "delete",
    "MergeStatement": "merge", "TruncateTableStatement": "truncate",
    "CreateTableStatement": "create-table", "DropTableStatement": "drop-table",
    "AlterTableAddTableElementStatement": "alter-table", "AlterTableAlterColumnStatement": "alter-table",
    "AlterTableDropTableElementStatement": "alter-table",
    "CreateIndexStatement": "create-index", "DropIndexStatement": "drop-index",
    "DeclareVariableStatement": "declare", "DeclareTableVariableStatement": "declare-table",
    "DeclareCursorStatement": "declare-cursor", "SetVariableStatement": "set",
    "ExecuteStatement": "exec", "IfStatement": "if", "WhileStatement": "while",
    "ReturnStatement": "return", "BreakStatement": "break", "ContinueStatement": "continue",
    "GoToStatement": "goto", "LabelStatement": "label",
    "BeginTransactionStatement": "begin-tran", "CommitTransactionStatement": "commit",
    "RollbackTransactionStatement": "rollback", "SaveTransactionStatement": "save-tran",
    "PrintStatement": "print", "RaiseErrorStatement": "raiserror", "ThrowStatement": "throw",
    "OpenCursorStatement": "open-cursor", "FetchCursorStatement": "fetch",
    "CloseCursorStatement": "close-cursor", "DeallocateCursorStatement": "deallocate-cursor",
    "PredicateSetStatement": "set-option", "SetTransactionIsolationLevelStatement": "set-option",
    "SetRowCountStatement": "set-option", "SetOnOffStatement": "set-option", "SetErrorLevelStatement": "set-option",
    "SetIdentityInsertStatement": "set-option", "SetStatisticsStatement": "set-option",
    "GeneralSetCommand": "set-option", "SetCommandStatement": "set-option",
    "WaitForStatement": "waitfor", "UseStatement": "use",
    "BulkInsertStatement": "bulk-insert", "InsertBulkStatement": "bulk-insert",
    "UpdateStatisticsStatement": "maintenance", "ExecuteAsStatement": "security", "RevertStatement": "security",
}


def exec_spec_of(node) -> dict:
    """The ExecuteSpecification run by an EXEC statement or an INSERT ... EXEC."""
    if typ(node) == "ExecuteStatement":
        return node.get("ExecuteSpecification") or {}
    if typ(node) == "InsertStatement":
        src = (node.get("InsertSpecification") or {}).get("InsertSource") or {}
        if typ(src) == "ExecuteInsertSource":
            return src.get("Execute") or {}
    return {}


def is_dynamic_exec(node) -> bool:
    ent = exec_spec_of(node).get("ExecutableEntity") or {}
    if typ(ent) == "ExecutableStringList":
        return True
    name = parts(((ent.get("ProcedureReference") or {}).get("ProcedureReference") or {}).get("Name"))
    return bool(name) and name[-1].lower() == "sp_executesql"


def step_kind(node) -> str:
    t = typ(node)
    kind = KINDS.get(t, "other")
    if t == "InsertStatement" and exec_spec_of(node):
        return "insert-exec"
    if kind == "select":
        if node.get("Into"):
            return "select-into"
        q = node.get("QueryExpression") or {}
        if typ(q) == "QuerySpecification" and any(typ(e) == "SelectSetVariable" for e in q.get("SelectElements", [])):
            return "select-assign"
    if kind == "exec" and is_dynamic_exec(node):
        return "exec-dynamic"
    if kind == "set-option" and t == "SetVariableStatement":
        return "set"
    return kind


@dataclass
class Env:
    scope: str
    conditions: List[Condition]
    text_id: str
    namespace: str
    prefix: Optional[str]                    # label prefix for nested steps
    origin: str = ""
    parent: Optional[str] = None
    loop: Optional[Tuple[str, List[str]]] = None    # (header step, break steps)
    try_steps: Optional[List[str]] = None           # steps created inside the innermost TRY
    return_to: str = EXIT
    depth: int = 0
    in_try: bool = False


class Fragment:
    __slots__ = ("entry", "exits")

    def __init__(self, entry=None, exits=()):
        self.entry = entry
        self.exits = set(exits)

    @property
    def empty(self):
        return self.entry is None


class ProgramBuilder:
    """Builds steps, scopes and the CFG. ``expand(step, env)`` may return nested statement
    lists to run inside an EXEC step: [(statements, text_id, namespace, origin, label)]."""

    def __init__(self, texts: Dict[str, Text], expand: Optional[Callable] = None):
        self.texts = texts
        self.expand = expand
        self.steps: List[Step] = []
        self.by_id: Dict[str, Step] = {}
        self.scopes: Dict[str, Scope] = {}
        self.succ: Dict[str, Set[str]] = defaultdict(set)
        self.counters: Dict[Optional[str], int] = defaultdict(int)
        self.labels: Dict[Tuple[str, str], str] = {}
        self.gotos: List[Tuple[str, str, str]] = []
        self._scope_n = 0

    # ------------------------------------------------------------------ public
    def build(self, statements: List[dict], text_id: str = "main", label: str = "Procedure") -> None:
        self.scopes["root"] = Scope("root", "procedure", label, None)
        env = Env(scope="root", conditions=[], text_id=text_id, namespace="", prefix=None)
        frag = self.stmts(statements, env)
        if frag.empty:
            self.edge(ENTRY, EXIT)
        else:
            self.edge(ENTRY, frag.entry)
            for x in frag.exits:
                self.edge(x, EXIT)
        for src, name, ns in self.gotos:
            target = self.labels.get((ns, name.lower()))
            if target:
                self.edge(src, target)
            else:
                self.edge(src, EXIT)

    def preds(self) -> Dict[str, Set[str]]:
        p: Dict[str, Set[str]] = defaultdict(set)
        for a, bs in self.succ.items():
            for b in bs:
                p[b].add(a)
        return p

    # ------------------------------------------------------------------ helpers
    def edge(self, a: str, b: str) -> None:
        if a and b:
            self.succ[a].add(b)

    def new_scope(self, kind: str, label: str, parent: str, step: Optional[str]) -> str:
        self._scope_n += 1
        sid = f"c{self._scope_n}"
        self.scopes[sid] = Scope(sid, kind, label, parent, step)
        self.scopes[parent].items.append(("scope", sid))
        return sid

    def new_step(self, node: dict, kind: str, env: Env) -> Step:
        self.counters[env.prefix] += 1
        n = self.counters[env.prefix]
        label = f"{env.prefix}.{n}" if env.prefix else str(n)
        sid = "s" + label.replace(".", "_")
        text = self.texts[env.text_id]
        st = Step(id=sid, label=label, kind=kind, node=node, text_id=env.text_id,
                  span=text.span(node), lines=text.line_range(node), scope=env.scope, depth=env.depth,
                  conditions=list(env.conditions), namespace=env.namespace, origin=env.origin,
                  parent=env.parent, in_try=env.in_try)
        self.steps.append(st)
        self.by_id[sid] = st
        self.scopes[env.scope].items.append(("step", sid))
        if env.try_steps is not None:
            env.try_steps.append(sid)
        return st

    @staticmethod
    def seq(a: Fragment, b: Fragment, edge) -> Fragment:
        if a.empty:
            return b
        if b.empty:
            return a
        for x in a.exits:
            edge(x, b.entry)
        return Fragment(a.entry, b.exits)

    # ------------------------------------------------------------------ statements
    def stmts(self, statements: List[dict], env: Env) -> Fragment:
        frag = Fragment()
        for st in statements or []:
            frag = self.seq(frag, self.stmt(st, env), self.edge)
        return frag

    def _list(self, node) -> List[dict]:
        if not isinstance(node, dict):
            return []
        if typ(node) == "StatementList":
            return node.get("Statements", []) or []
        if typ(node) in ("BeginEndBlockStatement", "BeginEndAtomicBlockStatement"):
            return (node.get("StatementList") or {}).get("Statements", []) or []
        return [node]

    def stmt(self, node: dict, env: Env) -> Fragment:
        t = typ(node)
        text = self.texts[env.text_id]
        if t in ("BeginEndBlockStatement", "BeginEndAtomicBlockStatement"):
            return self.stmts(self._list(node), env)
        if t == "TryCatchStatement":
            try_scope = self.new_scope("try", "TRY", env.scope, None)
            catch_scope = self.new_scope("catch", "CATCH", env.scope, None)
            created: List[str] = []
            tenv = Env(**{**env.__dict__, "scope": try_scope, "try_steps": created, "in_try": True,
                          "depth": env.depth + 1})
            tfrag = self.stmts((node.get("TryStatements") or {}).get("Statements", []) or [], tenv)
            cond = Condition(step="", text="an error was raised in the TRY block", branch="catch")
            cenv = Env(**{**env.__dict__, "scope": catch_scope, "conditions": env.conditions + [cond],
                          "depth": env.depth + 1})
            cfrag = self.stmts((node.get("CatchStatements") or {}).get("Statements", []) or [], cenv)
            if not cfrag.empty:
                for s in created:
                    self.edge(s, cfrag.entry)
            exits = set(tfrag.exits) | set(cfrag.exits)
            if tfrag.empty:
                return Fragment(cfrag.entry, cfrag.exits) if not cfrag.empty else Fragment()
            if cfrag.empty:
                # an error in TRY with an empty CATCH continues after the block
                exits |= set(created)
            return Fragment(tfrag.entry, exits)

        kind = step_kind(node)
        step = self.new_step(node, kind, env)

        if t == "IfStatement":
            pred = text.squeeze(node.get("Predicate"), 140)
            then_scope = self.new_scope("then", f"IF {pred}", env.scope, step.id)
            tc = Condition(step=step.id, text=pred, branch="then")
            tenv = Env(**{**env.__dict__, "scope": then_scope, "conditions": env.conditions + [tc],
                          "depth": env.depth + 1})
            tfrag = self.stmts(self._list(node.get("ThenStatement")), tenv)
            exits = set()
            if tfrag.empty:
                exits.add(step.id)
            else:
                self.edge(step.id, tfrag.entry)
                exits |= tfrag.exits
            if node.get("ElseStatement"):
                else_scope = self.new_scope("else", f"ELSE (not {pred})", env.scope, step.id)
                ec = Condition(step=step.id, text=pred, branch="else")
                eenv = Env(**{**env.__dict__, "scope": else_scope, "conditions": env.conditions + [ec],
                              "depth": env.depth + 1})
                efrag = self.stmts(self._list(node.get("ElseStatement")), eenv)
                if efrag.empty:
                    exits.add(step.id)
                else:
                    self.edge(step.id, efrag.entry)
                    exits |= efrag.exits
            else:
                exits.add(step.id)
            return Fragment(step.id, exits)

        if t == "WhileStatement":
            pred = text.squeeze(node.get("Predicate"), 140)
            label = f"WHILE {pred}"
            if "@@fetch_status" in pred.lower():
                label += "  (cursor loop)"
            loop_scope = self.new_scope("loop", label, env.scope, step.id)
            lc = Condition(step=step.id, text=pred, branch="loop")
            breaks: List[str] = []
            lenv = Env(**{**env.__dict__, "scope": loop_scope, "conditions": env.conditions + [lc],
                          "loop": (step.id, breaks), "depth": env.depth + 1})
            body = self.stmts(self._list(node.get("Statement")), lenv)
            if not body.empty:
                self.edge(step.id, body.entry)
                for x in body.exits:
                    self.edge(x, step.id)
            return Fragment(step.id, {step.id} | set(breaks))

        if t == "ReturnStatement":
            self.edge(step.id, env.return_to)
            return Fragment(step.id, ())
        if t == "BreakStatement":
            if env.loop:
                env.loop[1].append(step.id)
                return Fragment(step.id, ())
            return Fragment(step.id, {step.id})
        if t == "ContinueStatement":
            if env.loop:
                self.edge(step.id, env.loop[0])
                return Fragment(step.id, ())
            return Fragment(step.id, {step.id})
        if t == "GoToStatement":
            self.gotos.append((step.id, ident(node.get("LabelName")) or "", env.namespace))
            return Fragment(step.id, ())
        if t == "LabelStatement":
            name = (node.get("Value") or "").rstrip(":").strip().lower()
            self.labels[(env.namespace, name)] = step.id
            return Fragment(step.id, {step.id})
        if t == "ThrowStatement":
            # a THROW always ends the batch, or jumps to the CATCH block (edge added by the TRY)
            if not env.in_try:
                self.edge(step.id, env.return_to if env.prefix else EXIT)
            return Fragment(step.id, ())

        if kind in ("exec", "exec-dynamic", "insert-exec") and self.expand:
            nested = self.expand(step, env) or []
            if not nested:
                return Fragment(step.id, {step.id})
            # several rebuilt variants of one dynamic statement are alternatives, not a sequence
            exits: Set[str] = set()
            for statements, text_id, namespace, origin, label in nested:
                nscope = self.new_scope("dynamic" if origin == "dynamic" else "call", label, env.scope, step.id)
                nenv = Env(scope=nscope, conditions=list(env.conditions), text_id=text_id, namespace=namespace,
                           prefix=step.label, origin=origin, parent=step.id, loop=None,
                           try_steps=env.try_steps, return_to="", depth=env.depth + 1, in_try=env.in_try)
                body = self._nested(statements, nenv, step, [])
                self.edge(step.id, body.entry)
                exits |= body.exits
            return Fragment(step.id, exits)

        return Fragment(step.id, {step.id})

    def _nested(self, statements, nenv: Env, step: Step, end_holder) -> Fragment:
        # RETURN inside the nested list jumps to its end marker, which we only know afterwards:
        # build with a placeholder and patch the edges.
        placeholder = f"__end_{step.id}_{len(self.scopes)}"
        nenv.return_to = placeholder
        body = self.stmts(statements, nenv)
        end = self.new_step({"$": "EndOfNested", "@": [-1, 0, 0, 0]}, "nested-end", nenv)
        end.span, end.lines = None, None
        for a, bs in list(self.succ.items()):
            if placeholder in bs:
                bs.discard(placeholder)
                bs.add(end.id)
        if body.empty:
            return Fragment(end.id, {end.id})
        for x in body.exits:
            self.edge(x, end.id)
        return Fragment(body.entry, {end.id})


def reverse_postorder(succ: Dict[str, Set[str]], start: str = ENTRY) -> List[str]:
    seen, order = set(), []
    stack = [(start, iter(sorted(succ.get(start, ()))))]
    seen.add(start)
    while stack:
        node, it = stack[-1]
        nxt = next(it, None)
        if nxt is None:
            stack.pop()
            order.append(node)
            continue
        if nxt not in seen:
            seen.add(nxt)
            stack.append((nxt, iter(sorted(succ.get(nxt, ())))))
    order.reverse()
    return order
