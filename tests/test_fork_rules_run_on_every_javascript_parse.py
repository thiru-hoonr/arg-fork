"""The fork's rules run on every JavaScript-family parse, and a failing rule costs nothing.

Origin: the fork's rules package (crg-fork, branch ``fork/rules``). The witness in every test is
the callback-parameter rule: crg stores ``id => …`` as a Function ``id``, and the rule removes it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

import pytest

from code_review_graph import rules
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.rules import javascript
from code_review_graph.tools.query import query_graph

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


def functions(nodes: list[NodeInfo]) -> set[str]:
    return {node.name for node in nodes if node.kind == "Function"}


def graph_store(repo: Path) -> GraphStore:
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    return GraphStore(graph_dir / "graph.db")


def callers_of(repo: Path, file: str, name: str) -> set[str]:
    result = query_graph("callers_of", f"{(repo / file).as_posix()}::{name}", repo_root=str(repo))
    assert result["status"] == "ok"
    return {node["qualified_name"].split("::", 1)[1] for node in result["results"]}


def test_incremental_update_applies_the_rules(tmp_path):
    (tmp_path / "users.js").write_text("function loadUsers() {}\n")
    with graph_store(tmp_path) as store:
        full_build(tmp_path, store)
        (tmp_path / "users.js").write_text(dedent(LOAD_USERS))
        incremental_update(tmp_path, store, changed_files=["users.js"])

    assert callers_of(tmp_path, "users.js", "fetchUser") == {"loadUsers"}


def test_a_failing_rule_keeps_crgs_output_and_logs_a_warning(tmp_path, monkeypatch, caplog):
    def failing_rule(*_args):
        raise ValueError("broken rule")

    monkeypatch.setattr(javascript, "RULES", (failing_rule,))
    with caplog.at_level(logging.WARNING, logger=rules.__name__):
        nodes, _ = parse(tmp_path, "users.js", LOAD_USERS)

    assert "id" in functions(nodes)
    assert "failing_rule" in caplog.text


JAVASCRIPT_FAMILY_EXTENSIONS = [".js", ".jsx", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx"]


@pytest.mark.parametrize("extension", JAVASCRIPT_FAMILY_EXTENSIONS)
def test_every_javascript_family_file_type_runs_the_rules(tmp_path, extension):
    nodes, _ = parse(tmp_path, f"users{extension}", LOAD_USERS)

    assert functions(nodes) == {"fetchUser", "loadUsers"}
