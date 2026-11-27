"""A Ruby attribute assignment (``self.balance = x``) is a call to the setter ``balance=``.

Origin: fork rule ``setter-assignments`` (crg-fork, branch ``fork/ruby-calls``); on devise, crg linked
21 such assignments to the getter. crg has no twin for a setter call, so the expected graphs below
are written by hand (approved by Thiru, 2026-11-26). Findings SYN-F-625 and SYN-F-627.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.setter_assignments import store_setter_assignments_as_setter_calls

RULE_NAME = "setter-assignments"


def parse(directory: Path, name: str, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    path = directory / name
    path.write_text(dedent(source))
    return CodeParser(directory).parse_file(path)


def short(qualified_name: str) -> str:
    return qualified_name.split("::", 1)[1] if "::" in qualified_name else qualified_name


def calls_on_line(edges: list[EdgeInfo], line: int) -> list[tuple[str, str]]:
    return sorted(
        (short(edge.source), short(edge.target))
        for edge in edges
        if edge.kind == "CALLS" and edge.line == line
    )


def account(body: str) -> str:
    """A class with a getter `balance`, whose method `update` (line 6) runs *body*."""
    return (
        "class Account\n  def balance\n  end\n\n"
        f"  def update(ledger, amount)\n    {body}\n  end\nend\n"
    )


def test_assignment_to_self_calls_the_setter_not_the_getter(tmp_path):
    _, edges = parse(tmp_path, "account.rb", account("self.balance = amount"))

    assert calls_on_line(edges, 6) == [("Account.update", "balance=")]


def test_assignment_to_another_object_calls_its_setter(tmp_path):
    _, edges = parse(tmp_path, "account.rb", account("ledger.total = amount"))

    assert calls_on_line(edges, 6) == [("Account.update", "total=")]


@pytest.mark.parametrize("operator", ["+=", "||=", "-="])
def test_operator_assignment_calls_the_getter_and_the_setter(tmp_path, operator):
    _, edges = parse(tmp_path, "account.rb", account(f"self.balance {operator} amount"))

    assert calls_on_line(edges, 6) == [
        ("Account.update", "Account.balance"),
        ("Account.update", "balance="),
    ]


def test_getter_call_on_the_same_line_keeps_its_link(tmp_path):
    _, edges = parse(tmp_path, "account.rb", account("self.balance = self.balance + amount"))

    assert calls_on_line(edges, 6) == [
        ("Account.update", "Account.balance"),
        ("Account.update", "balance="),
    ]


def test_only_calls_are_renamed_not_the_class_containing_the_getter(tmp_path):
    one_line_getter = "class Account\n  def balance; self.balance = 1; end\nend\n"
    _, edges = parse(tmp_path, "account.rb", one_line_getter)

    contains = [short(edge.target) for edge in edges if edge.kind == "CONTAINS"]
    assert "Account.balance" in contains
    assert calls_on_line(edges, 2) == [("Account.balance", "balance=")]


def test_every_changed_or_added_edge_names_the_rule(tmp_path, monkeypatch):
    # rule self-receivers marks `self.balance=` later and its tag replaces this rule's
    monkeypatch.setattr(
        ruby, "RULES", tuple(r for r in ruby.RULES if r.__name__ != "mark_calls_on_self"),
    )
    _, edges = parse(tmp_path, "account.rb", account("self.balance += amount"))

    setter_calls = [edge for edge in edges if edge.kind == "CALLS" and edge.target == "balance="]
    assert setter_calls
    assert all(edge.extra.get("fork_rule") == RULE_NAME for edge in setter_calls)


def graph_rows(directory: Path, source: str) -> tuple[list[tuple], list[tuple]]:
    directory.mkdir()
    nodes, edges = parse(directory, "account.rb", source)
    root = f"{directory.as_posix()}/"
    node_rows = sorted(
        (n.kind, n.name.replace(root, ""), n.line_start, n.line_end, repr(sorted(n.extra.items())))
        for n in nodes
    )
    edge_rows = sorted(
        (e.kind, e.source.replace(root, ""), e.target.replace(root, ""), e.line,
         repr(sorted(e.extra.items())))
        for e in edges
    )
    return node_rows, edge_rows


def graphs_with_and_without_the_rule(tmp_path, monkeypatch, source):
    """The rows of *source* with every Ruby rule on, then with every Ruby rule but this one."""
    with_rule = graph_rows(tmp_path / "with_rule", source)
    monkeypatch.setattr(
        ruby,
        "RULES",
        tuple(rule for rule in ruby.RULES if rule is not store_setter_assignments_as_setter_calls),
    )
    return with_rule, graph_rows(tmp_path / "without_rule", source)


# Shapes the rule must leave exactly as crg makes them: no attribute assignment.
UNCHANGED_CASES = {
    "getter_call": "self.balance",
    "comparison": "self.balance == amount",
    "local_variable": "balance = amount",
}


@pytest.mark.parametrize("name", sorted(UNCHANGED_CASES))
def test_shapes_that_are_not_attribute_assignments_are_unchanged(tmp_path, monkeypatch, name):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, account(UNCHANGED_CASES[name])
    )

    assert with_rule == without_rule


# Assignments that look like an attribute assignment but call no setter named after a method.
FALSE_POSITIVE_CASES = {
    "instance_variable": "@balance = amount",
    "element_assignment": "ledger[:balance] = amount",
    "keyword_argument": "ledger.update(balance: amount)",
}


@pytest.mark.parametrize("name", sorted(FALSE_POSITIVE_CASES))
def test_assignments_that_call_no_attribute_setter_are_unchanged(tmp_path, monkeypatch, name):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, account(FALSE_POSITIVE_CASES[name])
    )

    assert with_rule == without_rule
