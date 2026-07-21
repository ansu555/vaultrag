"""Mermaid fences → ``diagram`` graph edges (P2 Stage 5).

A Mermaid diagram is the one place in a vault where a human draws the graph
*without* writing wikilinks. ``parsing-pipeline.md``: "Source kept as searchable text;
cheap regex extracts ``A --> B`` node labels/edges into the graph as ``diagram_edge``
(flowchart/sequence only)". The source text is already indexed — a fence is an atomic
code block (``markdown.py``) — so this module adds only the *edges*.

**Edges are anchored on the containing note, not node-to-node.** ``A --> B`` inside
``architecture.md`` yields ``architecture.md --diagram--> A`` and
``architecture.md --diagram--> B``, never ``A --> B``. That is forced by the schema
and it is the right shape anyway: ``links.src_doc`` is the key
:meth:`~vaultrag.store.base.MetaStore.replace_links` replaces on, so an edge sourced
at some *other* document could never be revoked when this note's diagram changes, and
the graph would stop being a pure function of the vault (``CLAUDE.md`` §6). What the
graph gains is real: "this note's diagram references the Gateway Service note".

**Only flowchart and sequence diagrams produce edges.** Every other Mermaid type
(class, ER, state, gantt, pie, mindmap, …) has connector syntax that means something
other than "these two things are related as documents", so they degrade to
searchable-text-only rather than being half-parsed into wrong edges — an edge nobody
drew is a wrong retrieval nobody notices (same rule as
:func:`~vaultrag.store.links.resolve_key` refusing to guess an ambiguous target).

Resolution is not this module's job: targets are emitted as raw text and go through
the ordinary ``path → basename → title → alias`` ladder, dangling when they name no
document (``store/links.py``).
"""

from __future__ import annotations

import re

from vaultrag.models import Link, LinkKind

__all__ = ["MERMAID_LANGS", "extract_diagram_links", "is_mermaid_lang"]

# Code-fence infostrings that mean "this is Mermaid". Obsidian renders ```mermaid;
# some vaults carry the ```mmd alias.
MERMAID_LANGS = frozenset({"mermaid", "mmd"})


def is_mermaid_lang(lang: str | None) -> bool:
    """Whether a code fence's infostring marks it as a Mermaid diagram."""
    return lang is not None and lang.strip().casefold() in MERMAID_LANGS


# ---------------------------------------------------------------------------
# Preamble
# ---------------------------------------------------------------------------
# `%% comment`, and the `%%{init: {...}}%%` config directive (which is a comment too).
_COMMENT_RE = re.compile(r"%%.*$")
# A YAML frontmatter block may open a diagram: `---\ntitle: X\n---`.
_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n.*?\n\s*---\s*\n", re.S)

_FLOWCHART_HEADER_RE = re.compile(r"^(?:flowchart|graph)\b", re.I)
_SEQUENCE_HEADER_RE = re.compile(r"^sequenceDiagram\b", re.I)

# Statements that are styling/structure, never an edge. `subgraph` names group boxes
# and `end` closes them; the rest configure rendering. Matched on the first word so a
# node id that merely *starts* with one of these (`endpoint --> db`) is unaffected.
_FLOW_SKIP_KEYWORDS = frozenset(
    {
        "subgraph",
        "end",
        "style",
        "classdef",
        "class",
        "click",
        "linkstyle",
        "direction",
        "accTitle",
        "accDescr",
    }
)
_SEQ_SKIP_KEYWORDS = frozenset(
    {
        "note",
        "loop",
        "alt",
        "else",
        "opt",
        "par",
        "and",
        "critical",
        "option",
        "break",
        "rect",
        "end",
        "activate",
        "deactivate",
        "autonumber",
        "title",
        "box",
        "create",
        "destroy",
        "link",
        "links",
        "properties",
        "details",
        "accTitle",
        "accDescr",
    }
)

# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------
# Every flowchart link form collapses to one sentinel before the line is split.
#
# Two details carry the whole thing:
#
# * **Order.** The *labelled* forms (`-- writes -->`, `== ships ==>`, `-. reads .->`)
#   are tried before their bare counterparts, or the bare pattern would match the
#   opening half and strand the label as a node.
# * **A labelled link opens with exactly two dashes** — `(?<!-)--(?!-)`. This is
#   Mermaid's own grammar (`A-- text -->B` vs the three-dash long link `A---B`), and
#   it is what stops `A --- B --- C` from being read as one connector whose "label"
#   is the node `B`. Without the exactly-two rule the middle node of every chained
#   long link would vanish.
_SENTINEL = "\x00"
_FLOW_CONNECTORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!-)--(?!-)[^>\n|]*?-{2,}(?:>|[ox](?!\w))?"),  # -- text --> / -- text ---
    re.compile(r"(?<!=)==(?!=)[^>\n|]*?={2,}(?:>|[ox](?!\w))?"),  # == text ==>
    re.compile(r"-\.[^.\n|]*?\.-+(?:>|[ox](?!\w))?"),  # -. text .->
    re.compile(r"<?-\.-+(?:>|[ox](?!\w))?"),  # -.-> / -.-
    re.compile(r"<?={2,}(?:>|[ox](?!\w))?"),  # ==> / === / <==>
    # --> / --- / o--o / x--x / <-->. The `o`/`x` arrowheads are word-bounded on both
    # ends so `foo-->bar` keeps its `o` and `--obs` keeps its `o`; `>` needs no guard
    # (it cannot be part of an id), which is what keeps the space-less `A-->B` working.
    re.compile(r"(?:\b[ox]|<)?-{2,}(?:>|[ox](?!\w))?"),
)

# `-->|label|` — the pipe form of an edge label, stripped before splitting.
_PIPE_LABEL_RE = re.compile(r"\|[^|\n]*\|")

# Sequence-diagram arrows: `->>`, `-->>`, `->`, `-->`, `-x`, `--x`, `-)`, `--)`.
_SEQ_ARROW_RE = re.compile(r"-{1,2}(?:>>?|x|\))")
_SEQ_PARTICIPANT_RE = re.compile(
    r"^(?:participant|actor)\s+(?P<id>\S+)(?:\s+as\s+(?P<as>.+))?$", re.I
)

# A node expression's shape wrapper: `id[Label]`, `id((Label))`, `id{{Label}}`, `id>Label]`…
_SHAPE_RE = re.compile(r"^(?P<id>[^\s\[\({>]+)\s*(?P<body>[\[\({>].*)$", re.S)
_OPEN_RUN_RE = re.compile(r"^[\[\({>]+")
_BR_RE = re.compile(r"<br\s*/?>", re.I)

# Unlabelled placeholder ids: `A`, `B2`, `X`. Mermaid's own docs use them and so does
# everyone copying them, so without this filter a single diagram dangles a dozen edges
# named after nothing and floods `unresolved_links` (surfaced by `overview`). A node
# that carries a *label* is never tested against this — the label is its name.
_PLACEHOLDER_ID_RE = re.compile(r"^[A-Za-z]\d{0,2}$")


def extract_diagram_links(source: str) -> list[Link]:
    """Node labels of every edge in one Mermaid fence, as ``diagram`` links.

    Deduped by target, in source order. Returns ``[]`` for diagram types outside
    flowchart/sequence, for empty fences, and for anything unparseable — a diagram
    that yields no edges is the expected case, never an error.
    """
    lines = _clean_lines(source)
    if not lines:
        return []

    header, body = lines[0], lines[1:]
    if _FLOWCHART_HEADER_RE.match(header):
        names = _flowchart_nodes(body)
    elif _SEQUENCE_HEADER_RE.match(header):
        names = _sequence_nodes(body)
    else:
        return []  # not a graph of documents — searchable text only (module docstring)

    out: list[Link] = []
    seen: set[str] = set()
    for name in names:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(Link(kind=LinkKind.diagram, target=name))
    return out


def _clean_lines(source: str) -> list[str]:
    """Strip the optional YAML header, comments/directives, and blank lines."""
    text = _FRONTMATTER_RE.sub("", source)
    out: list[str] = []
    for raw in text.splitlines():
        line = _COMMENT_RE.sub("", raw).strip()
        if line:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# Flowchart
# ---------------------------------------------------------------------------
def _flowchart_nodes(lines: list[str]) -> list[str]:
    """Every node name that takes part in a flowchart link, in source order.

    Two passes, because a flowchart routinely gives a node its label once and then
    refers to it by bare id::

        A[Gateway Service] --> B[Store]
        A --> C[Audit Log]

    A single pass would emit "Gateway Service" for the first ``A`` and drop the second
    as an unlabelled placeholder, so the *same node* would contribute an edge on one
    line and nothing on the next. Pass 1 collects every ``id → label`` declaration in
    the fence (including standalone declaration lines, which are not edges); pass 2
    resolves endpoints through it.
    """
    statements = [
        _to_sentinels(_PIPE_LABEL_RE.sub(" ", line).rstrip(";"))
        for line in lines
        if _first_word(line) not in _FLOW_SKIP_KEYWORDS
    ]

    labels: dict[str, str] = {}
    for stmt in statements:
        for expr in _endpoints(stmt):
            m = _SHAPE_RE.match(expr.strip().strip(";"))
            if m is None:
                continue
            label = _clean_label(_shape_label(m.group("body")))
            if label:
                labels.setdefault(m.group("id"), label)

    out: list[str] = []
    for stmt in statements:
        if _SENTINEL not in stmt:
            continue  # a bare node declaration is not an edge
        for expr in _endpoints(stmt):
            name = _node_name(expr, labels)
            if name:
                out.append(name)
    return out


def _endpoints(stmt: str) -> list[str]:
    """The node expressions in a normalized statement (`A & B --> C` fans out to 3)."""
    return [expr for side in stmt.split(_SENTINEL) for expr in side.split("&")]


def _to_sentinels(stmt: str) -> str:
    """Replace every flowchart connector with :data:`_SENTINEL`.

    Applied longest/labelled-first (see :data:`_FLOW_CONNECTORS`); each pattern runs
    over the output of the previous one, so a line can mix connector styles.
    """
    for pattern in _FLOW_CONNECTORS:
        stmt = pattern.sub(_SENTINEL, stmt)
    return stmt


def _node_name(expr: str, labels: dict[str, str]) -> str | None:
    """A node expression → the name to link by, or ``None`` if it carries none.

    Precedence: the shape wrapper's own label (``A[Gateway Service]`` is called
    "Gateway Service", not "A") → a label the same id declared elsewhere in the fence
    → the bare id, unless it is a placeholder (:data:`_PLACEHOLDER_ID_RE`).
    """
    expr = expr.strip().strip(";")
    if not expr:
        return None

    m = _SHAPE_RE.match(expr)
    ident = m.group("id") if m is not None else expr
    if m is not None:
        label = _clean_label(_shape_label(m.group("body")))
        if label:
            return label

    declared = labels.get(ident)
    if declared:
        return declared
    return None if _PLACEHOLDER_ID_RE.match(ident) else _clean_label(ident)


def _shape_label(body: str) -> str:
    """Unwrap a node shape's brackets: ``[[Sub]]`` → ``Sub``, ``[Gateway (v2)]`` → ``Gateway (v2)``.

    The closing run is taken to be the same length as the opening one — Mermaid's
    shapes are symmetric (``[…]``, ``((…))``, ``{{…}}``, ``[(…)]``), and matching by
    length rather than by "strip every trailing bracket" is what keeps a label that
    legitimately *ends* in a paren intact.
    """
    open_run = _OPEN_RUN_RE.match(body)
    if open_run is None:
        return ""
    n = len(open_run.group())
    inner = body[n:-n] if len(body) > 2 * n else ""
    return inner.strip(" \t/\\")


def _clean_label(label: str) -> str | None:
    """Normalize a node label to its indexable text (``None`` when nothing is left)."""
    text = _BR_RE.sub(" ", label).replace('"', " ").replace("'", " ")
    text = " ".join(text.split())
    return text or None


def _first_word(line: str) -> str:
    head = line.split(maxsplit=1)[0] if line.split() else ""
    return head.casefold().rstrip(":")


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------
def _sequence_nodes(lines: list[str]) -> list[str]:
    """Every participant that sends or receives a sequence-diagram message.

    ``participant db as Gateway DB`` is honoured, so the *displayed* name is what
    reaches the graph — the same "the label is the name" rule the flowchart side uses.
    """
    aliases: dict[str, str] = {}
    out: list[str] = []

    for line in lines:
        m = _SEQ_PARTICIPANT_RE.match(line)
        if m is not None:
            shown = (m.group("as") or "").strip()
            if shown:
                aliases[m.group("id")] = shown
            continue

        if _first_word(line) in _SEQ_SKIP_KEYWORDS:
            continue

        # The message text after the first `:` is prose, not a participant.
        stmt = line.split(":", 1)[0]
        parts = _SEQ_ARROW_RE.split(stmt)
        if len(parts) < 2:
            continue
        for part in parts:
            ident = part.strip().strip("+-")
            if not ident:
                continue
            if ident not in aliases and _PLACEHOLDER_ID_RE.match(ident):
                continue  # an unaliased placeholder names nothing (see flowchart side)
            cleaned = _clean_label(aliases.get(ident, ident))
            if cleaned:
                out.append(cleaned)
    return out
