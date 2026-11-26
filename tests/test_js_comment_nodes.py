"""Every JavaScript / TypeScript comment is a ``Comment`` node (SYN-F-616).

Consecutive own-line ``//`` lines are one comment. A comment's full text is kept, with no length
cap.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph import parser as parser_module
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
