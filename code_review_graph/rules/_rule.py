"""The signature every fork rule has."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # parser.py imports the rules package; its classes are needed for typing only
    from ..parser import CodeParser, EdgeInfo, NodeInfo

Rule = Callable[
    ["CodeParser", str, bytes, "list[NodeInfo]", "list[EdgeInfo]"],
    "tuple[list[NodeInfo], list[EdgeInfo]]",
]
