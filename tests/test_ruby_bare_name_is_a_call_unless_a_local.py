"""A bare Ruby name with no arguments (``resource``) is a method call unless the name is a local
variable at that point, so it must get the call crg gives the same name written ``resource()``.

Origin: fork rule ``bare-name-calls`` (crg-fork, branch ``fork/ruby-bare-calls``); on devise, crg
2.4.0 stored no call for 297 bare-name calls that devise's test suite really makes (TracePoint
answer key). The twin is the same call written with parentheses, ``resource()``, which crg already
extracts: every expected graph here is crg's graph for the twin, built with this rule off.
Finding SYN-F-629.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph import rules
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.bare_name_calls import store_bare_names_as_calls

RULE_NAME = "bare-name-calls"


def parse(directory: Path, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    directory.mkdir()
    path = directory / "subject.rb"
    path.write_text(dedent(source))
    return CodeParser(directory).parse_file(path)


def without_this_rule(monkeypatch) -> None:
    monkeypatch.setattr(
        ruby, "RULES", tuple(rule for rule in ruby.RULES if rule is not store_bare_names_as_calls),
    )


def graph_rows(
    directory: Path, source: str, drop_tag: bool = False,
) -> tuple[list[tuple], list[tuple]]:
    """Every node and edge of *source*, with the directory taken out of names; *drop_tag* leaves
    out this rule's tag, so a rule-made edge can be compared with crg's edge for the twin."""
    nodes, edges = parse(directory, source)
    root = f"{directory.as_posix()}/"

    def extra(item_extra: dict) -> str:
        kept = {
            k: v for k, v in item_extra.items()
            if not (drop_tag and k == "fork_rule" and v == RULE_NAME)
        }
        return repr(sorted(kept.items()))

    node_rows = sorted(
        (n.kind, n.name.replace(root, ""), n.line_start, n.line_end, extra(n.extra)) for n in nodes
    )
    edge_rows = sorted(
        (e.kind, e.source.replace(root, ""), e.target.replace(root, ""), e.line, extra(e.extra))
        for e in edges
    )
    return node_rows, edge_rows


def call_rows(edge_rows: list[tuple]) -> list[tuple]:
    return [row[1:4] for row in edge_rows if row[0] == "CALLS"]


def controller(body: str) -> str:
    """A class whose `build` method (body from line 3) can call `resource` and `helper`."""
    return (
        "class Controller\n  def build\n"
        + "".join(f"    {line}\n" for line in body.splitlines())
        + "  end\n\n  def resource\n  end\n\n  def helper(x = nil)\n  end\nend\n"
    )


# The bare name, and its twin written with parentheses (crg extracts the twin's call).
TWIN_CASES = {
    "statement": ("resource", "resource()"),
    "argument": ("helper(resource)", "helper(resource())"),
    "argument_without_parentheses": ("helper resource", "helper resource()"),
    "receiver": ("resource.save", "resource().save"),
    "condition": ("return if resource", "return if resource()"),
    "ternary": ("resource ? 1 : 2", "resource() ? 1 : 2"),
    "interpolation": ('"name: #{resource}"', '"name: #{resource()}"'),
    "index": ("items[resource]", "items()[resource()]"),
    "name_defined_in_another_file": ("warden", "warden()"),
    "inside_a_block": ("[1].each { |item| resource }", "[1].each { |item| resource() }"),
    "inside_a_do_block": ("[1].each do\n  resource\nend", "[1].each do\n  resource()\nend"),
    "default_value_of_a_parameter": ("->(x = resource) { x }", "->(x = resource()) { x }"),
    "used_before_its_assignment": ("resource\nresource = 1", "resource()\nresource = 1"),
    "block_local_does_not_leak": ("[1].each { |i| r = i }\nr", "[1].each { |i| r = i }\nr()"),
    "receiver_of_an_attribute_assignment": ("resource.name = 1", "resource().name = 1"),
    "two_on_one_line": ("helper(resource, resource)", "helper(resource(), resource())"),
    "after_the_same_call_in_parentheses": ("resource() || resource", "resource() || resource()"),
    "after_the_same_name_on_a_receiver": (
        "@record.resource == resource", "@record.resource == resource()",
    ),
}


@pytest.mark.parametrize("name", sorted(TWIN_CASES))
def test_bare_name_gets_the_call_its_twin_gets(tmp_path, monkeypatch, name):
    bare, twin = TWIN_CASES[name]

    bare_nodes, bare_edges = graph_rows(tmp_path / "bare", controller(bare), drop_tag=True)
    without_this_rule(monkeypatch)
    twin_nodes, twin_edges = graph_rows(tmp_path / "twin", controller(twin))

    assert bare_edges == twin_edges
    assert bare_nodes == twin_nodes


def test_local_of_another_method_does_not_hide_the_call(tmp_path, monkeypatch):
    bare = "class C\n  def a\n    x = 1\n  end\n\n  def b\n    x\n  end\n\n  def x\n  end\nend\n"
    twin = bare.replace("    x\n", "    x()\n")

    _, bare_edges = graph_rows(tmp_path / "bare", bare, drop_tag=True)
    without_this_rule(monkeypatch)
    _, twin_edges = graph_rows(tmp_path / "twin", twin)

    assert bare_edges == twin_edges
    assert ("subject.rb::C.b", "subject.rb::C.x", 7) in call_rows(bare_edges)


def test_one_line_method_calling_itself_gets_the_call_its_twin_gets(tmp_path, monkeypatch):
    bare = "class C\n  def again; again; end\nend\n"

    _, bare_edges = graph_rows(tmp_path / "bare", bare, drop_tag=True)
    without_this_rule(monkeypatch)
    _, twin_edges = graph_rows(tmp_path / "twin", bare.replace("again; end", "again(); end"))

    assert bare_edges == twin_edges


# crg stores a bare name that names a top-level `def` as a REFERENCES edge when it is an argument,
# an array element or a hash value; Ruby calls it, and crg's twin has the call and no reference.
TOP_LEVEL_DEF_CASES = {
    "argument": ("helper(docs)", "helper(docs())"),
    "argument_at_top_level": ("Klass.new(docs, 1)", "Klass.new(docs(), 1)"),
    "array_element": ("list = [docs]", "list = [docs()]"),
    "hash_value": ("h = { k: docs }", "h = { k: docs() }"),
    "beside_a_local_of_the_same_name": (
        "helper(docs, ->(docs) { helper(docs) })", "helper(docs(), ->(docs) { helper(docs) })",
    ),
}


@pytest.mark.parametrize("name", sorted(TOP_LEVEL_DEF_CASES))
def test_bare_name_of_a_top_level_def_gets_the_call_not_a_reference(tmp_path, monkeypatch, name):
    bare, twin = TOP_LEVEL_DEF_CASES[name]

    def source(statement: str) -> str:
        return f"def docs\nend\n\ndef a\n  {statement}\nend\n"

    _, bare_edges = graph_rows(tmp_path / "bare", source(bare), drop_tag=True)
    without_this_rule(monkeypatch)
    _, twin_edges = graph_rows(tmp_path / "twin", source(twin))

    assert bare_edges == twin_edges


def test_bare_name_in_a_class_body_gets_the_call_its_twin_gets(tmp_path, monkeypatch):
    bare = "class C\n  private\n\n  def a\n  end\nend\n"

    _, bare_edges = graph_rows(tmp_path / "bare", bare, drop_tag=True)
    without_this_rule(monkeypatch)
    _, twin_edges = graph_rows(tmp_path / "twin", bare.replace("private", "private()"))

    assert bare_edges == twin_edges


def graphs_with_and_without_the_rule(tmp_path, monkeypatch, source):
    with_rule = graph_rows(tmp_path / "with_rule", source)
    without_this_rule(monkeypatch)
    return with_rule, graph_rows(tmp_path / "without_rule", source)


# Shapes with no bare call: the rule must leave them exactly as crg makes them.
UNCHANGED_CASES = {
    "calls_with_parentheses": controller("helper(1)\nresource()"),
    "calls_on_a_receiver": controller("self.resource\n@items.first"),
    "method_names_and_symbols": controller("helper :resource\nsend(:resource)"),
    "constants_and_instance_variables": controller("Foo::BAR\n@resource"),
    "alias": "class C\n  def a\n  end\n  alias b a\n  alias_method :c, :a\nend\n",
    "one_line_method_calling_itself": "class C\n  def again; again(); end\nend\n",
    "one_line_class_method_calling_itself": "class C\n  def self.again; again(); end\nend\n",
}


@pytest.mark.parametrize("name", sorted(UNCHANGED_CASES))
def test_file_without_bare_calls_is_unchanged(tmp_path, monkeypatch, name):
    with_rule, without_rule = graphs_with_and_without_the_rule(
        tmp_path, monkeypatch, UNCHANGED_CASES[name],
    )

    assert with_rule == without_rule


# A bare name that is a local variable at that point: Ruby reads the variable, no call runs.
LOCAL_CASES = {
    "method_parameter": "def a(resource)\n  resource\nend\n",
    "every_parameter_kind": (
        "def a(x, y = 1, *rest, key:, opt: 2, **opts, &blk)\n"
        "  [x, y, rest, key, opt, opts, blk]\nend\n"
    ),
    "block_parameter": "def a\n  [1].each { |resource| resource }\nend\n",
    "destructured_block_parameter": "def a\n  {}.each { |k, (v, w)| [k, v, w] }\nend\n",
    "block_local_variable": "def a\n  [1].each { |i; resource| resource = i; resource }\nend\n",
    "lambda_parameter": "def a\n  ->(resource) { resource }\nend\n",
    "assigned_earlier": "def a\n  resource = 1\n  resource\nend\n",
    "x_equals_x": "def a\n  resource = resource\nend\n",
    "assigned_in_an_earlier_branch": "def a(c)\n  if c\n    resource = 1\n  end\n  resource\nend\n",
    "operator_assignment": "def a\n  resource ||= 1\n  resource += 1\n  resource\nend\n",
    "multiple_assignment": "def a\n  r, (s, *t) = 1, [2, 3]\n  [r, s, t]\nend\n",
    "modifier_condition_after_assignment": "def a\n  resource = 1 if resource\nend\n",
    "outer_local_seen_in_a_block": "def a\n  resource = 1\n  [1].each { resource }\nend\n",
    "rescue_variable": "def a\n  begin\n  rescue StandardError => e\n    e\n  end\nend\n",
    "for_variable": "def a(list)\n  for item in list\n    item\n  end\nend\n",
    "implicit_it": "def a\n  [1].each { it }\nend\n",
    "numbered_parameter": "def a\n  [1].each { _1 + _2 }\nend\n",
    "pattern_variable": "def a(v)\n  case v\n  in x then x\n  end\nend\n",
    "keyword_pattern_variable": "def a(v)\n  case v\n  in {key: x} then x\n  end\nend\n",
    "pattern_variables": (
        "def a(v)\n  case v\n  in [h, *t] then [h, t]\n  in {k:} then k\n"
        "  in Integer => n then n\n  end\nend\n"
    ),
    "defined": "def a\n  defined?(resource)\nend\n",
    "top_level_local": "resource = 1\nputs(resource)\n",
    "object_of_a_singleton_method": "enc = Object.new\ndef enc.generate(obj) obj end\n",
    "expression_object_of_a_singleton_method": "enc = Object.new\ndef (enc).generate; end\n",
    "superclass": "base = Struct.new(:a)\nclass Child < base\nend\n",
    "object_of_a_singleton_class": "obj = Object.new\nclass << obj\n  def x; end\nend\n",
}


@pytest.mark.parametrize("name", sorted(LOCAL_CASES))
def test_local_variable_is_not_stored_as_a_call(tmp_path, monkeypatch, name):
    source = LOCAL_CASES[name] + "\ndef resource\nend\n"

    with_rule, without_rule = graphs_with_and_without_the_rule(tmp_path, monkeypatch, source)

    assert with_rule == without_rule


def test_rule_tags_only_the_calls_it_adds(tmp_path):
    _, edges = parse(tmp_path / "subject", controller("helper(1)\nresource"))

    tags = sorted((short(e.target), e.extra.get("fork_rule")) for e in edges if e.kind == "CALLS")
    assert tags == [("Controller.helper", None), ("Controller.resource", RULE_NAME)]


def short(qualified_name: str) -> str:
    return qualified_name.split("::", 1)[1] if "::" in qualified_name else qualified_name


def test_rule_does_not_change_its_inputs(tmp_path):
    directory = tmp_path / "subject"
    directory.mkdir()
    path = directory / "subject.rb"
    path.write_text(controller("resource"))
    parser = CodeParser(directory)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())
    nodes_before, edges_before = list(nodes), [(e, dict(e.extra)) for e in edges]

    store_bare_names_as_calls(parser, "ruby", path.read_bytes(), nodes, edges)

    assert nodes == nodes_before
    assert [(e, dict(e.extra)) for e in edges] == edges_before


# Names that never run as calls (method names, locals bound by `for` or a pattern, the keywords
# `__FILE__`, `__LINE__` and `__ENCODING__`, which tree-sitter reads as identifiers) and the
# object of `def obj.name`, which Ruby takes without `()`; `()` after them would not be Ruby.
# Only `resource` may get `()`.
NAME_ONLY_SHAPES = {
    "file_keyword": "class_eval 'def a; end', __FILE__\nresource\n",
    "line_keyword": "class_eval 'def a; end', 'a.rb', __LINE__ + 1\nresource\n",
    "encoding_keyword": "puts __ENCODING__\nresource\n",
    "alias": "alias new_name old_name\nresource\n",
    "undef": "undef old_name\nresource\n",
    "setter_method": "def name=(value)\n  resource\nend\n",
    "method_name": "def name\n  resource\nend\n",
    "class_method_name": "def self.name\n  resource\nend\n",
    "for_variable": "def a(list)\n  for item in list\n    resource\n  end\nend\n",
    "pattern_variable": "def a(v)\n  case v\n  in x then resource\n  end\nend\n",
    "object_of_a_singleton_method": "def scope.name; 'World!'; end\nresource\n",
}


@pytest.mark.parametrize("name", sorted(NAME_ONLY_SHAPES))
def test_twin_the_rule_writes_is_valid_ruby(tmp_path, monkeypatch, name):
    directory = tmp_path / "subject"
    directory.mkdir()
    path = directory / "subject.rb"
    path.write_text(NAME_ONLY_SHAPES[name])
    parser = CodeParser(directory)
    twins: list[bytes] = []
    extract = parser._extract_bytes
    monkeypatch.setattr(
        parser, "_extract_bytes", lambda p, source: twins.append(source) or extract(p, source),
    )

    store_bare_names_as_calls(parser, "ruby", path.read_bytes(), *extract(path, path.read_bytes()))

    assert twins, "the rule wrote no twin"
    assert twins[-1].count(b"()") == 1, twins[-1].decode()
    assert not parser._get_parser("ruby").parse(twins[-1]).root_node.has_error


def test_file_without_bare_calls_is_not_extracted_twice(tmp_path, monkeypatch):
    directory = tmp_path / "subject"
    directory.mkdir()
    path = directory / "subject.rb"
    path.write_text(UNCHANGED_CASES["calls_with_parentheses"])
    parser = CodeParser(directory)
    extract = parser._extract_bytes
    nodes, edges = extract(path, path.read_bytes())
    extracted: list[bytes] = []
    monkeypatch.setattr(
        parser, "_extract_bytes", lambda p, source: extracted.append(source) or extract(p, source),
    )

    store_bare_names_as_calls(parser, "ruby", path.read_bytes(), nodes, edges)

    assert extracted == []


def test_bare_call_in_a_test_gets_the_tested_by_edge_its_twin_gets(tmp_path, monkeypatch):
    test_file = (
        "require 'minitest/autorun'\n\nclass ResourceTest < Minitest::Test\n"
        "  def test_resource\n    assert resource\n  end\nend\n"
    )

    def rows(directory: Path, source: str) -> list[tuple]:
        directory.mkdir()
        path = directory / "resource_test.rb"
        path.write_text(source)
        _, edges = CodeParser(directory).parse_file(path)
        root = f"{directory.as_posix()}/"
        return sorted(
            (e.kind, e.source.replace(root, ""), e.target.replace(root, ""), e.line)
            for e in edges if e.kind == "TESTED_BY"
        )

    bare = rows(tmp_path / "bare", test_file)
    without_this_rule(monkeypatch)
    twin = rows(tmp_path / "twin", test_file.replace("assert resource", "assert resource()"))

    assert twin != []
    assert bare == twin


def test_bare_call_in_a_test_after_the_same_name_on_a_receiver_gets_its_twins_edges(
    tmp_path, monkeypatch,
):
    spec = (
        "RSpec.describe Report do\n  let(:text) { 'a' }\n\n"
        "  it 'keeps the text' do\n    expect(report.text).to eq text\n  end\nend\n"
    )
    twin = spec.replace("eq text", "eq text()").replace("report.text", "report().text")

    def rows(directory: Path, source: str) -> list[tuple]:
        directory.mkdir()
        path = directory / "report_spec.rb"
        path.write_text(source)
        _, edges = CodeParser(directory).parse_file(path)
        root = f"{directory.as_posix()}/"
        return sorted(
            (e.kind, e.source.replace(root, ""), e.target.replace(root, ""), e.line,
             repr(sorted((k, v) for k, v in e.extra.items() if v != RULE_NAME)))
            for e in edges
        )

    bare = rows(tmp_path / "bare", spec)
    without_this_rule(monkeypatch)

    assert bare == rows(tmp_path / "twin", twin)


def test_file_ruby_cannot_parse_is_left_as_crg_made_it(tmp_path, monkeypatch):
    erb_template = (
        "class Add<%= name %>\n  def up\n<%= migration_data -%>\n    resource\n  end\nend\n"
    )

    with_rule, without_rule = graphs_with_and_without_the_rule(tmp_path, monkeypatch, erb_template)

    assert with_rule == without_rule


def parsed_without_the_file_node(tmp_path: Path, source: str):
    """crg's parse of *source*, minus the File node the rule takes the file's path from."""
    directory = tmp_path / "subject"
    directory.mkdir()
    path = directory / "subject.rb"
    path.write_text(source)
    parser = CodeParser(directory)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())
    return parser, path, [node for node in nodes if node.kind != "File"], edges


def test_missing_file_node_leaves_the_file_as_crg_made_it_and_says_why(tmp_path, caplog):
    parser, path, nodes, edges = parsed_without_the_file_node(tmp_path, controller("resource"))

    with caplog.at_level("WARNING"):
        result = rules.apply(parser, path, "ruby", path.read_bytes(), nodes, edges)

    assert result == (nodes, edges)
    assert "store_bare_names_as_calls" in caplog.text
    assert "no File node" in caplog.text


def test_file_ruby_cannot_parse_is_skipped_before_its_path_is_looked_up(tmp_path):
    erb_template = "class Add<%= name %>\n  def up\n    resource\n  end\nend\n"
    parser, path, nodes, edges = parsed_without_the_file_node(tmp_path, erb_template)

    result = store_bare_names_as_calls(parser, "ruby", path.read_bytes(), nodes, edges)

    assert result == (nodes, edges)
