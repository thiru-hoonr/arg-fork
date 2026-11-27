"""A Ruby call on ``self`` reaches the method the caller's class inherits from a parent in another
file, found by Ruby's constant lookup even when no ``require`` names that file.

Origin: fork change to ``inheritance.py`` with rules ``class-parents`` and ``self-receivers``
(crg-fork, branch ``fork/ruby-class-index``). crg 2.4.0's ``InheritanceIndex.resolve_type`` finds a
parent only in the same file or a ``require``d one, and does not split a ``::`` path; Rails loads
``DeviseController`` without a ``require``, so ``self.resource = x`` in
``Devise::ConfirmationsController`` was never linked to ``DeviseController#resource=``. On devise,
145 real calls were missed this way. Finding SYN-F-633.

Ruby looks a constant up in the modules written around the class, innermost first, then at the
top level; ``::Base`` is the top level only. Expected links are written by hand.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build


def linked_target(repo: Path, files: dict[str, str], caller: str = "go") -> str:
    """What *caller*'s call is linked to once crg has built *files* and resolved its calls."""
    for name, source in files.items():
        (repo / name).write_text(source)
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    with GraphStore(graph_dir / "graph.db") as store:
        full_build(repo, store)
        store.resolve_bare_call_targets()  # what crg's post-build step runs
    with sqlite3.connect(graph_dir / "graph.db") as db:
        targets = [
            target for (target,) in db.execute(
                "SELECT target_qualified FROM edges WHERE kind = 'CALLS' "
                "AND source_qualified LIKE ?", (f"%.{caller}",),
            )
        ]
    assert len(targets) == 1, targets
    return targets[0].replace(f"{repo.as_posix()}/", "")


CHILD = "class Child < {parent}\n  def go\n    work\n  end\nend\n"


def test_call_reaches_the_parent_method_in_a_file_never_required(tmp_path):
    files = {
        "base.rb": "class Base\n  def work; end\nend\n",
        "child.rb": CHILD.format(parent="Base"),
    }

    assert linked_target(tmp_path, files) == "base.rb::Base.work"


def test_setter_assignment_reaches_the_parent_setter(tmp_path):
    files = {
        "devise_controller.rb": "class DeviseController\n  def resource=(value); end\nend\n",
        "confirmations_controller.rb": (
            "module Devise\n  class ConfirmationsController < DeviseController\n"
            "    def create\n      self.resource = 1\n    end\n  end\nend\n"
        ),
    }

    setter = "devise_controller.rb::DeviseController.resource="
    assert linked_target(tmp_path, files, "create") == setter


def test_scoped_parent_names_its_module(tmp_path):
    files = {
        "shop_base.rb": "module Shop\n  class Base\n    def work; end\n  end\nend\n",
        "base.rb": "class Base\n  def work; end\nend\n",
        "child.rb": CHILD.format(parent="Shop::Base"),
    }

    assert linked_target(tmp_path, files) == "shop_base.rb::Shop.Base.work"


def test_parent_is_looked_up_in_the_modules_around_the_class_first(tmp_path):
    files = {
        "shop_base.rb": "module Shop\n  class Base\n    def work; end\n  end\nend\n",
        "base.rb": "class Base\n  def work; end\nend\n",
        "child.rb": (
            "module Shop\n  class Child < Base\n    def go\n      work\n    end\n  end\nend\n"
        ),
    }

    assert linked_target(tmp_path, files) == "shop_base.rb::Shop.Base.work"


def test_superclass_is_not_looked_up_inside_the_class_itself(tmp_path):
    # Ruby reads `< Base` before it opens `Child`, so `Shop::Child::Base` is no candidate
    files = {
        "shop_base.rb": "module Shop\n  class Base\n    def work; end\n  end\nend\n",
        "inner_base.rb": (
            "module Shop\n  class Child\n    class Base\n      def work; end\n    end\n  end\nend\n"
        ),
        "child.rb": (
            "module Shop\n  class Child < Base\n    def go\n      work\n    end\n  end\nend\n"
        ),
    }

    assert linked_target(tmp_path, files) == "shop_base.rb::Shop.Base.work"


def test_class_of_another_language_is_no_ruby_parent(tmp_path):
    files = {
        "base.py": "class Base:\n    def work(self):\n        pass\n",
        "child.rb": CHILD.format(parent="Base"),
    }

    assert linked_target(tmp_path, files) == "work"  # left unlinked


def test_top_level_parent_skips_the_modules_around_the_class(tmp_path):
    files = {
        "shop_base.rb": "module Shop\n  class Base\n    def work; end\n  end\nend\n",
        "base.rb": "class Base\n  def work; end\nend\n",
        "child.rb": (
            "module Shop\n  class Child < ::Base\n    def go\n      work\n    end\n  end\nend\n"
        ),
    }

    assert linked_target(tmp_path, files) == "base.rb::Base.work"


def test_parent_two_files_define_is_not_guessed(tmp_path):
    files = {
        "base_a.rb": "class Base\n  def work; end\nend\n",
        "base_b.rb": "class Base\n  def work; end\nend\n",
        "child.rb": CHILD.format(parent="Base"),
    }

    assert linked_target(tmp_path, files) == "work"  # left unlinked


def test_call_reaches_the_method_of_an_included_module(tmp_path):
    files = {
        "helpers.rb": "module Helpers\n  def work; end\nend\n",
        "cart.rb": "class Cart\n  include Helpers\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "helpers.rb::Helpers.work"


def test_own_method_wins_over_a_same_named_method_crg_linked_in_the_file(tmp_path):
    # crg links `target_path` in the file to the module's method; the class's own one runs
    files = {
        "views.rb": (
            "module ViewPathTemplates\n  def target_path; end\nend\n\n"
            "class MarkerbGenerator\n  include ViewPathTemplates\n\n"
            "  def go\n    target_path\n  end\n\n  def target_path; end\nend\n"
        ),
    }

    assert linked_target(tmp_path, files) == "views.rb::MarkerbGenerator.target_path"


def test_own_method_wins_over_the_parent_method(tmp_path):
    files = {
        "base.rb": "class Base\n  def work; end\nend\n",
        "child.rb": "class Child < Base\n  def work; end\n\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(tmp_path, files) == "child.rb::Child.work"
