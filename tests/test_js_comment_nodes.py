"""Every JavaScript / TypeScript comment is a ``Comment`` node (SYN-F-616).

Each comment is contained by its file. An own-line comment also ``DOCUMENTS`` the next code in
the same block, however many blank lines sit between, when that code is a node. Consecutive
own-line ``//`` lines are one comment. A comment's full text is kept, with no length cap.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph import parser as parser_module
from code_review_graph.graph import qualified_name_of
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo


def _parse(tmp_path: Path, name: str, source: str) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    path = tmp_path / name
    path.write_text(dedent(source).lstrip("\n"))
    return CodeParser(tmp_path).parse_file(path)


def _comments(nodes: list[NodeInfo]) -> list[NodeInfo]:
    return [node for node in nodes if node.kind == "Comment"]


def _comment_lines(nodes: list[NodeInfo]) -> list[tuple[int, int]]:
    return sorted((node.line_start, node.line_end) for node in _comments(nodes))


def _comment_on(nodes: list[NodeInfo], line: int) -> NodeInfo:
    starting_on_line = [node for node in _comments(nodes) if node.line_start == line]
    assert len(starting_on_line) == 1, (
        f"expected one comment starting on line {line}, comments found: {_comment_lines(nodes)}"
    )
    return starting_on_line[0]


def _documented(nodes: list[NodeInfo], edges: list[EdgeInfo]) -> set[tuple[int, str]]:
    names = {qualified_name_of(node): node.name for node in nodes}
    comment_lines = {qualified_name_of(node): node.line_start for node in _comments(nodes)}
    return {
        (comment_lines[edge.source], names[edge.target])
        for edge in edges
        if edge.kind == "DOCUMENTS"
    }


@pytest.mark.parametrize("name", ["leads.js", "leads.ts", "leads.tsx"])
@pytest.mark.parametrize(
    "comment", ["// Loads a lead.", "/* Loads a lead. */", "/** Loads a lead. */"]
)
def test_each_comment_style_is_one_comment_node(tmp_path, name, comment):
    nodes, _ = _parse(tmp_path, name, f"{comment}\nconst total = 1;\n")

    assert _comment_lines(nodes) == [(1, 1)]


def test_consecutive_line_comments_are_one_comment_node(tmp_path):
    nodes, _ = _parse(tmp_path, "leads.js", """
        // Loads a lead.
        // Throws when the lead is archived.
        // Cached for a minute.
        const total = 1;
    """)

    assert _comment_lines(nodes) == [(1, 3)]


def test_blank_line_splits_line_comments_into_two_nodes(tmp_path):
    nodes, _ = _parse(tmp_path, "leads.js", """
        // Section: leads.

        // Loads a lead.
        const total = 1;
    """)

    assert _comment_lines(nodes) == [(1, 1), (3, 3)]


def test_end_of_line_comment_is_not_merged_with_the_comment_below(tmp_path):
    nodes, _ = _parse(tmp_path, "leads.js", """
        const total = 1; // running total
        // Loads a lead.
        const load = () => {};
    """)

    assert _comment_lines(nodes) == [(1, 1), (2, 2)]


def test_comment_text_is_kept_whole(tmp_path):
    comment = dedent("""
        /**
         * Loads a lead.
         * @param {string} id - the lead id
         * @returns {Promise<Lead>}
         */
    """).strip()
    nodes, _ = _parse(tmp_path, "leads.js", f"{comment}\nconst load = (id) => {{}};\n")

    assert _comment_on(nodes, 1).extra["text"] == comment


def test_long_comment_text_is_not_cut(tmp_path):
    comment = "// " + "lead " * 200
    nodes, _ = _parse(tmp_path, "leads.js", f"{comment}\nconst total = 1;\n")

    assert _comment_on(nodes, 1).extra["text"] == comment


def test_jsx_comment_is_a_comment_node(tmp_path):
    nodes, _ = _parse(tmp_path, "card.jsx", "const Card = () => <p>{/* lead name */}</p>;\n")

    assert _comment_lines(nodes) == [(1, 1)]


@pytest.mark.parametrize(
    "source",
    ["const Card = () => <p>// lead name</p>;\n", 'const url = "http://example.com/leads";\n'],
    ids=["jsx-text", "string"],
)
def test_comment_marker_outside_a_comment_is_not_a_comment(tmp_path, source):
    nodes, _ = _parse(tmp_path, "card.jsx", source)

    assert _comment_lines(nodes) == []


def test_file_contains_every_comment(tmp_path):
    nodes, edges = _parse(tmp_path, "leads.js", """
        // Leads module.
        const load = () => {
          // Look the lead up.
          return 1; // cached
        };
    """)
    [file] = [node for node in nodes if node.kind == "File"]
    contained = {
        edge.target for edge in edges if edge.kind == "CONTAINS" and edge.source == file.file_path
    }

    contained_comment_lines = sorted(
        comment.line_start
        for comment in _comments(nodes)
        if qualified_name_of(comment) in contained
    )

    assert contained_comment_lines == [1, 3, 4]


@pytest.mark.parametrize(
    ("source", "documented"),
    [
        ("// Loads a lead.\nfunction load() {}\n", (1, "load")),
        ("// Loads a lead.\nconst load = () => {};\n", (1, "load")),
        ("// Loads a lead.\nexport const load = async () => {};\n", (1, "load")),
        ("// Lead store.\nclass Leads {}\n", (1, "Leads")),
        ("class Leads {\n  // Saves a lead.\n  save() {}\n}\n", (2, "save")),
    ],
    ids=["function", "arrow", "exported-arrow", "class", "method"],
)
def test_comment_documents_the_code_below_it(tmp_path, source, documented):
    nodes, edges = _parse(tmp_path, "leads.js", source)

    assert _documented(nodes, edges) == {documented}


@pytest.mark.parametrize(
    ("source", "documented"),
    [
        ("// Lead store.\n@Injectable()\nexport class Leads {}\n", (1, "Leads")),
        ("// Lead store.\n@Injectable()\n@Scope()\nclass Leads {}\n", (1, "Leads")),
        ("class Leads {\n  // Loads a lead.\n  @Get()\n  load() {}\n}\n", (2, "load")),
    ],
    ids=["exported-class", "class-with-two-decorators", "method"],
)
def test_comment_above_decorators_documents_the_decorated_code(tmp_path, source, documented):
    nodes, edges = _parse(tmp_path, "leads.ts", source)

    assert _documented(nodes, edges) == {documented}


def test_comment_documents_the_code_below_it_across_blank_lines(tmp_path):
    nodes, edges = _parse(tmp_path, "leads.js", """
        /** Loads a lead. */



        const load = () => {};
    """)

    assert _documented(nodes, edges) == {(1, "load")}


def test_comment_inside_a_body_documents_the_nested_function_below_it(tmp_path):
    nodes, edges = _parse(tmp_path, "leads.js", """
        const load = () => {
          // Formats the lead.
          function format() {}
        };
    """)

    assert _documented(nodes, edges) == {(2, "format")}


def test_header_comment_above_an_import_documents_nothing(tmp_path):
    nodes, edges = _parse(tmp_path, "leads.js", """
        // Copyright Acme.
        import express from "express";
        const load = () => {};
    """)

    assert _documented(nodes, edges) == set()


def test_comment_above_code_that_is_not_a_node_documents_nothing(tmp_path):
    nodes, edges = _parse(tmp_path, "leads.js", """
        // Retry count.
        const retries = 3;
    """)

    assert _documented(nodes, edges) == set()


def test_last_comment_in_a_block_documents_nothing(tmp_path):
    nodes, edges = _parse(tmp_path, "leads.js", """
        const load = () => {
          return 1;
          // TODO: cache this.
        };
        function save() {}
    """)

    assert _documented(nodes, edges) == set()


def test_end_of_line_comment_documents_nothing(tmp_path):
    nodes, edges = _parse(tmp_path, "leads.js", """
        const total = 1; // running total
        function save() {}
    """)

    assert _documented(nodes, edges) == set()


def test_comment_inside_a_function_records_its_enclosing_function(tmp_path):
    nodes, _ = _parse(tmp_path, "leads.js", """
        const load = () => {
          // Look the lead up.
          return 1;
        };
    """)
    [load] = [node for node in nodes if node.name == "load"]

    assert _comment_on(nodes, 2).extra["enclosing"] == qualified_name_of(load)


def test_top_level_comment_has_no_enclosing_function(tmp_path):
    nodes, _ = _parse(tmp_path, "leads.js", "// Leads module.\nconst total = 1;\n")

    assert "enclosing" not in _comment_on(nodes, 1).extra


def test_other_languages_get_no_comment_nodes(tmp_path):
    nodes, _ = _parse(tmp_path, "leads.py", "# Loads a lead.\ndef load():\n    pass\n")

    assert _comments(nodes) == []


def _fail(*_args: object) -> None:
    raise RuntimeError("comment pass failed")


def test_failing_comment_pass_leaves_the_file_as_crg_parsed_it(tmp_path, monkeypatch):
    source = "// Loads a lead.\nconst load = () => {};\n"
    monkeypatch.setattr(parser_module, "_JS_COMMENT_LANGUAGES", frozenset())
    without_comments = _parse(tmp_path, "leads.js", source)
    monkeypatch.undo()
    monkeypatch.setattr(parser_module, "_js_comment_runs", _fail)

    nodes, edges = _parse(tmp_path, "leads.js", source)

    assert (nodes, edges) == without_comments


def test_failing_comment_pass_logs_a_warning_naming_the_file(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(parser_module, "_js_comment_runs", _fail)

    _parse(tmp_path, "leads.js", "// Loads a lead.\nconst load = () => {};\n")

    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "leads.js" in caplog.records[0].getMessage()
