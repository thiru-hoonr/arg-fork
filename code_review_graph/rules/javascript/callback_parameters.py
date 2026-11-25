"""Remove the Function crg makes from a callback written with one bare parameter.

crg stores ``ids.map(id => load(id))`` as a Function ``id`` that owns the call to ``load``.
Written ``(id) => load(id)``, the same callback gives no Function and the call belongs to the
enclosing function. This rule makes the bare form give that same graph.
"""

from __future__ import annotations

import dataclasses
import functools
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Query, QueryCursor

from ._edges import given_to_owner, spans, unresolved, unresolved_targets
from ._syntax import (
    NAME_FIELD_OF_PARENT,
    CallSite,
    Lines,
    Scope,
    by_lines,
    call_site,
    enclosing_scopes,
    last_part,
    lines,
    owner,
    text,
)

if TYPE_CHECKING:
    from tree_sitter import Language, Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "callback-parameters"

_BARE_ARROW_QUERY = "(arrow_function parameter: (identifier) @parameter) @arrow"


class _NameAt(NamedTuple):
    """A name and the lines it spans: how a bare arrow and crg's Function for it are matched."""

    name: str
    lines: Lines


class _ContainsAt(NamedTuple):
    """The node a CONTAINS edge holds and the line it is on."""

    target: str
    line: int


class _BareArrow(NamedTuple):
    """An arrow with one bare parameter, as the syntax tree shows it."""

    parameter: str
    lines: Lines
    scopes: tuple[Scope, ...]  # innermost first
    calls: tuple[CallSite, ...]  # those crg gives it: not the ones inside a nested callback


@dataclasses.dataclass
class _Callback:
    """A Function crg made from a callback's parameter, and where its edges go."""

    node: NodeInfo
    qualified_name: str
    owner: NodeInfo | None  # None when the callback is not inside any function
    unclaimed_calls: Counter[CallSite]

    def span(self) -> int:
        """How many lines past its first this callback runs: the smaller, the more inner."""
        return self.node.line_end - self.node.line_start


def drop_callback_parameter_functions(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Remove callback-parameter Functions and give their edges to the function they are in."""
    if not any(node.kind == "Function" and node.params is None for node in nodes):
        return nodes, edges  # every such Function lacks a parameter list; skip the second parse
    arrows = _bare_arrows(parser, language, source)
    pairs = _pair_with_nodes(arrows, nodes)
    if not pairs:
        return nodes, edges

    removed = {id(node) for node, _ in pairs}
    kept = [node for node in nodes if id(node) not in removed]
    kept_by_lines = by_lines(kept)
    callbacks = [
        _Callback(
            node=node,
            qualified_name=parser._node_qualified(node),
            owner=owner(arrow.scopes, kept_by_lines),
            unclaimed_calls=Counter(arrow.calls),
        )
        for node, arrow in pairs
    ]
    targets = unresolved_targets(parser, [node for node, _ in pairs], kept)
    return kept, _move_edges(parser, edges, callbacks, targets)


def _bare_arrows(parser: CodeParser, language: str, source: bytes) -> list[_BareArrow]:
    """Every arrow with one bare parameter that crg can mistake for a function."""
    ts_parser = parser._get_parser(language)
    root = ts_parser.parse(source).root_node
    arrows = []
    for _, captures in QueryCursor(_bare_arrow_query(ts_parser.language)).matches(root):
        arrow, parameter = captures["arrow"][0], captures["parameter"][0]
        if arrow.parent.type in NAME_FIELD_OF_PARENT:
            continue
        arrows.append(_BareArrow(
            text(parameter), lines(arrow), enclosing_scopes(arrow), _own_calls(arrow)
        ))
    return arrows


@functools.lru_cache(maxsize=None)
def _bare_arrow_query(grammar: Language) -> Query:
    """The compiled query for *grammar*; compiling is slow, so it is done once per grammar."""
    return Query(grammar, _BARE_ARROW_QUERY)


def _own_calls(arrow: Node) -> tuple[CallSite, ...]:
    """The calls crg gives to *arrow*: all in its body except those of a nested callback."""
    calls = []
    pending = list(arrow.children)
    while pending:
        node = pending.pop()
        if node.type == "arrow_function" and node.child_by_field_name("parameter"):
            continue
        site = call_site(node)
        if site is not None:
            calls.append(site)
        pending.extend(node.children)
    return tuple(calls)


def _pair_with_nodes(
    arrows: list[_BareArrow], nodes: list[NodeInfo]
) -> list[tuple[NodeInfo, _BareArrow]]:
    """Match each bare arrow to the Function crg made from it.

    crg sets ``params`` for every function with a parameter list, so only nodes without one can
    come from a bare arrow; a real function with the same name and lines keeps its ``params``.
    """
    unmatched: defaultdict[_NameAt, list[_BareArrow]] = defaultdict(list)
    for arrow in arrows:
        unmatched[_NameAt(arrow.parameter, arrow.lines)].append(arrow)

    pairs = []
    for node in nodes:
        candidates = unmatched[_NameAt(node.name, Lines(node.line_start, node.line_end))]
        if node.kind == "Function" and node.params is None and candidates:
            pairs.append((node, candidates.pop(0)))
    return pairs


def _move_edges(
    parser: CodeParser,
    edges: list[EdgeInfo],
    callbacks: list[_Callback],
    unresolved_targets: dict[str, str],
) -> list[EdgeInfo]:
    """The file's edges as crg makes them when the callbacks are not Functions."""
    holding_edges = _holding_edges(edges, callbacks)
    by_name: defaultdict[str, list[_Callback]] = defaultdict(list)
    for callback in callbacks:
        by_name[callback.qualified_name].append(callback)

    moved = []
    for index, edge in enumerate(edges):
        if index in holding_edges:
            continue
        edge = unresolved(edge, unresolved_targets, RULE_NAME)
        source = _source_callback(edge, by_name[edge.source])
        moved.extend(given_to_owner(parser, edge, source.owner, RULE_NAME) if source else [edge])
    return moved


def _holding_edges(edges: list[EdgeInfo], callbacks: list[_Callback]) -> set[int]:
    """The index of the one CONTAINS edge that holds each callback.

    A real function with the same qualified name on the same line has its own CONTAINS edge,
    so exactly one edge is taken per callback.
    """
    contains: defaultdict[_ContainsAt, list[int]] = defaultdict(list)
    for index, edge in enumerate(edges):
        if edge.kind == "CONTAINS":
            contains[_ContainsAt(edge.target, edge.line)].append(index)

    taken = set()
    for callback in callbacks:
        indexes = contains[_ContainsAt(callback.qualified_name, callback.node.line_start)]
        if indexes:
            taken.add(indexes.pop())
    return taken


def _source_callback(edge: EdgeInfo, candidates: list[_Callback]) -> _Callback | None:
    """The callback *edge* comes from.

    Callbacks can share a qualified name and a line, so a CALLS edge goes to a callback that
    makes that call and has not given it away yet; other edges to the innermost callback.
    """
    holding = [callback for callback in candidates if spans(callback.node, edge.line)]
    if edge.kind == "CALLS":
        site = CallSite(edge.line, last_part(edge.target))
        for callback in holding:
            if callback.unclaimed_calls[site] > 0:
                callback.unclaimed_calls[site] -= 1
                return callback
    return min(holding, key=_Callback.span, default=None)
