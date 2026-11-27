"""Store Ruby's ``super`` as a call to the parent's method of the same name.

``super`` in a Ruby method runs the method of the same name one step up the class's ancestors.
tree-sitter-ruby gives it a ``super`` node, not a ``call`` with a method name, and crg 2.4.0
stores no call for it::

    class Devise::Strategies::Authenticatable < Base
      def store?
        super && !mapping.to.skip_session_storage.include?(authentication_type)
        # crg 2.4.0: no call; Ruby runs Base#store?
      end
    end

The rule adds the call crg writes for ``super.run()`` in other languages: from the method to its
own name, with ``extra["receiver"] = "super"``, which crg's resolver looks up in the parents of
the caller's class, never in the class itself (``GraphStore._class_backed_candidates``). Each
``super`` node is one call (``super``, ``super(x)``, ``super x``, ``super do … end``, and a
``super`` in a block of the method). A ``super`` in ``def self.x`` (a class method), in a method
crg has no Function for, or outside a method is left alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ... import parser as crg
from ._syntax import file_path_of, lines, text

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "super-calls"

_SUPER = "super"
_METHOD = "method"
# A class method, or a new scope: a `super` past one of these is not the instance method's.
_STOPS = frozenset({"singleton_method", "class", "module", "singleton_class", "program"})
_METHOD_KINDS = frozenset({"Function", "Test"})


def add_super_calls(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Add a call on `super` for each `super` in an instance method crg has a Function for."""
    root = parser._get_parser(language).parse(source).root_node
    methods = [node for node in nodes if node.kind in _METHOD_KINDS and node.parent_name]
    added: list[EdgeInfo] = []
    for node in _super_nodes(root):
        method = _enclosing_method(node)
        function = _crg_function(methods, method) if method is not None else None
        if function is None:
            continue
        added.append(crg.EdgeInfo(
            kind="CALLS", source=parser._node_qualified(function), target=function.name,
            file_path=file_path_of(nodes), line=lines(node).first,
            extra={"receiver": _SUPER, "fork_rule": RULE_NAME},
        ))
    return (nodes, [*edges, *added]) if added else (nodes, edges)


def _super_nodes(root: Node) -> list[Node]:
    """Every `super` in the file, in source order."""
    found = []
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.children))
        if node.type == _SUPER:
            found.append(node)
    return found


def _enclosing_method(node: Node) -> Node | None:
    """The instance method `def` *node* is in, or None past a class method or a new scope."""
    parent = node.parent
    while parent is not None and parent.type not in _STOPS:
        if parent.type == _METHOD:
            return parent
        parent = parent.parent
    return None


def _crg_function(methods: list[NodeInfo], method: Node) -> NodeInfo | None:
    """crg's Function for the `def` *method*, or None unless exactly one matches."""
    name = method.child_by_field_name("name")
    if name is None:
        return None
    matches = [
        node for node in methods
        if node.line_start == lines(method).first and node.name == text(name)
    ]
    return matches[0] if len(matches) == 1 else None
