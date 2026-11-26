# class-receiver-calls

**What crg does wrong:** crg 2.4.0 links `receiver.name` to the same file's `def name` whatever
the receiver is. On devise it links `resource_class.new` to the controller's own `new` action in
five controllers, and `self.class.timeout_in` to the instance method `def timeout_in`. A call on a
class runs a class method, never an instance method.

**What this change does:** a call is on a class when its receiver is a constant (`Other.find`), a
scoped constant (`Devise::Mapping.find`) or `x.class`, or when it is named `new`. When every
Function crg linked such a call to is an instance method of the file, the call goes back to its
plain name and is marked unresolved (`unresolved_targets = []`), so crg's post-build pass does not
link it again. A link to a class method (`def self.find`, or a `def` inside `class << self`) is
kept, and so is a link on a line that also calls the name on an object (`record.find`).

**Code:** `code_review_graph/rules/ruby/class_receiver_calls.py` (rule `class-receiver-calls`)
**Tests:** `tests/test_ruby_class_receiver_call_is_not_an_instance_method.py`

## Results

Measured 2026-11-26 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with change 1, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 209 → 209 | 814 → 814 | 40 → 26 |

The change tags 14 CALLS edges on devise with `fork_rule = class-receiver-calls`. No right link is
lost.

## Known limits

- crg gives `def self.find` and `def find` the same qualified name, so when a file has both, the
  link is kept: it may be the right one.
- A call on an object (`record.confirm`) is left alone. Its class is not known from the file, and
  on devise 15 such links were right because the object's class includes the module that defines
  the method.

## History

- 2026-11-26: added (SYN-F-625, SYN-F-627).
- 2026-11-26: changed by macro-methods: reads `ruby_class_method`; 14 → 15 tagged edges, `mapping.rb:64`.
