"""Give a Ruby class its superclass and included modules as INHERITS edges, as crg does elsewhere.

crg 2.4.0's ``_get_bases`` reads base classes for 13 languages and none for Ruby, so a Ruby class
gets no INHERITS edge and crg's inheritance index has no chain to walk (SYN-F-633)::

    class Devise::ConfirmationsController < DeviseController   # crg 2.4.0: no INHERITS edge
      include Devise::Controllers::ScopedViews                  # nor here
    end

The rule writes the edge crg writes for a base class in other languages: from the class to the
parent as written (``DeviseController``, ``Devise::Controllers::ScopedViews``), on the class's
first line. A parent is a superclass written as a constant, or a module that a statement of the
class body includes by a constant (``include A, B::C``); ``extend`` adds methods to the class
itself, not to its objects, and is left alone, as are parents built at run time.

Each edge also carries ``ruby_ancestor``, the parent's place in Ruby's method lookup order
(``Module#ancestors``), for crg's inheritance index to walk: the last ``include`` statement first
(``include A, B`` puts ``A`` before ``B``), then the superclass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ... import parser as crg
from ._syntax import crg_class, file_path_of, lines, text

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "class-parents"

_CLASS_SYNTAX = frozenset({"class", "module"})
# A parent the source names: `Base`, `Shop::Base`, `::Base`.
_CONSTANT_SYNTAX = frozenset({"constant", "scope_resolution"})
_SUPERCLASS_FIELD, _BODY_FIELD = "superclass", "body"
_INCLUDE = "include"
_INHERITS = "INHERITS"
# `extra` key: the parent's place in Ruby's lookup order, from 1 (read by crg's InheritanceIndex).
ANCESTOR_KEY = "ruby_ancestor"


def add_class_parents(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Add an INHERITS edge from each Ruby class to each parent the source names."""
    root = parser._get_parser(language).parse(source).root_node
    classes = [node for node in nodes if node.kind == "Class"]
    added: list[EdgeInfo] = []
    for syntax in _class_syntax(root):
        parents = [*_included_modules(syntax), *_superclass(syntax)]  # Ruby's lookup order
        owner = crg_class(classes, syntax) if parents else None
        if owner is None:
            continue
        file_path = file_path_of(nodes)
        added += [
            crg.EdgeInfo(
                kind=_INHERITS, source=parser._node_qualified(owner), target=parent,
                file_path=file_path, line=lines(syntax).first,
                extra={"fork_rule": RULE_NAME, ANCESTOR_KEY: place},
            )
            for place, parent in enumerate(parents, start=1)
        ]
    return (nodes, [*edges, *added]) if added else (nodes, edges)


def _class_syntax(root: Node) -> list[Node]:
    """Every `class` and `module` in the file, in source order."""
    found = []
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.children))
        if node.type in _CLASS_SYNTAX:
            found.append(node)
    return found


def _superclass(syntax: Node) -> list[str]:
    """The superclass of `class A < Base`, when it is written as a constant."""
    superclass = syntax.child_by_field_name(_SUPERCLASS_FIELD)
    if superclass is None:
        return []
    return [text(child) for child in superclass.named_children if child.type in _CONSTANT_SYNTAX]


def _included_modules(syntax: Node) -> list[str]:
    """The modules the class body includes by a constant (`include A, B::C`), in lookup order.

    Each `include` puts its modules in front of those included before it.
    """
    body = syntax.child_by_field_name(_BODY_FIELD)
    included: list[str] = []
    for statement in body.named_children if body is not None else []:
        method = statement.child_by_field_name("method") if statement.type == "call" else None
        if (
            method is None
            or text(method) != _INCLUDE
            or statement.child_by_field_name("receiver") is not None
        ):
            continue
        arguments = statement.child_by_field_name("arguments")
        included[:0] = [
            text(argument) for argument in arguments.named_children
            if argument.type in _CONSTANT_SYNTAX
        ] if arguments is not None else []
    return included
