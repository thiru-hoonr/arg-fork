"""Rules for crg's ``ruby`` files."""

from __future__ import annotations

from .._rule import Rule
from .bare_name_calls import store_bare_names_as_calls
from .class_receiver_calls import unlink_class_receiver_calls_from_instance_methods
from .macro_methods import add_methods_made_by_macros
from .setter_assignments import store_setter_assignments_as_setter_calls

RULES: tuple[Rule, ...] = (
    # First: adds the Functions macros make, so every later rule sees the file's methods.
    add_methods_made_by_macros,
    # Adds the call crg would make for `name()` before the rules below read the file's calls.
    store_bare_names_as_calls,
    # Renames the call crg makes for `self.x = v` to `x=` before the next rule reads call names:
    # the setter call has no receiver that is a class, and must not be read as a call to `x`.
    store_setter_assignments_as_setter_calls,
    unlink_class_receiver_calls_from_instance_methods,
)
