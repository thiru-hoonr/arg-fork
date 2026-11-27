"""A Ruby call on ``self`` reaches the method Ruby itself resolved when the app was booted, read
from an optional dump at ``<repo>/.code-review-graph/ruby-runtime.json``.

Origin: crg-fork change to ``inheritance.py`` / ``graph.py`` (branch ``fork/ruby-class-index``).
Many Ruby modules reach a class only at run time, where no static rule can see them::

    def self.include_helpers(scope)
      ActiveSupport.on_load(:action_controller) { include scope::Helpers }   # devise
    end
    class DeviseController < Devise.parent_controller.constantize

Booting the app and asking Ruby (``Module#ancestors``, ``UnboundMethod#source_location``) gives the
answer. The dump lists, per class or module, its ancestors and where each of its methods is
defined. crg 2.4.0 with changes 1-11 left such calls unlinked. Finding SYN-F-637.

No twin: crg has no run-time input; the expected links follow the dump, written by hand.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .test_ruby_call_reaches_the_method_its_class_inherits import linked_target

HELPERS = "module Helpers\n  def work; end\nend\n"
HOST = "class Host < Base\n  def go\n    work\n  end\nend\n"


def with_dump(repo: Path, modules: dict, files: dict[str, str]) -> Path:
    """*repo* with a dump of *modules*, made when the sources were *files*."""
    graph_dir = repo / ".code-review-graph"
    graph_dir.mkdir(exist_ok=True)
    hashes = {name: hashlib.sha256(source.encode()).hexdigest() for name, source in files.items()}
    (graph_dir / "ruby-runtime.json").write_text(json.dumps({"files": hashes, "modules": modules}))
    return repo


HOST_DUMP = {
    "Host": {"ancestors": ["Host", "Base", "Helpers"], "methods": {"work": "helpers.rb:2"}},
    "Helpers": {"ancestors": ["Helpers"], "methods": {"work": "helpers.rb:2"}},
}


def test_call_reaches_the_method_the_dump_names(tmp_path):
    files = {"helpers.rb": HELPERS, "host.rb": HOST}

    assert linked_target(with_dump(tmp_path, HOST_DUMP, files), files) == "helpers.rb::Helpers.work"


def test_without_a_dump_the_call_stays_unlinked(tmp_path):
    files = {"helpers.rb": HELPERS, "host.rb": HOST}

    assert linked_target(tmp_path, files) == "work"


def test_a_dump_line_with_no_method_there_links_nothing(tmp_path):
    dump = {"Host": {"ancestors": ["Host"], "methods": {"work": "helpers.rb:9"}}}
    files = {"helpers.rb": HELPERS, "host.rb": HOST}

    assert linked_target(with_dump(tmp_path, dump, files), files) == "work"


def test_the_class_s_own_method_comes_before_the_dump(tmp_path):
    files = {
        "helpers.rb": HELPERS,
        "host.rb": "class Host < Base\n  def go\n    work\n  end\n\n  def work; end\nend\n",
    }

    assert linked_target(with_dump(tmp_path, HOST_DUMP, files), files) == "host.rb::Host.work"


def test_a_nested_class_is_found_by_its_ruby_name(tmp_path):
    dump = {"Shop::Host": {"ancestors": ["Shop::Host"], "methods": {"work": "helpers.rb:2"}}}
    files = {
        "helpers.rb": HELPERS,
        "host.rb": (
            "module Shop\n  class Host < Base\n    def go\n      work\n    end\n  end\nend\n"
        ),
    }

    assert linked_target(with_dump(tmp_path, dump, files), files) == "helpers.rb::Helpers.work"


def test_module_call_reaches_the_one_method_its_runtime_includers_resolve(tmp_path):
    dump = {
        "Mixin": {"kind": "module", "ancestors": ["Mixin"], "methods": {}},
        "A": {"ancestors": ["A", "Mixin", "Base"], "methods": {"work": "helpers.rb:2"}},
        "B": {"ancestors": ["B", "Mixin"], "methods": {"work": "helpers.rb:2"}},
    }
    files = {"helpers.rb": HELPERS, "mixin.rb": "module Mixin\n  def go\n    work\n  end\nend\n"}

    assert linked_target(with_dump(tmp_path, dump, files), files) == "helpers.rb::Helpers.work"


def test_runtime_includers_resolving_different_methods_leave_the_call_unlinked(tmp_path):
    dump = {
        "Mixin": {"kind": "module", "ancestors": ["Mixin"], "methods": {}},
        "A": {"ancestors": ["A", "Mixin"], "methods": {"work": "helpers.rb:2"}},
        "B": {"ancestors": ["B", "Mixin"], "methods": {"work": "other.rb:2"}},
    }
    files = {
        "helpers.rb": HELPERS,
        "other.rb": "module Other\n  def work; end\nend\n",
        "mixin.rb": "module Mixin\n  def go\n    work\n  end\nend\n",
    }

    assert linked_target(with_dump(tmp_path, dump, files), files) == "work"


def test_a_class_is_not_answered_by_its_runtime_subclasses(tmp_path):
    dump = {
        "Base": {"kind": "class", "ancestors": ["Base"], "methods": {}},
        "Child": {"ancestors": ["Child", "Base"], "methods": {"work": "helpers.rb:2"}},
    }
    files = {"helpers.rb": HELPERS, "base.rb": "class Base\n  def go\n    work\n  end\nend\n"}

    assert linked_target(with_dump(tmp_path, dump, files), files) == "work"


def test_a_call_on_an_object_is_not_answered_by_the_dump(tmp_path):
    files = {
        "helpers.rb": HELPERS,
        "host.rb": "class Host < Base\n  def go(item)\n    item.work\n  end\nend\n",
    }

    assert linked_target(with_dump(tmp_path, HOST_DUMP, files), files) == "work"


def test_super_is_not_answered_by_the_dump(tmp_path):
    dump = {"Host": {"ancestors": ["Host", "Helpers"], "methods": {"go": "helpers.rb:2"}}}
    files = {
        "helpers.rb": "module Helpers\n  def go; end\nend\n",
        "host.rb": "class Host < Base\n  def go\n    super\n  end\nend\n",
    }

    assert linked_target(with_dump(tmp_path, dump, files), files) == "go"


def test_a_dump_that_is_not_json_is_ignored_with_a_warning(tmp_path, caplog):
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    (graph_dir / "ruby-runtime.json").write_text("{not json")
    files = {"helpers.rb": HELPERS, "host.rb": HOST}

    assert linked_target(tmp_path, files) == "work"
    assert "ignoring Ruby runtime dump" in caplog.text


def test_a_dump_that_is_not_an_object_is_ignored_with_a_warning(tmp_path, caplog):
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    (graph_dir / "ruby-runtime.json").write_text('["Host"]')
    files = {"helpers.rb": HELPERS, "host.rb": HOST}

    assert linked_target(tmp_path, files) == "work"
    assert "ignoring Ruby runtime dump" in caplog.text


def test_a_method_in_a_file_changed_since_the_dump_is_not_used(tmp_path):
    files = {"helpers.rb": HELPERS, "host.rb": HOST}
    with_dump(tmp_path, HOST_DUMP, {**files, "helpers.rb": "# before an edit\n" + HELPERS})

    assert linked_target(tmp_path, files) == "work"


def test_a_method_in_a_file_the_dump_has_no_hash_for_is_not_used(tmp_path):
    files = {"helpers.rb": HELPERS, "host.rb": HOST}
    with_dump(tmp_path, HOST_DUMP, {"host.rb": HOST})

    assert linked_target(tmp_path, files) == "work"


def test_a_dump_without_file_hashes_is_ignored_with_a_warning(tmp_path, caplog):
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    (graph_dir / "ruby-runtime.json").write_text(json.dumps({"modules": HOST_DUMP}))
    files = {"helpers.rb": HELPERS, "host.rb": HOST}

    assert linked_target(tmp_path, files) == "work"
    assert "ignoring Ruby runtime dump" in caplog.text
