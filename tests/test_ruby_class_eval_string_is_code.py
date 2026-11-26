"""Ruby code passed as a string to ``class_eval`` / ``module_eval`` / ``instance_eval`` is code.

Origin: fork rule ``eval-strings`` (crg-fork, branch ``fork/ruby-class-eval``); on devise crg 2.4.0
missed 46 real calls made from such strings (``lib/devise/controllers/helpers.rb``,
``lib/devise/mapping.rb``, ``lib/devise/models.rb``). Finding SYN-F-630.

The twin is the same code written in place: the ``class_eval`` call and the heredoc's closing line
left blank, so every line keeps its number. crg's output for the twin is the expected graph. A name
built with ``#{...}`` is known only at run time, so the expected values for those cases are written
by hand (approved by Thiru, 2026-11-26).
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.eval_strings import (
    _code_strings,
    _twin,
    add_code_from_eval_strings,
)

RULE_NAME = "eval-strings"
RULE_MODULE = "code_review_graph.rules.ruby.eval_strings"
FILE_NAME = "account.rb"


def parse(directory: Path, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    directory.mkdir(exist_ok=True)
    path = directory / FILE_NAME
    path.write_text(dedent(source))
    return CodeParser(directory).parse_file(path)


def short(qualified_name: str) -> str:
    """``Account.total`` for ``<dir>/account.rb::Account.total``; the file name for the file."""
    return qualified_name.split("::", 1)[1] if "::" in qualified_name else Path(qualified_name).name


def without_tag(extra: dict) -> str:
    return repr(sorted((key, value) for key, value in extra.items() if key != "fork_rule"))


def rows_on_lines(
    directory: Path, source: str, lines: range
) -> tuple[list[tuple], list[tuple]]:
    """The nodes and edges crg gives *source* on *lines*, with names relative to the file."""
    nodes, edges = parse(directory, source)
    node_rows = sorted(
        (n.kind, n.name, n.parent_name, n.line_start, n.line_end, without_tag(n.extra))
        for n in nodes
        if n.kind != "File" and n.line_start in lines
    )
    edge_rows = sorted(
        (e.kind, short(e.source), short(e.target), e.line, without_tag(e.extra))
        for e in edges
        if e.line in lines
    )
    return node_rows, edge_rows


def calls_on_line(edges: list[EdgeInfo], line: int) -> list[tuple[str, str]]:
    return sorted(
        (short(edge.source), short(edge.target))
        for edge in edges
        if edge.kind == "CALLS" and edge.line == line
    )


def functions(nodes: list[NodeInfo]) -> list[tuple[str, int]]:
    return sorted((node.name, node.line_start) for node in nodes if node.kind == "Function")


# ---- positive: the code in the string gives the twin's graph --------------------------------

# `{eval}` and `{open}` / `{close}` are filled in by each case; lines 3-5 are the string's code.
IN_CLASS_BODY = """\
class Account
  {eval} {open}, __FILE__, __LINE__ + 1
    def total
      compute(1)
    end
  {close}

  def compute(n)
  end
end
"""
IN_CLASS_BODY_TWIN = """\
class Account

    def total
      compute(1)
    end


  def compute(n)
  end
end
"""
STRING_CODE_LINES = range(3, 6)


@pytest.mark.parametrize("method", ["class_eval", "module_eval", "instance_eval"])
def test_heredoc_given_to_an_eval_method_gives_the_twin_graph(tmp_path, method):
    source = IN_CLASS_BODY.format(eval=method, open="<<-RUBY", close="RUBY")

    from_string = rows_on_lines(tmp_path / "string", source, STRING_CODE_LINES)
    twin = rows_on_lines(tmp_path / "twin", IN_CLASS_BODY_TWIN, STRING_CODE_LINES)

    assert twin[0] and twin[1]
    assert from_string == twin


@pytest.mark.parametrize(
    ("opening", "closing"),
    [("<<-RUBY", "RUBY"), ("<<~RUBY", "RUBY"), ("<<-'RUBY'", "RUBY"), ('<<-"RUBY"', "RUBY")],
)
def test_every_heredoc_form_gives_the_twin_graph(tmp_path, opening, closing):
    source = IN_CLASS_BODY.format(eval="class_eval", open=opening, close=closing)

    from_string = rows_on_lines(tmp_path / "string", source, STRING_CODE_LINES)
    twin = rows_on_lines(tmp_path / "twin", IN_CLASS_BODY_TWIN, STRING_CODE_LINES)

    assert from_string == twin


@pytest.mark.parametrize("receiver", ["self.", "Account.", "Account::"])
def test_eval_with_a_receiver_gives_the_twin_graph(tmp_path, receiver):
    source = IN_CLASS_BODY.format(eval=f"{receiver}class_eval", open="<<-RUBY", close="RUBY")

    from_string = rows_on_lines(tmp_path / "string", source, STRING_CODE_LINES)
    twin = rows_on_lines(tmp_path / "twin", IN_CLASS_BODY_TWIN, STRING_CODE_LINES)

    assert from_string == twin


# devise's shape: the string is evaluated by a class method; the setter rule runs on its code too.
IN_CLASS_METHOD = """\
class Account
  def self.define(name)
    class_eval <<-RUBY, __FILE__, __LINE__ + 1
      def total
        self.balance = compute(1)
      end
    RUBY
  end

  def compute(n)
  end
end
"""
IN_CLASS_METHOD_TWIN = """\
class Account
  def self.define(name)

      def total
        self.balance = compute(1)
      end

  end

  def compute(n)
  end
end
"""


def test_heredoc_evaluated_in_a_class_method_gives_the_twin_graph(tmp_path):
    lines = range(4, 7)

    from_string = rows_on_lines(tmp_path / "string", IN_CLASS_METHOD, lines)
    twin = rows_on_lines(tmp_path / "twin", IN_CLASS_METHOD_TWIN, lines)

    assert from_string == twin


def test_calls_in_the_string_are_on_the_lines_of_the_file(tmp_path):
    _, edges = parse(tmp_path, IN_CLASS_METHOD)

    assert calls_on_line(edges, 5) == [
        ("Account.total", "Account.compute"),
        ("Account.total", "balance="),
    ]


def test_code_in_a_one_line_string_gives_the_twin_graph(tmp_path):
    source = (
        "class Account\n"
        '  class_eval "def total; compute(1); end", __FILE__, __LINE__\n'
        "  def compute(n)\n  end\nend\n"
    )
    twin = (
        "class Account\n"
        "              def total; compute(1); end                     \n"
        "  def compute(n)\n  end\nend\n"
    )

    from_string = rows_on_lines(tmp_path / "string", source, range(2, 3))
    from_twin = rows_on_lines(tmp_path / "twin", twin, range(2, 3))

    # The call to class_eval itself stays as crg makes it; the twin has no class_eval.
    eval_call = ("CALLS", FILE_NAME, "class_eval", 2, "[]")
    assert from_string == (from_twin[0], sorted([*from_twin[1], eval_call]))


def test_code_beside_a_one_line_string_is_not_counted_twice(tmp_path):
    source = (
        "class Account\n"
        '  compute(0); class_eval "def total; compute(1); end"\n'
        "  def compute(n)\n  end\nend\n"
    )

    _, edges = parse(tmp_path, source)

    assert calls_on_line(edges, 2) == [
        ("Account.total", "Account.compute"),
        (FILE_NAME, "Account.compute"),
        (FILE_NAME, "class_eval"),
    ]


def test_string_inside_a_string_is_code_too(tmp_path):
    source = """\
    class Account
      class_eval <<-OUTER, __FILE__, __LINE__ + 1
        class_eval <<-INNER, __FILE__, __LINE__ + 1
          def total
            compute(1)
          end
        INNER
      OUTER

      def compute(n)
      end
    end
    """

    nodes, edges = parse(tmp_path, source)

    assert ("total", 4) in functions(nodes)
    assert calls_on_line(edges, 5) == [("Account.total", "Account.compute")]


def test_every_string_in_a_file_is_read(tmp_path):
    source = """\
    class Account
      class_eval <<-FIRST, __FILE__, __LINE__ + 1
        def total
          compute(1)
        end
      FIRST

      class_eval <<-SECOND, __FILE__, __LINE__ + 1
        def subtotal
          compute(2)
        end
      SECOND

      def compute(n)
      end
    end
    """

    nodes, edges = parse(tmp_path, source)

    assert functions(nodes) == [("compute", 14), ("subtotal", 9), ("total", 3)]
    assert calls_on_line(edges, 4) == [("Account.total", "Account.compute")]
    assert calls_on_line(edges, 10) == [("Account.subtotal", "Account.compute")]


@pytest.mark.parametrize("empty_eval", ["class_eval()", "self.class_eval"])
def test_eval_without_a_string_does_not_stop_the_next_string_being_read(tmp_path, empty_eval):
    source = (
        f"class Account\n  {empty_eval}\n"
        "  class_eval <<-RUBY, __FILE__, __LINE__ + 1\n    def total; compute(1); end\n  RUBY\n"
        "  def compute(n)\n  end\nend\n"
    )

    _, edges = parse(tmp_path, source)

    assert calls_on_line(edges, 4) == [("Account.total", "Account.compute")]


def test_method_on_the_line_of_a_one_line_string_is_not_counted_twice(tmp_path):
    source = (
        "class Account\n"
        '  def self.define; class_eval "def total; compute(1); end"; end\n'
        "  def compute(n)\n  end\nend\n"
    )

    nodes, _ = parse(tmp_path, source)

    assert functions(nodes) == [("compute", 3), ("define", 2), ("total", 2)]


# ---- positive: names built with #{...} (expected values written by hand) ---------------------

WITH_INTERPOLATION = """\
class Account
  def self.define(name)
    class_eval <<-RUBY, __FILE__, __LINE__ + 1
      def #{name}_total
        compute(:#{name})
        #{name}_balance(1)
        self.#{name}
      end

      def total
        compute(#{name})
      end
    RUBY
  end

  def compute(n)
  end
end
"""


def test_method_whose_name_is_built_at_run_time_gets_no_function(tmp_path):
    nodes, _ = parse(tmp_path, WITH_INTERPOLATION)

    assert functions(nodes) == [("compute", 16), ("define", 2), ("total", 10)]


def test_calls_in_a_method_with_a_built_name_belong_to_the_method_holding_the_eval(tmp_path):
    _, edges = parse(tmp_path, WITH_INTERPOLATION)

    assert calls_on_line(edges, 5) == [("Account.define", "Account.compute")]


def test_calls_whose_own_name_is_built_at_run_time_are_dropped(tmp_path):
    _, edges = parse(tmp_path, WITH_INTERPOLATION)

    assert calls_on_line(edges, 6) == []
    assert calls_on_line(edges, 7) == []


def test_method_with_a_plain_name_beside_built_names_keeps_its_calls(tmp_path):
    _, edges = parse(tmp_path, WITH_INTERPOLATION)

    assert calls_on_line(edges, 11) == [("Account.total", "Account.compute")]


def test_calls_in_a_built_name_method_belong_to_the_innermost_method_holding_the_eval(tmp_path):
    source = """\
    class Account
      def self.outer(name)
        def self.inner(name)
          class_eval <<-RUBY, __FILE__, __LINE__ + 1
            def #{name}
              compute(1)
            end
          RUBY
        end
      end

      def compute(n)
      end
    end
    """

    _, edges = parse(tmp_path, source)

    assert calls_on_line(edges, 6) == [("Account.inner", "Account.compute")]


def test_calls_in_a_built_name_method_evaluated_in_a_class_body_belong_to_the_file(tmp_path):
    source = """\
    class Account
      class_eval <<-RUBY, __FILE__, __LINE__ + 1
        def #{NAME}
          compute(1)
        end
      RUBY

      def compute(n)
      end
    end
    """

    _, edges = parse(tmp_path, source)

    assert calls_on_line(edges, 4) == [(FILE_NAME, "Account.compute")]


# ---- fail safe: code the rule cannot read as Ruby is left as a string, with a trace ----------

UNREADABLE_CASES = {
    # `class crg_...` is not Ruby: a class name must be a constant.
    "built_class_name": "class #{name}\n        def total; compute(1); end\n      end",
    # Ruby turns `\\n` into a line break before it evaluates the string, so the lines would move.
    "escape_sequence": 'def total\n        compute("a\\\\nb")\n      end',
}


@pytest.mark.parametrize("name", sorted(UNREADABLE_CASES))
def test_string_that_is_not_readable_as_code_is_left_alone_and_logged(tmp_path, caplog, name):
    source = (
        "class Account\n  def self.define(name)\n    class_eval <<-RUBY, __FILE__, __LINE__ + 1\n"
        f"      {UNREADABLE_CASES[name]}\n    RUBY\n  end\n\n  def compute(n)\n  end\nend\n"
    )

    with caplog.at_level(logging.WARNING, logger=RULE_MODULE):
        nodes, edges = parse(tmp_path, source)

    assert functions(nodes) == [("compute", 10), ("define", 2)]
    assert calls_on_line(edges, 5) == []
    warnings = [record.getMessage() for record in caplog.records]
    assert any(RULE_NAME in message and FILE_NAME in message for message in warnings)


def test_file_without_a_file_node_raises_a_clear_error(tmp_path):
    nodes, edges = parse(tmp_path, IN_CLASS_METHOD)
    without_file = [node for node in nodes if node.kind != "File"]

    with pytest.raises(ValueError, match="no File node"):
        add_code_from_eval_strings(
            CodeParser(tmp_path), "ruby", IN_CLASS_METHOD.encode(), without_file, edges
        )


def test_strings_out_of_source_order_raise_a_clear_error():
    source = (
        "class Account\n"
        '  class_eval "def total; end"\n'
        '  class_eval "def size; end"\n'
        "end\n"
    ).encode()
    tree = CodeParser()._get_parser("ruby").parse(source)
    strings = _code_strings(tree.root_node)

    with pytest.raises(ValueError, match="out of source order"):
        _twin(source, list(reversed(strings)))


# ---- tag -------------------------------------------------------------------------------------


def test_every_node_and_edge_from_the_string_names_the_rule(tmp_path):
    nodes, edges = parse(tmp_path, IN_CLASS_METHOD)

    added = [n for n in nodes if n.line_start in range(4, 7)]
    added += [e for e in edges if e.line in range(4, 7)]
    assert added
    assert all(item.extra.get("fork_rule") == RULE_NAME for item in added)


# ---- unchanged and false positives: rule on equals rule off ---------------------------------


def graph_rows(directory: Path, source: str) -> tuple[list[tuple], list[tuple]]:
    return rows_on_lines(directory, source, range(1, 10_000))


def graphs_with_and_without_the_rule(tmp_path, monkeypatch, source):
    """The rows of *source* with every Ruby rule on, then with every Ruby rule but this one."""
    with_rule = graph_rows(tmp_path / "with_rule", source)
    monkeypatch.setattr(
        ruby, "RULES", tuple(rule for rule in ruby.RULES if rule.__module__ != RULE_MODULE)
    )
    return with_rule, graph_rows(tmp_path / "without_rule", source)


def in_class_method(body: str) -> str:
    """A class whose class method `define` (line 3 on) runs *body*."""
    return (
        "class Account\n  def self.define(code)\n"
        f"    {body}\n  end\n\n  def compute(n)\n  end\nend\n"
    )


def test_code_outside_the_string_is_unchanged(tmp_path, monkeypatch):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, IN_CLASS_METHOD
    )
    outside = lambda rows: [row for row in rows if row[3] not in range(4, 7)]  # noqa: E731

    assert outside(with_rule[0]) == outside(without_rule[0])
    assert outside(with_rule[1]) == outside(without_rule[1])


def test_later_rules_change_code_outside_the_string_once(tmp_path):
    # The twin runs every rule; code outside the string is left to the rules that run later.
    setter_in_compute = "  def compute(n)\n    self.balance = n\n"
    source = IN_CLASS_METHOD.replace("  def compute(n)\n", setter_in_compute)

    _, edges = parse(tmp_path, source)

    assert calls_on_line(edges, 11) == [("Account.compute", "balance=")]


# Shapes crg already reads as code, or that hold no code.
UNCHANGED_CASES = {
    "block_form": "class_eval do\n      def total; compute(1); end\n    end",
    "no_eval": "compute(1)",
    "heredoc_assigned": "text = <<-RUBY\n      def total; compute(1); end\n    RUBY",
}


@pytest.mark.parametrize("name", sorted(UNCHANGED_CASES))
def test_shapes_with_no_code_string_are_unchanged(tmp_path, monkeypatch, name):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, in_class_method(UNCHANGED_CASES[name])
    )

    assert with_rule == without_rule


# Strings that look like code given to class_eval, but are not code given to a *_eval method.
FALSE_POSITIVE_CASES = {
    "eval_of_a_variable": "class_eval(code, __FILE__, __LINE__ + 1)",
    "no_arguments": "class_eval()",
    "heredoc_given_to_another_method": (
        "logger.info <<-RUBY\n      def total; compute(1); end\n    RUBY"
    ),
    "similar_method_name": "class_evaluate <<-RUBY\n      def total; compute(1); end\n    RUBY",
    "heredoc_not_first_argument": (
        "class_eval code, <<-RUBY\n      def total; compute(1); end\n    RUBY"
    ),
}


@pytest.mark.parametrize("name", sorted(FALSE_POSITIVE_CASES))
def test_strings_not_given_as_code_to_an_eval_method_are_unchanged(tmp_path, monkeypatch, name):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, in_class_method(FALSE_POSITIVE_CASES[name])
    )

    assert with_rule == without_rule
