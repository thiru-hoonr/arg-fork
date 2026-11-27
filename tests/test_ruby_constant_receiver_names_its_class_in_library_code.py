"""A Ruby call whose receiver the file names as a class (``Devise.paranoid``, ``Shop::Cart.new``,
``cart.total`` after ``cart = Cart.new``) carries that class in library code, as it does in tests.

Origin: fork rule ``constant-receivers`` (crg-fork, branch ``fork/ruby-class-index``); crg 2.4.0
stamps a Ruby call with the class its receiver names only in a test file, so in library code
``Devise.paranoid`` stays a bare ``paranoid`` that crg's resolver cannot follow to
``lib/devise.rb``. On devise, 117 real calls were missed this way. Finding SYN-F-633.

The twin is the same source in a test file (``subject_test.rb``), where crg stamps it. Every
expected value here is crg's stamp for the twin; a receiver the file says nothing about
(``item.total``), which crg stamps ``unknown`` in a test, is left as crg makes it in library code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, EdgeInfo
from code_review_graph.rules import ruby
from code_review_graph.rules.ruby.constant_receivers import stamp_constant_receivers

RULE_NAME = "constant-receivers"
RECEIVER_KEYS = ("receiver_binding", "receiver_class", "receiver_constant", "receiver_scopes")


def parse(directory: Path, name: str, source: str) -> list[EdgeInfo]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(source)
    return CodeParser(directory).parse_file(path)[1]


def class_stamped_calls(edges: list[EdgeInfo]) -> list[tuple]:
    """Each call stamped with a class: its line, its target and its receiver keys."""
    return sorted(
        (e.line, e.target, tuple((k, str(e.extra.get(k))) for k in RECEIVER_KEYS))
        for e in edges
        if e.kind == "CALLS" and e.extra.get("receiver_binding") == "class"
    )


def plain_name(qualified: str) -> str:
    return qualified.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def other_calls(edges: list[EdgeInfo], stamped: set[tuple[int, str]]) -> list[tuple]:
    """Every call but the *stamped* ones (line, plain name), as crg wrote it."""
    return sorted(
        (e.line, e.target.rsplit("::", 1)[-1], e.source.rsplit("::", 1)[-1],
         tuple(sorted((k, str(v)) for k, v in e.extra.items())))
        for e in edges
        if e.kind == "CALLS" and (e.line, plain_name(e.target)) not in stamped
    )


def without_this_rule(monkeypatch) -> None:
    monkeypatch.setattr(
        ruby, "RULES", tuple(rule for rule in ruby.RULES if rule is not stamp_constant_receivers),
    )


SHOP = (
    "module Shop\n  class Cart\n    def total; end\n    def self.build; end\n  end\n\n"
    "  class Till\n    def ring(item)\n{call}\n    end\n  end\nend\n"
)

# ---- positive: each call next to its twin, the same source in a test file ----------------------

CALLS = {
    "constant receiver": "      Cart.build",
    "scoped constant receiver": "      Shop::Cart.build",
    "top-level scoped constant receiver": "      ::Shop::Cart.build",
    "constructor": "      Shop::Cart.new",
    "call on what a constructor returns": "      Cart.new.total",
    "local assigned from a constructor": "      cart = Cart.new\n      cart.total",
    "constant receiver of a method in the same file": "      Cart.total",
    "attribute assignment on a constant": "      Cart.limit = 3",
    "attribute assignment that reads first": "      Cart.limit += 3",
    "two calls of one name on one line": "      Cart.total + item.total",
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_call_gets_the_class_stamp_of_its_twin(tmp_path, name):
    source = SHOP.format(call=CALLS[name])

    in_library = class_stamped_calls(parse(tmp_path / "lib", "till.rb", source))
    in_test = class_stamped_calls(parse(tmp_path / "test", "till_test.rb", source))

    assert in_library == in_test
    assert in_library, "the twin stamps at least one call"


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_other_call_is_left_as_crg_makes_it(tmp_path, monkeypatch, name):
    source = SHOP.format(call=CALLS[name])
    with_rule = parse(tmp_path / "on", "till.rb", source)
    # a later rule may rename a stamped call (`limit` -> `limit=`) and tag it with its own name
    stamped = {
        (e.line, plain_name(e.target).rstrip("=")) for e in with_rule
        if e.extra.get("receiver_binding") == "class"
    }
    stamped |= {(line, f"{name}=") for line, name in stamped}
    without_this_rule(monkeypatch)

    without_rule = parse(tmp_path / "off", "till.rb", source)

    assert other_calls(with_rule, stamped) == other_calls(without_rule, stamped)


def test_one_line_method_calling_its_own_name_on_a_constant_gets_its_twins_stamp(tmp_path):
    # The method's CONTAINS edge and the call share a line and a name; only the call is stamped.
    source = "class Till\n  def build; Cart.build; end\nend\n"

    with_rule = parse(tmp_path / "lib", "till.rb", source)

    assert class_stamped_calls(with_rule) == class_stamped_calls(
        parse(tmp_path / "test", "till_test.rb", source)
    )
    assert [e.kind for e in with_rule if e.extra.get("fork_rule") == RULE_NAME] == ["CALLS"]


# ---- unchanged and false positive: receivers the file does not name as a class ----------------

UNNAMED = {
    "argument": "      item.total",
    "instance variable": "      @cart.total",
    "result of a bare call": "      resource_class.new",
    "self": "      self.ring(1)",
    "no receiver": "      ring(1)",
    "local assigned from something else": "      cart = find_cart\n      cart.total",
}


@pytest.mark.parametrize("name", sorted(UNNAMED))
def test_receiver_the_file_does_not_name_is_unchanged(tmp_path, monkeypatch, name):
    source = SHOP.format(call=UNNAMED[name])
    with_rule = parse(tmp_path / "on", "till.rb", source)
    without_this_rule(monkeypatch)

    without_rule = parse(tmp_path / "off", "till.rb", source)

    assert other_calls(with_rule, set()) == other_calls(without_rule, set())


@pytest.mark.parametrize("call", [
    "      Cart.build\n      item.total",
    # crg stamps the first `total` and not the second; the rule must not move the stamp along
    "      Cart.new.total + total(1)",
])
def test_test_file_is_unchanged(tmp_path, monkeypatch, call):
    source = SHOP.format(call=call)
    with_rule = parse(tmp_path / "on", "till_test.rb", source)
    without_this_rule(monkeypatch)

    without_rule = parse(tmp_path / "off", "till_test.rb", source)

    assert other_calls(with_rule, set()) == other_calls(without_rule, set())


# ---- tag and contract ---------------------------------------------------------------------------

def test_stamped_call_names_the_rule(tmp_path):
    edges = parse(tmp_path, "till.rb", SHOP.format(call="      Cart.build\n      item.total"))

    tagged = [(e.line, e.target) for e in edges if e.extra.get("fork_rule") == RULE_NAME]
    assert tagged == [(9, "build")]


def test_rule_leaves_its_inputs_unchanged(tmp_path):
    path = tmp_path / "till.rb"
    path.write_text(SHOP.format(call="      Cart.build"))
    parser = CodeParser(tmp_path)
    nodes, edges = parser._extract_bytes(path, path.read_bytes())
    nodes_before, edges_before = [repr(n) for n in nodes], [repr(e) for e in edges]

    stamp_constant_receivers(parser, "ruby", path.read_bytes(), nodes, edges)

    assert [repr(n) for n in nodes] == nodes_before
    assert [repr(e) for e in edges] == edges_before


def linked_target(repo: Path, files: dict[str, str], caller: str = "message") -> str:
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


LOCKABLE = "class Lockable\n  def message\n    Devise.paranoid\n  end\nend\n"


def test_full_build_links_a_constant_receiver_to_the_class_method_of_another_file(tmp_path):
    files = {
        "devise.rb": "module Devise\n  def self.paranoid\n  end\nend\n",
        "other.rb": "module Other\n  def self.paranoid\n  end\nend\n",
        "lockable.rb": LOCKABLE,
    }

    assert linked_target(tmp_path, files) == "devise.rb::Devise.paranoid"
