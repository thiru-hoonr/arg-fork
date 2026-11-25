"""A callback written with one bare parameter (``id => …``) is not a Function called ``id``.

Origin: fork rule ``callback-parameters`` (crg-fork, branch ``fork/rules``);
crm-prototype, nestjs-realworld-example-app and vue-element-admin had 129 such Functions.
"""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import javascript
from code_review_graph.tools.query import query_graph

RULE_NAME = "callback-parameters"
FILE = "<file>"

LOAD_USERS = """\
    function fetchUser(id) {
      return db.find(id);
    }

    function loadUsers() {
      return ids.map(id => fetchUser(id));
    }
"""


def parse(directory: Path, name: str, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    path = directory / name
    path.write_text(dedent(source))
    return CodeParser(directory).parse_file(path)


def short(qualified_name: str) -> str:
    if "::" in qualified_name:
        return qualified_name.split("::", 1)[1]
    return FILE if Path(qualified_name).is_absolute() else qualified_name


def functions(nodes: list[NodeInfo]) -> set[str]:
    return {node.name for node in nodes if node.kind == "Function"}


def edges_of(kind: str, edges: list[EdgeInfo]) -> set[tuple[str, str]]:
    return {(short(edge.source), short(edge.target)) for edge in edges if edge.kind == kind}


def graph_store(repo: Path) -> GraphStore:
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    return GraphStore(graph_dir / "graph.db")


def callers_of(repo: Path, file: str, name: str) -> set[str]:
    result = query_graph("callers_of", f"{(repo / file).as_posix()}::{name}", repo_root=str(repo))
    assert result["status"] == "ok"
    return {short(node["qualified_name"]) for node in result["results"]}


def test_callback_parameter_is_not_stored_as_a_function(tmp_path):
    nodes, _ = parse(tmp_path, "users.js", LOAD_USERS)

    assert functions(nodes) == {"fetchUser", "loadUsers"}


def test_call_inside_a_callback_belongs_to_the_enclosing_function(tmp_path):
    _, edges = parse(tmp_path, "users.js", LOAD_USERS)

    assert edges_of("CALLS", edges) == {
        ("fetchUser", "find"),
        ("loadUsers", "map"),
        ("loadUsers", "fetchUser"),
    }


def test_function_named_like_a_callback_parameter_keeps_its_callers(tmp_path):
    (tmp_path / "users.js").write_text(dedent("""\
        function id(x) {
          return x;
        }

        function loadUsers() {
          return ids.map(id => fetchUser(id));
        }

        function other() {
          return id(5);
        }
    """))
    with graph_store(tmp_path) as store:
        full_build(tmp_path, store)

    assert callers_of(tmp_path, "users.js", "id") == {"other"}


def test_real_function_on_the_same_line_as_a_callback_with_its_name_is_kept(tmp_path):
    nodes, edges = parse(tmp_path, "x.js", """\
        function x() { return xs.map(x => f(x)); }
        function g() { return x(); }
    """)

    assert [(n.name, n.params) for n in nodes if n.kind == "Function"] == [("x", "()"), ("g", "()")]
    assert (FILE, "x") in edges_of("CONTAINS", edges)
    assert ("x", "f") in edges_of("CALLS", edges)


def test_owner_is_found_from_the_syntax_tree_when_functions_share_a_line(tmp_path):
    _, edges = parse(tmp_path, "pair.js", """\
        const b = () => 1, a = () => xs.map(x => f(x));
    """)

    assert ("a", "f") in edges_of("CALLS", edges)
    assert ("b", "f") not in edges_of("CALLS", edges)


# Functions sharing one line, each with an ``id =>`` callback: crg gives the callbacks one name.
ONE_LINE_CASES = {
    "call": (
        "function a() { xs.map(id => f(id)); } function b() { ys.map(id => g(id)); }",
        {("a", "f"), ("b", "g")},
    ),
    "method_call": (
        "function b() { ys.map(id => api.g(id)); } function a() { xs.map(id => api.f(id)); }",
        {("a", "f"), ("b", "g")},
    ),
    "function_defined_in_the_file": (
        "function f() {} function b() { y.map(id => g(id)); } function a() { x.map(id => f(id)); }",
        {("a", "f"), ("b", "g")},
    ),
    "method_defined_in_the_class": (
        "class A { b() { ys.map(id => g(id)); } a() { xs.map(id => this.f(id)); } f() {} }",
        {("A.a", "A.f"), ("A.b", "g")},
    ),
    "jsx_element": (
        "function b() { ys.map(id => g(id)); } function a() { xs.map(id => <F></F>); }",
        {("a", "F"), ("b", "g")},
    ),
    "jsx_self_closing_element": (
        "function b() { ys.map(id => g(id)); } function a() { xs.map(id => <F />); }",
        {("a", "F"), ("b", "g")},
    ),
    "unnamed_inner_function": (
        "function b() { ys.map(id => g(id)); } function a() { xs.map(id => xs.map((y) => f(y))); }",
        {("a", "f"), ("b", "g")},
    ),
    "named_inner_function": (
        "function a() { xs.map(id => { function h() { ys.map(id => f(id)); } }); }",
        {("h", "f")},
    ),
}


@pytest.mark.parametrize("case", sorted(ONE_LINE_CASES))
def test_same_name_callbacks_on_one_line_give_each_call_to_its_own_function(tmp_path, case):
    source, expected = ONE_LINE_CASES[case]
    _, edges = parse(tmp_path, "pair.jsx", source)

    callees = {callee for _, callee in expected}
    assert {call for call in edges_of("CALLS", edges) if call[1] in callees} == expected


def test_same_name_callbacks_calling_the_same_function_on_one_line_keep_both_callers(tmp_path):
    _, edges = parse(tmp_path, "pair.js", """\
        function a() { return xs.map(id => f(id)); } function b() { return ys.map(id => f(id)); }
    """)

    assert {("a", "f"), ("b", "f")} <= edges_of("CALLS", edges)


def test_call_in_a_top_level_callback_belongs_to_the_file(tmp_path):
    nodes, edges = parse(tmp_path, "top.js", """\
        xs.forEach(x => top(x));
    """)

    assert functions(nodes) == set()
    assert edges_of("CALLS", edges) == {(FILE, "forEach"), (FILE, "top")}


def test_nested_callbacks_give_their_calls_to_the_outer_function(tmp_path):
    nodes, edges = parse(tmp_path, "nested.js", """\
        function f() {
          return xs.map(x => ys.map(y => g(x, y)));
        }
    """)

    assert functions(nodes) == {"f"}
    assert ("f", "g") in edges_of("CALLS", edges)


def test_call_in_a_callback_inside_a_test_is_tested_by_that_test(tmp_path):
    _, edges = parse(tmp_path, "users.test.js", """\
        describe('users', () => {
          it('checks each id', () => {
            ids.forEach(id => check(id));
          });
        });
    """)

    assert ("check", "it:checks each id@L2") in edges_of("TESTED_BY", edges)


def test_decorator_callbacks_are_not_functions(tmp_path):
    nodes, _ = parse(tmp_path, "article.ts", """\
        class Article {
          @ManyToOne(type => User, user => user.articles)
          author: User;
        }
    """)

    assert functions(nodes) == set()


def test_arrow_named_by_its_variable_is_kept_even_when_named_like_its_parameter(tmp_path):
    nodes, edges = parse(tmp_path, "named.js", """\
        const double = x => x * 2;
        const id = id => lookup(id);
    """)

    assert functions(nodes) == {"double", "id"}
    assert ("id", "lookup") in edges_of("CALLS", edges)


def test_edges_the_rule_changes_name_the_rule(tmp_path):
    _, edges = parse(tmp_path, "load.js", """\
        function load() {
          ids.map(id => f(id));
          return id(1);
        }
    """)

    changed = [edge for edge in edges if edge.extra.get("fork_rule") == RULE_NAME]
    assert edges_of("CALLS", changed) == {("load", "f"), ("load", "id")}


# Each source uses bare parameters; its twin has the listed parameters in parentheses.
TWIN_CASES = {
    "typed.ts": (["id"], """\
        function f(ids: number[]) {
          return ids.map(id => g(id));
        }
    """),
    "component.tsx": (["item"], """\
        export function List() {
          return items.map(item => <Row key={item.id} />);
        }
    """),
    "multiline.js": (["id"], """\
        function f() {
          return ids.map(id =>
            g(id)
          );
        }
    """),
    "block_body.js": (["id"], """\
        function f() {
          ids.forEach(id => {
            save(id);
          });
        }
    """),
    "async.js": (["id"], """\
        function f() {
          return ids.map(async id => load(id));
        }
    """),
    "generator.js": (["x", "y"], """\
        function* ids() {
          yield xs.map(x => g(x));
        }

        const more = function* () {
          yield ys.map(y => h(y));
        };
    """),
    "method.ts": (["x"], """\
        class A {
          m() {
            return xs.map(x => baz(x));
          }
        }
    """),
    "calls_its_parameter.js": (["resolve"], """\
        function load() {
          return new Promise(resolve => {
            resolve(1);
          });
        }
    """),
    "class_field.ts": (["y"], """\
        class A {
          q = ys => ys.map(y => qux(y));
        }
    """),
    "class_field.js": (["ys", "y"], """\
        class A {
          q = ys => ys.map(y => qux(y));
        }
    """),
    "references.js": (["id"], """\
        function onA() {}
        function onB() {}

        function a() {
          return xs.map(id => run(onA));
        }

        function b() {
          return ys.map(id => run(onB));
        }
    """),
    "exported_function.js": (["x"], """\
        module.exports.load = function () {
          return xs.map(x => g(x));
        };
    """),
    "named_like_a_function.js": (["id"], """\
        function id(x) {
          return x;
        }

        function load() {
          return id(ids.map(id => f(id)));
        }
    """),
    "nested_helper.js": (["id"], """\
        function onB() {}

        function a() {
          return xs.map(id => {
            function h() {
              return ys.map(id => run(onB));
            }
            return h;
          });
        }
    """),
    "test_named.js": (["id"], """\
        function test_ids() {
          ids.forEach(id => check(id));
        }
    """),
    "typed_component.tsx": (["id"], """\
        export const List: React.FC<{
          ids: string[];
        }> = ({ ids }) => {
          return ids.map(id => <Row key={id} />);
        };
    """),
    "object_property.js": (["config"], """\
        const routes = {
          response: config => build(config),
        };
    """),
    # crg points a test's `sum()` at the Function it made from `sum =>`, and so does TESTED_BY.
    "test_calls_a_name_a_callback_has.test.js": (["sum"], """\
        import { sum } from './math';

        const totals = carts.map(sum => sum.total);

        it('adds numbers', () => {
          expect(sum(1, 2)).toBe(3);
        });
    """),
    "test_calls_a_name_a_callback_has_on_the_same_line.test.js": (["sum"], """\
        it('adds', () => { carts.map(sum => sum.total); sum(1, 2); });
    """),
    "test_function_calls_a_name_a_callback_has.js": (["load"], """\
        const all = ids.map(load => load.id);

        function test_load() {
          load(1);
        }
    """),
    "test_calls_a_real_function_a_callback_is_named_like.test.js": (["sum"], """\
        function sum(a, b) { return a + b; }

        const totals = carts.map(sum => sum.total);

        it('adds numbers', () => {
          expect(sum(1, 2)).toBe(3);
        });
    """),
}


def parenthesise(source: str, parameters: list[str]) -> str:
    for parameter in parameters:
        source = re.sub(rf"\b{parameter} =>", f"({parameter}) =>", source)
    return source


def graph_rows(directory: Path, name: str, source: str) -> tuple[list[tuple], list[tuple]]:
    directory.mkdir()
    nodes, edges = parse(directory, name, source)
    root = f"{directory.as_posix()}/"
    node_rows = sorted(
        (
            n.kind, n.name.replace(root, ""), n.line_start, n.line_end,
            n.parent_name, n.params, n.return_type, n.is_test, repr(sorted(n.extra.items())),
        )
        for n in nodes
    )
    edge_rows = sorted(
        (e.kind, e.source.replace(root, ""), e.target.replace(root, ""), e.line) for e in edges
    )
    return node_rows, edge_rows


@pytest.mark.parametrize("name", sorted(TWIN_CASES))
def test_bare_parameter_callback_gives_the_same_graph_as_its_parenthesised_twin(tmp_path, name):
    parameters, source = TWIN_CASES[name]

    bare_nodes, bare_edges = graph_rows(tmp_path / "bare", name, source)
    twin_nodes, twin_edges = graph_rows(tmp_path / "twin", name, parenthesise(source, parameters))

    assert bare_nodes == twin_nodes
    assert bare_edges == twin_edges


# Shapes the rule must leave exactly as crg makes them.
UNCHANGED_CASES = {
    # A parent names the arrow, so crg's Function is real.
    "named_by_its_variable.js": "const load = id => fetchUser(id);\n",
    "named_like_its_parameter.js": "const id = id => lookup(id);\n",
    "class_field.ts": "class A {\n  handle = event => go(event);\n}\n",
    "member_assignment.js": "module.exports.load = id => fetchUser(id);\n",
    # Not a bare parameter.
    "parenthesised_parameter.js": "function f() {\n  return ids.map((id) => load(id));\n}\n",
    "two_parameters.js": "function f() {\n  return ids.map((id, i) => load(id, i));\n}\n",
    "destructured_parameter.js": "function f() {\n  return xs.map(({ id }) => load(id));\n}\n",
}


@pytest.mark.parametrize("name", sorted(UNCHANGED_CASES))
def test_shapes_that_are_not_bare_parameter_callbacks_are_unchanged(tmp_path, monkeypatch, name):
    source = UNCHANGED_CASES[name]

    with_rules = graph_rows(tmp_path / "with_rules", name, source)
    monkeypatch.setattr(javascript, "RULES", ())
    without_rules = graph_rows(tmp_path / "without_rules", name, source)

    assert with_rules == without_rules
