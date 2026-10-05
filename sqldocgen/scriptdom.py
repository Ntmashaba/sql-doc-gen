"""Bridge to the ScriptDom helper (``sqldocgen/parser``, a small .NET program).

The helper is built on first use with the .NET SDK into a per-user cache folder and then
run with the .NET runtime. Requests and responses are JSON over stdin/stdout, so the SQL
never leaves the machine and never touches a temporary file.

Overrides (environment variables):
  SQLDOCGEN_PARSER     path to an already built sqldocgen-parser.dll (skips building)
  SQLDOCGEN_SCRIPTDOM  path to a local Microsoft.SqlServer.TransactSql.ScriptDom.dll; the
                       helper is then built offline against it (no nuget.org needed)
  SQLDOCGEN_CACHE      cache folder (default: %LOCALAPPDATA%\\sql-doc-gen or ~/.cache/sql-doc-gen)
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PARSER_SRC = Path(__file__).with_name("parser")
SOURCES = ("SqlDocGen.Parser.csproj", "Program.cs")
DLL_NAME = "sqldocgen-parser.dll"


class ParserUnavailable(RuntimeError):
    """The ScriptDom helper cannot be built or run on this machine."""


def cache_root() -> Path:
    env = os.environ.get("SQLDOCGEN_CACHE")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "sql-doc-gen"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "sql-doc-gen"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sql-doc-gen"


def find_dotnet() -> Optional[str]:
    exe = shutil.which("dotnet")
    if exe:
        return exe
    root = os.environ.get("DOTNET_ROOT")
    if root:
        cand = Path(root) / ("dotnet.exe" if os.name == "nt" else "dotnet")
        if cand.exists():
            return str(cand)
    for cand in (r"C:\Program Files\dotnet\dotnet.exe", "/usr/local/share/dotnet/dotnet", "/usr/share/dotnet/dotnet",
                 "/usr/lib/dotnet/dotnet", str(Path.home() / ".dotnet" / "dotnet")):
        if Path(cand).exists():
            return cand
    return None


def _dotnet_list(dotnet: str, what: str) -> List[str]:
    try:
        out = subprocess.run([dotnet, what], capture_output=True, text=True, errors="replace", timeout=60)
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def _scriptdom_override() -> Optional[Path]:
    env = os.environ.get("SQLDOCGEN_SCRIPTDOM")
    return Path(env).expanduser().resolve() if env else None


def _build_hash(scriptdom: Optional[Path]) -> str:
    h = hashlib.sha256()
    for name in SOURCES:
        h.update((PARSER_SRC / name).read_bytes())
    if scriptdom:
        st = scriptdom.stat()
        h.update(f"{scriptdom}|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def helper_path(build: bool = True, log=None) -> Path:
    """Path to the helper DLL, building it first when needed (and allowed)."""
    env = os.environ.get("SQLDOCGEN_PARSER")
    if env:
        p = Path(env).expanduser()
        if not p.exists():
            raise ParserUnavailable(f"SQLDOCGEN_PARSER points to {p}, which does not exist.")
        return p
    scriptdom = _scriptdom_override()
    if scriptdom and not scriptdom.exists():
        raise ParserUnavailable(f"SQLDOCGEN_SCRIPTDOM points to {scriptdom}, which does not exist.")
    out_dir = cache_root() / f"parser-{_build_hash(scriptdom)}"
    dll = out_dir / DLL_NAME
    if dll.exists():
        return dll
    if not build:
        raise ParserUnavailable("The ScriptDom helper has not been built yet. Run: sql-doc-gen --build-parser")
    build_helper(out_dir, scriptdom, log=log)
    return dll


def build_helper(out_dir: Path, scriptdom: Optional[Path] = None, log=None) -> None:
    dotnet = find_dotnet()
    if not dotnet:
        raise ParserUnavailable(
            ".NET was not found. sql-doc-gen parses T-SQL with Microsoft ScriptDom through a small .NET helper.\n"
            "Install the .NET SDK 8 or later (https://dotnet.microsoft.com/download), then run again.")
    sdks = _dotnet_list(dotnet, "--list-sdks")
    if not sdks:
        raise ParserUnavailable(
            "Only the .NET runtime is installed; building the ScriptDom helper needs the .NET SDK 8 or later.\n"
            "Install the SDK, or point SQLDOCGEN_PARSER at a helper built on another machine "
            "(sql-doc-gen --build-parser there, then copy the folder it prints).")
    src = out_dir.parent / (out_dir.name + "-src")
    if src.exists():
        shutil.rmtree(src, ignore_errors=True)
    src.mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        shutil.copy2(PARSER_SRC / name, src / name)
    cmd = [dotnet, "build", str(src / SOURCES[0]), "-c", "Release", "-o", str(out_dir), "-nologo",
           "-p:GenerateDocumentationFile=false"]
    if scriptdom:
        empty = src / "no-package-sources"
        empty.mkdir(exist_ok=True)
        cmd += [f"-p:ScriptDomPath={scriptdom}", f"-p:RestoreSources={empty}"]
    if log:
        log("Building the ScriptDom helper (first run only)…")
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
               DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1")
    # errors="replace": a localised build message must not crash the build report
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env, timeout=900)
    if proc.returncode != 0 or not (out_dir / DLL_NAME).exists():
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        hint = ""
        if "nuget.org" in tail.lower() or "NU1301" in tail or "service index" in tail:
            hint = ("\nnuget.org could not be reached. Point SQLDOCGEN_SCRIPTDOM at a local "
                    "Microsoft.SqlServer.TransactSql.ScriptDom.dll (SSMS, SqlPackage and Visual Studio SSDT "
                    "ship one) to build offline.")
        raise ParserUnavailable("Building the ScriptDom helper failed:\n" + tail + hint)


def parse(items: Iterable[Tuple[str, str]], parser: str = "auto", quoted_identifier: bool = True,
          log=None) -> Dict:
    """Parse texts with ScriptDom. ``items`` are (id, text) pairs.

    Returns the helper's response: {"scriptDom", "parser", "results": {id: {"errors", "tree"}}}.
    """
    items = list(items)
    dll = helper_path(log=log)
    dotnet = find_dotnet()
    if not dotnet:
        raise ParserUnavailable(".NET was not found; install the .NET runtime 8 or later.")
    request = json.dumps({"parser": parser, "quotedIdentifier": quoted_identifier,
                          "items": [{"id": i, "text": t} for i, t in items]}, ensure_ascii=False)
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    try:
        proc = subprocess.run([dotnet, str(dll)], input=request.encode("utf-8"), capture_output=True,
                              env=env, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ParserUnavailable(f"Could not run the ScriptDom helper: {exc}") from exc
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", errors="replace").strip()
        if "You must install or update .NET" in msg or "framework" in msg.lower() and "not found" in msg.lower():
            msg += "\nInstall the .NET runtime 8 or later."
        raise ParserUnavailable("The ScriptDom helper failed: " + (msg or f"exit code {proc.returncode}"))
    data = json.loads(proc.stdout.decode("utf-8"))
    results = {r["id"]: {"errors": r.get("errors") or [], "tree": r.get("tree")} for r in data.get("results", [])}
    return {"scriptDom": data.get("scriptDom"), "parser": data.get("parser"),
            "helperVersion": data.get("helperVersion"), "results": results}


def doctor() -> List[Tuple[str, str, str]]:
    """(check, result, advice) rows describing what this machine can do."""
    rows = [("Python", platform.python_version(), "3.9 or later is needed.")]
    dotnet = find_dotnet()
    if not dotnet:
        rows.append((".NET", "not found", "Install the .NET SDK 8 or later: https://dotnet.microsoft.com/download"))
        return rows
    sdks = _dotnet_list(dotnet, "--list-sdks")
    runtimes = [r for r in _dotnet_list(dotnet, "--list-runtimes") if r.startswith("Microsoft.NETCore.App")]
    rows.append((".NET runtime", ", ".join(r.split(" ")[1] for r in runtimes) or "none",
                 "" if runtimes else "Install the .NET runtime 8 or later."))
    rows.append((".NET SDK", ", ".join(s.split(" ")[0] for s in sdks) or "none",
                 "" if sdks else "Needed once, to build the ScriptDom helper (or set SQLDOCGEN_PARSER)."))
    try:
        dll = helper_path(build=False)
        rows.append(("ScriptDom helper", str(dll), ""))
    except ParserUnavailable as exc:
        rows.append(("ScriptDom helper", "not built", str(exc)))
    sd = _scriptdom_override()
    if sd:
        rows.append(("SQLDOCGEN_SCRIPTDOM", str(sd), "" if sd.exists() else "file not found"))
    return rows
