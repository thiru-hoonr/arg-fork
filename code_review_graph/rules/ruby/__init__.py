"""Rules for crg's ``ruby`` files."""

from __future__ import annotations

from .._rule import Rule
from .bare_name_calls import store_bare_names_as_calls
from .class_receiver_calls import unlink_class_receiver_calls_from_instance_methods
from .setter_assignments import store_setter_assignments_as_setter_calls

RULES: tuple[Rule, ...] = (
    store_bare_names_as_calls,  # first: it adds the calls crg would make for `name()`
    # Renames the call crg makes for `self.x = v` to `x=` before the next rule reads call names:
    # the setter call has no receiver that is a class, and must not be read as a call to `x`.
    store_setter_assignments_as_setter_calls,
    unlink_class_receiver_calls_from_instance_methods,
)
