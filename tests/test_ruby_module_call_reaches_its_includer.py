"""A call on ``self`` in a Ruby module that neither the module nor its ancestors answer reaches the
method of the classes and modules that include it, when they agree on one.

Origin: Ruby lookup for modules in ``InheritanceIndex`` / ``GraphStore._class_backed_candidates``
(crg-fork, branch ``fork/ruby-class-index``). In a module's method, ``self`` is an object of a class
that includes the module, so a module may call a method only its includer declares::

    module Devise::Controllers::Rememberable
      def forget_cookie(scope)
        cookies.delete(...)       # Devise::Hooks::Proxy, which includes Rememberable, delegates it
      end
    end

crg 2.4.0 with changes 1-10 left such a call unlinked. Finding SYN-F-636.

No twin: crg writes no Ruby parents; the expected links are written by hand.
"""

from __future__ import annotations

from .test_ruby_call_reaches_the_method_its_class_inherits import linked_target

MODULE = "module Mixin\n  def go\n    work\n  end\nend\n"


def test_module_call_reaches_the_method_of_its_one_includer(tmp_path):
    files = {
        "mixin.rb": MODULE,
        "host.rb": "class Host\n  include Mixin\n\n  def work; end\nend\n",
    }

    assert linked_target(tmp_path, files) == "host.rb::Host.work"


def test_includers_declaring_different_methods_leave_the_call_unlinked(tmp_path):
    files = {
        "mixin.rb": MODULE,
        "a.rb": "class A\n  include Mixin\n\n  def work; end\nend\n",
        "b.rb": "class B\n  include Mixin\n\n  def work; end\nend\n",
    }

    assert linked_target(tmp_path, files) == "work"  # left unlinked


def test_an_includer_without_the_method_does_not_stop_the_one_that_has_it(tmp_path):
    files = {
        "mixin.rb": MODULE,
        "a.rb": "class A\n  include Mixin\nend\n",
        "b.rb": "class B\n  include Mixin\n\n  def work; end\nend\n",
    }

    assert linked_target(tmp_path, files) == "b.rb::B.work"


def test_the_module_s_own_ancestors_come_first(tmp_path):
    files = {
        "helpers.rb": "module Helpers\n  def work; end\nend\n",
        "mixin.rb": "module Mixin\n  include Helpers\n\n  def go\n    work\n  end\nend\n",
        "host.rb": "class Host\n  include Mixin\n\n  def work; end\nend\n",
    }

    assert linked_target(tmp_path, files) == "helpers.rb::Helpers.work"


def test_a_class_is_not_answered_by_its_subclasses(tmp_path):
    # a superclass's object is not a subclass's: only included modules look down
    files = {
        "base.rb": "class Base\n  def go\n    work\n  end\nend\n",
        "child.rb": "class Child < Base\n  def work; end\nend\n",
    }

    assert linked_target(tmp_path, files) == "work"  # left unlinked


def test_a_call_on_an_object_in_a_module_is_not_answered_by_its_includer(tmp_path):
    files = {
        "mixin.rb": "module Mixin\n  def go(item)\n    item.work\n  end\nend\n",
        "host.rb": "class Host\n  include Mixin\n\n  def work; end\nend\n",
    }

    assert linked_target(tmp_path, files) == "work"  # left unlinked


def test_super_in_a_module_is_not_answered_by_its_includer(tmp_path):
    # `super` looks up the ancestors, and the includer is below the module
    files = {
        "mixin.rb": "module Mixin\n  def go\n    super\n  end\nend\n",
        "host.rb": "class Host\n  include Mixin\n\n  def go; end\nend\n",
    }

    assert linked_target(tmp_path, files) == "go"  # left unlinked
