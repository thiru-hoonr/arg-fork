"""Mark a Ruby call made on ``self`` inside a method of a class with ``receiver: self``.

crg's resolver looks a call up in the caller's class, and then up its parents, when the call's
``extra`` says ``receiver: self`` (``self.x()``, ``this.x()``). crg 2.4.0 writes no receiver for a
Ruby call, so a call on ``self`` is never looked up in its class's parents (SYN-F-633)::

    class Devise::ConfirmationsController < DeviseController
      def create
        self.resource = resource_class.send_confirmation_instructions(resource_params)
        # `resource=`, `resource_class`, `resource_params`: all on self, all in DeviseController
      end
    end

The rule marks each call in a method of a class (a Function or Test with a parent) that has no
receiver or the receiver ``self``: a call crg's receiver reader lists without a receiver, a bare
name (an identifier crg's tree has on that line, which rule ``bare-name-calls`` stored as a call),
and a setter call ``self.x = v`` (named ``x=`` by rule ``setter-assignments``). The target becomes
the plain name, so the resolver looks it up: the class's own method first, then its parents'. A
call crg linked in the file to a method of the caller's own class is left as it is, since that is
the method that runs, and so is one linked to a top-level ``def``, which every object can call;
one crg linked to a same-named method of another class in the file is not.
A call on an object or a class is left alone; so is every call crg stamped with a class or
``class-receiver-calls`` unlinked, as each of those has a receiver. A call in a class method
(``def self.x``, a ``def`` inside ``class << self``) is marked too, with ``ruby_class_method``: crg
stores such a method as a method of the class, but there ``self`` is the class itself, so only the
class's own method answers it (``mattr_accessor :x`` makes one), never an inherited instance method.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Iterator, NamedTuple

from ._syntax import Lines, changed, last_part, lines, text

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "self-receivers"

_RECEIVER, _SELF = "receiver", "self"
# `extra` key on a call made in a class method (read by crg's resolver, graph.py).
CLASS_METHOD_KEY = "ruby_class_method"
_METHOD_KINDS = frozenset({"Function", "Test"})
_IDENTIFIER = "identifier"
_SETTER_SUFFIX = "="
_CLASS_METHOD, _METHOD, _SINGLETON_CLASS = "singleton_method", "method", "singleton_class"
_CLASS_SYNTAX = frozenset({"class", "module"})


class Place(NamedTuple):
    """A call's line and the name it is written with."""

    line: int
    name: str


def mark_calls_on_self(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Mark each call on `self` in a method of a class with `receiver: self`."""
    root = parser._get_parser(language).parse(source).root_node
    # (line, method) -> one (has a receiver, class) per call crg reads, in the order of its edges;
    # crg counts `self.x` as having no receiver
    receivers = parser._collect_ruby_test_receivers(root)
    calls = _CallsOnSelf(
        {Place(*place): iter(found) for place, found in receivers.items()}, _identifiers(root),
    )
    # by line, not by method: crg names `def self.build` and `def build` both `A.build`
    class_methods = _class_method_lines(root)
    methods = {
        parser._node_qualified(node) for node in nodes
        if node.kind in _METHOD_KINDS and node.parent_name
    }
    marked = []
    for edge in edges:
        # every call is read, in order, so crg's calls on a line pair with their own edges
        place = Place(edge.line, last_part(edge.target))
        on_self = edge.kind == "CALLS" and calls.is_on_self(place)
        if on_self and edge.source in methods and not _is_settled(edge):
            in_class_method = any(first <= edge.line <= last for first, last in class_methods)
            edge = _on_self(edge, in_class_method)
        marked.append(edge)
    return nodes, marked


def _class_method_lines(root: Node) -> list[Lines]:
    """The lines of every method whose `self` is the class: `def self.x`, or a `def` in
    `class << self`."""
    found = []
    pending = [(root, False)]
    while pending:
        node, in_singleton_class = pending.pop()
        if node.type == _CLASS_METHOD or (node.type == _METHOD and in_singleton_class):
            found.append(lines(node))
        if node.type == _SINGLETON_CLASS:
            in_singleton_class = True
        elif node.type in _CLASS_SYNTAX:
            in_singleton_class = False  # a class written inside `class << self` has instances
        pending.extend((child, in_singleton_class) for child in node.children)
    return found


def _is_settled(edge: EdgeInfo) -> bool:
    """Whether crg's link in the file answers it: the caller's own class or a top-level def."""
    if "::" not in edge.target:
        return False  # unlinked
    target_class = edge.target.partition("::")[2].rpartition(".")[0]
    return not target_class or edge.target.rpartition(".")[0] == edge.source.rpartition(".")[0]


def _identifiers(root: Node) -> set[Place]:
    """Every identifier in the tree, by line and name: where a bare call can be.

    A method's name (`item.total`, `def total`) is an identifier too, but its call has its own
    entry in crg's receiver list, which is read first.
    """
    found = set()
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        if node.type == _IDENTIFIER:
            found.add(Place(lines(node).first, text(node)))
    return found


class _CallsOnSelf:
    """Which call edges are made on `self`, read in the order crg writes them."""

    def __init__(self, pending: dict[Place, Iterator[tuple[bool, object]]], names: set[Place]):
        self._pending = pending
        self._names = names
        # the last call read at a place, for the setter call that follows its own getter
        self._last_on_self: dict[Place, bool] = {}

    def is_on_self(self, place: Place) -> bool:
        """Whether the call at *place* has no receiver or the receiver `self`."""
        on_self = self._next_on_self(place)
        if on_self is not None:
            return on_self
        setter_of = Place(place.line, place.name.removesuffix(_SETTER_SUFFIX))
        if place.name.endswith(_SETTER_SUFFIX) and setter_of in self._pending:
            # `self.x = v` is crg's call `x`, renamed `x=`; `self.x += v` adds `x=` beside `x`
            on_self = self._next_on_self(setter_of)
            return self._last_on_self.get(setter_of, False) if on_self is None else on_self
        return place in self._names  # a bare name: no call in crg's tree, past its calls

    def _next_on_self(self, place: Place) -> bool | None:
        """Whether crg's next call at *place* is on `self`; None when it has no more there."""
        found = next(self._pending.get(place, iter(())), None)
        if found is None:
            return None
        self._last_on_self[place] = not found[0]
        return not found[0]


def _on_self(edge: EdgeInfo, in_class_method: bool) -> EdgeInfo:
    """*edge* as a call on `self`: the plain name, for crg's resolver to look up in the class."""
    class_method = {CLASS_METHOD_KEY: True} if in_class_method else {}
    extra = {**edge.extra, _RECEIVER: _SELF, **class_method}
    marked = dataclasses.replace(edge, extra=extra)
    return changed(marked, RULE_NAME, target=last_part(edge.target))
