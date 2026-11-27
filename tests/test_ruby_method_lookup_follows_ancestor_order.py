"""A Ruby call on ``self`` or ``super`` is looked up in Ruby's ancestor order: the modules the class
includes, the last included first, each with the modules it includes, and then the superclass.

Origin: rule ``class-parents`` (which now numbers each parent's place in that order) and a Ruby
walk in ``InheritanceIndex`` (crg-fork, branch ``fork/ruby-class-index``). crg 2.4.0's walk gives
up on a class with more than one parent when any parent is outside the graph, so
``SimpleFormForGenerator < Rails::Generators::Base`` with ``include ViewPathTemplates`` never
reached ``ViewPathTemplates#copy_views``, though Ruby looks in the module before the superclass.
Finding SYN-F-635.

No twin: crg writes no Ruby parents; the expected order is Ruby's (``Module#ancestors``), written
by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser

from .test_ruby_call_reaches_the_method_its_class_inherits import linked_target


def ancestor_places(source: str) -> list[tuple[str, int]]:
    """Each parent the parse gives the class, with its place in Ruby's lookup order."""
    _, edges = CodeParser().parse_bytes(Path("/repo/a.rb"), source.encode())
    return sorted(
        ((e.target, e.extra["ruby_ancestor"]) for e in edges if e.kind == "INHERITS"),
        key=lambda parent: parent[1],
    )


# ---- the order rule class-parents writes ------------------------------------------------------

@pytest.mark.parametrize(("body", "expected"), [
    ("class C < S\nend\n", [("S", 1)]),
    ("class C < S\n  include A\nend\n", [("A", 1), ("S", 2)]),
    ("class C < S\n  include A\n  include B\nend\n", [("B", 1), ("A", 2), ("S", 3)]),
    ("class C\n  include A, B\nend\n", [("A", 1), ("B", 2)]),
    ("module M\n  include A\n  include B, C\nend\n", [("B", 1), ("C", 2), ("A", 3)]),
])
def test_each_parent_carries_its_place_in_ruby_s_lookup_order(body, expected):
    assert ancestor_places(body) == expected


# ---- the walk ---------------------------------------------------------------------------------

def test_included_module_is_searched_before_a_superclass_outside_the_graph(tmp_path):
    files = {
        "views.rb": "module Views\n  def work; end\nend\n",
        "gen.rb": (
            "class Gen < Rails::Generators::Base\n  include Views\n\n"
            "  def go\n    work\n  end\nend\n"
        ),
    }

    assert linked_target(tmp_path, files) == "views.rb::Views.work"


def test_super_reaches_the_included_module_before_a_superclass_outside_the_graph(tmp_path):
    files = {
        "views.rb": "module Views\n  def go; end\nend\n",
        "gen.rb": (
            "class Gen < Rails::Generators::Base\n  include Views\n\n"
            "  def go\n    super\n  end\nend\n"
        ),
    }

    assert linked_target(tmp_path, files) == "views.rb::Views.go"


def test_the_last_included_module_wins(tmp_path):
    files = {
        "a.rb": "module A\n  def work; end\nend\n",
        "b.rb": "module B\n  def work; end\nend\n",
        "c.rb": "class C\n  include A\n  include B\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "b.rb::B.work"


def test_included_module_wins_over_the_superclass(tmp_path):
    files = {
        "a.rb": "module A\n  def work; end\nend\n",
        "base.rb": "class Base\n  def work; end\nend\n",
        "c.rb": "class C < Base\n  include A\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "a.rb::A.work"


def test_a_module_s_own_includes_come_before_the_next_parent(tmp_path):
    files = {
        "n.rb": "module N\n  def work; end\nend\n",
        "m.rb": "module M\n  include N\nend\n",
        "base.rb": "class Base\n  def work; end\nend\n",
        "c.rb": "class C < Base\n  include M\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "n.rb::N.work"


def test_superclass_chain_is_searched_after_the_modules(tmp_path):
    files = {
        "a.rb": "module A\n  def other; end\nend\n",
        "base.rb": "class Base\n  def work; end\nend\n",
        "c.rb": "class C < Base\n  include A\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "base.rb::Base.work"


def test_a_parent_outside_the_graph_searched_first_leaves_the_call_unlinked(tmp_path):
    # Ruby looks in `Unknown` before `A`: it may hold `work`
    files = {
        "a.rb": "module A\n  def work; end\nend\n",
        "c.rb": "class C\n  include A\n  include Unknown\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "work"


def test_a_module_included_twice_is_searched_once(tmp_path):
    files = {
        "a.rb": "module A\n  def other; end\nend\n",
        "m.rb": "module M\n  include A\nend\n",
        "base.rb": "class Base\n  def work; end\nend\n",
        "c.rb": "class C < Base\n  include A\n  include M\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "base.rb::Base.work"


def test_modules_that_include_each_other_end_the_walk(tmp_path):
    files = {
        "a.rb": "module A\n  include B\nend\n",
        "b.rb": "module B\n  include A\nend\n",
        "c.rb": "class C\n  include A\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "work"  # left unlinked
