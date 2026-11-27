"""How the Ruby rules read a syntax node, and how they change an edge the way crg would."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo


# tree-sitter's Ruby assignments, and whether each reads the attribute before it writes it:
# `a.b = v` only writes (a call to `b=`); `a.b += v` and `a.b ||= v` read `a.b` first (`b`, `b=`).
READS_BEFORE_WRITING = {"assignment": False, "operator_assignment": True}
_LEFT_FIELD = "left"
# `class Devise::Mapping`: the class's own name is the last part of a scoped name.
_SCOPED_NAME_SYNTAX = "scope_resolution"
_SETTER_SUFFIX = "="


class Lines(NamedTuple):
    """The first and last line of a syntax node, counted from 1."""

    first: int
    last: int


def lines(node: Node) -> Lines:
    """The lines *node* spans."""
    return Lines(node.start_point[0] + 1, node.end_point[0] + 1)


def text(node: Node) -> str:
    """The source text of *node*."""
    return node.text.decode("utf-8", errors="replace")


def last_part(qualified_name: str) -> str:
    """The plain name at the end of a qualified name: ``f`` for ``path::A.f``."""
    return qualified_name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def names_called(call: Node, method: Node) -> list[str]:
    """The methods *call* runs: `b=` for the target of `a.b = v`, `b` and `b=` for `a.b += v`."""
    assignment = call.parent
    if (
        assignment is None
        or assignment.type not in READS_BEFORE_WRITING
        or assignment.child_by_field_name(_LEFT_FIELD) != call
    ):
        return [text(method)]
    setter = text(method) + _SETTER_SUFFIX
    return [text(method), setter] if READS_BEFORE_WRITING[assignment.type] else [setter]


def crg_class(classes: list[NodeInfo], syntax: Node) -> NodeInfo | None:
    """crg's Class node for the class or module *syntax*, or None unless exactly one matches.

    The lines alone are not enough: `class A; class B; end; end` gives both the same lines.
    """
    name = syntax.child_by_field_name("name")
    if name is not None and name.type == _SCOPED_NAME_SYNTAX:
        name = name.child_by_field_name("name")
    if name is None:
        return None
    matches = [
        node for node in classes
        if (node.line_start, node.line_end) == lines(syntax) and node.name == text(name)
    ]
    return matches[0] if len(matches) == 1 else None


def file_path_of(nodes: list[NodeInfo]) -> str:
    """The parsed file's path, from crg's File node: a rule is not given the path."""
    for node in nodes:
        if node.kind == "File":
            return node.file_path
    raise ValueError(f"no File node among crg's {len(nodes)} nodes: the file's path is unknown")


def changed(edge: EdgeInfo, rule_name: str, **fields: Any) -> EdgeInfo:
    """*edge* with *fields* changed, tagged with the rule that changed it."""
    return dataclasses.replace(edge, **fields, extra={**edge.extra, "fork_rule": rule_name})


# `extra` key on a Function a rule adds for a class method that has no `def self.name` in the
# syntax (`mattr_accessor :name`), so rules that read class methods from the syntax see it too.
CLASS_METHOD_KEY = "ruby_class_method"


def relinked(
    parser: CodeParser,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
    file_path: str,
    names: set[str],
    rule_name: str,
) -> list[EdgeInfo]:
    """*edges* with each call to one of *names* linked again by crg's same-file linking.

    A rule that adds Functions calls this so crg's linking sees them; an edge it links elsewhere
    names *rule_name*.
    """
    # crg links a Ruby call only within its file while it parses, so every link here is local
    indexes = [
        index for index, edge in enumerate(edges)
        if edge.kind == "CALLS" and last_part(edge.target) in names
    ]
    bare = [dataclasses.replace(edges[i], target=last_part(edges[i].target)) for i in indexes]
    linked = parser._resolve_call_targets(nodes, bare, file_path)
    result = list(edges)
    # crg resolves edge for edge; a different count is not understood and fails the rule
    for index, edge in zip(indexes, linked, strict=True):
        if edge.target != edges[index].target:
            result[index] = changed(edge, rule_name)
    return result
