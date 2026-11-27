# Ruby calls

Ruby call links that crg 2.4.0 gets wrong, fixed by fork rules in `code_review_graph/rules/ruby/`.
Each rule reads crg's own tree-sitter tree after the file is parsed; if a rule raises, the file
keeps crg's nodes and edges and a warning is logged. Findings: SYN-F-625, SYN-F-627, SYN-F-629,
SYN-F-630, SYN-F-631, SYN-F-632.

Results are measured on devise (`heartcombo/devise` @ `05811fb`) against the calls its own test
suite really makes: the suite (878 runs, 0 failures) runs with Ruby's `TracePoint` on `:call`,
which records every caller line and the definition of the method that ran, and with `Coverage`,
which records the lines that ran. A link counts once per (call line, method definition), on the
68 non-test files Ruby's parser reads. A link on a line that never ran is not judged.

crg has no twin for these calls: it links `<receiver>.name` to the same file's `def name` whatever
the receiver is, and stores `self.x = v` as a call to the getter `x`. The expected graphs in the
tests are written by hand (approved by Thiru, 2026-11-26).
Change 3 has a twin, the same call written `name()`: its expected graphs are crg's graphs for
the twin.

| # | Change | Fixes | Added |
|---|---|---|---|
| 1 | [setter-assignments](01-setter-assignments.md) | an attribute assignment linked to the getter | 2026-11-26 |
| 2 | [class-receiver-calls](02-class-receiver-calls.md) | a call on a class linked to an instance method of the caller's file | 2026-11-26 |
| 3 | [bare-name-calls](03-bare-name-calls.md) | a method called by its bare name (`resource`) stored as no call | 2026-11-26 |
| 4 | [macro-methods](04-macro-methods.md) | no Function for a method a class-body macro makes (`mattr_accessor`, `delegate`, `attr_reader`, …) | 2026-11-26 |
| 5 | [eval-strings](05-eval-strings.md) | code in a `class_eval` / `module_eval` / `instance_eval` string stored as a string (twin: the code written in place) | 2026-11-26 |
| 6 | [setter-definitions](06-setter-definitions.md) | no Function for a setter (`def resource=`), so no setter call can link | 2026-11-26 |
| 7 | [constant-receivers](07-constant-receivers.md) | a library call on a class (`Devise.paranoid`) carries no class, so crg cannot follow it to another file | 2026-11-27 |
