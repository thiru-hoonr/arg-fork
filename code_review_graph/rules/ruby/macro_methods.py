"""Give a method that a Ruby class-body macro makes the Function crg gives its ``def`` twin.

Ruby runs these macros while it reads the class, and each one defines methods:

    module Devise
      mattr_accessor :paranoid               # Devise.paranoid, Devise.paranoid=
      delegate :flash, to: :request          # flash
      attr_reader :name                      # name
      define_method(:short) { |v| cut(v) }   # short
    end

crg 2.4.0 reads only ``def`` as a method, so it has no Function for any of these and every call
to them stays unlinked: on devise, 129 real calls go to methods made by macros (SYN-F-631).

This rule makes crg give each such method what it gives the same method written out as a ``def``
on the macro's line (``def self.paranoid``, ``def flash``, ``def name``): a Function (a Test when
crg reads the name as one) in the enclosing class, its CONTAINS edge, and the links crg makes
from calls in the file to it. A ``define_method`` block is the method's body, so its calls move
into the new Function, as they are in ``def short(v) cut(v) end``.

Left alone: writers (``x=``; crg stores no Function for ``def x=``, SYN-F-632), names built at
run time, ``delegate`` with ``prefix:``, macros on a receiver, macros outside a class body, and
a name the class already defines.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, NamedTuple

from ... import parser as crg
from ._syntax import (
    CLASS_METHOD_KEY,
    Lines,
    changed,
    file_path_of,
    last_part,
    lines,
    relinked,
    text,
)

if TYPE_CHECKING:
    from tree_sitter import Node

    from ...parser import CodeParser, EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)

RULE_NAME = "macro-methods"

# Macros whose every name argument makes a reader method crg would name like `def name`.
_READER_MACROS = frozenset({
    "mattr_accessor", "mattr_reader", "cattr_accessor", "cattr_reader", "class_attribute",
    "attr_reader", "attr_accessor",
})
# Of those, the ones whose method belongs to the class (`def self.name`); ActiveSupport also
# makes `def name`, which crg gives the same qualified name, so one Function stands for both.
_CLASS_METHOD_MACROS = frozenset({
    "mattr_accessor", "mattr_reader", "cattr_accessor", "cattr_reader", "class_attribute",
})
# `delegate :a, :b, to: :c` makes `a` and `b`; `prefix: true` or `prefix: :x` changes the
# names, `prefix: false` or `prefix: nil` does not.
_DELEGATE = "delegate"
_PREFIX_OPTION = "prefix"
_NO_PREFIX_SYNTAX = frozenset({"false", "nil"})
# `define_method(:name) { ... }`: one method, the block is its body.
_DEFINE_METHOD = "define_method"
# A name crg stores a Function for: no writer (`x=`) and no operator (`[]`, `==`).
_METHOD_NAME = re.compile(r"[A-Za-z_]\w*[?!]?")
# What tree-sitter calls the statements of a body, and the bodies that hold them.
_STATEMENTS_SYNTAX = frozenset({"body_statement", "block_body"})
_CLASS_SYNTAX = frozenset({"class", "module"})
_SINGLETON_CLASS_SYNTAX = "singleton_class"
_BLOCK_SYNTAX = frozenset({"block", "do_block"})
# `class Devise::Mapping`: the class's own name is the last part of a scoped name.
_SCOPED_NAME_SYNTAX = "scope_resolution"


class Owner(NamedTuple):
    """The class or module a macro runs in, and whether it runs in its `class << self`."""

    syntax: Node
    in_singleton_class: bool


class MacroMethod(NamedTuple):
    """One method a macro makes: name, lines, class, class-method flag and body, if any."""

    name: str
    lines: Lines
    owner: Node
    is_class_method: bool
    body: Node | None


class AddedMethod(NamedTuple):
    """A Function the rule adds, its CONTAINS edge, and the method it stands for."""

    node: NodeInfo
    contains: EdgeInfo
    method: MacroMethod


def add_methods_made_by_macros(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Add the Function, CONTAINS edge and call links crg makes for each macro's `def` twin."""
    root = parser._get_parser(language).parse(source).root_node
    methods = _macro_methods(root)
    if not methods:
        return nodes, edges
    file_path = file_path_of(nodes)
    added = _functions(parser, language, file_path, nodes, methods)
    if not added:
        return nodes, edges
    nodes = _in_line_order(nodes, [new.node for new in added])
    edges = [*edges, *(new.contains for new in added)]
    edges = _with_block_calls_moved(parser, edges, file_path, added)
    names = {new.node.name for new in added}
    return nodes, relinked(parser, nodes, edges, file_path, names, RULE_NAME)


def _macro_methods(root: Node) -> list[MacroMethod]:
    """Every method a macro in a class body makes, in the order of the source."""
    found = []
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.children))
        if node.type != "call" or node.child_by_field_name("receiver") is not None:
            continue
        macro = node.child_by_field_name("method")
        owner = _owner(node)
        if macro is None or owner is None:
            continue
        found.extend(_methods_of(node, text(macro), owner))
    return found


def _owner(call: Node) -> Owner | None:
    """The class or module whose body runs *call*, or None when *call* is not in a class body.

    A statement of `class << self` belongs to the class around it, and a statement of a block
    given to a class-body call (`included do`) to that call's class, as crg places a `def` there.
    """
    statement = call
    in_singleton_class = False
    while statement.parent is not None and statement.parent.type in _STATEMENTS_SYNTAX:
        holder = statement.parent.parent
        if holder is None:
            return None
        if holder.type in _CLASS_SYNTAX:
            return Owner(holder, in_singleton_class)
        if holder.type == _SINGLETON_CLASS_SYNTAX:
            in_singleton_class = True
            statement = holder
        elif holder.type in _BLOCK_SYNTAX and holder.parent is not None:
            statement = holder.parent
        else:
            return None
    return None


def _methods_of(call: Node, macro: str, owner: Owner) -> list[MacroMethod]:
    """The methods *macro* makes in *owner*, one per name written as a literal."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return []
    first_line = Lines(lines(call).first, lines(call).first)
    is_class_method = owner.in_singleton_class or macro in _CLASS_METHOD_MACROS
    makes_one_method_per_name = macro in _READER_MACROS or (
        macro == _DELEGATE and not _adds_a_prefix(arguments)
    )
    if makes_one_method_per_name:
        names = [_literal_name(argument) for argument in arguments.named_children]
        return [
            MacroMethod(name, first_line, owner.syntax, is_class_method, None)
            for name in names if name
        ]
    if macro == _DEFINE_METHOD and arguments.named_children:
        name = _literal_name(arguments.named_children[0])
        body = call.child_by_field_name("block")
        return [MacroMethod(name, lines(call), owner.syntax, is_class_method, body)] if name else []
    return []


def _adds_a_prefix(arguments: Node) -> bool:
    """Whether `delegate` *arguments* pass a `prefix:` that changes the names it makes."""
    for argument in arguments.named_children:
        key = argument.child_by_field_name("key") if argument.type == "pair" else None
        value = argument.child_by_field_name("value") if key is not None else None
        if key is not None and text(key) == _PREFIX_OPTION:
            return value is None or value.type not in _NO_PREFIX_SYNTAX
    return False


def _literal_name(argument: Node) -> str | None:
    """The method name *argument* writes out (`:name`, `"name"`), or None if there is none.

    None for a name built at run time, an option (`to: :x`), and a writer or operator name,
    for which crg stores no Function.
    """
    string_parts = [part.type for part in argument.named_children]
    if argument.type == "simple_symbol":
        name = text(argument)[1:]
    elif argument.type == "string" and string_parts == ["string_content"]:
        name = text(argument.named_children[0])
    else:
        return None
    return name if _METHOD_NAME.fullmatch(name) else None


def _functions(
    parser: CodeParser,
    language: str,
    file_path: str,
    nodes: list[NodeInfo],
    methods: list[MacroMethod],
) -> list[AddedMethod]:
    """The Function and CONTAINS edge crg makes for each method's twin, one per qualified name.

    A name the class already has is skipped: one Function stands for every definition crg
    names alike. A method whose class has no Class node of crg's is skipped and logged.
    """
    classes = [node for node in nodes if node.kind == "Class"]
    known = {parser._node_qualified(node) for node in nodes if node.kind in ("Function", "Test")}
    added = []
    for method in methods:
        owner = _crg_class(classes, method.owner)
        if owner is None:
            logger.warning(
                "%s: no Class node of crg's for the class at line %d; %s skipped",
                RULE_NAME, lines(method.owner).first, method.name,
            )
            continue
        node = _function(parser, language, file_path, owner, method)
        qualified = parser._node_qualified(node)
        if qualified in known:
            continue
        known.add(qualified)
        contains = crg.EdgeInfo(
            kind="CONTAINS", source=parser._node_qualified(owner), target=qualified,
            file_path=file_path, line=method.lines.first, extra={"fork_rule": RULE_NAME},
        )
        added.append(AddedMethod(node, contains, method))
    return added


def _crg_class(classes: list[NodeInfo], syntax: Node) -> NodeInfo | None:
    """crg's Class node for the class or module *syntax*, or None unless exactly one matches.

    The lines alone are not enough: `class A; class B; end; end` gives both the same lines.
    """
    name = syntax.child_by_field_name("name")
    if name is not None and name.type == _SCOPED_NAME_SYNTAX:
        name = name.child_by_field_name("name")
    if name is None:
        return None
    matches = [
        node for node in classes
        if (node.line_start, node.line_end) == lines(syntax) and node.name == text(name)
    ]
    return matches[0] if len(matches) == 1 else None


def _function(
    parser: CodeParser, language: str, file_path: str, owner: NodeInfo, method: MacroMethod,
) -> NodeInfo:
    """The node crg makes for `def <name>` in *owner*: a Test when crg reads the name as one.

    A class method is marked so that `class-receiver-calls` keeps a call on the class linked to
    it, as it does for `def self.name`, which crg reads from the syntax alone.
    """
    is_test = crg._is_test_function(method.name, file_path, repo_root=parser._repo_root)
    # crg names a method's scope by its class's scope and name: `Devise.Mapping`
    scope = f"{owner.parent_name}.{owner.name}" if owner.parent_name else owner.name
    return crg.NodeInfo(
        kind="Test" if is_test else "Function", name=method.name, file_path=file_path,
        line_start=method.lines.first, line_end=method.lines.last, language=language,
        parent_name=scope, is_test=is_test, extra=_tags(method),
        identity_name=method.name,
    )


def _tags(method: MacroMethod) -> dict[str, object]:
    """The `extra` of a new node: the rule's name, and the class-method mark when it is one."""
    if method.is_class_method:
        return {"fork_rule": RULE_NAME, CLASS_METHOD_KEY: True}
    return {"fork_rule": RULE_NAME}


def _in_line_order(nodes: list[NodeInfo], added: list[NodeInfo]) -> list[NodeInfo]:
    """*nodes* with each added node before the first definition that starts after it.

    crg links a call to the first definition of its name in the list, so a method made at the
    top of a class must come before a same-named method of a nested module, as its twin does.
    """
    result = list(nodes)
    for new in sorted(added, key=lambda node: node.line_start):
        index = next(
            (i for i, node in enumerate(result)
             if node.kind != "File" and node.line_start > new.line_start),
            len(result),
        )
        result.insert(index, new)
    return result


def _with_block_calls_moved(
    parser: CodeParser, edges: list[EdgeInfo], file_path: str, added: list[AddedMethod],
) -> list[EdgeInfo]:
    """*edges* with the calls in each `define_method` block made by its new Function.

    crg makes a call in a class body from the file; the twin makes it from the method.
    """
    moved: list[EdgeInfo] = list(edges)
    for new in added:
        if new.method.body is None:
            continue
        in_body = _call_names(new.method.body)
        source = parser._node_qualified(new.node)
        for index, edge in enumerate(moved):
            call = (edge.line, last_part(edge.target))
            if edge.kind == "CALLS" and edge.source == file_path and in_body[call] > 0:
                in_body[call] -= 1
                moved[index] = changed(edge, RULE_NAME, source=source)
    return moved


def _call_names(body: Node) -> Counter[tuple[int, str]]:
    """How many calls of each name start on each line of *body*."""
    found: Counter[tuple[int, str]] = Counter()
    pending = [body]
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        method = node.child_by_field_name("method") if node.type == "call" else None
        if method is not None:
            found[(lines(node).first, text(method))] += 1
    return found

