"""Unlink a Ruby call on a class from the instance method of the same name in its file.

crg 2.4.0 links ``receiver.name`` to the ``def name`` of the same file whatever the receiver is.
A call on a class runs a class method, never an instance method, so these links are wrong:

- ``resource_class.new`` linked to the controller's own ``def new`` action (devise: 5 controllers);
- ``self.class.timeout_in`` linked to the instance method ``def timeout_in``;
- ``Other.find(1)`` or ``Devise::Mapping.find(1)`` linked to this file's instance ``def find``.

A call is on a class when its receiver is a constant (``Other``), a scoped constant
(``Devise::Mapping``) or ``x.class``. A call named ``new`` is on a class too: ``new`` makes an
object and never runs an instance method called ``new``.

When every Function the call is linked to is an instance method (``def name``), the rule points
the call at the plain name and marks it unresolved (``unresolved_targets`` = [], which crg's
post-build pass leaves alone). A link to a class method (``def self.name`` or a method inside
``class << self``) is kept. crg gives ``def self.find`` and ``def find`` the same qualified name,
so a link that could be either is kept.

crg has no twin for these calls, so the expected graphs are written by hand in
``tests/test_ruby_class_receiver_call_is_not_an_instance_method.py``.
"""

from __future__ import annotations

import dataclasses
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, NamedTuple

from ._syntax import CLASS_METHOD_KEY, changed, last_part, lines, names_called, text

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "class-receiver-calls"

# Receivers that are a class: `Other.find`, `Devise::Mapping.find`.
_CLASS_RECEIVER_SYNTAX = frozenset({"constant", "scope_resolution"})
# `new` makes an object whatever the receiver: `resource_class.new`.
_CONSTRUCTOR = "new"
# `x.class.find` calls `find` on the object's class.
_CLASS_OF = "class"
# A method that belongs to the class: `def self.find`, or `def find` inside `class << self`.
_CLASS_METHOD_SYNTAX = "singleton_method"
_CLASS_BODY_SYNTAX = "singleton_class"


class CallName(NamedTuple):
    """A call as crg records it: the line it starts on and the called method's name."""

    line: int
    name: str


def unlink_class_receiver_calls_from_instance_methods(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Point each call on a class that crg linked to an instance method back at the plain name."""
    root = parser._get_parser(language).parse(source).root_node
    on_a_class, on_anything_else = _calls_by_receiver(root)
    if not on_a_class:
        return nodes, edges
    instance_methods_only = _instance_methods_only(parser, nodes, _class_method_lines(root))
    return nodes, [
        _unlinked(edge) if _is_wrong_link(edge, on_a_class, on_anything_else, instance_methods_only)
        else edge
        for edge in edges
    ]


def _calls_by_receiver(root: Node) -> tuple[Counter[CallName], Counter[CallName]]:
    """How many calls of each name on each line are on a class, and how many on anything else."""
    on_a_class: Counter[CallName] = Counter()
    on_anything_else: Counter[CallName] = Counter()
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        method = node.child_by_field_name("method") if node.type == "call" else None
        if method is None:
            continue
        # `A.x = v` runs `x=`: rule setter-assignments names its call so
        for name in names_called(node, method):
            call = CallName(lines(node).first, name)
            if _is_on_a_class(node.child_by_field_name("receiver"), call.name):
                on_a_class[call] += 1
            else:
                on_anything_else[call] += 1
    return on_a_class, on_anything_else


def _is_on_a_class(receiver: Node | None, name: str) -> bool:
    """Whether a call named *name* on *receiver* runs a class method."""
    if receiver is None:
        return False
    if name == _CONSTRUCTOR or receiver.type in _CLASS_RECEIVER_SYNTAX:
        return True
    method = receiver.child_by_field_name("method") if receiver.type == "call" else None
    return method is not None and text(method) == _CLASS_OF


def _class_method_lines(root: Node) -> set[int]:
    """The first line of every class method: `def self.find`, or a `def` in `class << self`."""
    found = set()
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        in_class_body = node.type == "method" and node.parent is not None and any(
            ancestor.type == _CLASS_BODY_SYNTAX for ancestor in _ancestors(node)
        )
        if node.type == _CLASS_METHOD_SYNTAX or in_class_body:
            found.add(lines(node).first)
    return found


def _ancestors(node: Node) -> list[Node]:
    """The syntax nodes around *node*, innermost first."""
    found = []
    ancestor = node.parent
    while ancestor is not None:
        found.append(ancestor)
        ancestor = ancestor.parent
    return found


def _instance_methods_only(
    parser: CodeParser, nodes: list[NodeInfo], class_method_lines: set[int]
) -> set[str]:
    """The qualified names crg gives only to instance methods in this file."""
    is_class_method: defaultdict[str, bool] = defaultdict(bool)
    for node in nodes:
        if node.kind == "Function":
            qualified = parser._node_qualified(node)
            is_class_method[qualified] |= (
                node.line_start in class_method_lines or node.extra.get(CLASS_METHOD_KEY, False)
            )
    return {qualified for qualified, class_method in is_class_method.items() if not class_method}


def _is_wrong_link(
    edge: EdgeInfo,
    on_a_class: Counter[CallName],
    on_anything_else: Counter[CallName],
    instance_methods_only: set[str],
) -> bool:
    """Whether crg linked a call that is only ever made on a class to an instance method."""
    if edge.kind != "CALLS" or edge.target not in instance_methods_only:
        return False
    call = CallName(edge.line, last_part(edge.target))
    return on_a_class[call] > 0 and on_anything_else[call] == 0


def _unlinked(edge: EdgeInfo) -> EdgeInfo:
    """*edge* pointing at the plain name, marked unresolved so no later pass links it again."""
    unlinked = changed(edge, RULE_NAME, target=last_part(edge.target))
    return dataclasses.replace(unlinked, extra={**unlinked.extra, "unresolved_targets": []})
