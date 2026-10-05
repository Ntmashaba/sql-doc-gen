"""Procedure details that people maintain by hand and that survive regeneration.

They live in the generated HTML as a small JSON block. Regenerating over an existing file
keeps them; the page can edit them and download an updated copy. Only plain text reference
fields are accepted, never secrets.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any, Optional

from .textutil import scrub_text

DETAILS_ID = "sql-documentation-details"
FIELDS = {
    "owner": "Owner or team",
    "job": "SQL Agent job / schedule",
    "server": "Server and database",
    "environment": "Environment (e.g. Production)",
    "runbook": "Runbook or support page",
    "sourceLocation": "Source location (repository, branch)",
    "folder": "Home page group (e.g. Finance / Nightly)",
    "notes": "Notes",
}
MAX_LEN = 4000


def validate_details(value: Any) -> dict:
    if value in (None, ""):
        return {k: "" for k in FIELDS}
    if not isinstance(value, dict):
        raise ValueError("Procedure details must be a JSON object")
    unknown = set(value) - set(FIELDS)
    if unknown:
        raise ValueError(f"Unsupported procedure details: {', '.join(sorted(unknown))}; accepted: {', '.join(FIELDS)}")
    out = {}
    for key in FIELDS:
        text = value.get(key, "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError(f"{key} must be text")
        if len(text) > MAX_LEN:
            raise ValueError(f"{key} is longer than {MAX_LEN} characters")
        if scrub_text(text)[1]:
            raise ValueError(f"{key} looks like it contains a secret (password, key or token); "
                             f"record where the secret lives instead")
        out[key] = text
    return out


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture, self.text, self.found = False, "", None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == DETAILS_ID:
            self.capture, self.text = True, ""

    def handle_data(self, data):
        if self.capture:
            self.text += data

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            self.capture = False
            try:
                self.found = json.loads(self.text)
            except ValueError:
                self.found = None


def read_details(html: str) -> Optional[dict]:
    reader = _Reader()
    reader.feed(html)
    if reader.found is None:
        return None
    try:
        return validate_details(reader.found)
    except ValueError:
        return None


def json_script(value: Any) -> str:
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
