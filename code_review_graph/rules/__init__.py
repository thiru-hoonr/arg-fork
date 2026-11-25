"""Post-parse rules of this fork: each corrects one known extraction error in a parsed file.

:meth:`CodeParser.parse_bytes` calls :func:`apply` once per file, before the file is stored.
Rules rely on these crg internals; check each when rebasing the fork onto a new crg release:
``CodeParser._get_parser``, ``_node_qualified``, and how ``_extract_bytes`` makes TESTED_BY
edges.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from . import javascript

if TYPE_CHECKING:
    from ..parser import CodeParser, EdgeInfo, NodeInfo

logger = logging.getLogger(__name__)

# crg's names for the languages the JavaScript rules apply to.
_JAVASCRIPT_FAMILY = frozenset({"javascript", "typescript", "tsx"})


def apply(
    parser: CodeParser,
    path: Path,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Run the JavaScript rules on a JavaScript-family file; a failing rule changes nothing."""
    if language not in _JAVASCRIPT_FAMILY:
        return nodes, edges
    for rule in javascript.RULES:
        try:
            nodes, edges = rule(parser, language, source, nodes, edges)
        except Exception:  # a correction must never cost the file its parse
            logger.warning("fork rule %s failed on %s", rule.__name__, path, exc_info=True)
    return nodes, edges
