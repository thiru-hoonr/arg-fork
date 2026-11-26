# setter-definitions

**What crg does wrong:** crg 2.4.0 stores no Function for a setter, `def resource=(v)` or
`def self.mailer=(v)`. tree-sitter-ruby names a setter with a `setter` node, and crg's
`_get_name` (`parser.py:23286`) reads only `identifier`, `constant` and the like, so it returns no
name. The setter gets no Function and no CONTAINS edge, every call to it stays unlinked
(`self.resource = x`, which change 1 stores as a call to `resource=`), and the calls in its body
are made from the file (or from the method around it).

**What this change does:** each `def name=` and `def self.name=` gets what crg makes for the
same file with the setter renamed to the plain name of the same length, `name_` (the twin): a
Function (a Test when crg reads the name as one) in the class, module, or class around
`class << self`, its CONTAINS edge, and the calls and references in its body made from it. The
rule extracts that twin (`CodeParser._extract_bytes`) and takes its Functions and CONTAINS edges
under the setter's own name. It moves crg's own body edges, so the calls the rules before it
added (a bare call in a setter's body, change 3) move too. When the body's calls were made from a
test method around the setter, crg had also derived `called TESTED_BY test` from them; the twin
has no such edge, and the rule drops it. Last, crg's same-file linking runs again on the calls
named after the new setters, so `self.resource = x` in the setter's own file links to it. crg
gives `def self.x=` and `def x=` one qualified name and, like the twin, keeps a Function for each.

Left alone: a setter on another object (`def config.mode=`; crg names `def config.mode` after
`config`, so the twin has no Function to take) and operators (`def []=`, `def ==`, `def +`),
which crg drops the same way but which are no setters.

**Code:** `code_review_graph/rules/ruby/setter_definitions.py` (rule `setter-definitions`). It
runs after change 1, which makes the `x=` calls it links, and before change 2, which can then
unlink a call on a class (`Account.total = 0`) that crg's linking gave an instance setter.

**Shared code** (`code_review_graph/rules/ruby/_syntax.py`):
- `relinked()`: crg's same-file linking run again on the calls to some names; moved from
  change 4's `macro_methods.py`, which now uses it.
- `READS_BEFORE_WRITING` and `names_called()`: which methods the target of an assignment runs
  (`a.b = v` runs `b=`; `a.b += v` runs `b` and `b=`); the table moved from change 1's
  `setter_assignments.py`, which now uses it.

**Changes rule 02 (`class-receiver-calls`):** it reads the target of an attribute assignment the
way change 1 names its call (`Account.total = 0` is a call to `total=`, `Account.total += 1` to
`total` and `total=`), so a setter call on a class is unlinked from an instance setter. Two tests
are added to its test file (`Other.timeout += 1`, `self.level = Other.find(1)`).

**Tests:** `tests/test_ruby_setter_definition_is_a_function.py` (30 tests; 19 fail with the rule
off; 24 mutations of the rule and of the shared code it added, each caught)

## Results

Measured 2026-11-26 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1–5, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 384 → 386 | 639 → 637 | 40 → 42 |

The rows it gets right, misses and invents are exactly those of crg's build of the twin. The
change adds 6 Functions on devise and tags 18 edges (6 CONTAINS, 12 CALLS) with
`fork_rule = setter-definitions`.

- +2 right: `self.mailer = "Devise::Mailer"` (`lib/devise.rb:361`) and
  `self.password = self.password_confirmation = nil`
  (`models/database_authenticatable.rb:77`), each in its setter's own file.
- +2 invented: `self.error_status = :ok` and `self.redirect_status = :found`
  (`controllers/responder.rb` 9–10) run the Rails setters (the `if` branch is taken); the twin
  links them to the fallback `def self.error_status=` in the `else` branch of the same file, as
  crg does for any `def`.
- 17 of the 19 missed setter calls stay missed: the setter is in another file (15
  `self.resource =` in controllers, `def resource=` in their parent `DeviseController`; 2
  `password=` from a mixin). Linking them needs cross-file resolution; the Function they need now
  exists.

**Whole-file twin check** (every Ruby file with a setter, the rule's graph against crg's graph of
the twin, links of `x=` calls left out as the twin would need every assignment rewritten):
devise 5 files, sinatra 7, jekyll 3, mastodon 19; no file differs.

**Timing:** parsing every devise Ruby file takes 0.87 s with the rule off and 0.92 s with it on
(197 files, best of 5). The rule extracts each file with a setter a second time.

## Known limits

- A setter defined inside another setter: its body's edges stay with the outer setter.
- A setter call in another file than its setter is left unlinked (cross-file resolution).

## History

- 2026-11-26: added (SYN-F-632).
