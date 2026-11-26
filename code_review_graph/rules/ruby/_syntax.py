"""How the Ruby rules read a syntax node, and how they change an edge the way crg would."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import EdgeInfo, NodeInfo


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
