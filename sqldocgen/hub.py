"""The library home page (sql-home.html): one offline page listing every procedure document.

It reads the payload embedded in each generated HTML file in a folder, so it can be rebuilt
at any time without the original SQL. It shows procedure cards (grouped by the "Home page
group" detail, or by database and schema), a Shared objects view (which procedures read or
write each table), and the results of the last batch run.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from .details import json_script, read_details, validate_details

HUB_MARKER = 'name="sql-documentation-hub"'
HUB_NAME = "sql-home.html"
BATCH_RESULTS = "sql-batch-results.json"
TEMPLATE = Path(__file__).with_name("hub.html")


def _payload(text: str) -> Optional[dict]:
    m = re.search(r"const DATA\s*=\s*", text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[m.end():])
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("generator") == "sql-doc-gen" else None


def _num(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _text(v) -> str:
    return v if isinstance(v, str) else ""


def describe(path: Path) -> Optional[dict]:
    text = path.read_text(encoding="utf-8-sig")
    if HUB_MARKER in text:
        return None
    payload = _payload(text)
    if payload is None:
        return None
    try:
        details = read_details(text) or validate_details(payload.get("details"))
    except ValueError:
        details = validate_details(None)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    issues = summary.get("issues") if isinstance(summary.get("issues"), dict) else {}
    proc = payload.get("procedure") if isinstance(payload.get("procedure"), dict) else {}
    objects = []
    for o in summary.get("objects") or []:
        if not isinstance(o, dict):
            continue
        ep = o.get("endpoint") if isinstance(o.get("endpoint"), dict) else {}
        objects.append({"name": _text(o.get("name")), "kind": _text(o.get("kind")),
                        "server": _text(ep.get("server")), "database": _text(ep.get("database")),
                        "read": bool(o.get("read")), "written": bool(o.get("written")),
                        "ops": [x for x in o.get("ops") or [] if isinstance(x, str)][:10]})
    return {
        "file": path.name, "href": quote(path.name, safe=""),
        "title": _text(payload.get("title")) or path.stem,
        "generated": _text(payload.get("generated")),
        "schemaVersion": _num(payload.get("schemaVersion")),
        "mode": _text(payload.get("mode")),
        "database": _text(proc.get("database")), "schema": _text(proc.get("schema")),
        "kind": _text(proc.get("kind")), "source": _text(proc.get("source")),
        "what": _text(payload.get("what")),
        "details": details,
        "counts": {k: _num(counts.get(k)) for k in ("statements", "inputs", "outputs", "temp", "branches", "dynamic",
                                                    "outputColumns", "calls")},
        "issues": {k: _num(issues.get(k)) for k in ("high", "medium", "low", "info")},
        "unresolved": _num(summary.get("unresolved")),
        "objects": objects[:400],
        "calls": [c for c in summary.get("calls") or [] if isinstance(c, str)][:50],
    }


def build_hub(folder, output=None) -> Path:
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    output = Path(output) if output else folder / HUB_NAME
    if output.exists() and HUB_MARKER not in output.read_text(encoding="utf-8-sig"):
        raise ValueError(f"Refusing to replace a file that is not a documentation home: {output}")
    rows: List[dict] = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if path.suffix.lower() != ".html" or path.resolve() == output.resolve() or path.name.startswith("trace-"):
            continue
        try:
            row = describe(path)
        except (OSError, UnicodeError) as exc:
            row = {"file": path.name, "href": quote(path.name, safe=""), "title": path.stem,
                   "error": f"Could not be read: {exc}"}
        if row:
            rows.append(row)
    batch = None
    results = folder / BATCH_RESULTS
    if results.exists():
        try:
            value = json.loads(results.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("documents"), list):
                batch = value
        except (ValueError, OSError):
            batch = None
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("/*__DOCS__*/[]", json_script(rows))
            .replace("/*__BATCH__*/null", json_script(batch)))
    output.write_text(html, encoding="utf-8")
    return output
