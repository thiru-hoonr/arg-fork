"""Store a Ruby attribute assignment as a call to the setter, as Ruby runs it.

``self.balance = amount`` and ``ledger.total = amount`` run the setter methods ``balance=`` and
``total=``. crg 2.4.0 stores each as a call named after the getter, ``balance`` or ``total``, and
links it to ``def balance`` when the file defines one: on devise, 21 of crg's wrong links were
``self.resource = …`` linked to ``def resource``. This rule makes crg store the setter's name
instead. crg stores no Function for ``def balance=``, so the setter call keeps its plain name.

``self.balance += amount`` and ``self.balance ||= amount`` read the getter before they call the
setter. crg's call to ``balance`` is right for the read, so the rule keeps it and adds the call
to ``balance=``.

crg has no twin for a setter call (``self.send(:balance=, x)`` gives ``send``), so the expected
graphs are written by hand in ``tests/test_ruby_setter_assignment_calls_the_setter.py``.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, NamedTuple

from ._syntax import READS_BEFORE_WRITING, changed, last_part, lines, text

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "setter-assignments"



class SetterCall(NamedTuple):
    """An attribute assignment: the line crg records its call on and the attribute's name."""

    line: int
    attribute: str
    reads_first: bool


def store_setter_assignments_as_setter_calls(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Give each attribute assignment a call to its setter; keep the getter call where it reads."""
    root = parser._get_parser(language).parse(source).root_node
    setter_calls = _setter_calls(root)
    if not setter_calls:
        return nodes, edges
    return nodes, _with_setter_calls(edges, setter_calls)


def _setter_calls(root: Node) -> list[SetterCall]:
    """Every assignment whose left side is a call: `self.b = v`, `a.b ||= v` (Ruby allows no
    assignment to a call without a receiver)."""
    found = []
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        if node.type not in READS_BEFORE_WRITING:
            continue
        target = node.child_by_field_name("left")
        # only a call has a `method` field: `@b = v`, `a[:b] = v` and `B = v` have none
        attribute = target.child_by_field_name("method") if target else None
        if attribute is not None:
            reads_first = READS_BEFORE_WRITING[node.type]
            found.append(SetterCall(lines(target).first, text(attribute), reads_first))
    return found


def _with_setter_calls(edges: list[EdgeInfo], setter_calls: list[SetterCall]) -> list[EdgeInfo]:
    """*edges* with crg's getter call renamed to the setter, or kept and joined by one."""
    to_rename = Counter((c.line, c.attribute) for c in setter_calls if not c.reads_first)
    to_add = Counter((c.line, c.attribute) for c in setter_calls if c.reads_first)
    result = []
    for edge in edges:
        site = (edge.line, last_part(edge.target))
        if edge.kind != "CALLS" or site not in to_rename and site not in to_add:
            result.append(edge)
            continue
        setter_call = changed(edge, RULE_NAME, target=f"{site[1]}=")
        if to_rename[site] > 0:
            to_rename[site] -= 1
            result.append(setter_call)
        elif to_add[site] > 0:
            to_add[site] -= 1
            result.extend([edge, setter_call])
        else:
            result.append(edge)  # a further call with this name on the line: a real getter call
    return result
