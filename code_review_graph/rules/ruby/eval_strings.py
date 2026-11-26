"""Read Ruby code given as a string to ``class_eval`` / ``module_eval`` / ``instance_eval`` as code.

crg 2.4.0 stores a string as a string, so the methods and calls in this devise code are missing::

    def self.define_helpers(mapping)
      class_eval <<-METHODS, __FILE__, __LINE__ + 1
        def authenticate_#{mapping}!(opts = {})
          warden.authenticate!(opts) if !devise_controller? || opts.delete(:force)
        end
      METHODS
    end

Ruby runs that string as code, and ``__FILE__, __LINE__ + 1`` makes it report the code's own lines.
The rule builds the twin: the file with each such string written in place as code (the eval call
and the heredoc's closing line left blank, so every line keeps its number), and gives it to crg's
own ``parse_bytes``, fork rules included. What crg finds on the string's lines is added.

A ``#{...}`` becomes the identifier ``crg_interpolation_``; a name that contains it is known only
at run time. Such a method gets no Function, and the calls inside it belong to the Function that
holds the eval call (the file, when none does); a call or link to such a name is dropped. A string
the twin cannot read as Ruby (an escape sequence, or a ``#{...}`` where a constant must be) is left
as crg made it, and logged.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ._syntax import Lines, changed, file_path_of, lines, text

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "eval-strings"

logger = logging.getLogger(__name__)

# The methods that run a string as code in a class or object; plain `eval` runs it in a binding
# the rule cannot see.
_EVAL_METHODS = frozenset({"class_eval", "module_eval", "instance_eval"})
_STRING_SYNTAX = "string"
_HEREDOC_START_SYNTAX = "heredoc_beginning"
_HEREDOC_BODY_SYNTAX = "heredoc_body"
# The text of a string or heredoc, as written.
_CODE_SYNTAX = frozenset({"string_content", "heredoc_content"})
_INTERPOLATION_SYNTAX = "interpolation"
# Ruby rewrites an escape sequence before it runs the string, so the text is not the code.
_ESCAPE_SYNTAX = "escape_sequence"
# What a `#{...}` becomes in the twin; a name that contains it is built at run time.
HOLE = "crg_interpolation_"


class CodeString(NamedTuple):
    """A string given as code to an eval method, and the bytes its twin replaces."""

    eval_line: int
    first_byte: int
    last_byte: int
    pieces: tuple[Node, ...]
    code_lines: Lines


def add_code_from_eval_strings(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Add what crg finds in each string given as code to an eval method, written in place."""
    ruby = parser._get_parser(language)
    strings = _code_strings(ruby.parse(source).root_node)
    if not strings:
        return nodes, edges
    file_path = file_path_of(nodes)
    readable = [string for string in strings if _is_readable(ruby, source, string, file_path)]
    if not readable:
        return nodes, edges
    twin_nodes, twin_edges = parser.parse_bytes(Path(file_path), _twin(source, readable))
    holders = _holders(parser, nodes, file_path, readable)
    return (
        nodes + _added_nodes(twin_nodes, nodes, holders),
        edges + _added_edges(twin_edges, edges, holders),
    )


def _code_strings(root: Node) -> list[CodeString]:
    """Every string or heredoc given as the first argument to an eval method, in source order."""
    found = []
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        method = node.child_by_field_name("method") if node.type == "call" else None
        arguments = node.child_by_field_name("arguments")
        if method is None or text(method) not in _EVAL_METHODS or arguments is None:
            continue
        if not arguments.named_children:  # `class_eval()`
            continue
        string = _code_string(node, arguments.named_children[0])
        if string is not None:
            found.append(string)
    return sorted(found, key=lambda string: string.first_byte)


def _code_string(call: Node, first_argument: Node) -> CodeString | None:
    """The code string *call* runs, when its first argument is a string or a heredoc."""
    eval_line = lines(call).first
    if first_argument.type == _STRING_SYNTAX:
        return CodeString(
            eval_line, call.start_byte, call.end_byte, tuple(first_argument.children),
            lines(first_argument),
        )
    body = call.next_named_sibling
    if first_argument.type != _HEREDOC_START_SYNTAX or body is None:
        return None
    # tree-sitter puts a heredoc's text after the line that opens it, beside the call. The body
    # starts with the line break that ends the opening line, and its last child is the closing
    # word (`RUBY`) on a line of its own: the code is on the lines between the two.
    closing = body.children[-1]
    code_lines = Lines(lines(body).first + 1, lines(closing).first - 1)
    return CodeString(eval_line, call.start_byte, body.end_byte, tuple(body.children), code_lines)


def _is_readable(ruby: Parser, source: bytes, string: CodeString, file_path: str) -> bool:
    """Whether *string*, written in place, is Ruby the rule can read; logs when it is not."""
    where = f"{file_path}:{string.eval_line}"
    if any(piece.type == _ESCAPE_SYNTAX for piece in string.pieces):
        logger.warning(
            "%s: escape sequence in eval string at %s, left as a string", RULE_NAME, where
        )
        return False
    if ruby.parse(_twin(source, [string])).root_node.has_error:
        logger.warning("%s: eval string at %s is not Ruby, left as a string", RULE_NAME, where)
        return False
    return True


def _twin(source: bytes, strings: list[CodeString]) -> bytes:
    """*source* with each of *strings* written in place as code, every line kept where it is."""
    parts = []
    cursor = 0
    for string in strings:
        # Going back would copy the rest of the file again, eval strings included, and every
        # parse of the twin would find them again.
        if string.first_byte < cursor:
            raise ValueError(f"{RULE_NAME}: eval strings out of source order or overlapping")
        parts.append(source[cursor:string.first_byte])
        cursor = string.first_byte
        for piece in string.pieces:
            if piece.type in _CODE_SYNTAX:
                parts += [_blank(source[cursor:piece.start_byte]), piece.text]
            elif piece.type == _INTERPOLATION_SYNTAX:
                # The hole joins the text around it (`current_#{m}` is one name); only the
                # interpolation's line breaks are kept.
                breaks = b"\n" * piece.text.count(b"\n")
                parts += [_blank(source[cursor:piece.start_byte]), HOLE.encode(), breaks]
            else:
                continue
            cursor = piece.end_byte
        parts.append(_blank(source[cursor:string.last_byte]))
        cursor = string.last_byte
    parts.append(source[cursor:])
    return b"".join(parts)


def _blank(code: bytes) -> bytes:
    """*code* as spaces, its line breaks kept."""
    return bytes(byte if byte == ord("\n") else ord(" ") for byte in code)


def _holders(
    parser: CodeParser, nodes: list[NodeInfo], file_path: str, strings: list[CodeString]
) -> dict[int, str]:
    """For each line of string code, the Function holding its eval call (or the file)."""
    holders = {}
    for string in strings:
        around = [
            node for node in nodes
            if node.kind == "Function" and node.line_start <= string.eval_line <= node.line_end
        ]
        innermost = max(around, key=lambda node: node.line_start, default=None)
        holder = file_path if innermost is None else parser._node_qualified(innermost)
        for line in range(string.code_lines.first, string.code_lines.last + 1):
            holders[line] = holder
    return holders


def _added_nodes(
    twin_nodes: list[NodeInfo], nodes: list[NodeInfo], holders: dict[int, str]
) -> list[NodeInfo]:
    """The twin's nodes on string code lines that crg did not already have, minus built names."""
    known = {(node.kind, node.name, node.parent_name, node.line_start) for node in nodes}
    return [
        dataclasses.replace(node, extra={**node.extra, "fork_rule": RULE_NAME})
        for node in twin_nodes
        if node.line_start in holders and HOLE not in node.name
        and (node.kind, node.name, node.parent_name, node.line_start) not in known
    ]


def _added_edges(
    twin_edges: list[EdgeInfo], edges: list[EdgeInfo], holders: dict[int, str]
) -> list[EdgeInfo]:
    """The twin's edges on string code lines that crg did not already have, minus built names.

    An edge from a method whose name is built at run time comes from the holder of the eval call.
    """
    known = {(edge.kind, edge.source, edge.target, edge.line) for edge in edges}
    added = []
    for edge in twin_edges:
        if edge.line not in holders or HOLE in edge.target:
            continue
        source = holders[edge.line] if HOLE in edge.source else edge.source
        if (edge.kind, source, edge.target, edge.line) not in known:
            added.append(changed(edge, RULE_NAME, source=source))
    return added
