"""A Ruby call on a class (``Other.find``, ``self.class.timeout``, ``x.new``) never runs an
instance method, so it must not be linked to one.

Origin: fork rule ``class-receiver-calls`` (crg-fork, branch ``fork/ruby-calls``); on devise, crg linked
``resource_class.new`` to the controller's own ``new`` action in five controllers, and
``self.class.timeout_in`` to the instance method ``timeout_in``. crg has no twin for these calls,
so the expected graphs are written by hand (approved by Thiru, 2026-11-26). Findings SYN-F-625 and
SYN-F-627.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.class_receiver_calls import (
    unlink_class_receiver_calls_from_instance_methods,
)
from code_review_graph.tools.query import query_graph

RULE_NAME = "class-receiver-calls"


def parse(directory: Path, name: str, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    path = directory / name
    path.write_text(dedent(source))
    return CodeParser(directory).parse_file(path)


def short(qualified_name: str) -> str:
    return qualified_name.split("::", 1)[1] if "::" in qualified_name else qualified_name


def calls_on_line(edges: list[EdgeInfo], line: int) -> list[str]:
    return sorted(
        short(edge.target) for edge in edges if edge.kind == "CALLS" and edge.line == line
    )


def controller(call: str) -> str:
    """A class with instance methods `new`, `find` and `timeout`; `build` (line 9) makes *call*."""
    return (
        "class Controller\n  def new\n  end\n\n  def find\n  end\n\n  def build\n"
        f"    {call}\n  end\n\n  def timeout\n  end\nend\n"
    )


@pytest.mark.parametrize("call, name", [
    ("resource_class.new", "new"),
    ("Other.find(1)", "find"),
    ("Devise::Mapping.find(1)", "find"),
    ("self.class.timeout", "timeout"),
])
def test_call_on_a_class_is_not_linked_to_an_instance_method(tmp_path, call, name):
    _, edges = parse(tmp_path, "controller.rb", controller(call))

    assert name in calls_on_line(edges, 9)
    assert f"Controller.{name}" not in calls_on_line(edges, 9)


def test_call_without_a_receiver_elsewhere_in_the_file_does_not_stop_the_rule(tmp_path):
    _, edges = parse(tmp_path, "controller.rb", controller("find(2)\n    Other.find(1)"))

    assert calls_on_line(edges, 10) == ["find"]


def test_unlinked_call_is_marked_unresolved_and_names_the_rule(tmp_path):
    _, edges = parse(tmp_path, "controller.rb", controller("resource_class.new"))

    unlinked = [edge for edge in edges if edge.kind == "CALLS" and edge.target == "new"]
    assert len(unlinked) == 1
    assert unlinked[0].extra["unresolved_targets"] == []
    assert unlinked[0].extra["fork_rule"] == RULE_NAME


def test_only_calls_are_unlinked_not_the_class_containing_the_method(tmp_path):
    one_line_new = "class Controller\n  def new; resource_class.new; end\nend\n"
    _, edges = parse(tmp_path, "controller.rb", one_line_new)

    contains = [short(edge.target) for edge in edges if edge.kind == "CONTAINS"]
    assert "Controller.new" in contains
    assert calls_on_line(edges, 2) == ["new"]


def graph_store(repo: Path) -> GraphStore:
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    return GraphStore(graph_dir / "graph.db")


def test_full_build_does_not_link_the_call_back_to_the_instance_method(tmp_path):
    (tmp_path / "controller.rb").write_text(controller("resource_class.new"))
    with graph_store(tmp_path) as store:
        full_build(tmp_path, store)

    result = query_graph(
        "callers_of", f"{(tmp_path / 'controller.rb').as_posix()}::Controller.new",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "ok"
    assert [short(node["qualified_name"]) for node in result["results"]] == []


def graph_rows(directory: Path, source: str) -> tuple[list[tuple], list[tuple]]:
    directory.mkdir()
    nodes, edges = parse(directory, "subject.rb", source)
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
        tuple(
            rule for rule in ruby.RULES
            if rule is not unlink_class_receiver_calls_from_instance_methods
        ),
    )
    return with_rule, graph_rows(tmp_path / "without_rule", source)


# Shapes the rule must leave exactly as crg makes them: calls on an object or on self.
UNCHANGED_CASES = {
    "object_receiver": controller("record.find"),
    "self_receiver": controller("self.find"),
    "no_receiver": controller("find(1)"),
}


@pytest.mark.parametrize("name", sorted(UNCHANGED_CASES))
def test_calls_on_an_object_or_self_are_unchanged(tmp_path, monkeypatch, name):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, UNCHANGED_CASES[name]
    )

    assert with_rule == without_rule


# Calls on a class whose link is right: the file defines the class method they run.
FALSE_POSITIVE_CASES = {
    "class_method_with_self": (
        "class Mapping\n  def self.find(x)\n  end\n\n  def go\n    Mapping.find(1)\n  end\nend\n"
    ),
    "class_method_in_class_self_block": (
        "class Mapping\n  class << self\n    def find(x)\n    end\n  end\n\n"
        "  def go\n    Mapping.find(1)\n  end\nend\n"
    ),
    "self_class_calls_a_class_method": (
        "class Mapping\n  def self.find(x)\n  end\n\n  def go\n    self.class.find(1)\n  end\nend\n"
    ),
    # The line also calls `find` on an object, which may run this file's instance `def find`.
    "same_line_also_calls_on_an_object": controller("Other.find(1) || record.find"),
}


@pytest.mark.parametrize("name", sorted(FALSE_POSITIVE_CASES))
def test_calls_linked_to_a_class_method_keep_their_link(tmp_path, monkeypatch, name):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, FALSE_POSITIVE_CASES[name]
    )

    assert with_rule == without_rule
