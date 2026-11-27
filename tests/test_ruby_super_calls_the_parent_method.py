"""Ruby's ``super`` is a call to the method of the same name in the caller's parents.

Origin: rule ``super-calls`` (crg-fork, branch ``fork/ruby-class-index``). tree-sitter-ruby gives
``super`` a ``super`` node, not a ``call``, and crg 2.4.0 stored no call for it, so
``Devise::Strategies::Authenticatable#store?`` never reached ``Base#store?``. On devise, 12 real
calls were missed this way. Finding SYN-F-634.

No twin: crg writes no call for any spelling of ``super`` in Ruby; the expected edge is the one
crg writes for ``super.run()`` in other languages (``receiver: super``), written by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser

from .test_ruby_call_reaches_the_method_its_class_inherits import linked_target

PATH = Path("/repo/app.rb")


def super_calls(source: str) -> list[tuple[str, str, int, str | None]]:
    """(caller, target, line, receiver) of each call the parse stores on `super`."""
    _, edges = CodeParser().parse_bytes(PATH, source.encode())
    return [
        (edge.source.rpartition("::")[2], edge.target, edge.line, edge.extra.get("receiver"))
        for edge in edges
        if edge.kind == "CALLS" and edge.extra.get("receiver") == "super"
    ]


@pytest.mark.parametrize("spelling", ["super", "super(x)", "super x", "super() do\n    end"])
def test_each_spelling_of_super_is_one_call(spelling):
    source = f"class A < B\n  def go(x)\n    {spelling}\n  end\nend\n"

    assert super_calls(source) == [("A.go", "go", 3, "super")]


def test_super_in_a_block_of_the_method_is_the_method_s_call():
    source = "class A < B\n  def go\n    [1].each { super }\n  end\nend\n"

    assert super_calls(source) == [("A.go", "go", 3, "super")]


def test_two_supers_are_two_calls():
    source = "class A < B\n  def go\n    super || super\n  end\nend\n"

    assert super_calls(source) == [("A.go", "go", 3, "super"), ("A.go", "go", 3, "super")]


def test_super_in_a_module_method_is_a_call():
    source = "module M\n  def go\n    super\n  end\nend\n"

    assert super_calls(source) == [("M.go", "go", 3, "super")]


def test_super_in_a_class_method_is_left_alone():
    source = "class A < B\n  def self.go\n    super\n  end\nend\n"

    assert super_calls(source) == []


def test_super_outside_a_method_is_left_alone():
    source = "class A < B\n  super\nend\n"

    assert super_calls(source) == []


def test_super_call_carries_the_rule_tag():
    source = "class A < B\n  def go\n    super\n  end\nend\n"
    _, edges = CodeParser().parse_bytes(PATH, source.encode())

    tags = [e.extra.get("fork_rule") for e in edges if e.extra.get("receiver") == "super"]
    assert tags == ["super-calls"]


def test_super_reaches_the_parent_method_in_another_file(tmp_path):
    files = {
        "base.rb": "class Base\n  def go; end\nend\n",
        "child.rb": "class Child < Base\n  def go\n    super\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "base.rb::Base.go"


def test_super_reaches_the_method_of_an_included_module(tmp_path):
    files = {
        "views.rb": "module Views\n  def go; end\nend\n",
        "gen.rb": "class Gen\n  include Views\n\n  def go\n    super\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "views.rb::Views.go"


def test_super_never_links_the_method_to_itself(tmp_path):
    # the parent is outside the graph: crg must not fall back to the caller's own `go`
    files = {"child.rb": "class Child < ActiveRecord::Base\n  def go\n    super\n  end\nend\n"}

    assert linked_target(tmp_path, files) != "child.rb::Child.go"


def test_super_in_a_top_level_method_is_left_alone():
    assert super_calls("def go\n  super\nend\n") == []


def test_each_class_s_super_belongs_to_its_own_method():
    source = (
        "class A < X\n  def go\n    super\n  end\nend\n"
        "class B < X\n  def go\n    super\n  end\nend\n"
    )

    assert super_calls(source) == [("A.go", "go", 3, "super"), ("B.go", "go", 8, "super")]


def test_super_in_a_method_two_classes_define_on_one_line_is_left_alone():
    source = "class A < X; def go; super; end; end; class B < X; def go; super; end; end\n"

    assert super_calls(source) == []


def test_super_in_a_class_method_defined_inside_a_method_is_left_alone():
    source = "class A < B\n  def go\n    def self.k\n      super\n    end\n  end\nend\n"

    assert super_calls(source) == []
