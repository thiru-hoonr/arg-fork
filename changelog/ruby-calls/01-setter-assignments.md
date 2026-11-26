# setter-assignments

**What crg does wrong:** crg 2.4.0 stores `self.resource = resource_class.new` as a call named
`resource` and links it to the getter `def resource`. Ruby runs the setter `resource=`. The same
happens for `ledger.total = amount` (a call to `total`).

**What this change does:** an assignment whose left side is a call (`self.x = v`, `a.x = v`) gets a
call to the setter `x=` instead of crg's call to `x`. An assignment that reads first
(`self.x += v`, `self.x ||= v`) keeps crg's call to the getter `x` and gains a call to `x=`. crg
stores no Function for `def x=`, so the setter call keeps its plain name. Assignments to `@x`,
`a[:x]` or a local variable are left as crg makes them.

**Code:** `code_review_graph/rules/ruby/setter_assignments.py` (rule `setter-assignments`)
**Tests:** `tests/test_ruby_setter_assignment_calls_the_setter.py`

## Results

Measured 2026-11-26 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0, **after** is crg 2.4.0 with this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 209 → 209 | 814 → 814 | 46 → 40 |

The change tags 288 CALLS edges on devise with `fork_rule = setter-assignments`. Most setter calls
cannot be scored as right: the setter has no Function in crg, so its call stays a plain name.

## Known limits

- `a.x, b.y = 1, 2` (several targets at once) is left as crg makes it.
- The setter call is linked to `def x=` only by change 6 (`setter-definitions`), and only
  within the setter's own file.

## History

- 2026-11-26: added (SYN-F-627).
- 2026-11-26: the table of assignments moved to `_syntax.py` (`READS_BEFORE_WRITING`) for change 6.
