"""Store a bare Ruby method name as the call it is, unless the name is a local variable there.

tree-sitter-ruby parses a name with no receiver, no arguments and no parentheses as an
``identifier``, and crg 2.4.0 only reads ``call`` nodes, so it stores no call for it::

    def update
      if resource.save      # crg 2.4.0: no call to `resource`; `resource().save` gets one
        redirect_to root_path
      end
    end

Ruby runs ``resource`` as a method call unless the name is a local variable at that point: a
parameter of the method, block or lambda, or a name assigned earlier in the same scope (Ruby
decides this as it reads, so ``x = x`` and a name assigned in an earlier ``if`` branch are
locals). On devise, crg missed 297 such calls that devise's tests really make.

The twin is the same call written with parentheses, ``resource()``, which crg extracts. The rule
writes that twin: it inserts ``()`` after every bare call it finds (at byte offsets from the
tree), lets crg extract the twin source, and adds the twin's calls at those places, tagged. Every
other node and edge is the one crg made for the file; ``()`` adds no line, so lines match. crg
derives a TESTED_BY edge from a call made in a test, so the twin's TESTED_BY edge comes too.

crg reads a bare name that names a top-level ``def`` of the file as a function reference when it
is an argument, an array element or a hash value (``helper(docs)``: REFERENCES to ``docs``, the
JavaScript callback pattern). In Ruby it is a call, and the twin has no reference there, so the
rule drops that REFERENCES edge.

A file tree-sitter cannot parse cleanly (an ERB template such as ``class Add<%= name %>``) is
left as crg made it: around a parse error, ``()`` can change what the rest of the file parses to.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ._syntax import changed, file_path_of, last_part, lines, text

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "bare-name-calls"

logger = logging.getLogger(__name__)

# tree-sitter-ruby's names for the syntax the rule reads.
_IDENTIFIER = "identifier"
_ASSIGNMENTS = frozenset({"assignment", "operator_assignment"})  # `x = v`, `x ||= v`
_UNARY = "unary"  # `defined?(x)`, `!x`, `-x`
_KEYWORD_PATTERN = "keyword_pattern"  # `in {name:}`, `in {name: Integer}`
# Field names: the target of an assignment, the pattern of `for` and `in`, a default value or a
# pattern's value, a pattern's key, a unary operator, and a block's parameters.
_LEFT, _PATTERN, _VALUE, _KEY, _OPERATOR = "left", "pattern", "value", "key", "operator"
_PARAMETERS = "parameters"
# Syntax whose `pattern` field names locals: `for x in list`, `in [a, b]`.
_PATTERN_BINDERS = frozenset({"for", "in_clause"})
# A `def`, `class` or `module` starts an empty scope: the locals around it are not seen inside.
_SCOPE_GATES = frozenset({
    "program", "method", "singleton_method", "class", "module", "singleton_class",
})
# The parts of a scope gate Ruby reads in the scope around it: `class A < base`, `class << obj`.
_READ_OUTSIDE = frozenset({("class", "superclass"), ("singleton_class", "value")})
# A block or lambda sees the locals around it; its own locals end with it.
_BLOCKS = frozenset({"block", "do_block", "lambda"})
# A block with no parameters can read its arguments as `it` (Ruby 3.4) or `_1` to `_9`.
_IMPLICIT_PARAMETERS = frozenset({"it", *(f"_{n}" for n in range(1, 10))})
# Syntax that only names locals (parameters, assignment targets, patterns); a `value` field in it
# is an expression (`def a(x = default)`).
_BINDING_SYNTAX = frozenset({
    "method_parameters", "block_parameters", "lambda_parameters", "optional_parameter",
    "keyword_parameter", "splat_parameter", "hash_splat_parameter", "block_parameter",
    "destructured_parameter", "left_assignment_list", "destructured_left_assignment",
    "rest_assignment", "exception_variable", "array_pattern", "find_pattern", "hash_pattern",
    "alternative_pattern", "as_pattern", _KEYWORD_PATTERN,
})
# Where an identifier is not a call Ruby would take `()` for: a method's name (`x.name`,
# `def name`, `def self.name`) and the object of `def obj.name`.
_NOT_A_CALL_FIELDS = frozenset({
    ("call", "method"), ("method", "name"), ("singleton_method", "name"),
    ("singleton_method", "object"),
})
# Syntax whose names never run: `alias new old`, `undef name`, `def name=`.
_NAMES_ONLY = frozenset({"alias", "undef", "setter"})
_DEFINED = "defined?"
# Keywords tree-sitter-ruby reads as identifiers; they are values, never calls (`__FILE__()` is
# not Ruby). `__method__` and `__dir__` are methods, and stay calls.
_KEYWORD_VALUES = frozenset({"__FILE__", "__LINE__", "__ENCODING__"})
# The edges crg makes for a call, and the end of each edge that names the called method.
_CALL_EDGES = {"CALLS": "target", "TESTED_BY": "source"}
# In a test, crg stamps every call it reads on a receiver (`report.text`) with this key, and
# leaves a call with no receiver unstamped: a bare call's edge is an unstamped one.
_RECEIVER_STAMP = "receiver_binding"


class BareCall(NamedTuple):
    """A bare name Ruby runs as a call: its line, its name, and the byte it ends at."""

    line: int
    name: str
    end_byte: int


def store_bare_names_as_calls(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Add the call crg makes for `name()` wherever a bare `name` is a call, not a local."""
    root = parser._get_parser(language).parse(source).root_node
    if root.has_error:
        logger.debug("fork rule %s skipped a file that does not parse cleanly", RULE_NAME)
        return nodes, edges
    bare_calls = _bare_calls(root)
    if not bare_calls:
        return nodes, edges
    file_path = Path(file_path_of(nodes))
    _, twin_edges = parser._extract_bytes(file_path, _twin_source(source, bare_calls))
    return nodes, _without_references_at(edges, bare_calls) + _calls_at(twin_edges, bare_calls)


def _bare_calls(root: Node) -> list[BareCall]:
    """Every identifier that Ruby runs as a method call."""
    found: list[BareCall] = []
    _visit(root, set(), found)
    return found


def _visit(node: Node, locals_: set[str], found: list[BareCall]) -> None:
    """Walk *node* in source order with the *locals_* in scope, recording bare calls in *found*.

    Source order matters: Ruby makes a name a local from its first assignment onwards.
    """
    if node.type in _NAMES_ONLY or _is_defined_check(node):
        return
    outside = locals_
    if node.type in _SCOPE_GATES:
        locals_ = set()
    elif node.type in _BLOCKS:
        locals_ = set(locals_)
        if node.child_by_field_name(_PARAMETERS) is None:
            locals_ |= _IMPLICIT_PARAMETERS
    if node.type == _IDENTIFIER:
        name = text(node)
        if _is_call_position(node) and name not in locals_ and name not in _KEYWORD_VALUES:
            found.append(BareCall(lines(node).first, name, node.end_byte))
        return
    for index, child in enumerate(node.children):
        field = node.field_name_for_child(index)
        if _names_locals(node, field, child):
            _bind(child, locals_, found)
        else:
            _visit(child, outside if (node.type, field) in _READ_OUTSIDE else locals_, found)


def _is_defined_check(node: Node) -> bool:
    """Whether *node* is `defined?(x)`, which asks about `x` without running it."""
    operator = node.child_by_field_name(_OPERATOR) if node.type == _UNARY else None
    return operator is not None and text(operator) == _DEFINED


def _is_call_position(identifier: Node) -> bool:
    """Whether *identifier* stands where Ruby would run it as a call, not where it names one."""
    parent = identifier.parent
    return parent is None or not any(
        parent.type == syntax and parent.child_by_field_name(field) == identifier
        for syntax, field in _NOT_A_CALL_FIELDS
    )


def _names_locals(parent: Node, field: str | None, child: Node) -> bool:
    """Whether *child* introduces locals: parameters, an assignment target, a pattern."""
    if child.type in _BINDING_SYNTAX:
        return True
    if parent.type in _ASSIGNMENTS and field == _LEFT:
        return True
    return parent.type in _PATTERN_BINDERS and field == _PATTERN


def _bind(node: Node, locals_: set[str], found: list[BareCall]) -> None:
    """Add the names *node* introduces to *locals_*; the expressions inside it are visited."""
    if node.type == _IDENTIFIER:
        locals_.add(text(node))
    elif node.type == _KEYWORD_PATTERN and node.child_by_field_name(_VALUE) is None:
        key = node.child_by_field_name(_KEY)  # `in {name:}` binds `name`
        if key is not None:
            locals_.add(text(key).rstrip(":"))
    elif node.type not in _BINDING_SYNTAX:
        _visit(node, locals_, found)  # `self.x = v`, `a[i] = v`, `in Integer`: expressions
    else:
        for index, child in enumerate(node.children):
            if _is_expression_part(node, node.field_name_for_child(index)):
                _visit(child, locals_, found)
            else:
                _bind(child, locals_, found)


def _is_expression_part(binding: Node, field: str | None) -> bool:
    """Whether the *field* of *binding* syntax is an expression: a default value
    (`def a(x = default)`), but not a pattern's value (`in {key: x}` binds `x`)."""
    return field == _VALUE and binding.type != _KEYWORD_PATTERN


def _twin_source(source: bytes, bare_calls: list[BareCall]) -> bytes:
    """*source* with `()` after every bare call: the same calls written the way crg reads them."""
    twin = source
    for end_byte in sorted({call.end_byte for call in bare_calls}, reverse=True):
        twin = twin[:end_byte] + b"()" + twin[end_byte:]
    return twin


def _without_references_at(edges: list[EdgeInfo], bare_calls: list[BareCall]) -> list[EdgeInfo]:
    """*edges* without crg's REFERENCES edge for a bare call: the name is called, not passed."""
    sites = Counter((call.line, call.name) for call in bare_calls)
    kept = []
    for edge in edges:
        site = (edge.line, last_part(edge.target))
        if edge.kind == "REFERENCES" and sites[site] > 0:
            sites[site] -= 1
        else:
            kept.append(edge)
    return kept


def _calls_at(twin_edges: list[EdgeInfo], bare_calls: list[BareCall]) -> list[EdgeInfo]:
    """crg's call edges in the twin at the bare calls' places, tagged with this rule."""
    sites = Counter((call.line, call.name) for call in bare_calls)
    wanted = {kind: Counter(sites) for kind in _CALL_EDGES}
    added = []
    for edge in sorted(twin_edges, key=lambda twin_edge: _RECEIVER_STAMP in twin_edge.extra):
        if edge.kind not in _CALL_EDGES:
            continue
        site = (edge.line, last_part(getattr(edge, _CALL_EDGES[edge.kind])))
        if wanted[edge.kind][site] > 0:
            wanted[edge.kind][site] -= 1
            added.append(changed(edge, RULE_NAME))
    return added
