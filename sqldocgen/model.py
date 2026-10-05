"""Data structures shared by the analysis modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

ROWS = "(rows)"      # pseudo-column: the set of rows in a relation
VALUE = "value"      # the single column of a scalar variable


@dataclass
class Column:
    name: str
    type: str = ""
    nullable: Optional[bool] = None
    identity: bool = False
    computed: str = ""
    default: str = ""


@dataclass
class CatalogObject:
    """A table, view, function, procedure, synonym or table type found in the inputs."""
    kind: str                          # table | view | function | procedure | synonym | table-type
    database: str
    schema: str
    name: str
    columns: List[Column] = field(default_factory=list)
    keys: List[List[str]] = field(default_factory=list)       # primary key / unique constraints
    source: str = ""                   # file it came from
    node: Optional[dict] = None        # CREATE statement (views, functions, procedures)
    text: object = None                # syntax.Text of the file
    target: Optional[Tuple[str, str, str, str]] = None        # synonym target
    params: List[dict] = field(default_factory=list)
    function_kind: str = ""            # scalar | inline | multi-statement

    @property
    def display(self) -> str:
        return ".".join(p for p in (self.schema, self.name) if p)

    @property
    def full(self) -> str:
        return ".".join(p for p in (self.database, self.schema, self.name) if p)


@dataclass
class Relation:
    """Anything rows or values live in while the procedure runs."""
    key: str                           # e.g. table:sales.dbo.orders, temp:#stage, var:@rate
    kind: str                          # table view function system remote temp global-temp table-variable
                                       # variable parameter cursor result return cte derived file unknown
    name: str                          # display name
    columns: List[Column] = field(default_factory=list)
    complete: bool = False             # True when every column is known (definition seen)
    endpoint: Optional[dict] = None
    catalog: Optional[CatalogObject] = None
    star_from: List[str] = field(default_factory=list)   # filled by SELECT * from these relations
    created: List[str] = field(default_factory=list)     # steps that create it
    dropped: List[str] = field(default_factory=list)
    note: str = ""
    output: bool = False               # parameter declared OUTPUT
    data_type: str = ""                # variables and parameters
    default: str = ""                  # parameter default

    def find(self, col: str) -> Optional[Column]:
        lc = col.lower()
        for c in self.columns:
            if c.name.lower() == lc:
                return c
        return None

    def learn(self, col: str, **kw) -> Column:
        c = self.find(col)
        if c is None:
            c = Column(col, **kw)
            self.columns.append(c)
        return c


@dataclass
class Use:
    """A read of a column (or row set, or variable) by a step."""
    rel: str
    col: str
    role: str                          # value join filter group having case window order top rows condition
                                       # previous argument merge output key subquery
    direct: bool
    span: Optional[Tuple[int, int]] = None
    status: str = "resolved"           # resolved | partial | unresolved
    candidates: List[str] = field(default_factory=list)  # possible relations when partial
    local: Optional[int] = None        # node id when it reads a statement-local column (CTE, derived)
    text: str = ""                     # the reference as written
    same_step: bool = False            # reads a value written by the same step (OUTPUT inserted.x)
    defs: List[int] = field(default_factory=list)   # versions that can reach this read (filled by dataflow)


@dataclass
class ColNode:
    """One version of a column: written by a step, present before the procedure, or statement-local."""
    id: int
    rel: str
    col: str
    step: Optional[str]                # None: the value existed before the procedure ran
    op: str                            # initial insert update merge-insert merge-update delete truncate create
                                       # select-into declare set select-assign fetch exec-output result default
                                       # local output-into return drop
    expr: str = ""
    span: Optional[Tuple[int, int]] = None
    uses: List[Use] = field(default_factory=list)
    keeps_previous: bool = False       # partial overwrite: rows not touched keep the earlier value
    kills: bool = False                # replaces every earlier version
    status: str = "resolved"
    note: str = ""
    transforms: List[str] = field(default_factory=list)
    text_id: str = "main"
    ast: Optional[dict] = field(default=None, repr=False)   # expression assigned (variables), for dynamic SQL


@dataclass
class Condition:
    step: str                          # the IF / WHILE step
    text: str
    branch: str                        # then | else | loop | catch


@dataclass
class Step:
    id: str
    label: str                         # "14" or "14.2" for nested (dynamic SQL, expanded calls)
    kind: str
    node: dict
    text_id: str = "main"
    span: Optional[Tuple[int, int]] = None
    lines: Optional[Tuple[int, int]] = None
    scope: str = "root"
    depth: int = 0
    conditions: List[Condition] = field(default_factory=list)
    nodes: List[int] = field(default_factory=list)      # ColNodes written by this step
    uses: List[Use] = field(default_factory=list)       # reads not tied to a written column
    reads: List[str] = field(default_factory=list)      # relation keys read
    writes: List[str] = field(default_factory=list)     # relation keys written
    summary: str = ""
    comment: str = ""
    namespace: str = ""                # variable namespace (expanded calls / dynamic batches)
    origin: str = ""                   # dynamic | call:<proc>
    parent: Optional[str] = None       # the EXEC step that runs this nested step
    detail: dict = field(default_factory=dict)  # kind-specific facts for summaries and checks
    in_try: bool = False
    transaction: bool = False


@dataclass
class Scope:
    id: str
    kind: str                          # procedure then else loop try catch dynamic call block
    label: str
    parent: Optional[str]
    step: Optional[str] = None         # header step (IF / WHILE / EXEC)
    items: List[Tuple[str, str]] = field(default_factory=list)   # ("step", id) | ("scope", id)


@dataclass
class Issue:
    rule: str
    severity: str                      # high | medium | low | info
    title: str
    why: str
    next_step: str
    steps: List[str] = field(default_factory=list)
    spans: List[Tuple[int, int]] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)    # "rel|col" of columns concerned
    relations: List[str] = field(default_factory=list)
    certainty: str = "definite"        # definite | possible
    text_id: str = "main"
