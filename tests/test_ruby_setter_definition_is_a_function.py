"""A Ruby setter definition (``def name=(v)``, ``def self.name=(v)``) is a method, so it must get
the Function crg gives the same method with a plain name.

Origin: fork rule ``setter-definitions`` (crg-fork, branch ``fork/ruby-setter-defs``); crg 2.4.0
stores no Function for a setter, because tree-sitter-ruby gives its name a ``setter`` node, which
crg's ``_get_name`` does not read. Finding SYN-F-632.

The twin is the same file with each setter renamed to the plain name of the same length
(``name=`` → ``name_``), which crg extracts. Every expected graph here is crg's graph for the twin,
with ``name_`` written back as ``name=``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.setter_definitions import add_setter_definitions
from code_review_graph.tools.query import query_graph

RULE_NAME = "setter-definitions"


def parse(directory: Path, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "subject.rb"
    path.write_text(source)
    return CodeParser(directory).parse_file(path)


def graph(directory: Path, source: str, renamed: tuple[str, ...] = ()) -> tuple[list, list]:
    """crg's graph of *source*, paths relative, each `<name>_` in *renamed* written `<name>=`."""
    nodes, edges = parse(directory, source)
    root = f"{directory.as_posix()}/"

    def plain(value):
        if not isinstance(value, str):
            return value
        value = value.replace(root, "")
        for name in renamed:
            value = value.replace(f"{name}_", f"{name}=")
        return value

    node_rows = sorted(
        tuple(plain(v) for v in (n.kind, n.name, n.parent_name, n.line_start, n.line_end,
                                 n.identity_name, n.is_test, n.language))
        for n in nodes
    )
    edge_rows = sorted(tuple(plain(v) for v in (e.kind, e.source, e.target, e.line)) for e in edges)
    return node_rows, edge_rows


def graph_store(repo: Path) -> GraphStore:
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    return GraphStore(graph_dir / "graph.db")


def made_by_the_rule(items: list) -> list:
    return [item for item in items if item.extra.get("fork_rule") == RULE_NAME]


# ---- positive: each setter next to its twin, the same file with the setter renamed `name_` ------

TWINS = {
    "instance setter with a call in its body": (
        "class Account\n  def total=(value)\n    store(value)\n  end\n\n"
        "  def store(value); end\nend\n",
        "class Account\n  def total_(value)\n    store(value)\n  end\n\n"
        "  def store(value); end\nend\n",
        ("total",),
    ),
    "class setter": (
        "class Account\n  def self.limit=(value)\n    @limit = value\n  end\nend\n",
        "class Account\n  def self.limit_(value)\n    @limit = value\n  end\nend\n",
        ("limit",),
    ),
    "setter in class << self": (
        "class Account\n  class << self\n    def mode=(value); check(value); end\n  end\n\n"
        "  def self.check(value); end\nend\n",
        "class Account\n  class << self\n    def mode_(value); check(value); end\n  end\n\n"
        "  def self.check(value); end\nend\n",
        ("mode",),
    ),
    "setter in a nested module": (
        "module Devise\n  module Models\n    def password=(new_password)\n"
        "      @password = new_password\n    end\n  end\nend\n",
        "module Devise\n  module Models\n    def password_(new_password)\n"
        "      @password = new_password\n    end\n  end\nend\n",
        ("password",),
    ),
    "top-level setter": (
        "def level=(value)\n  puts(value)\nend\n",
        "def level_(value)\n  puts(value)\nend\n",
        ("level",),
    ),
    "setter beside its getter": (
        "class Account\n  def total\n    @total\n  end\n\n  def total=(value)\n    @total = value\n"
        "  end\nend\n",
        "class Account\n  def total\n    @total\n  end\n\n  def total_(value)\n    @total = value\n"
        "  end\nend\n",
        ("total",),
    ),
    "bare call in a setter's body": (
        "class Account\n  def total=(value)\n    refresh\n  end\n\n  def refresh; end\nend\n",
        "class Account\n  def total_(value)\n    refresh\n  end\n\n  def refresh; end\nend\n",
        ("total",),
    ),
    "setter assignment calls the setter of its own file": (
        "class Account\n  def total=(value); end\n\n  def reset\n    self.total = 0\n  end\nend\n",
        "class Account\n  def total_(value); end\n\n  def reset\n    self.total_(0)\n  end\nend\n",
        ("total",),
    ),
    "two setters": (
        "class Account\n  def a=(v); end\n  def b=(v); a(v); end\nend\n",
        "class Account\n  def a_(v); end\n  def b_(v); a(v); end\nend\n",
        ("a", "b"),
    ),
    # crg gives `def self.x` and `def x` one qualified name and keeps a Function for each.
    "class setter and instance setter of the same name": (
        "class Account\n  def self.total=(v); one(v); end\n  def total=(v); two(v); end\nend\n",
        "class Account\n  def self.total_(v); one(v); end\n  def total_(v); two(v); end\nend\n",
        ("total",),
    ),
    "setter defined inside a method": (
        "class Account\n  def setup\n    def total=(v)\n      store(v)\n    end\n  end\nend\n",
        "class Account\n  def setup\n    def total_(v)\n      store(v)\n    end\n  end\nend\n",
        ("total",),
    ),
    "setter defined two methods deep": (
        "class Account\n  def outer\n    def middle\n      def total=(v)\n        store(v)\n"
        "      end\n    end\n  end\nend\n",
        "class Account\n  def outer\n    def middle\n      def total_(v)\n        store(v)\n"
        "      end\n    end\n  end\nend\n",
        ("total",),
    ),
    "setter defined inside a test method": (
        "class AccountTest\n  def test_setup\n    def total=(v)\n      store(v)\n    end\n  end\nend\n",
        "class AccountTest\n  def test_setup\n    def total_(v)\n      store(v)\n    end\n  end\nend\n",
        ("total",),
    ),
    "test method beside a setter keeps its tested-by edge": (
        "class AccountTest\n  def test_total\n    check(1)\n  end\n\n  def total=(v); end\nend\n",
        "class AccountTest\n  def test_total\n    check(1)\n  end\n\n  def total_(v); end\nend\n",
        ("total",),
    ),
    "method defined inside a setter": (
        "class Account\n  def total=(v)\n    def helper\n      work\n    end\n    store(v)\n"
        "  end\nend\n",
        "class Account\n  def total_(v)\n    def helper\n      work\n    end\n    store(v)\n"
        "  end\nend\n",
        ("total",),
    ),
}


@pytest.mark.parametrize("name", sorted(TWINS))
def test_setter_gives_the_graph_of_its_twin(tmp_path, name):
    source, twin, renamed = TWINS[name]

    with_rule = graph(tmp_path / "subject", source)
    from_twin = graph(tmp_path / "twin", twin, renamed)

    assert with_rule == from_twin


def test_call_on_a_class_is_not_linked_to_an_instance_setter(tmp_path):
    # The twin `Account.total_(0)` with only an instance `def total_`: rule class-receiver-calls
    # unlinks it, as a call on a class never runs an instance method.
    source = "class Account\n  def total=(value); end\n\n  def reset\n    Account.total = 0\n  end\nend\n"
    twin = "class Account\n  def total_(value); end\n\n  def reset\n    Account.total_(0)\n  end\nend\n"

    assert graph(tmp_path / "subject", source) == graph(tmp_path / "twin", twin, ("total",))


# ---- unchanged: shapes with no setter definition ------------------------------------------------

UNCHANGED = {
    "plain methods": "class A\n  def total\n    store(1)\n  end\n\n  def store(v); end\nend\n",
    "comparison": "class A\n  def same?(o)\n    total == o.total\n  end\nend\n",
    "equality operator": "class A\n  def ==(other)\n    check(other)\n  end\nend\n",
    "index setter operator": "class A\n  def []=(key, value)\n    store(value)\n  end\nend\n",
    "plus operator": "class A\n  def +(other)\n    add(other)\n  end\nend\n",
    "predicate method": "class A\n  def total?\n    check\n  end\nend\n",
    "setter call with no setter definition": "class A\n  def go\n    self.total = 1\n  end\nend\n",
    "setter on another object": "class A\n  def config.mode=(value)\n    check(value)\n  end\nend\n",
}


@pytest.mark.parametrize("name", sorted(UNCHANGED))
def test_shape_without_a_setter_definition_is_unchanged(tmp_path, monkeypatch, name):
    with_rule = graph(tmp_path / "on", UNCHANGED[name])
    monkeypatch.setattr(
        ruby, "RULES", tuple(rule for rule in ruby.RULES if rule is not add_setter_definitions),
    )
    without_rule = graph(tmp_path / "off", UNCHANGED[name])

    assert with_rule == without_rule


# ---- tag ----------------------------------------------------------------------------------------

def test_new_node_its_contains_edge_and_its_body_calls_name_the_rule(tmp_path):
    source = "class Account\n  def total=(value)\n    store(value)\n  end\n\n  def store(v); end\nend\n"

    nodes, edges = parse(tmp_path, source)

    assert [(n.kind, n.name) for n in made_by_the_rule(nodes)] == [("Function", "total=")]
    assert sorted((e.kind, e.line) for e in made_by_the_rule(edges)) == [
        ("CALLS", 3), ("CONTAINS", 2),
    ]


def test_setter_call_linked_to_the_new_setter_names_the_rule(tmp_path):
    source = "class Account\n  def total=(value); end\n\n  def reset\n    self.total = 0\n  end\nend\n"

    _, edges = parse(tmp_path, source)

    call = next(e for e in edges if e.kind == "CALLS" and e.line == 5)
    assert call.target.endswith("::Account.total=")
    assert call.extra["fork_rule"] == RULE_NAME


# ---- the rule's contract ------------------------------------------------------------------------

def test_rule_leaves_its_inputs_unchanged(tmp_path):
    source = "class Account\n  def total=(value)\n    store(value)\n  end\n\n  def store(v); end\nend\n"
    directory = tmp_path / "plain"
    directory.mkdir()
    path = directory / "subject.rb"
    path.write_text(source)
    parser = CodeParser(directory)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())
    nodes_before, edges_before = [repr(n) for n in nodes], [repr(e) for e in edges]

    add_setter_definitions(parser, "ruby", path.read_bytes(), nodes, edges)

    assert [repr(n) for n in nodes] == nodes_before
    assert [repr(e) for e in edges] == edges_before


def test_file_without_setters_is_not_extracted_twice(tmp_path, monkeypatch):
    path = tmp_path / "subject.rb"
    path.write_text("class Account\n  def total\n    store(1)\n  end\nend\n")
    parser = CodeParser(tmp_path)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())
    extracted: list[bytes] = []
    monkeypatch.setattr(parser, "_extract_bytes", lambda p, source: extracted.append(source))

    add_setter_definitions(parser, "ruby", path.read_bytes(), nodes, edges)

    assert extracted == []


def test_rule_without_a_file_node_fails_with_a_clear_error(tmp_path):
    source = "class Account\n  def total=(value); end\nend\n"
    path = tmp_path / "subject.rb"
    path.write_text(source)
    parser = CodeParser(tmp_path)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())

    with pytest.raises(ValueError, match="no File node"):
        add_setter_definitions(
            parser, "ruby", path.read_bytes(), [n for n in nodes if n.kind != "File"], edges,
        )


def test_full_build_links_a_setter_assignment_to_the_setter_of_its_own_file(tmp_path):
    (tmp_path / "account.rb").write_text(
        "class Account\n  def total=(value); end\n\n  def reset\n    self.total = 0\n  end\nend\n"
    )
    with graph_store(tmp_path) as store:
        full_build(tmp_path, store)

    result = query_graph(
        "callers_of", f"{(tmp_path / 'account.rb').as_posix()}::Account.total=",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "ok"
    assert [node["name"] for node in result["results"]] == ["reset"]
