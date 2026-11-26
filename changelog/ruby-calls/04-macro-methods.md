# macro-methods

**What crg does wrong:** crg 2.4.0 reads only `def` as a method. A method Ruby makes from a
class-body macro (`mattr_accessor :paranoid`, `delegate :flash, to: :request`,
`attr_reader :name`, `class_attribute :devise_modules`, `define_method(:short) { … }`) gets no
Function, so no call can be linked to it. On devise, 129 real calls go to methods made by macros.

**What this change does:** each such method gets what crg makes for the same method written out
as a `def` on the macro's line (the twin): a Function (a Test when crg reads the name as one) in
the enclosing class, module or `class << self`, its CONTAINS edge, and the links crg makes from
calls in the file to it. A `define_method` block is the method's body: the Function spans the
call and the block's calls move into it. One Function per name: crg gives `def self.x` and
`def x` the same name, and two nodes with one name would make every call to it ambiguous.

Covered macros: `mattr_accessor`, `mattr_reader`, `cattr_accessor`, `cattr_reader`,
`class_attribute`, `attr_reader`, `attr_accessor`, `delegate` (without a `prefix:` that renames;
`prefix: false` keeps the names) and
`define_method` with a literal name. A method from the first five, or from any macro in
`class << self`, is marked as a class method (`extra["ruby_class_method"]`), so
`class-receiver-calls` keeps `Devise.paranoid` linked to it as it does for `def self.paranoid`.

Left alone: writers (`attr_writer`, `x=`), because crg stores no Function for `def x=`
(SYN-F-632); names built at run time; macros on a receiver, inside a method, or outside a class
body; a name the class already defines.

**Code:** `code_review_graph/rules/ruby/macro_methods.py` (rule `macro-methods`); `_syntax.py`
gains `CLASS_METHOD_KEY`.

**Changes rule 02 (`class-receiver-calls`):** two lines in `class_receiver_calls.py` read the
class-method mark, so a call on a class stays linked to a class method a macro made. On devise
that rule now tags 15 edges instead of 14 (`mapping.rb:64`, see Results).
**Tests:** `tests/test_ruby_macro_defines_a_method.py`

## Results

Measured 2026-11-26 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1 and 2, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 209 → 216 | 814 → 807 | 26 → 27 |

The change adds 93 Functions on devise and tags 108 edges with `fork_rule = macro-methods`. The
rows it gets right and invents are exactly those of crg's build of the twin (every macro in a
copy of devise written out as `def`); a whole-file comparison of the two builds differs in no
file.

- +10 right: `Devise.default_scope` and `warden_config` in `lib/devise.rb`, `devise_modules`
  (3), and five calls in `lockable.rb` to the `delegate` at line 27.
- −3 right, +3 invented: `klass.lock_strategy_enabled?` (`lockable.rb` 31–33) runs the
  `ClassMethods` method; crg linked it there only because no other method of that name was in the
  file. It now links it to the `delegate`, as it does for the twin.
- +1 invented: `warden.request` (`proxy.rb:19`) linked to the file's own `delegate :request`, as
  crg does for the twin; a call on an object is not resolved by its class.
- +2 invented that are right: links to `attr_accessor :authentication_hash`
  (`strategies/authenticatable.rb` 129, 161). `attr_*` methods are C methods, which TracePoint's
  `:call` never records, so the answer key cannot contain them.
- `class-receiver-calls` tags 15 edges instead of 14: `Devise.sign_out_via` (`mapping.rb:64`) was
  linked to `Mapping#sign_out_via`, which now exists (from `attr_reader`); the call is on a class,
  so that rule unlinks it.

Of the other 119 missed macro calls, a node alone cannot fix them: 40 are `Devise.x` called from
another file (crg links across files only with import evidence), 23 are bare names
(SYN-F-629), 31 sit in the `class_eval` string at `models.rb:43` with a built name (SYN-F-630),
the rest have names built at run time, an alias, or a definition in a test file. Every row:
`crg-fork/lab/ruby-calls-changelog/calls-devise-macro-methods.tsv`.

The whole-file check was also run on the four other Ruby repos of the survey (sinatra, jekyll,
mastodon, rails; rule on in 13, 39, 140 and 746 files, no rule failure logged). Every file that
differs was traced to the scratch rewriter that writes the twin copies (comments after a comma,
multi-line arguments, `prefix: true`, a lambda default it broke), to one Function per name, or to
a macro outside a class body (`Class.new do … end` in specs), except `delegate … prefix: false`
on mastodon, which the rule then skipped; it is now covered and tested.

Time: the rule takes 0.40 ms per file on devise (79 ms for 197 files; a parse of all of them
goes from 414 ms to 506 ms), most of it parsing the tree again.

## Known limits

- One Function per name: where Ruby makes both `self.x` and `x`, crg cannot tell them apart
  either.
- No `x=` Function for an accessor until crg stores `def x=` (SYN-F-632).
- A macro spanning several lines gets a Function on its first line, where Ruby reports it.
- The calls of a `delegate` or `helper_method` (the method it forwards to) are not added.
- A macro in a `Class.new do … end` block is not in a class body and is left alone.
- A method whose class crg has no single Class node for (same lines and name, as in
  `class A; class A; end; attr_reader :y; end`) is skipped and logged.

## History

- 2026-11-26: added (SYN-F-631).
