"""A Ruby class's superclass (``class Child < Base``) and the modules it includes
(``include Helpers``) are its parents, so each gets the INHERITS edge crg writes for a base class.

Origin: fork rule ``class-parents`` (crg-fork, branch ``fork/ruby-class-index``); crg 2.4.0's
``_get_bases`` has no Ruby case, so a Ruby class has no INHERITS edge and crg's inheritance index
has no chain to walk (on devise, 0 INHERITS edges). Finding SYN-F-633.

crg writes no Ruby INHERITS edge, so there is no twin: the expected edges are written by hand in
the form crg writes a base for other languages (source the class, target the name as written,
line the class's first line).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser, EdgeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.class_parents import add_class_parents

RULE_NAME = "class-parents"


def parse(directory: Path, source: str) -> tuple[list, list[EdgeInfo]]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "subject.rb"
    path.write_text(source)
    return CodeParser(directory).parse_file(path)


def parents(directory: Path, source: str) -> list[tuple[str, str, int]]:
    """Each INHERITS edge: the class (after the file), the parent as written, and the line."""
    _, edges = parse(directory, source)
    return sorted(
        (e.source.split("::", 1)[1], e.target, e.line) for e in edges if e.kind == "INHERITS"
    )


def relative(edges: list[EdgeInfo], directory: Path) -> list[str]:
    return [repr(e).replace(directory.as_posix(), "") for e in edges]


# ---- positive: parents written by hand ----------------------------------------------------------

PARENTS = {
    "superclass": (
        "class Child < Base\nend\n",
        [("Child", "Base", 1)],
    ),
    "scoped superclass": (
        "class Child < Shop::Base\nend\n",
        [("Child", "Shop::Base", 1)],
    ),
    "top-level superclass": (
        "class Child < ::Base\nend\n",
        [("Child", "::Base", 1)],
    ),
    "class in a module": (
        "module Shop\n  class Cart < Base\n  end\nend\n",
        [("Shop.Cart", "Base", 2)],
    ),
    "class with a scoped name": (
        "class Devise::ConfirmationsController < DeviseController\nend\n",
        [("Devise.ConfirmationsController", "DeviseController", 1)],
    ),
    "include in a class": (
        "class Cart\n  include Helpers\nend\n",
        [("Cart", "Helpers", 1)],
    ),
    "several modules in one include": (
        "class Cart\n  include Helpers, Shop::Pricing\nend\n",
        [("Cart", "Helpers", 1), ("Cart", "Shop::Pricing", 1)],
    ),
    "include in a module": (
        "module Shop\n  module Pricing\n    include Rounding\n  end\nend\n",
        [("Shop.Pricing", "Rounding", 2)],
    ),
    "superclass and include": (
        "class Child < Base\n  include Helpers\nend\n",
        [("Child", "Base", 1), ("Child", "Helpers", 1)],
    ),
}


@pytest.mark.parametrize("name", sorted(PARENTS))
def test_class_gets_an_inherits_edge_for_each_parent(tmp_path, name):
    source, expected = PARENTS[name]

    assert parents(tmp_path, source) == expected


# ---- unchanged and false positive: no parent the file names as a constant -----------------------

NO_PARENT = {
    "class without a superclass": "class Cart\n  def total; end\nend\n",
    "superclass built at run time": "class Child < Devise.parent_controller.constantize\nend\n",
    "include of something built at run time": "class Cart\n  include helpers_for(:cart)\nend\n",
    "include on a receiver": (
        "class Cart\n  def self.setup(base)\n    base.include Helpers\n  end\nend\n"
    ),
    "include on a receiver in the class body": "class Cart\n  Other.include Helpers\nend\n",
    "include inside a method": "class Cart\n  def setup\n    include Helpers\n  end\nend\n",
    "include inside a block": "module Shop\n  included do\n    include Helpers\n  end\nend\n",
    "extend": "class Cart\n  extend Helpers\nend\n",
    "class << self": "class Cart\n  class << self\n    def build; end\n  end\nend\n",
}


@pytest.mark.parametrize("name", sorted(NO_PARENT))
def test_shape_without_a_named_parent_is_unchanged(tmp_path, monkeypatch, name):
    with_rule = parse(tmp_path / "on", NO_PARENT[name])
    monkeypatch.setattr(
        ruby, "RULES", tuple(rule for rule in ruby.RULES if rule is not add_class_parents),
    )
    without_rule = parse(tmp_path / "off", NO_PARENT[name])

    assert relative(with_rule[1], tmp_path / "on") == relative(without_rule[1], tmp_path / "off")


# ---- tag and contract ---------------------------------------------------------------------------

def test_inherits_edge_names_the_rule(tmp_path):
    _, edges = parse(tmp_path, "class Child < Base\n  include Helpers\nend\n")

    assert [e.extra for e in edges if e.kind == "INHERITS"] == [
        {"fork_rule": RULE_NAME, "ruby_ancestor": 1},
        {"fork_rule": RULE_NAME, "ruby_ancestor": 2},
    ]


def test_rule_leaves_its_inputs_unchanged(tmp_path):
    path = tmp_path / "subject.rb"
    path.write_text("class Child < Base\nend\n")
    parser = CodeParser(tmp_path)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())
    nodes_before, edges_before = [repr(n) for n in nodes], [repr(e) for e in edges]

    add_class_parents(parser, "ruby", path.read_bytes(), nodes, edges)

    assert [repr(n) for n in nodes] == nodes_before
    assert [repr(e) for e in edges] == edges_before


def test_rule_without_a_file_node_fails_with_a_clear_error(tmp_path):
    path = tmp_path / "subject.rb"
    path.write_text("class Child < Base\nend\n")
    parser = CodeParser(tmp_path)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())

    with pytest.raises(ValueError, match="no File node"):
        add_class_parents(
            parser, "ruby", path.read_bytes(), [n for n in nodes if n.kind != "File"], edges,
        )
