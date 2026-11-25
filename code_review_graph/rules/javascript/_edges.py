"""How a rule changes crg's edges the way crg itself would, and tags each edge it changes."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...parser import CodeParser, EdgeInfo, NodeInfo


def changed(edge: EdgeInfo, rule_name: str, **fields: Any) -> EdgeInfo:
    """*edge* with *fields* changed, tagged with the rule that changed it."""
    return dataclasses.replace(edge, **fields, extra={**edge.extra, "fork_rule": rule_name})


def spans(node: NodeInfo, line: int) -> bool:
    """Whether *line* is within *node*'s lines."""
    return node.line_start <= line <= node.line_end


def unresolved_targets(
    parser: CodeParser, removed: list[NodeInfo], kept: list[NodeInfo]
) -> dict[str, str]:
    """For each removed Function no kept node replaces, the target crg writes in its place.

    crg points a call such as ``resolve()`` at a same-file Function ``resolve``; with no such
    Function, the target is the name as written.
    """
    names = {node.name for node in removed}
    still_defined = {parser._node_qualified(node) for node in kept if node.name in names}
    return {
        parser._node_qualified(node): node.name
        for node in removed
        if parser._node_qualified(node) not in still_defined
    }


def unresolved(edge: EdgeInfo, targets: dict[str, str], rule_name: str) -> EdgeInfo:
    """*edge*, pointing at the plain name wherever it pointed at a removed Function."""
    if edge.target in targets:
        edge = changed(edge, rule_name, target=targets[edge.target])
    if edge.kind == "TESTED_BY" and edge.source in targets:
        # The reverse of a test's call to the removed Function: it follows the call to the name.
        edge = changed(edge, rule_name, source=targets[edge.source])
    return edge


def given_to_owner(
    parser: CodeParser, edge: EdgeInfo, owner: NodeInfo | None, rule_name: str
) -> list[EdgeInfo]:
    """*edge* moved to *owner* or the file, with the TESTED_BY crg adds when *owner* is a test."""
    owner_name = parser._node_qualified(owner) if owner else edge.file_path
    moved = changed(edge, rule_name, source=owner_name)
    if moved.kind == "CALLS" and owner is not None and owner.is_test:
        tested_by = dataclasses.replace(
            moved, kind="TESTED_BY", source=moved.target, target=owner_name
        )
        return [moved, tested_by]
    return [moved]
