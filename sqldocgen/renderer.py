"""Inject the payload into template.html.

The payload is the single source of truth: HTML, Word, CSV, the agent document and the
standalone JSON are all renderings of it. Injection replaces a /*__DATA__*/null placeholder
and escapes <, > and & so the JSON can never end the script block early (same discipline as
the other bi-doc engines).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from .details import DETAILS_ID, json_script, read_details, validate_details
from .textutil import scrub_text

TEMPLATE = Path(__file__).with_name("template.html")
SOURCE_KEYS = {"texts"}          # embedded code is masked at load time; scrubbing it again would move offsets


def scrub_payload(value, path: Tuple[str, ...] = ()) -> Tuple[object, int]:
    """Safety net over every string in the payload except the embedded code."""
    count = 0
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in SOURCE_KEYS and not path:
                out[k] = v
                continue
            out[k], n = scrub_payload(v, path + (str(k),))
            count += n
        return out, count
    if isinstance(value, list):
        out = []
        for v in value:
            nv, n = scrub_payload(v, path)
            out.append(nv)
            count += n
        return out, count
    if isinstance(value, str) and "=" in value:
        s, n = scrub_text(value)
        return s, n
    return value, 0


def finalize(payload: dict) -> dict:
    payload, extra = scrub_payload(payload)
    payload["redactions"] = payload.get("redactions", 0) + extra
    return payload


def render_html(payload: dict, out_path, details=None) -> Path:
    out_path = Path(out_path)
    if details is None and out_path.exists():
        # regenerating over an earlier document keeps its maintained details
        try:
            details = read_details(out_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError):
            details = None
    details = validate_details(details)
    payload = {**payload, "details": details, "documentationFilename": out_path.name}
    template = TEMPLATE.read_text(encoding="utf-8")
    template = template.replace("<!--__DETAILS__-->",
                                f'<script type="application/json" id="{DETAILS_ID}">{json_script(details)}</script>')
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    blob = blob.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = (template
            .replace("__TITLE__", payload["title"].replace("&", "&amp;").replace("<", "&lt;"))
            .replace("/*__DATA__*/null", blob))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
