"""A method a Ruby class-body macro makes (``mattr_accessor :x``, ``attr_reader :x``,
``delegate :x, to: :y``, ``class_attribute :x``, ``define_method(:x)``) gets the Function crg
gives the same method written out as a ``def``.

Origin: fork rule ``macro-methods`` (crg-fork, branch ``fork/ruby-macro-methods``); on devise,
129 real calls go to methods made by macros and crg has no node for any of them (SYN-F-631).
Expected graphs are crg's own output for the twin: the same class with each macro written out as
plain ``def`` lines on the macro's line (approved by Thiru, 2026-11-26).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import ruby
from code_review_graph.tools.query import query_graph

RULE_NAME = "macro-methods"
# crg stores a call to the macro itself; the twin has no macro, so that call is left out of the
# comparison.
MACRO_NAMES = frozenset({
    "mattr_accessor", "mattr_reader", "cattr_accessor", "cattr_reader", "class_attribute",
    "attr_reader", "attr_accessor", "delegate", "define_method",
})


def parse(directory: Path, name: str, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(dedent(source))
    return CodeParser(directory).parse_file(path)


def graph(directory: Path, source: str) -> tuple[list[tuple], list[tuple]]:
    """The nodes and edges crg makes for *source*, paths relative, the macro's own call left out."""
    nodes, edges = parse(directory, "subject.rb", source)
    root = f"{directory.as_posix()}/"
    node_rows = sorted(
        (n.kind, n.name.replace(root, ""), n.parent_name, n.line_start, n.line_end,
         n.identity_name, n.is_test, n.language)
        for n in nodes
    )
    edge_rows = sorted(
        (e.kind, e.source.replace(root, ""), e.target.replace(root, ""), e.line)
        for e in edges
        if not (e.kind == "CALLS" and e.target in MACRO_NAMES)
    )
    return node_rows, edge_rows


def made_by_the_rule(edges_or_nodes: list) -> list:
    return [item for item in edges_or_nodes if item.extra.get("fork_rule") == RULE_NAME]


# Each macro next to its twin: the same file with the macro written out as `def` lines on the
# macro's first line (later lines of a macro that spans several are left empty).
TWINS = {
    "mattr_accessor in a module, called in the file": (
        """\
        module Devise
          mattr_accessor :paranoid, :timeout_in
          mattr_accessor :default_scope, default: nil

          def self.check
            paranoid(1)
            Devise.timeout_in(2)
            default_scope(3)
          end
        end
        """,
        """\
        module Devise
          def self.paranoid; end; def self.timeout_in; end
          def self.default_scope; end

          def self.check
            paranoid(1)
            Devise.timeout_in(2)
            default_scope(3)
          end
        end
        """,
    ),
    "mattr_reader and cattr_* with options": (
        """\
        module Config
          mattr_reader :helpers
          cattr_accessor :router_name, instance_reader: false
          cattr_reader :omniauth_configs

          def self.go
            helpers(1)
            router_name(2)
          end
        end
        """,
        """\
        module Config
          def self.helpers; end
          def self.router_name; end
          def self.omniauth_configs; end

          def self.go
            helpers(1)
            router_name(2)
          end
        end
        """,
    ),
    "class_attribute inside an included block": (
        """\
        module Authenticatable
          extend ActiveSupport::Concern

          included do
            class_attribute :devise_modules, instance_writer: false
          end

          def modules
            devise_modules(1)
          end
        end
        """,
        """\
        module Authenticatable
          extend ActiveSupport::Concern

          included do
            def self.devise_modules; end
          end

          def modules
            devise_modules(1)
          end
        end
        """,
    ),
    "attr_reader with several names over several lines": (
        """\
        class Mapping
          attr_reader :singular, :path,
                      :format

          def go
            path(1)
            format(2)
          end
        end
        """,
        """\
        class Mapping
          def singular; end; def path; end; def format; end


          def go
            path(1)
            format(2)
          end
        end
        """,
    ),
    "attr_accessor and delegate in class << self": (
        """\
        class TimeInflector
          class << self
            attr_accessor :instance
            delegate :time_ago_in_words, to: :instance
          end

          def self.ago
            time_ago_in_words(1)
            TimeInflector.instance(2)
          end
        end
        """,
        """\
        class TimeInflector
          class << self
            def instance; end
            def time_ago_in_words; end
          end

          def self.ago
            time_ago_in_words(1)
            TimeInflector.instance(2)
          end
        end
        """,
    ),
    "delegate whose name a nested module also defines": (
        """\
        module Lockable
          delegate :lock_strategy_enabled?, :unlock_strategy_enabled?, to: "self.class"

          def locked?
            lock_strategy_enabled?(:failed_attempts)
          end

          module ClassMethods
            def lock_strategy_enabled?(strategy)
            end
          end
        end
        """,
        """\
        module Lockable
          def lock_strategy_enabled?; end; def unlock_strategy_enabled?; end

          def locked?
            lock_strategy_enabled?(:failed_attempts)
          end

          module ClassMethods
            def lock_strategy_enabled?(strategy)
            end
          end
        end
        """,
    ),
    "delegate with prefix: false keeps the names": (
        """\
        class NotificationGroup
          delegate :type,
                   :target_status,
                   to: :notification, prefix: false

          def go
            type(1)
          end
        end
        """,
        """\
        class NotificationGroup
          def type; end; def target_status; end



          def go
            type(1)
          end
        end
        """,
    ),
    "define_method with a literal name": (
        """\
        class Helpers
          define_method(:short) { |value| shorten(value) }
          define_method "long" do
            lengthen(1)
            short(2)
          end

          def go
            long(3)
          end
        end
        """,
        """\
        class Helpers
          def short(value) shorten(value) end
          def long()
            lengthen(1)
            short(2)
          end

          def go
            long(3)
          end
        end
        """,
    ),
    "define_method on the line of a method calling the same name": (
        """\
        class Helpers
          def go() pick(1) end; define_method(:short) { pick(2) }
        end
        """,
        """\
        class Helpers
          def go() pick(1) end; def short() pick(2) end
        end
        """,
    ),
    "define_method on the line of a class-body call of the same name": (
        """\
        class Helpers
          define_method(:short) { pick(1) }; pick(2)
        end
        """,
        """\
        class Helpers
          def short() pick(1) end; pick(2)
        end
        """,
    ),
    "macro in a class with a scoped name": (
        """\
        module Devise
          class Devise::Mapping
            attr_reader :name

            def show
              name(1)
            end
          end
        end
        """,
        """\
        module Devise
          class Devise::Mapping
            def name; end

            def show
              name(1)
            end
          end
        end
        """,
    ),
    "macro after a nested class on the same line": (
        "class A; class B; end; attr_reader :y; end\n",
        "class A; class B; end; def y; end; end\n",
    ),
    "macro whose name crg reads as a test": (
        """\
        class Settings
          attr_reader :test_mode
        end
        """,
        """\
        class Settings
          def test_mode; end
        end
        """,
    ),
}


@pytest.mark.parametrize("macro, twin", TWINS.values(), ids=TWINS.keys())
def test_macro_gives_the_graph_of_its_twin(tmp_path, macro, twin):
    expected = graph(tmp_path / "twin", twin)

    assert graph(tmp_path / "macro", macro) == expected


def test_one_node_per_name_when_the_class_already_has_it_from_another_macro(tmp_path):
    source = "module Devise\n  mattr_accessor :paranoid\n  mattr_reader :paranoid\nend\n"
    nodes, _ = parse(tmp_path, "subject.rb", source)

    assert [(n.name, n.line_start) for n in nodes if n.kind == "Function"] == [("paranoid", 2)]


def test_define_method_without_a_name_does_not_stop_the_other_macros(tmp_path):
    source = "class A\n  define_method do\n  end\n  define_method()\n  attr_reader :x\nend\n"
    nodes, _ = parse(tmp_path, "subject.rb", source)

    assert [n.name for n in made_by_the_rule(nodes)] == ["x"]


# Shapes the rule must leave exactly as crg makes them.
UNCHANGED = {
    "no macro": "class A\n  def x\n  end\nend\n",
    "writer-only macros (crg stores no Function for def x=)": (
        "class A\n  attr_writer :x\n  mattr_writer :y\n  cattr_writer :z\nend\n"
    ),
    "a name the class also defines with def": (
        "class A\n  attr_reader :x\n\n  def x\n    @x\n  end\n\n  def go\n    x(1)\n  end\nend\n"
    ),
    "a setter or operator name": "class A\n  delegate :x=, :[], to: :y\nend\n",
}


def rows_with_all_rules(directory: Path, source: str) -> tuple[list[tuple], list[tuple]]:
    nodes, edges = parse(directory, "subject.rb", source)
    root = f"{directory.as_posix()}/"
    return (
        sorted((n.kind, n.name.replace(root, ""), n.parent_name, n.line_start, n.line_end,
                repr(sorted(n.extra.items()))) for n in nodes),
        sorted((e.kind, e.source.replace(root, ""), e.target.replace(root, ""), e.line,
                repr(sorted(e.extra.items()))) for e in edges),
    )


def graphs_with_and_without_the_rule(tmp_path, monkeypatch, source):
    """The rows of *source* with every Ruby rule on, then with every Ruby rule but this one."""
    from code_review_graph.rules.ruby.macro_methods import add_methods_made_by_macros

    with_rule = rows_with_all_rules(tmp_path / "with_rule", source)
    monkeypatch.setattr(
        ruby, "RULES", tuple(rule for rule in ruby.RULES if rule is not add_methods_made_by_macros),
    )
    return with_rule, rows_with_all_rules(tmp_path / "without_rule", source)


@pytest.mark.parametrize("source", UNCHANGED.values(), ids=UNCHANGED.keys())
def test_shape_is_left_as_crg_makes_it(tmp_path, monkeypatch, source):
    with_rule, without_rule = graphs_with_and_without_the_rule(tmp_path, monkeypatch, source)

    assert with_rule == without_rule


# Shapes that look like a macro but do not define a method crg can name.
NOT_A_MACRO_METHOD = {
    "macro on another receiver": (
        "class A\n  self.class.attr_reader :a\n  Other.delegate :b, to: :c\n"
        "  klass.define_method(:d) { }\nend\n"
    ),
    "name built at run time": (
        "class A\n  define_method(\"x_#{1}\") { }\n  define_method(name) { }\n"
        "  attr_reader *NAMES\n  attr_reader :\"y#{1}\"\nend\n"
    ),
    "delegate with a prefix": "class A\n  delegate :x, to: :y, prefix: true\nend\n",
    "macro outside a class body": (
        "attr_reader :top\n\nclass A\n  def setup\n    attr_reader :in_method\n  end\n"
        "  attr_reader :maybe if ENV['X']\nend\n"
    ),
    "class-body call that is not a macro": (
        "class A\n  before_action :authenticate\n  validates :email\n  helper_method :current\n"
        "  alias_method :b, :a\nend\n"
    ),
}


@pytest.mark.parametrize("source", NOT_A_MACRO_METHOD.values(), ids=NOT_A_MACRO_METHOD.keys())
def test_no_method_is_added(tmp_path, source):
    nodes, edges = parse(tmp_path, "subject.rb", source)

    assert made_by_the_rule(nodes) == []
    assert made_by_the_rule(edges) == []


def test_new_node_and_its_contains_edge_name_the_rule(tmp_path):
    nodes, edges = parse(tmp_path, "subject.rb", "class A\n  attr_reader :x\nend\n")

    assert [n.name for n in made_by_the_rule(nodes)] == ["x"]
    assert [(e.kind, e.target.rsplit("::", 1)[-1]) for e in made_by_the_rule(edges)] == [
        ("CONTAINS", "A.x"),
    ]


def test_call_linked_to_a_new_method_names_the_rule(tmp_path):
    source = "class A\n  attr_reader :x\n\n  def go\n    x(1)\n  end\nend\n"
    _, edges = parse(tmp_path, "subject.rb", source)

    linked = [e for e in edges if e.kind == "CALLS" and e.target.endswith("::A.x")]
    assert len(linked) == 1
    assert linked[0].extra["fork_rule"] == RULE_NAME


def test_call_in_a_define_method_block_names_the_rule(tmp_path, monkeypatch):
    # rule self-receivers marks this call later and its tag replaces this rule's
    monkeypatch.setattr(
        ruby, "RULES", tuple(r for r in ruby.RULES if r.__name__ != "mark_calls_on_self"),
    )
    _, edges = parse(tmp_path, "subject.rb", "class A\n  define_method(:x) { y(1) }\nend\n")

    moved = [e for e in edges if e.kind == "CALLS" and e.target == "y"]
    assert len(moved) == 1
    assert moved[0].source.endswith("::A.x")
    assert moved[0].extra["fork_rule"] == RULE_NAME


def test_rule_leaves_its_inputs_unchanged(tmp_path):
    source = (
        "class A\n  define_method(:x) { y(1) }\n  attr_reader :z\n\n"
        "  def go\n    z(1)\n  end\nend\n"
    )
    nodes, edges = parse(tmp_path / "plain", "subject.rb", source)
    nodes_before = [repr(n) for n in nodes]
    edges_before = [repr(e) for e in edges]
    from code_review_graph.rules.ruby.macro_methods import add_methods_made_by_macros

    parser = CodeParser(tmp_path / "plain")
    add_methods_made_by_macros(parser, "ruby", source.encode(), nodes, edges)

    assert [repr(n) for n in nodes] == nodes_before
    assert [repr(e) for e in edges] == edges_before


def test_rule_fails_when_crg_relinks_a_different_number_of_calls(tmp_path, monkeypatch):
    from code_review_graph.rules.ruby.macro_methods import add_methods_made_by_macros

    source = "class A\n  attr_reader :x\n\n  def go\n    x(1)\n  end\nend\n"
    monkeypatch.setattr(ruby, "RULES", ())
    nodes, edges = parse(tmp_path, "subject.rb", source)
    parser = CodeParser(tmp_path)
    monkeypatch.setattr(parser, "_resolve_call_targets", lambda nodes, edges, file_path: [])

    with pytest.raises(ValueError):
        add_methods_made_by_macros(parser, "ruby", source.encode(), nodes, edges)


def test_method_whose_class_crg_did_not_make_is_skipped_and_others_kept(tmp_path, monkeypatch):
    from code_review_graph.rules.ruby.macro_methods import add_methods_made_by_macros

    source = "class A\n  attr_reader :x\nend\n\nclass B\n  attr_reader :y\nend\n"
    monkeypatch.setattr(ruby, "RULES", ())
    nodes, edges = parse(tmp_path, "subject.rb", source)
    without_a = [n for n in nodes if not (n.kind == "Class" and n.name == "A")]

    added, _ = add_methods_made_by_macros(
        CodeParser(tmp_path), "ruby", source.encode(), without_a, edges,
    )

    assert [(n.name, n.parent_name) for n in made_by_the_rule(added)] == [("y", "B")]


def test_method_in_a_class_crg_cannot_tell_apart_is_skipped_and_logged(tmp_path, caplog):
    nodes, _ = parse(tmp_path, "subject.rb", "class A; class A; end; attr_reader :y; end\n")

    assert made_by_the_rule(nodes) == []
    assert "no Class node of crg's for the class at line 1; y skipped" in caplog.text


def test_rule_without_a_file_node_fails_with_a_clear_error(tmp_path, monkeypatch):
    from code_review_graph.rules.ruby.macro_methods import add_methods_made_by_macros

    source = "class A\n  attr_reader :x\nend\n"
    monkeypatch.setattr(ruby, "RULES", ())
    nodes, edges = parse(tmp_path, "subject.rb", source)
    without_file = [n for n in nodes if n.kind != "File"]

    with pytest.raises(ValueError, match="no File node"):
        add_methods_made_by_macros(
            CodeParser(tmp_path), "ruby", source.encode(), without_file, edges,
        )


def graph_store(repo: Path) -> GraphStore:
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    return GraphStore(graph_dir / "graph.db")


def test_full_build_links_a_call_from_another_file_to_the_new_method(tmp_path):
    (tmp_path / "devise.rb").write_text("module Devise\n  mattr_accessor :paranoid\nend\n")
    (tmp_path / "lockable.rb").write_text(
        'require_relative "devise"\n\nclass Lockable\n  def message\n    Devise.paranoid\n'
        "  end\nend\n"
    )
    with graph_store(tmp_path) as store:
        full_build(tmp_path, store)

    result = query_graph(
        "callers_of", f"{(tmp_path / 'devise.rb').as_posix()}::Devise.paranoid",
        repo_root=str(tmp_path),
    )
    assert result["status"] == "ok"
    assert [node["name"] for node in result["results"]] == ["message"]
