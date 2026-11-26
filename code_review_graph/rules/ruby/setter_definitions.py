"""Give a Ruby setter (``def name=``, ``def self.name=``) the Function crg gives a method.

tree-sitter-ruby names a setter with a ``setter`` node, and crg 2.4.0's ``_get_name`` reads no such
node, so it stores no Function for it (SYN-F-632)::

    class DeviseController
      def resource=(new_resource)          # crg 2.4.0: no Function, no CONTAINS
        instance_variable_set(:"@#{resource_name}", new_resource)   # a call from the file
      end
    end

Every call to the setter stays unlinked (``self.resource = x``, which rule ``setter-assignments``
stores as a call to ``resource=``), and the calls in its body are made from the file.

The twin is the same file with each setter renamed to the plain name of the same length
(``resource=`` → ``resource_``), which crg extracts: a Function in the class (or in the class
around ``class << self``), its CONTAINS edge, and the body's calls made from the method. The rule
extracts that twin and adds its Functions and CONTAINS edges with the setter's own name, makes the
body's calls from the new Function, and links the file's calls to the setters again with crg's own
same-file linking. A setter on another object (``def config.mode=``) is left alone: crg names
``def config.mode`` after ``config``, so the twin has no Function to take.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ._syntax import Lines, changed, file_path_of, lines, relinked, text

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "setter-definitions"

# A `def name` and a `def obj.name`; a setter's `name` field is a `setter` node (`name=`).
_DEFINITION_SYNTAX = frozenset({"method", "singleton_method"})
_SETTER_SYNTAX = "setter"
_NAME_FIELD = "name"
# The twin's name for `name=`: the same length, so every byte and line stays where it is.
_TWIN_ENDING = b"_"
# The edges crg makes from the method a statement is in, or from the file outside any method.
_FROM_THE_ENCLOSING_METHOD = frozenset({"CALLS", "REFERENCES"})
_CONTAINS, _TESTED_BY = "CONTAINS", "TESTED_BY"
_METHOD_KINDS = frozenset({"Function", "Test"})


class SetterDefinition(NamedTuple):
    """A `def name=`: its name, its twin's name, the byte of its `=`, and the lines it spans."""

    name: str
    twin_name: str
    equals_byte: int
    lines: Lines


class AddedSetter(NamedTuple):
    """A Function the rule adds for a setter, and the setter it stands for."""

    node: NodeInfo
    setter: SetterDefinition


def add_setter_definitions(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Add the Function, CONTAINS edge and call links crg makes for each setter's twin."""
    setters = _setter_definitions(parser._get_parser(language).parse(source).root_node)
    if not setters:
        return nodes, edges
    file_path = file_path_of(nodes)
    twin_nodes, twin_edges = parser._extract_bytes(Path(file_path), _twin_source(source, setters))
    added = _setter_functions(twin_nodes, setters)
    if not added:
        return nodes, edges
    holders = _holders(parser, nodes, file_path, added)
    nodes = [*nodes, *(new.node for new in added)]
    edges = [*edges, *_contains_edges(parser, twin_edges, added)]
    edges = _with_body_edges_moved(parser, edges, added, holders)
    names = {new.node.name for new in added}
    return nodes, relinked(parser, nodes, edges, file_path, names, RULE_NAME)


def _setter_definitions(root: Node) -> list[SetterDefinition]:
    """Every setter definition (`def name=`, `def self.name=`) in the file, in source order."""
    found = []
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.children))
        if node.type not in _DEFINITION_SYNTAX:
            continue
        name = node.child_by_field_name(_NAME_FIELD)
        if name is None or name.type != _SETTER_SYNTAX:
            continue
        setter_name = text(name)
        twin_name = setter_name[:-1] + _TWIN_ENDING.decode()
        found.append(SetterDefinition(setter_name, twin_name, name.end_byte - 1, lines(node)))
    return found


def _twin_source(source: bytes, setters: list[SetterDefinition]) -> bytes:
    """*source* with each setter's `=` written `_`: the same methods under names crg reads."""
    twin = bytearray(source)
    for setter in setters:
        twin[setter.equals_byte:setter.equals_byte + 1] = _TWIN_ENDING
    return bytes(twin)


def _setter_functions(
    twin_nodes: list[NodeInfo], setters: list[SetterDefinition],
) -> list[AddedSetter]:
    """The twin's Function for each setter, under the setter's name.

    A `def config.mode=` finds none: crg names the twin's `def config.mode_` after `config`.
    """
    by_place = {(setter.twin_name, setter.lines.first): setter for setter in setters}
    added = []
    for twin_node in twin_nodes:
        setter = by_place.get((twin_node.name, twin_node.line_start))
        if setter is not None:
            node = dataclasses.replace(
                twin_node, name=setter.name, identity_name=setter.name,
                extra={**twin_node.extra, "fork_rule": RULE_NAME},
            )
            added.append(AddedSetter(node, setter))
    return added


def _holders(
    parser: CodeParser, nodes: list[NodeInfo], file_path: str, added: list[AddedSetter],
) -> list[str]:
    """What crg made each new Function's body edges from: the method around it, or the file."""
    methods = [node for node in nodes if node.kind in _METHOD_KINDS]
    holders = []
    for new in added:
        around = [
            node for node in methods
            if node.line_start <= new.node.line_start and new.node.line_end <= node.line_end
        ]
        innermost = max(around, key=lambda node: node.line_start, default=None)
        holders.append(file_path if innermost is None else parser._node_qualified(innermost))
    return holders


def _contains_edges(
    parser: CodeParser, twin_edges: list[EdgeInfo], added: list[AddedSetter],
) -> list[EdgeInfo]:
    """The twin's CONTAINS edge for each new Function, pointing at it under the setter's name."""
    twin_targets = {
        parser._node_qualified(_under_twin_name(new)): parser._node_qualified(new.node)
        for new in added
    }
    return [
        changed(edge, RULE_NAME, target=twin_targets[edge.target])
        for edge in twin_edges
        if edge.kind == _CONTAINS and edge.target in twin_targets
    ]


def _under_twin_name(new: AddedSetter) -> NodeInfo:
    """The new Function as the twin names it (`name_`), to find the twin's edges to it."""
    twin_name = new.setter.twin_name
    return dataclasses.replace(new.node, name=twin_name, identity_name=twin_name)


def _with_body_edges_moved(
    parser: CodeParser, edges: list[EdgeInfo], added: list[AddedSetter], holders: list[str],
) -> list[EdgeInfo]:
    """*edges* with the calls and references in each setter's body made from its new Function.

    crg makes them from what holds the setter (the file, or a method around it), as it knows no
    method there; the twin makes them from the setter. A method defined inside the setter keeps
    its own edges. Moving crg's edges keeps what the rules before this one added.
    """
    moved = list(edges)
    calls_moved_out_of: set[tuple[str, int, str]] = set()
    for new, holder in zip(added, holders, strict=True):
        source = parser._node_qualified(new.node)
        body = new.setter.lines
        for index, edge in enumerate(moved):
            if (
                edge.kind in _FROM_THE_ENCLOSING_METHOD
                and edge.source == holder
                and body.first <= edge.line <= body.last
            ):
                moved[index] = changed(edge, RULE_NAME, source=source)
                calls_moved_out_of.add((holder, edge.line, edge.target))
    return _without_tested_by_from(moved, calls_moved_out_of)


def _without_tested_by_from(
    edges: list[EdgeInfo], calls_moved_out_of: set[tuple[str, int, str]],
) -> list[EdgeInfo]:
    """*edges* without the TESTED_BY edge crg derived from each call the rule moved out of a test.

    crg makes `called TESTED_BY test` for a call made in a test; the twin makes the call from the
    setter, which is no test, so it has no such edge.
    """
    return [
        edge for edge in edges
        if not (
            edge.kind == _TESTED_BY
            and (edge.target, edge.line, edge.source) in calls_moved_out_of
        )
    ]
