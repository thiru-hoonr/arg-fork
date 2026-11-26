# eval-strings

**What crg does wrong:** crg 2.4.0 stores code given as a string to `class_eval`, `module_eval` or
`instance_eval` as a string. In devise, `define_helpers` writes
`class_eval <<-METHODS, __FILE__, __LINE__ + 1` with `def authenticate_#{mapping}!` and
`warden.authenticate!(opts) if !devise_controller?` inside. Ruby runs that code, but crg has no
Function and no call for it.

**What this change does:** the rule builds the twin, which is the file with each such string
written in place as code. The eval call and the heredoc's closing line are left blank, so every
line keeps its number. Ruby reports the same lines because of `__FILE__, __LINE__ + 1`. The rule
gives the twin to crg's own `parse_bytes`, so every fork rule also runs on the string's code. It
adds the nodes and edges that crg finds on the string's lines and that the file did not already
have. A `#{...}` becomes the identifier `crg_interpolation_`, and a name that contains it is known
only at run time:

- a method with such a name gets no Function;
- the calls inside that method belong to the Function holding the eval call, or to the file when
  no Function holds it;
- a call or link to such a name is dropped.

Plain `eval`, a string held in a variable, and the block form `class_eval do … end` are left as
crg makes them; crg already reads the block form as code. Every added node and edge has
`fork_rule = eval-strings`.

**Code:** `code_review_graph/rules/ruby/eval_strings.py` (rule `eval-strings`)
**Tests:** `tests/test_ruby_class_eval_string_is_code.py`

## Results

Measured 2026-11-26 on crg 2.4.0 with changes 1 and 2 (**before**), and with this change added
(**after**). Scored on devise against the calls devise's test suite really makes (1,023).

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 209 → 212 | 814 → 811 | 26 → 26 |

The 3 new right links:

- `lib/devise/controllers/helpers.rb` lines 50 and 120: `devise_controller?` → `def devise_controller?`;
- `lib/devise/mapping.rb` line 116: `self.modules` → `def modules`.

No other row changes. *Not judged* goes 10 → 13 for these same 3 links. Ruby's `Coverage` counts
the lines of a loaded file, not code run through `eval`, so a heredoc line never shows as run,
even though TracePoint records the call made on it.

The other 43 of the 46 "code built from a string" rows stay missed:

- **36:** the called name is built with `#{...}` (`Devise.#{accessor}`, `superclass.#{accessor}`,
  `current_#{mapping}`, `#{group_name}_signed_in?`). The name is known only at run time.
- **7:** the bare name `warden`. crg stores no call for a bare name, in the twin or anywhere else
  (rule `bare-name-calls`, SYN-F-629). Once that rule is in `RULES`, it runs inside the strings
  too, through the twin's parse.

The change tags 29 CALLS edges, 2 CONTAINS edges and 2 Function nodes on devise. Every row is in
`crg-fork/lab/ruby-calls-changelog/calls-devise-eval-strings.tsv`.

**Whole-file twin check:** each of the 3 devise files with eval strings was written as its twin by
a separate scratch script, and crg's output on the string lines was compared. Nodes and edges
match, except for 4 calls inside `#{group_name.to_s.pluralize}` (`to_s`, `pluralize` on lines 71
and 78 of `helpers.rb`). crg already extracts those calls from the file itself, and the twin
replaces the interpolation, so they appear only on the rule's side.

**Timing:** parsing all 197 devise `.rb` files goes from 347 ms to 419 ms. The rule reads each
file's tree once, and the 3 files that hold strings are parsed again as their twins (12.5 ms →
23.1 ms). A full devise build takes about 1.3 s, with or without the rule.

## Known limits

- A method defined with a plain name in the string gets the class the code is written in as its
  parent, as in the twin. For `mod.class_eval`, the class it really lands in is not known from the
  file.
- A string with an escape sequence (`\n`, `\\`) is skipped, because Ruby rewrites it before it runs
  the code. So is a string whose twin is not Ruby, such as `class #{name}`. Both are logged.
- An eval call inside the arguments of another eval call (`class_eval "…", class_eval("…")`)
  gives overlapping strings. The rule raises a clear error, and the whole file is left as crg
  made it and logged.
- A node or edge that another fork rule tagged inside the string keeps the tag `eval-strings`
  instead.

## History

- 2026-11-26: added (SYN-F-630).
