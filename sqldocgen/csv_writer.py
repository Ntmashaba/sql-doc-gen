"""CSV outputs: columns, edges, steps and issues (UTF-8 with BOM so Excel opens them correctly)."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List

from .view import ROWS, View, plain


def _write(path: Path, header: List[str], rows: List[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def write_csv(payload: dict, folder: Path) -> List[Path]:
    v = View(payload)
    folder = Path(folder)
    out = []
    rows = []
    for o in payload["outputs"]:
        if o["col"] == ROWS:
            continue
        rows.append([v.rel_name(o["rel"]), o["col"], o["status"],
                     "; ".join(v.col_name(r, c) for r, c in o["direct"]),
                     "; ".join(v.rel_name(r) for r in o["indirect"]),
                     " ".join(v.label(s) for s in o["steps"]),
                     "; ".join(("NOT " if c["branch"] == "else" else "") + c["text"] for c in o["conditions"])])
    out.append(_write(folder / "columns.csv", ["output", "column", "status", "direct_sources", "deciding_inputs",
                                               "steps", "conditions"], rows))
    rows = []
    for n in payload["nodes"]:
        for u in n.get("uses", []):
            for d in u["defs"]:
                rows.append([v.node_label(d), v.version(d), v.node_label(n["id"]), v.version(n["id"]),
                             "direct" if u["d"] else "indirect", u["role"],
                             v.label(n["step"]) if n["step"] else ""])
    for st in payload["steps"]:
        targets = [nid for nid in st["nodes"]]
        for u in st["uses"]:
            for d in u["defs"]:
                for t in targets:
                    rows.append([v.node_label(d), v.version(d), v.node_label(t), v.version(t), "indirect", u["role"],
                                 st["label"]])
    out.append(_write(folder / "edges.csv", ["from", "from_version", "to", "to_version", "kind", "role", "step"], rows))
    sections = {s["id"]: s for s in payload.get("sections") or []}
    rows = [[s["label"], s["kind"], s["lines"][0] if s["lines"] else "", s["lines"][1] if s["lines"] else "",
             plain(s["summary"]), "; ".join(v.rel_name(k) for k in s["reads"]), "; ".join(v.rel_name(k) for k in s["writes"]),
             " AND ".join(("NOT " if c["branch"] == "else" else "") + c["text"] for c in s["conditions"]),
             s.get("origin") or "", "" if s.get("reachable", True) else "unreachable",
             _section(sections, s.get("section")), s.get("role") or "logic", s.get("why") or ""]
            for s in payload["steps"]]
    out.append(_write(folder / "steps.csv", ["step", "kind", "first_line", "last_line", "summary", "reads", "writes",
                                             "conditions", "origin", "note", "section", "role", "housekeeping_reason"],
                      rows))
    rows = [[i["severity"], i["certainty"], i["rule"], plain(i["title"]), i["why"], i["next"],
             " ".join(v.label(s) for s in i["steps"])] for i in payload["issues"]]
    out.append(_write(folder / "issues.csv", ["severity", "certainty", "rule", "title", "why", "next_step", "steps"], rows))
    return out


def _section(sections: dict, sid) -> str:
    s = sections.get(sid) if sid else None
    return f"{s['number']} {s['title']}".strip() if s else ""
