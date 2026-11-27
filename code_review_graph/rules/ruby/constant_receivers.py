"""Stamp a Ruby call in library code with the class its receiver names, as crg does in a test.

crg 2.4.0 reads which class a Ruby call is made on (``_collect_ruby_test_receivers``) only in a
test file. In library code the call keeps no receiver, so its name alone decides its link
(SYN-F-633)::

    class Lockable
      def message
        Devise.paranoid        # crg 2.4.0: a bare `paranoid`; in a test, `receiver_class: Devise`
      end
    end

crg's resolver follows a call stamped with a class to the method of that class, in any file (a
Ruby constant names its class wherever it is defined), and to no other method of that name.

The twin is the same source in a test file. The rule reads the receivers with crg's own reader
and gives each call whose receiver the file names as a class (a constant, ``X.new``, a local last
assigned from ``X.new``) the stamp crg gives it there. A receiver the file says nothing about,
which crg stamps ``unknown`` in a test, is left alone: in library code that stamp would take away
links that are right today. A call crg already stamped (in a test file) is left as it is.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from ._syntax import changed, last_part

if TYPE_CHECKING:
    from ...parser import CodeParser, EdgeInfo, NodeInfo

RULE_NAME = "constant-receivers"

_BINDING = "receiver_binding"
_CLASS_BINDING = "class"


def stamp_constant_receivers(
    parser: CodeParser,
    language: str,
    source: bytes,
    nodes: list[NodeInfo],
    edges: list[EdgeInfo],
) -> tuple[list[NodeInfo], list[EdgeInfo]]:
    """Give each call on a receiver the file names as a class the stamp crg gives it in a test."""
    root = parser._get_parser(language).parse(source).root_node
    # (line, method) -> one (has a receiver, the class the file names or None) per call, in order;
    # crg names a class only for a call with a receiver
    receivers = parser._collect_ruby_test_receivers(root)
    pending = {place: iter(calls) for place, calls in receivers.items()}
    stamped = []
    for edge in edges:
        if edge.kind == "CALLS":
            method = last_part(edge.target)
            calls = pending.get((edge.line, method))
            # crg pairs a line's calls with its edges in the order it emits them; so does the rule,
            # reading every call, so one crg already stamped keeps its own entry
            _, class_ref = next(calls, (False, None)) if calls else (False, None)
            if class_ref is not None and _BINDING not in edge.extra:
                edge = _stamped(edge, method, class_ref)
        stamped.append(edge)
    return nodes, stamped


def _stamped(edge: EdgeInfo, method: str, class_ref: tuple[str, tuple[str, ...]]) -> EdgeInfo:
    """*edge* as crg writes a test call on the class *class_ref*: a bare name with the class."""
    class_path, scopes = class_ref
    stamp = {_BINDING: _CLASS_BINDING, "receiver_class": class_path, "receiver_constant": True}
    if scopes:
        stamp["receiver_scopes"] = list(scopes)
    with_stamp = dataclasses.replace(edge, extra={**edge.extra, **stamp})
    return changed(with_stamp, RULE_NAME, target=method)
