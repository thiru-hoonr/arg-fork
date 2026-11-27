"""Rules for crg's ``ruby`` files."""

from __future__ import annotations

from .._rule import Rule
from .bare_name_calls import store_bare_names_as_calls
from .class_parents import add_class_parents
from .class_receiver_calls import unlink_class_receiver_calls_from_instance_methods
from .constant_receivers import stamp_constant_receivers
from .eval_strings import add_code_from_eval_strings
from .macro_methods import add_methods_made_by_macros
from .self_receivers import mark_calls_on_self
from .setter_assignments import store_setter_assignments_as_setter_calls
from .setter_definitions import add_setter_definitions
from .super_calls import add_super_calls

RULES: tuple[Rule, ...] = (
    # First, as crg stamps a test file's receivers while it extracts: every later rule reads a
    # library call on a class (`Devise.paranoid`) the way it reads the same call in a test.
    stamp_constant_receivers,
    # Adds the Functions macros make, so every later rule sees the file's methods.
    add_methods_made_by_macros,
    # Runs crg, these rules included, on the code in class_eval strings, and adds what it finds
    # on the strings' lines; the rules below then read the file with that code in it.
    add_code_from_eval_strings,
    # Adds the call crg would make for `name()` before the rules below read the file's calls.
    store_bare_names_as_calls,
    # Renames the call crg makes for `self.x = v` to `x=` before the rules below read call names:
    # the setter call has no receiver that is a class, and must not be read as a call to `x`.
    store_setter_assignments_as_setter_calls,
    # Adds the Function for `def x=` and links the `x=` calls of the rule above to it, so the next
    # rule can unlink a call on a class (`A.x = v`) that crg's linking gave an instance setter.
    add_setter_definitions,
    add_class_parents,
    unlink_class_receiver_calls_from_instance_methods,
    mark_calls_on_self,
    # Last: its calls already carry their receiver (`super`), which no rule above should read.
    add_super_calls,
)
