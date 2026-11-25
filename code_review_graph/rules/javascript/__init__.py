"""Rules for the JavaScript family: crg's ``javascript``, ``typescript`` and ``tsx`` files."""

from __future__ import annotations

from .._rule import Rule
from .callback_parameters import drop_callback_parameter_functions

RULES: tuple[Rule, ...] = (drop_callback_parameter_functions,)
