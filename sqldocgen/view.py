"""Read-only helpers over a finished payload, shared by the Word, CSV, agent and trace writers.

They mirror the page's JavaScript (names, the backward walk, the trace model), so every
output tells the same story as the HTML.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional

ROWS = "(rows)"
LOCAL_SUFFIX = {"cte": " (CTE)", "view": " (view)", "function": " (function)", "values": " (VALUES)", "pivot": " (pivot)"}
KIND_NAMES = {"table": "table", "view": "view", "function": "function", "system": "system view",
              "remote": "remote / linked server", "file": "file", "temp": "temp table", "global-temp": "global temp table",
              "table-variable": "table variable", "cursor": "cursor", "result": "result set", "return": "return value",
              "parameter": "parameter", "variable": "variable", "table-parameter": "table-valued parameter",
              "dynamic": "named at run time"}


class View:
    def __init__(self, payload: dict):
        self.p = payload
        self.rel = {r["key"]: r for r in payload["relations"]}
        self.step = {s["id"]: s for s in payload["steps"]}
        self.idx = {s["id"]: i for i, s in enumerate(payload["steps"])}
        self.nodes = payload["nodes"]
        self.out = {o["key"]: o for o in payload["outputs"]}
        self.local = {l["key"]: l for l in payload.get("locals", [])}

    def rel_name(self, key: str) -> str:
        if key in self.rel:
            return self.rel[key]["name"]
        if key in self.local:
            l = self.local[key]
            return l["name"] + LOCAL_SUFFIX.get(l["kind"], " (derived)")
        if key.startswith("system:"):
            return key[7:].upper()
        if key.startswith("unresolved:"):
            return key[11:] + " (unresolved)"
        if key.startswith("proc-result:"):
            return "result of " + key[12:]
        return key

    def col_name(self, rel: str, col: str) -> str:
        r = self.rel.get(rel)
        if col == "value" and (r is None or r["kind"] in ("variable", "parameter", "return") or rel.startswith("system:")):
            return self.rel_name(rel)
        if col == ROWS:
            return "rows of " + self.rel_name(rel)
        if col == "*":
            return self.rel_name(rel) + ".*"
        return f"{self.rel_name(rel)}.{col}"

    def node_label(self, nid: int) -> str:
        n = self.nodes[nid]
        return self.col_name(n["rel"], n["col"])

    def version(self, nid: int) -> str:
        n = self.nodes[nid]
        if n["step"] is None:
            return "before the procedure" if n["op"] == "initial" else ""
        s = self.step.get(n["step"])
        return ("inside step " if n["op"] == "local" else "after step ") + s["label"] if s else ""

    def label(self, sid: str) -> str:
        s = self.step.get(sid)
        return s["label"] if s else sid

    @staticmethod
    def lines(st: dict) -> str:
        ln = st.get("lines")
        if not ln:
            return ""
        return f"line {ln[0]}" if ln[0] == ln[1] else f"lines {ln[0]}–{ln[1]}"

    # ------------------------------------------------------------------ walks (same rules as the page)
    def backward(self, starts: List[int], indirect: bool = True, limit: int = 20000) -> Dict[int, dict]:
        seen: Dict[int, dict] = {}
        q = deque()
        for s in starts:
            seen[s] = {"direct": True, "depth": 0, "role": "start"}
            q.append(s)

        def visit(nid, direct, depth, role):
            if self.nodes[nid]["op"] == "create":
                return
            cur = seen.get(nid)
            if cur is None:
                if len(seen) >= limit:
                    return
                seen[nid] = {"direct": direct, "depth": depth, "role": role}
                q.append(nid)
            elif direct and not cur["direct"]:
                cur.update(direct=True, depth=min(depth, cur["depth"]), role=role)
                q.append(nid)

        done_steps = set()
        while q:
            nid = q.popleft()
            info, n = seen[nid], self.nodes[nid]
            d0 = info["depth"] + 1
            for u in n.get("uses", []):
                if not u["d"] and not indirect:
                    continue
                for d in u["defs"]:
                    visit(d, info["direct"] and bool(u["d"]), d0, u["role"])
            if not indirect or n["step"] is None or n["op"] == "local" or n["step"] in done_steps:
                continue
            done_steps.add(n["step"])
            st = self.step.get(n["step"])
            if st is None:
                continue
            for u in st["uses"]:
                for d in u["defs"]:
                    visit(d, False, d0, u["role"])
            for c in st["conditions"]:
                cs = self.step.get(c["step"]) if c["step"] else None
                if cs is None:
                    continue
                for u in cs["uses"]:
                    for d in u["defs"]:
                        visit(d, False, d0, "condition")
        return seen

    def trace_model(self, key: str, indirect: bool = True) -> dict:
        o = self.out[key]
        sl = self.backward(o["defs"], indirect)
        steps: Dict[str, dict] = {}
        for nid, info in sl.items():
            n = self.nodes[nid]
            if n["step"] is None:
                continue
            e = steps.setdefault(n["step"], {"sid": n["step"], "nodes": [], "role": "rows"})
            e["nodes"].append((nid, info["direct"]))
            if info["direct"]:
                e["role"] = "value"
        if indirect:
            for sid in list(steps):
                for c in self.step[sid]["conditions"]:
                    if c["step"] and c["step"] in self.step and c["step"] not in steps:
                        steps[c["step"]] = {"sid": c["step"], "nodes": [], "role": "cond"}
        ordered = sorted(steps.values(), key=lambda e: self.idx.get(e["sid"], 10 ** 6))
        bases = sorted({self.node_label(n) for n, i in sl.items()
                        if self.nodes[n]["op"] == "initial" and i["direct"] and self.nodes[n]["col"] != ROWS})
        decided = sorted({self.node_label(n) for n, i in sl.items()
                          if self.nodes[n]["op"] == "initial" and (not i["direct"] or self.nodes[n]["col"] == ROWS)})
        return {"output": o, "slice": sl, "steps": ordered, "bases": bases, "decided": decided}

    def find_output(self, want: str) -> Optional[str]:
        """An output column key from 'dbo.Table.Column', 'Table.Column', '#temp.Column', '@Out' or a key."""
        if want in self.out:
            return want
        w = want.lower().replace("[", "").replace("]", "")
        cands = []
        for k, o in self.out.items():
            full = self.col_name(o["rel"], o["col"]).lower()
            if full == w or full.endswith("." + w) or (o["col"].lower() == w):
                cands.append((0 if full == w else (1 if full.endswith("." + w) else 2), k))
        cands.sort()
        return cands[0][1] if cands else None


def plain(s: str) -> str:
    return (s or "").replace("`", "")
