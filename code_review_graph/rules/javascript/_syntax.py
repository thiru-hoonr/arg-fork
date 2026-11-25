"""How crg reads JavaScript syntax: which functions it names, and which node gets a call."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import NodeInfo

# The syntax crg stores as a Function or Test when it can name it.
FUNCTION_SYNTAX = frozenset({
    "arrow_function",
    "function_declaration",
    "function_expression",
    "generator_function",
    "generator_function_declaration",
    "method_definition",
})
# crg names an unnamed function after these parents, from the field given: `const a = () => …`.
NAME_FIELD_OF_PARENT = {
    "assignment_expression": "left",
    "public_field_definition": "name",
    "variable_declarator": "name",
}
# crg records a call for each of these, with the callee in the field given; JSX counts as a call.
CALL_SYNTAX = {
    "call_expression": "function",
    "jsx_opening_element": "name",
    "jsx_self_closing_element": "name",
}


class Lines(NamedTuple):
    """The first and last line of a syntax node or a stored node, counted from 1."""

    first: int
    last: int


class Scope(NamedTuple):
    """A syntax node crg may store as a node: a function, or a call it stores as a Test."""

    lines: Lines
    name: str | None
    is_call: bool


class CallSite(NamedTuple):
    """A call as crg records it: the line it starts on and the callee's last name."""

    line: int
    callee: str


def enclosing_scopes(node: Node) -> tuple[Scope, ...]:
    """The scopes around *node*, innermost first."""
    scopes = []
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type in FUNCTION_SYNTAX:
            scopes.append(function_scope(ancestor))
        elif ancestor.type == "call_expression":
            scopes.append(Scope(lines(ancestor), None, is_call=True))
        ancestor = ancestor.parent
    return tuple(scopes)


def function_scope(function: Node) -> Scope:
    """The name and lines crg gives *function*: its parent's when the parent names it."""
    name_field = NAME_FIELD_OF_PARENT.get(function.parent.type)
    if name_field is not None:
        name = function.parent.child_by_field_name(name_field)
        return Scope(lines(function.parent), text(name), is_call=False)
    own_name = function.child_by_field_name("name")
    return Scope(lines(function), text(own_name) if own_name else None, is_call=False)


def by_lines(nodes: list[NodeInfo]) -> defaultdict[Lines, list[NodeInfo]]:
    """The stored nodes grouped by the lines they span."""
    index: defaultdict[Lines, list[NodeInfo]] = defaultdict(list)
    for node in nodes:
        index[Lines(node.line_start, node.line_end)].append(node)
    return index


def owner(
    scopes: tuple[Scope, ...], nodes_by_lines: defaultdict[Lines, list[NodeInfo]]
) -> NodeInfo | None:
    """The stored node of the innermost scope: the node crg gives a call made in *scopes*."""
    for scope in scopes:
        kinds = {"Test"} if scope.is_call else {"Function", "Test"}
        for node in nodes_by_lines[scope.lines]:
            if node.kind in kinds and (scope.is_call or node.name == scope.name):
                return node
    return None


def call_site(call: Node) -> CallSite | None:
    """The line and callee crg records for *call*, or None when *call* is not a call."""
    callee_field = CALL_SYNTAX.get(call.type)
    callee = call.child_by_field_name(callee_field) if callee_field else None
    if callee is None:
        return None
    return CallSite(lines(call).first, text(callee.child_by_field_name("property") or callee))


def lines(node: Node) -> Lines:
    """The lines *node* spans."""
    return Lines(node.start_point[0] + 1, node.end_point[0] + 1)


def last_part(qualified_name: str) -> str:
    """The plain name at the end of a qualified name: ``f`` for ``path::A.f``."""
    return qualified_name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def text(node: Node) -> str:
    """The source text of *node*."""
    return node.text.decode("utf-8", errors="replace")
