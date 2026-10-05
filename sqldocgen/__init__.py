"""sql-doc-gen: living documentation and column lineage for T-SQL stored procedures.

Parsing is done by Microsoft ScriptDom through a small .NET helper (``sqldocgen/parser``);
everything else (analysis, lineage, checks and every output) is standard-library Python.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = 1
GENERATOR = "sql-doc-gen"
