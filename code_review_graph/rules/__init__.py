"""Post-parse rules of this fork: each corrects one known extraction error in a parsed file.

:meth:`CodeParser.parse_bytes` calls :func:`apply` once per file, before the file is stored.
Rules rely on these crg internals; check each when rebasing the fork onto a new crg release:
``CodeParser._get_parser`` (the tree-sitter parser crg uses for a language),
``CodeParser._node_qualified`` (the qualified name crg gives a node, the form call targets use),
``CodeParser._extract_bytes`` (crg's extraction of one file's bytes, without the rules),
and the ``unresolved_targets`` key in an edge's ``extra``, which crg's post-build pass leaves alone.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from . import ruby
from ._rule import Rule

if TYPE_CHECKING:
    from ..parser import CodeParser, EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)


def _rules_for(language: str) -> tuple[Rule, ...]:
    """The rules for crg's *language*; read at call time, so a test can swap a package's RULES."""
    if language == "ruby":
        return ruby.RULES
    return ()


def apply(
    parser: CodeParser,
    path: Path,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Run the rules for the file's language; a failing rule changes nothing."""
    for rule in _rules_for(language):
        try:
            nodes, edges = rule(parser, language, source, nodes, edges)
        except Exception:  # a correction must never cost the file its parse
            logger.warning("fork rule %s failed on %s", rule.__name__, path, exc_info=True)
    return nodes, edges
