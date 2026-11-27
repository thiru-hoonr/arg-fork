"""A Ruby call with no receiver, or on ``self``, inside a method of a class is made on ``self``,
so it carries ``receiver: self``, the mark crg's resolver needs to look in the caller's class and
then up its parents.

Origin: fork rule ``self-receivers`` (crg-fork, branch ``fork/ruby-class-index``); crg 2.4.0 marks
``self.x()`` / ``this.x()`` in other languages but never a Ruby call, so ``resource`` in a
controller is never looked up in its parent ``DeviseController``. Finding SYN-F-633.

crg writes no receiver for a Ruby call, so there is no twin: the expected marks are written by
hand, in the form crg's resolver reads for ``self.x()`` (``extra["receiver"] == "self"``, the
target the plain name).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser, EdgeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.self_receivers import mark_calls_on_self

from .test_ruby_call_reaches_the_method_its_class_inherits import linked_target

RULE_NAME = "self-receivers"


def parse(directory: Path, source: str) -> list[EdgeInfo]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "subject.rb"
    path.write_text(source)
    return CodeParser(directory).parse_file(path)[1]


def calls_on_self(edges: list[EdgeInfo]) -> list[tuple[int, str]]:
    return sorted(
        (e.line, e.target) for e in edges
        if e.kind == "CALLS" and e.extra.get("receiver") == "self"
    )


def relative(edges: list[EdgeInfo], directory: Path) -> list[str]:
    return [repr(e).replace(directory.as_posix(), "") for e in edges]


def without_this_rule(monkeypatch) -> None:
    monkeypatch.setattr(
        ruby, "RULES", tuple(rule for rule in ruby.RULES if rule is not mark_calls_on_self),
    )


# `item` is a parameter: `item.total` is a call on an object, not on self
ACCOUNT = "class Account\n  def total; end\n\n  def report(item)\n{body}\n  end\nend\n"

# ---- positive: calls on self, written by hand ---------------------------------------------------

# The class does not define these names, so crg leaves each call unlinked in the file.
ON_SELF = {
    "call with no receiver": ("    refresh(1)", [(5, "refresh")]),
    "call on self": ("    self.refresh", [(5, "refresh")]),
    "bare name": ("    refresh", [(5, "refresh")]),
    "setter on self": ("    self.limit = 1", [(5, "limit=")]),
    "setter on self that reads first": ("    self.limit += 1", [(5, "limit"), (5, "limit=")]),
    "call to an inherited method": ("    resource_class", [(5, "resource_class")]),
}


@pytest.mark.parametrize("name", sorted(ON_SELF))
def test_call_on_self_is_marked(tmp_path, name):
    body, expected = ON_SELF[name]

    assert calls_on_self(parse(tmp_path, ACCOUNT.format(body=body))) == expected


def test_call_in_a_class_method_is_marked(tmp_path):
    source = "class Account\n  def self.build\n    defaults\n  end\nend\n"

    assert calls_on_self(parse(tmp_path, source)) == [(3, "defaults")]


@pytest.mark.parametrize(("source", "expected"), [
    ("class A\n  def self.build\n    defaults\n  end\nend\n", True),
    ("class A\n  class << self\n    def build\n      defaults\n    end\n  end\nend\n", True),
    ("class A\n  class << self\n    class B\n      def build\n        defaults\n      end\n"
     "    end\n  end\nend\n", False),
    ("class A\n  def build\n    defaults\n  end\nend\n", False),
])
def test_a_call_in_a_class_method_says_so(tmp_path, source, expected):
    marked = [
        edge for edge in parse(tmp_path, source)
        if edge.kind == "CALLS" and edge.extra.get("receiver") == "self"
    ]

    assert [bool(edge.extra.get("ruby_class_method")) for edge in marked] == [expected]


def test_a_call_in_a_class_method_reaches_the_class_s_own_method(tmp_path):
    files = {"a.rb": "module A\n  mattr_accessor :name\n\n  def self.go\n    name\n  end\nend\n"}

    assert linked_target(tmp_path, files) == "a.rb::A.name"


@pytest.mark.parametrize("source", [
    "class A\n  def self.build\n    work\n  end\n\n  def build\n    work\n  end\nend\n",
    "class A\n  def build\n    work\n  end\n\n  def self.build\n    work\n  end\nend\n",
])
def test_a_class_method_and_an_instance_method_of_one_name_are_told_apart(tmp_path, source):
    # crg names both `A.build`; the call's line says which one it is in
    class_method_line = 3 if "self.build\n    work\n  end\n\n  def build" in source else 7
    marked = {
        edge.line: bool(edge.extra.get("ruby_class_method")) for edge in parse(tmp_path, source)
        if edge.kind == "CALLS" and edge.extra.get("receiver") == "self"
    }

    assert marked == {3: class_method_line == 3, 7: class_method_line == 7}


def test_call_on_self_beside_a_call_on_an_object_of_the_same_name(tmp_path):
    # crg pairs a line's calls with its edges in source order: only the second is on self
    source = ACCOUNT.format(body="    item.refresh + refresh(1)")

    assert calls_on_self(parse(tmp_path, source)) == [(5, "refresh")]


def test_call_crg_linked_in_its_file_is_left_as_crg_made_it(tmp_path, monkeypatch):
    # `total` is the class's own method: crg linked it, and the class's own method wins anyway
    source = ACCOUNT.format(body="    total\n    self.total")
    with_rule = parse(tmp_path / "on", source)
    without_this_rule(monkeypatch)

    without_rule = parse(tmp_path / "off", source)

    assert relative(with_rule, tmp_path / "on") == relative(without_rule, tmp_path / "off")


def test_call_outside_a_method_keeps_its_own_receiver_entry(tmp_path):
    # the class-body call `Other.refresh` comes first on the line and is not on self
    source = "class Account; Other.refresh; def go; refresh(2); end; end\n"

    assert calls_on_self(parse(tmp_path, source)) == [(1, "refresh")]


def test_call_crg_linked_to_a_top_level_method_is_left_as_crg_made_it(tmp_path, monkeypatch):
    source = "def helper\nend\n\nclass Account\n  def report\n    helper\n  end\nend\n"
    with_rule = parse(tmp_path / "on", source)
    without_this_rule(monkeypatch)

    without_rule = parse(tmp_path / "off", source)

    assert relative(with_rule, tmp_path / "on") == relative(without_rule, tmp_path / "off")


# ---- unchanged and false positive: calls not on self ---------------------------------------------

NOT_ON_SELF = {
    "call on an object": ACCOUNT.format(body="    item.total"),
    "call on a constant": ACCOUNT.format(body="    Ledger.post"),
    "call in a class body": "class Account\n  validates :total\nend\n",
    "call in a top-level method": "def helper\n  work(1)\nend\n",
    "call at the top of the file": "puts(1)\n",
    "call unlinked by class-receiver-calls": (
        "class A\n  def new; end\n  def b\n    A.new\n  end\nend\n"
    ),
}


@pytest.mark.parametrize("name", sorted(NOT_ON_SELF))
def test_call_not_on_self_is_unchanged(tmp_path, monkeypatch, name):
    with_rule = parse(tmp_path / "on", NOT_ON_SELF[name])
    without_this_rule(monkeypatch)

    without_rule = parse(tmp_path / "off", NOT_ON_SELF[name])

    assert relative(with_rule, tmp_path / "on") == relative(without_rule, tmp_path / "off")


# ---- tag and contract ---------------------------------------------------------------------------

def test_marked_call_names_the_rule(tmp_path):
    edges = parse(tmp_path, ACCOUNT.format(body="    refresh(1)\n    item.total"))

    assert [(e.line, e.extra.get("fork_rule")) for e in edges if e.extra.get("receiver")] == [
        (5, RULE_NAME),
    ]


def test_rule_leaves_its_inputs_unchanged(tmp_path):
    path = tmp_path / "subject.rb"
    path.write_text(ACCOUNT.format(body="    total(1)"))
    parser = CodeParser(tmp_path)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())
    nodes_before, edges_before = [repr(n) for n in nodes], [repr(e) for e in edges]

    mark_calls_on_self(parser, "ruby", path.read_bytes(), nodes, edges)

    assert [repr(n) for n in nodes] == nodes_before
    assert [repr(e) for e in edges] == edges_before


# ---- a class method's `self` is the class, not an object of it ---------------------------------

@pytest.mark.parametrize("caller", [
    "class Child < Base\n  def self.go\n    work\n  end\nend\n",
    "class Child < Base\n  class << self\n    def go\n      work\n    end\n  end\nend\n",
])
def test_a_call_in_a_class_method_does_not_reach_an_inherited_instance_method(tmp_path, caller):
    files = {"base.rb": "class Base\n  def work; end\nend\n", "child.rb": caller}

    assert linked_target(tmp_path, files) == "work"  # left unlinked

